"""Where each source container lands in the destination. Shared by the
migration engine, the loader, fallback sync and the validator so the collision
check and the actual writes can never disagree.

A container maps to ONE OR MORE keyspaces (bucket.scope.collection). The first
one is the primary copy -- verification counts it, reconciliation compares it
against the source, and fallback sync reads changes back from it. Every target
receives the same upserts and deletes.
"""
from __future__ import annotations

from typing import NamedTuple

from app.core.connectors.couchbase_source import split_container
from app.core.couchbase_client import sanitize_couchbase_name
from app.models.enums import COUCHBASE_SOURCE_TYPES
from app.models.schemas import ContainerMigrationSpec, SourceConnectionConfig


class Keyspace(NamedTuple):
    bucket: str
    scope: str
    collection: str

    @property
    def name(self) -> str:
        return f"{self.bucket}.{self.scope}.{self.collection}"

    @property
    def short(self) -> str:
        """scope.collection, as the loader's journal and collection manager use it."""
        return f"{self.scope}.{self.collection}"

    @property
    def n1ql(self) -> str:
        return f"`{self.bucket}`.`{self.scope}`.`{self.collection}`"


def _name(explicit: str) -> str:
    """An explicitly chosen scope/collection name. `_default` is Couchbase's
    own name for the default scope/collection and must survive sanitizing
    (which strips leading underscores from derived names)."""
    return "_default" if explicit == "_default" else sanitize_couchbase_name(explicit)


def qualify_keyspace(keyspace: str, default_bucket: str) -> str:
    """Upgrade a "scope.collection" keyspace recorded by an earlier version to
    "bucket.scope.collection". Already-qualified names pass through."""
    if keyspace.count(".") >= 2:
        return keyspace
    return f"{default_bucket}.{keyspace}"


def parse_keyspace(keyspace: str, default_bucket: str) -> Keyspace:
    parts = qualify_keyspace(keyspace, default_bucket).split(".", 2)
    return Keyspace(parts[0], parts[1], parts[2])


def targets_for(source: SourceConnectionConfig, default_bucket: str, spec: ContainerMigrationSpec) -> list[Keyspace]:
    """Every keyspace one container is written to, primary first.

    Couchbase sources keep their own scope.collection names (cbbackupmgr and XDCR
    restore the structure as-is, and CAS-poll sync must write to the same place);
    only the bucket can differ per target. Every other source defaults to a scope
    named after the database/keyspace (or `_default`) and a collection named after
    the container, both sanitized, unless the target names them explicitly."""
    out: list[Keyspace] = []
    for t in spec.resolved_targets():
        bucket = t.bucket or default_bucket
        if source.source_type in COUCHBASE_SOURCE_TYPES:
            scope, collection = split_container(spec.container_name)
        else:
            default_scope = sanitize_couchbase_name(source.database) if source.database else "_default"
            scope = _name(t.scope) if t.scope else default_scope
            collection = _name(t.collection) if t.collection else sanitize_couchbase_name(spec.container_name)
        ks = Keyspace(bucket, scope, collection)
        if ks not in out:
            out.append(ks)
    return out


def target_for(source: SourceConnectionConfig, default_bucket: str, spec: ContainerMigrationSpec) -> Keyspace:
    """The primary keyspace for one container."""
    return targets_for(source, default_bucket, spec)[0]


def target_map(
    source: SourceConnectionConfig, default_bucket: str, containers: list[ContainerMigrationSpec],
) -> dict[str, list[Keyspace]]:
    return {c.container_name: targets_for(source, default_bucket, c) for c in containers if c.include}


def all_keyspaces(targets: dict[str, list[Keyspace]]) -> set[str]:
    return {ks.name for lst in targets.values() for ks in lst}


def buckets_in(targets: dict[str, list[Keyspace]]) -> list[str]:
    seen: list[str] = []
    for lst in targets.values():
        for ks in lst:
            if ks.bucket not in seen:
                seen.append(ks.bucket)
    return seen
