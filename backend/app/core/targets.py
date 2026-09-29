"""Where each source container lands in the destination bucket. Shared by the
migration engine and the validator so the collision check and the actual
writes can never disagree."""
from __future__ import annotations

from app.core.connectors.couchbase_source import split_container
from app.core.couchbase_client import sanitize_couchbase_name
from app.models.enums import COUCHBASE_SOURCE_TYPES
from app.models.schemas import ContainerMigrationSpec, SourceConnectionConfig


def target_for(source: SourceConnectionConfig, spec: ContainerMigrationSpec) -> tuple[str, str]:
    """(scope, collection) for one container.

    Couchbase sources keep their own scope.collection names (cbbackupmgr and XDCR
    restore the structure as-is, and CAS-poll sync must write to the same place).
    Every other source defaults to a scope named after the database/keyspace (or
    `_default`) and a collection named after the container, both sanitized, unless
    the wizard set explicit names."""
    if source.source_type in COUCHBASE_SOURCE_TYPES:
        return split_container(spec.container_name)
    default_scope = sanitize_couchbase_name(source.database) if source.database else "_default"
    scope = sanitize_couchbase_name(spec.target_scope_name) if spec.target_scope_name else default_scope
    collection = (
        sanitize_couchbase_name(spec.target_collection_name) if spec.target_collection_name
        else sanitize_couchbase_name(spec.container_name)
    )
    return scope, collection


def target_map(source: SourceConnectionConfig, containers: list[ContainerMigrationSpec]) -> dict[str, tuple[str, str]]:
    return {c.container_name: target_for(source, c) for c in containers if c.include}
