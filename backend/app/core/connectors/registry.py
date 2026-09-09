"""Maps SourceType -> SourceConnector implementation. Import connector modules
lazily inside get_connector() rather than at module load time, since each
connector's third-party SDK (pymongo, boto3, redis, cassandra-driver, azure-cosmos)
is an optional dependency only needed if that source type is actually used --
keeps startup fast and avoids a hard crash if, say, the Cassandra driver's compiled
extension isn't available on some platform."""
from __future__ import annotations

from app.core.connectors.base import SourceConnector
from app.models.enums import SourceType
from app.models.schemas import SourceConnectionConfig


def get_connector(config: SourceConnectionConfig) -> SourceConnector:
    if config.source_type == SourceType.MONGODB:
        from app.core.connectors.mongodb import MongoDBConnector

        return MongoDBConnector(config)
    if config.source_type == SourceType.DYNAMODB:
        from app.core.connectors.dynamodb import DynamoDBConnector

        return DynamoDBConnector(config)
    if config.source_type == SourceType.REDIS:
        from app.core.connectors.redis_connector import RedisConnector

        return RedisConnector(config)
    if config.source_type == SourceType.CASSANDRA:
        from app.core.connectors.cassandra_connector import CassandraConnector

        return CassandraConnector(config)
    if config.source_type == SourceType.COSMOSDB:
        from app.core.connectors.cosmosdb import CosmosDBConnector

        return CosmosDBConnector(config)
    if config.source_type in (SourceType.COUCHBASE, SourceType.COUCHBASE_ENTERPRISE, SourceType.COUCHBASE_CAPELLA):
        from app.core.connectors.couchbase_source import CouchbaseSourceConnector

        return CouchbaseSourceConnector(config)
    raise ValueError(f"Unsupported source_type: {config.source_type}")
