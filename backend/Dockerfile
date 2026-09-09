# cbbackupmgr isn't distributed as a standalone package -- it ships inside the
# full Couchbase Server install. Pulling it (plus the shared libraries it's
# dynamically linked against) out of the official server image via a
# multi-stage build avoids installing the whole ~500MB+ server into this image
# just for one binary. Needed for Couchbase-to-Couchbase migrations only (see
# core/couchbase_native.py) -- every other source connector doesn't touch it.
#
# CONFIRMED BROKEN, then fixed (2026-07-30): setting LD_LIBRARY_PATH globally
# for the whole image made every process resolve libstdc++.so.6 from the
# Couchbase Enterprise image's (older, Ubuntu-based) copy instead of Debian's
# own -- including apt-get itself, which then failed with
# "libstdc++.so.6: version `CXXABI_1.3.15' not found" partway through this
# same build (apt's libapt-pkg.so.7.0 needs the newer Debian libstdc++).
# Fix: don't export LD_LIBRARY_PATH image-wide. Only cbbackupmgr needs the
# Couchbase-provided shared libraries, so CbBackupMgrRunner._run() in
# core/couchbase_native.py sets LD_LIBRARY_PATH on that one subprocess's own
# environment instead, leaving every other process (apt, pip, python, uvicorn)
# to resolve libstdc++ and friends from the base image as normal.
FROM couchbase:enterprise-7.6.2 AS cbtools

FROM python:3.11-slim AS base

WORKDIR /app

COPY --from=cbtools /opt/couchbase/bin/cbbackupmgr /opt/couchbase/bin/cbbackupmgr
COPY --from=cbtools /opt/couchbase/lib /opt/couchbase/lib
# PATH is safe to extend image-wide (it only affects which cbbackupmgr binary
# is *found*, not which shared libraries anything links against).
# LD_LIBRARY_PATH is deliberately NOT set here -- see the comment above.
ENV PATH="/opt/couchbase/bin:${PATH}" \
    COUCHBASE_TOOLS_LIB="/opt/couchbase/lib"

COPY requirements.txt .

# Trust an optional corporate/proxy root CA before installing anything from PyPI --
# see scripts/setup-corporate-ca.sh at the repo root. On a corporate laptop behind
# an SSL-inspecting proxy (Zscaler, Netskope, etc.), pip verifies against its own
# bundled CA store, not the OS trust store Docker Desktop's own image pulls rely
# on, so intercepted HTTPS traffic to PyPI fails with CERTIFICATE_VERIFY_FAILED
# unless that proxy's root cert is trusted here too. A no-op on machines that
# don't need it (certs/ stays empty).
COPY certs/ /usr/local/share/ca-certificates/extra/
RUN if find /usr/local/share/ca-certificates/extra -name '*.crt' 2>/dev/null | grep -q .; then \
      apt-get update && \
      apt-get install -y --no-install-recommends ca-certificates && \
      update-ca-certificates && \
      rm -rf /var/lib/apt/lists/*; \
    fi
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt

# The Cassandra driver's optional C extension (cython-based murmur3/libev) needs a
# compiler toolchain to build from source on platforms without a prebuilt wheel;
# the driver works fine (just slightly slower token-hashing) without it, but build
# tools are kept here so `pip install` never fails outright for lack of them.
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
