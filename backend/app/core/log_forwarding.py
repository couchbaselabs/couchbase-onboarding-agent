"""Log forwarding (Settings -> Log forwarding).

A logging.Handler on the root logger queues every record at or above the
configured level -- application logs and the `onboarding_agent.auth` audit
trail alike -- and a background thread ships them in batches to ONE configured
destination:

  splunk_hec      Splunk HTTP Event Collector (/services/collector/event)
  datadog         Datadog Logs API v2 (http-intake.logs.<site>)
  elasticsearch   Elasticsearch / OpenSearch bulk API (also works for Elastic Cloud)
  loki            Grafana Loki push API (/loki/api/v1/push)
  syslog          RFC 5424 syslog over UDP, TCP or TLS (rsyslog, syslog-ng, SIEMs)

Delivery is best effort: the queue is bounded (oldest dropped on overflow),
a failed batch is retried once on the next flush, and the handler never
blocks the request that produced the log line. Credentials are stored
Fernet-encrypted in `<state>/log_forwarding.json`. HTTPS destinations are
verified against the public CAs plus Settings -> Certificates.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import queue
import socket
import ssl
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

from app.config import get_settings
from app.core.secrets import SecretBox

logger = logging.getLogger("onboarding_agent.logfwd")   # never forwarded (see handler)
audit = logging.getLogger("onboarding_agent.auth")

DESTINATIONS: dict[str, dict] = {
    "splunk_hec": {
        "label": "Splunk (HTTP Event Collector)",
        "fields": [
            {"key": "url", "label": "HEC URL", "placeholder": "https://splunk.corp.example:8088", "required": True},
            {"key": "token", "label": "HEC token", "secret": True, "required": True},
            {"key": "index", "label": "Index (optional)"},
            {"key": "sourcetype", "label": "Sourcetype", "default": "couchbase:onboarding-agent"},
        ],
    },
    "datadog": {
        "label": "Datadog Logs",
        "fields": [
            {"key": "site", "label": "Site", "default": "datadoghq.com",
             "hint": "datadoghq.com, us3.datadoghq.com, us5.datadoghq.com, datadoghq.eu, ap1.datadoghq.com, ddog-gov.com"},
            {"key": "api_key", "label": "API key", "secret": True, "required": True},
            {"key": "service", "label": "Service", "default": "couchbase-onboarding-agent"},
            {"key": "tags", "label": "Tags (comma-separated)", "placeholder": "env:prod,team:data"},
        ],
    },
    "elasticsearch": {
        "label": "Elasticsearch / OpenSearch",
        "fields": [
            {"key": "url", "label": "URL", "placeholder": "https://es.corp.example:9200", "required": True},
            {"key": "index", "label": "Index / data stream", "default": "couchbase-onboarding-agent"},
            {"key": "api_key", "label": "API key (base64 id:key)", "secret": True, "hint": "Or use username + password"},
            {"key": "username", "label": "Username"},
            {"key": "password", "label": "Password", "secret": True},
        ],
    },
    "loki": {
        "label": "Grafana Loki",
        "fields": [
            {"key": "url", "label": "URL", "placeholder": "http://loki.corp.example:3100", "required": True},
            {"key": "tenant", "label": "Tenant (X-Scope-OrgID, optional)"},
            {"key": "username", "label": "Basic-auth username (optional)"},
            {"key": "password", "label": "Basic-auth password", "secret": True},
            {"key": "labels", "label": "Extra labels (k=v, comma-separated)", "placeholder": "env=prod,cluster=hal"},
        ],
    },
    "syslog": {
        "label": "Syslog (RFC 5424)",
        "fields": [
            {"key": "host", "label": "Host", "required": True},
            {"key": "port", "label": "Port", "default": "514"},
            {"key": "protocol", "label": "Protocol", "default": "udp", "options": ["udp", "tcp", "tls"]},
            {"key": "facility", "label": "Facility", "default": "local0",
             "options": ["local0", "local1", "local2", "local3", "local4", "local5", "local6", "local7", "user", "auth", "daemon"]},
            {"key": "app_name", "label": "APP-NAME", "default": "couchbase-onboarding-agent"},
        ],
    },
}

SECRET_KEYS = {"token", "api_key", "password"}
LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


class ForwardError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ForwardConfig:
    enabled: bool = False
    destination: str = "splunk_hec"
    settings: dict = field(default_factory=dict)
    min_level: str = "INFO"
    include_audit: bool = True
    include_app: bool = True
    batch_size: int = 50
    flush_interval_s: int = 5
    hostname: str = ""        # defaults to the container hostname
    extra_fields: dict = field(default_factory=dict)

    def public(self) -> dict:
        d = asdict(self)
        d["settings"] = {k: ("" if k in SECRET_KEYS and v else v) for k, v in self.settings.items()}
        d["secrets_set"] = sorted(k for k, v in self.settings.items() if k in SECRET_KEYS and v)
        return d


# ====================================================================== store

class ForwardStore:
    _instance: ForwardStore | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._config = ForwardConfig()
        self._load()

    @classmethod
    def instance(cls) -> ForwardStore:
        if cls._instance is None:
            cls._instance = ForwardStore()
        return cls._instance

    @staticmethod
    def path() -> Path:
        return Path(get_settings().migration_state_file).parent / "log_forwarding.json"

    def _load(self) -> None:
        p = self.path()
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text())
            box = SecretBox.instance()
            raw["settings"] = {k: (box.decrypt(v) if k in SECRET_KEYS and v else v) for k, v in raw.get("settings", {}).items()}
            self._config = ForwardConfig(**{k: v for k, v in raw.items() if k in ForwardConfig.__dataclass_fields__})
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not read %s: %s -- log forwarding stays off until it's saved again.", p, exc)

    def _persist(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self._config)
        box = SecretBox.instance()
        data["settings"] = {k: (box.encrypt(v) if k in SECRET_KEYS and v else v) for k, v in data["settings"].items()}
        fd, tmp = tempfile.mkstemp(prefix=".logfwd.", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @property
    def config(self) -> ForwardConfig:
        return self._config

    def merge(self, incoming: dict) -> ForwardConfig:
        """Incoming config with blank secrets replaced by the stored ones."""
        cfg = ForwardConfig(**{k: v for k, v in incoming.items() if k in ForwardConfig.__dataclass_fields__})
        cfg.settings = {k: (v.strip() if isinstance(v, str) else v) for k, v in (cfg.settings or {}).items()}
        if cfg.destination == self._config.destination:
            for k in SECRET_KEYS:
                if not cfg.settings.get(k) and self._config.settings.get(k):
                    cfg.settings[k] = self._config.settings[k]
        self.validate(cfg)
        return cfg

    def save(self, incoming: dict) -> ForwardConfig:
        cfg = self.merge(incoming)
        with self._lock:
            self._config = cfg
            self._persist()
        audit.warning("logfwd: configuration saved (enabled=%s, destination=%s, level=%s)",
                      cfg.enabled, cfg.destination, cfg.min_level)
        Forwarder.instance().apply(cfg)
        return cfg

    @staticmethod
    def validate(cfg: ForwardConfig) -> None:
        if cfg.destination not in DESTINATIONS:
            raise ForwardError(422, f"Unknown destination '{cfg.destination}'.")
        if cfg.min_level not in LEVELS:
            raise ForwardError(422, f"Level must be one of {', '.join(LEVELS)}.")
        if not 1 <= cfg.batch_size <= 1000 or not 1 <= cfg.flush_interval_s <= 300:
            raise ForwardError(422, "Batch size 1-1000 and flush interval 1-300 s.")
        if not (cfg.include_app or cfg.include_audit):
            raise ForwardError(422, "Forward the application log, the audit log, or both.")
        if cfg.enabled:
            for f in DESTINATIONS[cfg.destination]["fields"]:
                if f.get("required") and not cfg.settings.get(f["key"]):
                    raise ForwardError(422, f"{f['label']} is required for {DESTINATIONS[cfg.destination]['label']}.")
            s = cfg.settings
            if cfg.destination in ("splunk_hec", "elasticsearch", "loki") and not str(s.get("url", "")).startswith(("http://", "https://")):
                raise ForwardError(422, "URL must start with http:// or https://.")
            if cfg.destination == "syslog":
                try:
                    port = int(s.get("port") or 514)
                except ValueError as exc:
                    raise ForwardError(422, "Port must be a number.") from exc
                if not 1 <= port <= 65535:
                    raise ForwardError(422, "Port must be 1-65535.")
                if s.get("protocol", "udp") not in ("udp", "tcp", "tls"):
                    raise ForwardError(422, "Protocol must be udp, tcp or tls.")

    def reset_for_tests(self) -> None:
        self._config = ForwardConfig()
        p = self.path()
        if p.exists():
            p.unlink()
        Forwarder.instance().apply(self._config)


# ====================================================================== shippers

def _hostname(cfg: ForwardConfig) -> str:
    return cfg.hostname or platform.node() or "onboarding-agent"


def _verify() -> str | bool:
    from app.core.certs import TrustStore  # noqa: PLC0415

    return TrustStore.instance().bundle_path() or True


def _level_num(name: str) -> int:
    return logging.getLevelName(name) if isinstance(logging.getLevelName(name), int) else logging.INFO


class _Shipper:
    def __init__(self, cfg: ForwardConfig) -> None:
        self.cfg = cfg
        self.s = cfg.settings
        self.host = _hostname(cfg)

    def send(self, events: list[dict]) -> None:  # raises on failure
        raise NotImplementedError

    def close(self) -> None:
        pass


class _Splunk(_Shipper):
    def send(self, events):
        url = self.s["url"].rstrip("/") + "/services/collector/event"
        body = "".join(json.dumps({
            "time": e["ts"], "host": self.host, "source": e["logger"],
            "sourcetype": self.s.get("sourcetype") or "couchbase:onboarding-agent",
            **({"index": self.s["index"]} if self.s.get("index") else {}),
            "event": {k: v for k, v in e.items() if k != "ts"},
        }) for e in events)
        r = requests.post(url, data=body, headers={"Authorization": f"Splunk {self.s['token']}"}, timeout=10, verify=_verify())
        if r.status_code >= 300:
            raise ForwardError(502, f"Splunk HEC answered {r.status_code}: {r.text[:200]}")


class _Datadog(_Shipper):
    def send(self, events):
        site = (self.s.get("site") or "datadoghq.com").strip()
        url = f"https://http-intake.logs.{site}/api/v2/logs"
        base_tags = self.s.get("tags", "").strip()
        payload = [{
            "ddsource": "couchbase-onboarding-agent", "service": self.s.get("service") or "couchbase-onboarding-agent",
            "hostname": self.host, "message": e["message"], "status": e["level"].lower(),
            "ddtags": ",".join(t for t in [base_tags, f"level:{e['level'].lower()}"] if t),
            "timestamp": int(e["ts"] * 1000), "logger": e["logger"], **e.get("extra", {}),
        } for e in events]
        r = requests.post(url, json=payload, headers={"DD-API-KEY": self.s["api_key"]}, timeout=10, verify=_verify())
        if r.status_code >= 300:
            raise ForwardError(502, f"Datadog answered {r.status_code}: {r.text[:200]}")


class _Elastic(_Shipper):
    def send(self, events):
        index = self.s.get("index") or "couchbase-onboarding-agent"
        lines = []
        for e in events:
            lines.append(json.dumps({"create": {"_index": index}}))
            lines.append(json.dumps({"@timestamp": e["time"], "log.level": e["level"].lower(), "message": e["message"],
                                     "log.logger": e["logger"], "host.name": self.host,
                                     "service.name": "couchbase-onboarding-agent", **e.get("extra", {})}))
        headers = {"Content-Type": "application/x-ndjson"}
        auth = None
        if self.s.get("api_key"):
            headers["Authorization"] = f"ApiKey {self.s['api_key']}"
        elif self.s.get("username"):
            auth = (self.s["username"], self.s.get("password", ""))
        r = requests.post(self.s["url"].rstrip("/") + "/_bulk", data="\n".join(lines) + "\n", headers=headers,
                          auth=auth, timeout=10, verify=_verify())
        if r.status_code >= 300:
            raise ForwardError(502, f"Elasticsearch answered {r.status_code}: {r.text[:200]}")
        try:
            if r.json().get("errors"):
                first = next((i for i in r.json()["items"] if "error" in i.get("create", {})), None)
                raise ForwardError(502, f"Elasticsearch rejected documents: {json.dumps(first)[:200]}")
        except ValueError:
            pass


class _Loki(_Shipper):
    def send(self, events):
        labels = {"app": "couchbase-onboarding-agent", "host": self.host}
        for part in (self.s.get("labels") or "").split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                labels[k.strip()] = v.strip()
        streams: dict[str, list] = {}
        for e in events:
            lab = json.dumps({**labels, "level": e["level"].lower(), "logger": e["logger"]}, sort_keys=True)
            streams.setdefault(lab, []).append([str(int(e["ts"] * 1e9)), e["message"]])
        payload = {"streams": [{"stream": json.loads(k), "values": v} for k, v in streams.items()]}
        headers = {"Content-Type": "application/json"}
        if self.s.get("tenant"):
            headers["X-Scope-OrgID"] = self.s["tenant"]
        auth = (self.s["username"], self.s.get("password", "")) if self.s.get("username") else None
        r = requests.post(self.s["url"].rstrip("/") + "/loki/api/v1/push", json=payload, headers=headers, auth=auth,
                          timeout=10, verify=_verify())
        if r.status_code >= 300:
            raise ForwardError(502, f"Loki answered {r.status_code}: {r.text[:200]}")


_FACILITIES = {"user": 1, "daemon": 3, "auth": 4, **{f"local{i}": 16 + i for i in range(8)}}
_SEVERITY = {"DEBUG": 7, "INFO": 6, "WARNING": 4, "ERROR": 3, "CRITICAL": 2}


class _Syslog(_Shipper):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.sock = None

    def _connect(self):
        host, port = self.s["host"], int(self.s.get("port") or 514)
        proto = self.s.get("protocol", "udp")
        if proto == "udp":
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.connect((host, port))
            return
        raw = socket.create_connection((host, port), timeout=10)
        if proto == "tls":
            ctx = ssl.create_default_context(cafile=_verify() if isinstance(_verify(), str) else None)
            raw = ctx.wrap_socket(raw, server_hostname=host)
        self.sock = raw

    def send(self, events):
        if self.sock is None:
            self._connect()
        fac = _FACILITIES.get(self.s.get("facility", "local0"), 16)
        app = self.s.get("app_name") or "couchbase-onboarding-agent"
        proto = self.s.get("protocol", "udp")
        for e in events:
            pri = fac * 8 + _SEVERITY.get(e["level"], 6)
            sd = "".join(f' {k}="{str(v).replace(chr(34), chr(39))}"' for k, v in e.get("extra", {}).items())
            sd_block = f"[coa@47476 logger=\"{e['logger']}\"{sd}]"
            line = f"<{pri}>1 {e['time']} {self.host} {app} {os.getpid()} - {sd_block} {e['message']}"
            data = line.encode("utf-8", "replace")
            try:
                if proto == "udp":
                    self.sock.send(data[:8192])
                else:
                    self.sock.sendall(f"{len(data)} ".encode() + data)   # RFC 6587 octet counting
            except OSError:
                self.close()
                raise

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None


_SHIPPERS = {"splunk_hec": _Splunk, "datadog": _Datadog, "elasticsearch": _Elastic, "loki": _Loki, "syslog": _Syslog}


def make_shipper(cfg: ForwardConfig) -> _Shipper:
    return _SHIPPERS[cfg.destination](cfg)


# ====================================================================== handler + worker

def record_to_event(record: logging.LogRecord, cfg: ForwardConfig) -> dict:
    ts = record.created
    return {
        "ts": ts,
        "time": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "level": record.levelname, "logger": record.name, "message": record.getMessage(),
        "extra": {**cfg.extra_fields, **({"exception": record.exc_text} if record.exc_text else {})},
    }


class Forwarder(logging.Handler):
    """Root-logger handler + background shipper. One per process."""

    _instance: Forwarder | None = None

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.cfg = ForwardConfig()
        self.queue: queue.Queue[dict] = queue.Queue(maxsize=5000)
        self.shipper: _Shipper | None = None
        self.stats = {"sent": 0, "failed": 0, "dropped": 0, "last_error": None, "last_success": None, "active": False}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: list[dict] = []

    @classmethod
    def instance(cls) -> Forwarder:
        if cls._instance is None:
            cls._instance = Forwarder()
            logging.getLogger().addHandler(cls._instance)
        return cls._instance

    # -- configuration
    def apply(self, cfg: ForwardConfig) -> None:
        self.cfg = cfg
        if self.shipper:
            self.shipper.close()
        self.shipper = make_shipper(cfg) if cfg.enabled else None
        self.setLevel(_level_num(cfg.min_level))
        self.stats["active"] = bool(self.shipper)
        if self.shipper and (self._thread is None or not self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="log-forwarder", daemon=True)
            self._thread.start()
        self._wake.set()

    # -- logging.Handler
    def emit(self, record: logging.LogRecord) -> None:
        if self.shipper is None or record.name.startswith("onboarding_agent.logfwd"):
            return
        is_audit = record.name.startswith("onboarding_agent.auth")
        if (is_audit and not self.cfg.include_audit) or (not is_audit and not self.cfg.include_app):
            return
        try:
            self.queue.put_nowait(record_to_event(record, self.cfg))
        except queue.Full:
            self.stats["dropped"] += 1
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(record_to_event(record, self.cfg))
            except (queue.Empty, queue.Full):
                pass
        if self.queue.qsize() >= self.cfg.batch_size:
            self._wake.set()

    # -- worker
    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self.cfg.flush_interval_s)
            self._wake.clear()
            self.flush_now()

    def flush_now(self) -> None:
        shipper = self.shipper
        if shipper is None:
            return
        batch = self._pending
        self._pending = []
        while len(batch) < self.cfg.batch_size:
            try:
                batch.append(self.queue.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return
        try:
            shipper.send(batch)
            self.stats["sent"] += len(batch)
            self.stats["last_success"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.stats["last_error"] = None
        except Exception as exc:  # noqa: BLE001
            self.stats["failed"] += 1
            self.stats["last_error"] = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}: {str(exc)[:300]}"
            logger.warning("log forwarding to %s failed: %s", self.cfg.destination, exc)
            # keep this batch for one retry; beyond that let it go rather than grow
            self._pending = batch if not self._pending else []

    def status(self) -> dict:
        return {**self.stats, "queued": self.queue.qsize() + len(self._pending), "destination": self.cfg.destination if self.shipper else None}


def send_test_event(cfg: ForwardConfig) -> dict:
    """Synchronously ship one event with an unsaved config (Settings -> Test)."""
    shipper = make_shipper(cfg)
    event = {
        "ts": time.time(),
        "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "level": "INFO", "logger": "onboarding_agent.test",
        "message": f"Couchbase Onboarding Agent log-forwarding test from {_hostname(cfg)}",
        "extra": {**cfg.extra_fields, "test": True},
    }
    started = time.monotonic()
    try:
        shipper.send([event])
    except ForwardError:
        raise
    except requests.RequestException as exc:
        raise ForwardError(502, f"Couldn't reach the destination: {exc}") from exc
    except OSError as exc:
        raise ForwardError(502, f"Couldn't reach the destination: {exc}") from exc
    finally:
        shipper.close()
    return {"ok": True, "destination": DESTINATIONS[cfg.destination]["label"], "ms": int((time.monotonic() - started) * 1000)}
