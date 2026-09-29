"""Test configuration. Point every file the app writes at a temp directory
before any app module (and its cached settings) is imported."""
from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="coa-tests-")
os.environ.setdefault("MIGRATION_STATE_FILE", os.path.join(_TMP, "state", "migrations.json"))
os.environ.setdefault("SCRATCH_DIR", os.path.join(_TMP, "scratch"))
os.environ.setdefault("STATE_ENCRYPTION_KEY", "Lz3wVX7Cq8Gl3hH2m0Vnq6r1C9Y5t2uOe4dKf7bQx0A=")
os.environ.setdefault("CDC_BATCH_MAX_WAIT_MS", "20")
os.environ.setdefault("CDC_RETRY_MAX_BACKOFF_S", "1")
os.environ.setdefault("CDC_WRITE_RETRIES", "1")
os.environ.setdefault("VERIFY_SOURCE_COUNTS", "true")
os.environ.setdefault("AUTO_RESUME_ON_STARTUP", "false")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_store():
    """Each test gets an empty migration store."""
    from app.core.store import MigrationStore

    path = os.environ["MIGRATION_STATE_FILE"]
    if os.path.exists(path):
        os.unlink(path)
    MigrationStore._instance = None
    yield
    MigrationStore._instance = None
