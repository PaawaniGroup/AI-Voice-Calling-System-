"""MongoDB connection, schema versioning and indexes (dedicated `mme_telephony` database)."""
from __future__ import annotations

import logging
import time
from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.core.ids import utcnow
from config.settings import Settings

log = logging.getLogger("mme_telephony.db")

SCHEMA_VERSION = 4

COLLECTIONS = [
    "calls", "call_events", "idempotency", "recordings", "audit", "tenant_policies",
    "inbound_routes", "webhook_endpoints", "webhook_deliveries", "provider_dedup", "sessions", "ai_sessions", "ai_config",
    "users", "_schema",
]


class Database:
    def __init__(self, settings: Settings):
        self.s = settings
        self.client: AsyncMongoClient | None = None
        self.db: AsyncDatabase | None = None
        self.last_ping_ms: float | None = None
        self.failures = 0

    async def connect(self) -> None:
        self.client = AsyncMongoClient(
            self.s.mongo_uri.get_secret_value(),
            tz_aware=True,
            connectTimeoutMS=self.s.mongo_connect_timeout_ms,
            serverSelectionTimeoutMS=self.s.mongo_server_selection_timeout_ms,
            maxPoolSize=self.s.mongo_max_pool,
            minPoolSize=self.s.mongo_min_pool,
            retryWrites=True,
            appname=self.s.app_name,
        )
        self.db = self.client[self.s.mongo_db]
        await self.ping()
        if self.s.mongo_init_indexes:
            await self.ensure_schema()

    def __getitem__(self, name: str):
        assert self.db is not None, "database not connected"
        return self.db[name]

    async def ping(self) -> float:
        assert self.client is not None
        t = time.perf_counter()
        try:
            await self.client.admin.command("ping")
        except Exception:
            self.failures += 1
            raise
        self.last_ping_ms = (time.perf_counter() - t) * 1000
        return self.last_ping_ms

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None

    # ------------------------------------------------------------------ schema
    async def ensure_schema(self) -> None:
        """Idempotent index creation + schema version record.

        Strategy: additive, forward-only. `_schema` holds {_id:'version', v:N}. A service
        that finds v > SCHEMA_VERSION refuses to start (downgrade protection).
        """
        cur = await self["_schema"].find_one({"_id": "version"})
        if cur and cur["v"] > SCHEMA_VERSION:
            raise RuntimeError(f"database schema v{cur['v']} is newer than this service (v{SCHEMA_VERSION})")
        calls, ev =self["calls"], self["call_events"]
        await calls.create_index([("tenant_id", ASCENDING), ("created_at", DESCENDING)], name="tenant_created")
        await calls.create_index([("tenant_id", ASCENDING), ("state", ASCENDING), ("created_at", DESCENDING)],
                                 name="tenant_state_created")
        await calls.create_index([("state", ASCENDING), ("queued_at", ASCENDING)], name="state_queued")
        await calls.create_index([("fs_uuid", ASCENDING)], name="fs_uuid", sparse=True)
        await calls.create_index([("fs_uuids", ASCENDING)], name="fs_uuids", sparse=True)
        await calls.create_index([("job_uuid", ASCENDING)], name="job_uuid", sparse=True)
        # backstop: one inbound call record per FreeSWITCH channel even if two events race past the in-memory guard
        await calls.create_index([("fs_uuid", ASCENDING)], name="uniq_inbound_fs_uuid", unique=True,
                                 partialFilterExpression={"direction": "inbound", "fs_uuid": {"$type": "string"}})
        await calls.create_index([("provider", ASCENDING), ("provider_call_id", ASCENDING)],
                                 name="provider_call", sparse=True)
        # backstop: duplicate Plivo Answer callbacks (retries, races) can never create a second inbound call
        await calls.create_index([("provider", ASCENDING), ("provider_call_id", ASCENDING)], name="uniq_inbound_provider_call",
                                 unique=True, partialFilterExpression={"direction": "inbound", "provider_call_id": {"$type": "string"}})
        await calls.create_index([("provider", ASCENDING), ("state", ASCENDING), ("stream_expected_by", ASCENDING)],
                                 name="plivo_stream_watch", sparse=True)
        await calls.create_index([("created_at", ASCENDING), ("tenant_id", ASCENDING), ("state", ASCENDING)],
                                 name="reporting")
        await calls.create_index([("purge_at", ASCENDING)], name="purge_ttl", expireAfterSeconds=0)
        await ev.create_index([("call_id", ASCENDING), ("dedup_id", ASCENDING)], name="dedup", unique=True)
        await ev.create_index([("tenant_id", ASCENDING), ("call_id", ASCENDING), ("ts", ASCENDING)], name="by_call")
        await ev.create_index([("purge_at", ASCENDING)], name="purge_ttl", expireAfterSeconds=0)
        await self["idempotency"].create_index(
            [("tenant_id", ASCENDING), ("operation", ASCENDING), ("key", ASCENDING)], name="uniq", unique=True)
        await self["idempotency"].create_index([("purge_at", ASCENDING)], name="purge_ttl", expireAfterSeconds=0)
        await self["recordings"].create_index([("tenant_id", ASCENDING), ("call_id", ASCENDING)], name="by_call")
        await self["recordings"].create_index([("status", ASCENDING), ("created_at", ASCENDING)], name="status")
        await self["audit"].create_index([("tenant_id", ASCENDING), ("ts", DESCENDING)], name="tenant_ts")
        await self["audit"].create_index([("purge_at", ASCENDING)], name="purge_ttl", expireAfterSeconds=0)
        await self["tenant_policies"].create_index([("tenant_id", ASCENDING)], name="tenant", unique=True)
        await self["inbound_routes"].create_index([("number", ASCENDING)], name="number", unique=True)
        await self["inbound_routes"].create_index([("tenant_id", ASCENDING)], name="tenant")
        await self["webhook_endpoints"].create_index([("tenant_id", ASCENDING)], name="tenant")
        await self["webhook_deliveries"].create_index(
            [("status", ASCENDING), ("next_attempt_at", ASCENDING)], name="due")
        await self["webhook_deliveries"].create_index(
            [("endpoint_id", ASCENDING), ("event_id", ASCENDING)], name="uniq_event", unique=True)
        await self["webhook_deliveries"].create_index([("tenant_id", ASCENDING), ("created_at", DESCENDING)], name="tenant")
        await self["webhook_deliveries"].create_index([("purge_at", ASCENDING)], name="purge_ttl", expireAfterSeconds=0)
        await self["provider_dedup"].create_index([("key", ASCENDING)], name="key", unique=True)
        await self["provider_dedup"].create_index([("purge_at", ASCENDING)], name="purge_ttl", expireAfterSeconds=0)
        await self["sessions"].create_index([("tenant_id", ASCENDING), ("call_ref", ASCENDING)], name="uniq_call_ref", unique=True,
                                            partialFilterExpression={"call_ref": {"$type": "string"}})
        await self["sessions"].create_index([("tenant_id", ASCENDING), ("created_at", DESCENDING)], name="tenant_created")
        await self["sessions"].create_index([("tenant_id", ASCENDING), ("status", ASCENDING)], name="tenant_status")
        await self["sessions"].create_index([("call_id", ASCENDING)], name="call_id", sparse=True)
        await self["sessions"].create_index([("purge_at", ASCENDING)], name="purge_ttl", expireAfterSeconds=0)
        await self["ai_sessions"].create_index([("session_id", ASCENDING)], name="session", unique=True)
        await self["ai_sessions"].create_index([("tenant_id", ASCENDING), ("state", ASCENDING)], name="tenant_state")
        await self["ai_config"].create_index([("tenant_id", ASCENDING)], name="tenant", unique=True)
        await self["users"].create_index([("username", ASCENDING)], name="username", unique=True)
        await self["users"].create_index([("tenant_id", ASCENDING)], name="tenant")
        await self["_schema"].update_one(
            {"_id": "version"}, {"$set": {"v": SCHEMA_VERSION, "updated_at": utcnow()}}, upsert=True)
        log.info("schema ready v%s", SCHEMA_VERSION)

    async def stats(self) -> dict[str, Any]:
        return {"last_ping_ms": self.last_ping_ms, "failures": self.failures}
