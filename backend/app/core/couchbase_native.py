"""
Native Couchbase-to-Couchbase migration tooling: `cbbackupmgr` for one-time /
full-load transfer, XDCR (Cross Data Center Replication) for continuous
replication. Used ONLY when the source is itself a Couchbase cluster (see
models.enums.COUCHBASE_SOURCE_TYPES) -- migration_engine.py routes those
source types here instead of through the generic
SourceConnector.extract()/stream_changes() + CouchbaseLoader pipeline every
other source uses.

This is a deliberate architectural exception, not an oversight -- three ways:

1. READ-ONLY INVARIANT: broken, on purpose, for this one source family. XDCR
   requires registering the destination as a "remote cluster reference" on the
   SOURCE cluster and creating a replication there via its own REST management
   API -- a configuration write to the source, unlike every other connector in
   this app (see connectors/base.py's docstring). cbbackupmgr's backup step
   itself doesn't modify source data, but the overall exception still applies.

2. PER-DOCUMENT PROGRESS / VERIFY-BY-TAG / PURGE-BY-TAG: don't apply here.
   Every other connector's loader tags each document with a `_migration`
   envelope (see couchbase_loader.py) so the engine can report exact
   per-container progress and later verify/roll back precisely by migration
   ID. cbbackupmgr and XDCR both move documents byte-for-byte unmodified --
   there's no tag to filter on. Progress is necessarily coarser here
   (cbbackupmgr's own log output, XDCR's changesLeft/docsWritten task
   counters), and rollback can only mean "tear down the XDCR replication"
   (always safe -- it doesn't touch data already written) or "drop the
   destination collection yourself" for cbbackupmgr-loaded data, not a
   targeted per-migration delete.

3. XDCR FROM A CAPELLA SOURCE ISN'T IMPLEMENTED. Capella manages
   cross-cluster replication through its own Management API rather than
   exposing the classic per-node REST endpoints used below to arbitrary
   external callers (this is why the destination side of this app already has
   a separate CapellaClient for bucket provisioning instead of reusing
   CouchbaseClusterClient's REST calls) -- implementing that Capella-specific
   path is a real gap, not a silent assumption. A Capella *source* therefore
   only gets one-time migration via cbbackupmgr here; supports_cdc is False
   for it (see couchbase_source.py). Self-managed Enterprise Edition sources
   get full continuous/hybrid support via the classic REST XDCR API below.

LIVE-VERIFIED: exercised end-to-end against a real self-managed Enterprise
Edition source (EC2) and a real Capella destination -- cbbackupmgr full-load
backup/restore and continuous XDCR replication have both completed
successfully, including the config/backup/restore flag shapes, the
remoteClusters/createReplication/tasks/cancelXDCR REST payloads, the
XDCR-vbucket-count precheck, and the remote-cluster-reference collision
self-heal in create_remote_cluster_ref() below.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import requests

from app.config import get_settings
from app.models.schemas import CouchbaseConnectionConfig, SourceConnectionConfig

logger = logging.getLogger(__name__)

_LINE_SPLIT_RE = re.compile(rb"[\r\n]")


class CouchbaseNativeError(RuntimeError):
    pass


def _cluster_string_for_cli(connection_string: str, external_network: bool) -> str:
    """Appends `?network=external` to a cbbackupmgr `--cluster` connection
    string when the source is on a cloud VM / Kubernetes with alternate
    addressing configured (the wizard's "Cluster is on a cloud VM or
    Kubernetes" checkbox, `SourceConnectionConfig.couchbase_external_network`).

    Without this, cbbackupmgr's initial connection over the public hostname
    still succeeds (it can reach the management port), but the cluster map it
    gets back lists each node's INTERNAL address by default -- the actual
    key-value data transfer then tries to reach those internal-only
    addresses from outside the cluster's VPC/network and can't, silently
    completing with 0 items transferred rather than a clear connection error
    (confirmed against a live EC2-hosted cluster on 2026-07-30: the same
    symptom the SDK-based connector's `network="external"` ClusterOptions
    already exists to avoid for introspection -- see couchbase_source.py --
    but that fix never applied to this module's separate CLI subprocess
    calls until now).
    """
    if not external_network or not connection_string:
        return connection_string
    if "network=" in connection_string:
        return connection_string  # caller already specified one explicitly
    separator = "&" if "?" in connection_string else "?"
    return f"{connection_string}{separator}network=external"


def _escape_collection_string_part(name: str) -> str:
    """Escapes literal '.' in a bucket/scope/collection name for cbbackupmgr's
    dot-separated `bucket.scope.collection` collection-string format (used by
    --include-data and --map-data) -- otherwise a period in the name itself
    would be misread as a scope/collection separator."""
    return name.replace(".", "\\.")


def _redact(args: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    redact_next = False
    for a in args:
        if redact_next:
            out.append("***")
            redact_next = False
            continue
        out.append(a)
        if a in ("-p", "--password"):
            redact_next = True
    return out


class CbBackupMgrRunner:
    """Shells out to cbbackupmgr for the one-time/full-load half of a
    Couchbase-to-Couchbase migration. One archive+repository per migration ID
    (see migration_engine.py), so concurrent migrations never collide."""

    def __init__(self, log_fn: Callable[[str], Awaitable[None]]):
        self.settings = get_settings()
        self.log_fn = log_fn

    async def _run(self, *args: str) -> str:
        cmd = [self.settings.cbbackupmgr_path, *args]
        logger.info("Running: %s %s", cmd[0], " ".join(_redact(tuple(args))))
        # LD_LIBRARY_PATH is set on THIS subprocess's environment only, not the
        # whole image (see Dockerfile) -- cbbackupmgr is dynamically linked
        # against the Couchbase Enterprise image's own libstdc++/libssl, which
        # is older than Debian's and breaks unrelated tools (apt-get, python)
        # if put ahead of the system libs on every process's search path.
        env = dict(os.environ)
        cb_lib = env.get("COUCHBASE_TOOLS_LIB")
        if cb_lib:
            existing = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = f"{cb_lib}:{existing}" if existing else cb_lib
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
            )
        except FileNotFoundError as exc:
            raise CouchbaseNativeError(
                f"'{self.settings.cbbackupmgr_path}' was not found -- cbbackupmgr must be installed in the "
                "backend image (see Dockerfile) for Couchbase-to-Couchbase migrations."
            ) from exc

        lines: list[str] = []
        assert proc.stdout is not None

        # cbbackupmgr redraws its "[====...] NN.NN%" progress bar via bare \r
        # (no \n) several times a second while transferring data. Reading
        # line-by-line the default way (asyncio's StreamReader splits on \n
        # only) buffers all of those \r-separated redraws together with
        # whatever real \n-terminated message comes after them, producing
        # garbled, interleaved log lines (confirmed against a live cluster on
        # 2026-07-30 -- e.g. "Deciding which key value data toBacking up to
        # '...'0B"). Reading raw chunks and splitting on \r *and* \n fixes
        # that; throttling progress-bar lines to once a second keeps a long
        # backup's log readable instead of flooding it with every redraw.
        buf = b""
        last_progress_log = 0.0

        async def _emit(raw: bytes) -> None:
            nonlocal last_progress_log
            line = raw.decode(errors="replace").strip()
            if not line:
                return
            lines.append(line)
            is_progress = line.startswith("[") and line.endswith("%")
            now = time.monotonic()
            if is_progress and now - last_progress_log < 1.0:
                return
            if is_progress:
                last_progress_log = now
            await self.log_fn(f"cbbackupmgr: {line}")

        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                match = _LINE_SPLIT_RE.search(buf)
                if not match:
                    break
                piece, buf = buf[: match.start()], buf[match.end() :]
                await _emit(piece)
        if buf:
            await _emit(buf)

        returncode = await proc.wait()
        if returncode != 0:
            raise CouchbaseNativeError(
                f"`cbbackupmgr {args[0] if args else ''}` failed (exit {returncode}). Last output:\n"
                + "\n".join(lines[-15:])
            )
        return "\n".join(lines)

    async def backup(self, source: SourceConnectionConfig, archive_dir: str, repo: str) -> None:
        # `--include-data` belongs to the `config` command, NOT `backup` --
        # confirmed against a live run on 2026-07-30: passing it to `backup`
        # fails immediately with exit 64 (a usage error) and cbbackupmgr
        # dumps its help text. `config` is where a repository's scope gets
        # fixed for its lifetime ("Once a backup repository is created its
        # configuration cannot be changed" -- per Couchbase's own docs), which
        # is exactly why it lives here and not on the `backup` call itself.
        #
        # Scoping to just the bucket configured in the wizard matters because
        # cbbackupmgr otherwise backs up EVERY bucket on the source cluster --
        # if the source has other buckets too (e.g. Couchbase's own
        # "travel-sample" sample bucket sitting alongside the real one),
        # those get pulled into the same archive/repo and restore then tries
        # to restore them too, which can hit the exact collection-ID-mismatch
        # error --map-data (see restore() below) was added for, just for a
        # bucket this migration was never supposed to touch.
        #
        # `config` initializes the archive/repository on first use; treat
        # "already exists" as fine so re-running a migration (or the
        # full-load half of a hybrid strategy) is idempotent.
        config_args = ["config", "--archive", archive_dir, "--repo", repo]
        if source.database:
            config_args += ["--include-data", _escape_collection_string_part(source.database)]
        try:
            await self._run(*config_args)
        except CouchbaseNativeError as exc:
            if "already exists" not in str(exc).lower():
                raise

        cluster_str = _cluster_string_for_cli(
            source.connection_string or "", getattr(source, "couchbase_external_network", False)
        )
        args = [
            "backup",
            "--archive", archive_dir,
            "--repo", repo,
            "--cluster", cluster_str,
            "--username", source.username or "",
            "--password", source.password or "",
        ]
        if source.use_tls and not source.ca_cert_path:
            args.append("--no-ssl-verify")
        await self._run(*args)

    async def restore(
        self,
        dest: CouchbaseConnectionConfig,
        archive_dir: str,
        repo: str,
        source_bucket: str,
        dest_bucket: str,
        collection_pairs: list[tuple[str, str]] | None = None,
    ) -> None:
        args = [
            "restore",
            "--archive", archive_dir,
            "--repo", repo,
            "--cluster", dest.connection_string,
            "--username", dest.username,
            "--password", dest.password,
            "--force-updates",
        ]
        # By default cbbackupmgr matches scopes/collections to the backup
        # manifest by their internal ID, not by name -- if the destination
        # collection already exists (e.g. a retried migration, or one
        # independently created on the destination cluster), its ID won't
        # match the source's, and restore refuses with "exists with a
        # different name/id ... a manual remap using '--map-data' is
        # required" even though the names line up. --map-data switches
        # restore to explicit name-based matching for whatever's listed,
        # sidestepping the ID check.
        #
        # A bucket-level mapping (source=dest) alone does NOT cascade down to
        # cover this for individual scopes/collections -- confirmed against a
        # live cluster on 2026-07-30, where a bucket-level-only map still hit
        # this error per-collection. Couchbase's documented fix is a full,
        # comma-separated bucket.scope.collection=bucket.scope.collection
        # mapping for every collection being restored (see
        # CouchbaseSourceConnector.list_scopes_and_collections(), which the
        # caller uses to build `collection_pairs`). Fall back to a bucket-level
        # map only if that enumeration wasn't possible.
        src_esc = _escape_collection_string_part(source_bucket)
        dst_esc = _escape_collection_string_part(dest_bucket)
        if collection_pairs:
            mapping = ",".join(
                f"{src_esc}.{scope}.{coll}={dst_esc}.{scope}.{coll}"
                for scope, coll in collection_pairs
            )
            args += ["--map-data", mapping]
        else:
            args += ["--map-data", f"{src_esc}={dst_esc}"]
        # Restrict restore to just the bucket this migration is for, keyed by
        # its name AS RECORDED IN THE BACKUP (i.e. source_bucket, before
        # --map-data is applied) -- same reasoning as backup()'s
        # --include-data above: our backup only ever contains one bucket now,
        # but a migration re-run against an archive from before that fix, or
        # any other bucket that ended up in the same repo, should never be
        # able to touch destination buckets this migration wasn't approved to
        # write to.
        if source_bucket:
            args += ["--include-data", src_esc]
        # Capella's cluster access (database) credentials -- the only kind of
        # credential this app has for a Capella destination -- aren't
        # authorized to manage cluster-level services like Query, Analytics,
        # or Views. Restoring into Capella without telling cbbackupmgr that
        # fails partway through with "authentication error executing 'POST'
        # request to '/api/v1/bucket/<bucket>/backup' check credentials" once
        # it reaches the Query Service metadata step (confirmed against a
        # live Capella cluster on 2026-07-30, right after the actual
        # key-value data itself finishes fine). --capella tells restore to
        # skip everything the database credentials can't touch (Query,
        # Analytics, Views, users) instead of trying and failing -- Couchbase's
        # own documented fix for restoring into Capella specifically.
        if dest.is_capella:
            args.append("--capella")
        if dest.use_tls and not dest.ca_cert_path:
            args.append("--no-ssl-verify")
        await self._run(*args)


class XdcrManager:
    """Configures continuous replication FROM a self-managed Enterprise
    Edition Couchbase source cluster TO this app's destination, via the
    classic REST management API (Capella sources aren't supported here -- see
    module docstring). XDCR itself runs entirely inside the source cluster
    once created; this class only sets it up, polls its progress, and tears
    it down -- there's no ongoing process on this app's side keeping it alive
    the way stream_changes() does for every other connector's CDC."""

    def __init__(self, source: SourceConnectionConfig):
        self.source = source

    def _mgmt_base_url(self) -> str:
        host = (
            (self.source.connection_string or "")
            .replace("couchbases://", "").replace("couchbase://", "")
            .split(",")[0].split("/")[0]
        )
        scheme = "https" if self.source.use_tls else "http"
        port = 18091 if self.source.use_tls else 8091
        return f"{scheme}://{host}:{port}"

    def _auth(self) -> tuple[str, str]:
        return (self.source.username or "", self.source.password or "")

    def _verify(self):
        return self.source.ca_cert_path if self.source.ca_cert_path else False

    def list_remote_cluster_refs(self) -> list[dict[str, Any]]:
        resp = requests.get(
            f"{self._mgmt_base_url()}/pools/default/remoteClusters",
            auth=self._auth(), verify=self._verify(), timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _remove_stale_refs_for_host(self, dest_host: str, keep_name: str) -> bool:
        """Delete any *other* reference this app previously created (named
        "onboarding-agent-<migration id>") that points at the same destination
        host. Couchbase refuses to register a second remote-cluster reference
        for a target that's already registered under a different name -- and
        the 400 it returns for that case also contains "already exists",
        which create_remote_cluster_ref() used to treat as "nothing to do."
        In practice that meant a *stale* reference left over from an earlier
        test run (e.g. after `docker compose down -v`, which wipes this app's
        own state but has no effect on what's registered on the live source
        cluster) silently blocked every subsequent migration's freshly-named
        reference from ever being created -- confirmed live on 2026-07-30,
        where the Couchbase console's XDCR page showed exactly one lingering
        reference from a prior migration attempt, under a name the current
        migration's createReplication() call was never going to find. Returns
        True if anything was removed.
        """
        removed = False
        try:
            for ref in self.list_remote_cluster_refs():
                name = ref.get("name", "")
                hostname = (ref.get("hostname") or "").split(":")[0]
                if name == keep_name or not name.startswith("onboarding-agent-"):
                    continue
                if hostname != dest_host:
                    continue
                del_resp = requests.delete(
                    f"{self._mgmt_base_url()}/pools/default/remoteClusters/{quote(name, safe='')}",
                    auth=self._auth(), verify=self._verify(), timeout=20,
                )
                if del_resp.status_code in (200, 404):
                    removed = True
                else:
                    logger.warning(
                        "Could not remove stale XDCR remote-cluster reference %s: %s %s",
                        name, del_resp.status_code, del_resp.text,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error while checking for stale XDCR remote-cluster references: %s", exc)
        return removed

    def create_remote_cluster_ref(self, dest: CouchbaseConnectionConfig, ref_name: str) -> None:
        dest_host = (
            (dest.connection_string or "")
            .replace("couchbases://", "").replace("couchbase://", "")
            .split(",")[0].split("/")[0]
        )
        dest_port = 18091 if (dest.use_tls or dest.is_capella) else 8091
        payload: dict[str, Any] = {
            "name": ref_name,
            "hostname": f"{dest_host}:{dest_port}",
            "username": dest.username,
            "password": dest.password,
        }
        if dest.use_tls or dest.is_capella:
            payload["demandEncryption"] = 1
        resp = requests.post(
            f"{self._mgmt_base_url()}/pools/default/remoteClusters",
            data=payload, auth=self._auth(), verify=self._verify(), timeout=20,
        )
        if resp.ok:
            return
        if resp.status_code == 400 and "already exists" in resp.text.lower():
            # Two very different situations produce this same 400: (a) *this exact*
            # reference name is already registered (a genuine retry of this call --
            # nothing to do), or (b) a *different*, stale onboarding-agent-* reference
            # already claims this destination host, silently blocking ours from ever
            # being created. Tell these apart by checking what's actually registered.
            existing = {r.get("name"): r for r in self.list_remote_cluster_refs()}
            if ref_name in existing:
                return
            if self._remove_stale_refs_for_host(dest_host, keep_name=ref_name):
                retry = requests.post(
                    f"{self._mgmt_base_url()}/pools/default/remoteClusters",
                    data=payload, auth=self._auth(), verify=self._verify(), timeout=20,
                )
                if retry.ok:
                    return
                raise CouchbaseNativeError(
                    f"Failed to register destination as an XDCR remote cluster reference, even "
                    f"after removing a conflicting stale reference to the same host: "
                    f"{retry.status_code} {retry.text}"
                )
        raise CouchbaseNativeError(
            f"Failed to register destination as an XDCR remote cluster reference: "
            f"{resp.status_code} {resp.text}"
        )

    def create_replication(self, source_bucket: str, ref_name: str, dest_bucket: str) -> str:
        payload = {
            "fromBucket": source_bucket,
            "toCluster": ref_name,
            "toBucket": dest_bucket,
            "replicationType": "continuous",
            "type": "xmem",
        }
        resp = requests.post(
            f"{self._mgmt_base_url()}/controller/createReplication",
            data=payload, auth=self._auth(), verify=self._verify(), timeout=20,
        )
        if not resp.ok:
            raise CouchbaseNativeError(f"Failed to create XDCR replication: {resp.status_code} {resp.text}")
        data = resp.json()
        repl_id = data.get("id")
        if not repl_id:
            raise CouchbaseNativeError(f"XDCR replication created but no replication id was returned: {data}")
        return repl_id

    def get_progress(self, replication_id: str) -> dict[str, Any]:
        """Best-effort: returns {} (not an error) if the task can't be found or
        the tasks endpoint doesn't return the fields expected -- callers should
        treat a missing field as unknown, not zero."""
        try:
            resp = requests.get(
                f"{self._mgmt_base_url()}/pools/default/tasks",
                auth=self._auth(), verify=self._verify(), timeout=15,
            )
            resp.raise_for_status()
            for task in resp.json():
                if task.get("type") == "xdcr" and task.get("id") == replication_id:
                    return task
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch XDCR progress for %s: %s", replication_id, exc)
        return {}

    def remove_replication(self, replication_id: str) -> None:
        resp = requests.delete(
            f"{self._mgmt_base_url()}/controller/cancelXDCR/{quote(replication_id, safe='')}",
            auth=self._auth(), verify=self._verify(), timeout=20,
        )
        if resp.status_code not in (200, 404):
            logger.warning("Failed to cancel XDCR replication %s: %s %s", replication_id, resp.status_code, resp.text)

    def remove_remote_cluster_ref(self, ref_name: str) -> None:
        resp = requests.delete(
            f"{self._mgmt_base_url()}/pools/default/remoteClusters/{quote(ref_name, safe='')}",
            auth=self._auth(), verify=self._verify(), timeout=20,
        )
        if resp.status_code not in (200, 404):
            logger.warning("Failed to remove XDCR remote cluster ref %s: %s %s", ref_name, resp.status_code, resp.text)
