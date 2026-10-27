"""Voice Studio storage (MongoDB): voice records, the prepared clip (GridFS), usage events for the rate limits, and the spend counter.

Everything is looked up by `(tenant_id, id)`: a record of another tenant is simply not found. The clip is the PREPARED one (mono, 24 kHz, 16-bit, trimmed); the original
upload is not kept. No audio and no text is ever written anywhere but the clip bucket. See docs/VOICE_STUDIO_DESIGN.md section 7 for what is and is not provided
(encryption at rest, retention)."""
from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

from gridfs.asynchronous import AsyncGridFSBucket
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.ids import utcnow
from app.persistence.db import Database

BUCKET = "voice_clips"
STATUSES = ("active", "disabled", "deleted")


def new_voice_id() -> str:
    return uuid.uuid4().hex[:24]


def worker_name_for(voice_id: str) -> str:
    """The name of the voice on the TTS worker: unique across tenants, and valid for the worker (letters, digits, _, -; at most 40) and for `tts_voice`."""
    return f"v_{voice_id}"


def voice_out(doc: dict) -> dict:
    """What the API shows of a voice: no storage ids, no audio."""
    return {"id": doc["_id"], "name": doc["name"], "status": doc["status"], "worker_name": doc["worker_name"], "duration_s": doc.get("duration_s"),
            "checksum": doc.get("checksum"), "report": doc.get("report"), "consent_ref": doc.get("consent_ref"), "created_by": doc.get("created_by"),
            "created_at": doc.get("created_at"), "updated_at": doc.get("updated_at")}


class VoiceStore:
    def __init__(self, db: Database):
        self.db = db

    @property
    def bucket(self) -> AsyncGridFSBucket:
        return AsyncGridFSBucket(self.db.db, bucket_name=BUCKET)

    # ------------------------------------------------------------------ voices
    async def create(self, tenant_id: str, name: str, wav: bytes, report: dict, consent_ref: dict, created_by: str) -> dict:
        """Store the clip and the record. Raises DuplicateKeyError when the tenant already has a voice with this name (any case)."""
        vid, now = new_voice_id(), utcnow()
        gid = await self.bucket.upload_from_stream(vid, wav, metadata={"tenant_id": tenant_id, "voice_id": vid})
        doc = {"_id": vid, "tenant_id": tenant_id, "name": name, "name_lower": name.lower(), "worker_name": worker_name_for(vid), "status": "active",
               "checksum": hashlib.sha256(wav).hexdigest(), "gridfs_id": gid, "duration_s": report.get("duration_s"), "report": report,
               "consent_ref": consent_ref, "created_by": created_by, "created_at": now, "updated_at": now}
        try:
            await self.db["voices"].insert_one(doc)
        except Exception:
            await self.bucket.delete(gid)      # no orphan clip when the record could not be written
            raise
        return doc

    async def get(self, tenant_id: str, voice_id: str) -> dict | None:
        if not isinstance(voice_id, str) or len(voice_id) > 64:
            return None
        return await self.db["voices"].find_one({"_id": voice_id, "tenant_id": tenant_id, "status": {"$ne": "deleted"}})

    async def list(self, tenant_id: str) -> list[dict]:
        return await self.db["voices"].find({"tenant_id": tenant_id, "status": {"$ne": "deleted"}}).sort("created_at", -1).to_list(200)

    async def count_active(self, tenant_id: str) -> int:
        return await self.db["voices"].count_documents({"tenant_id": tenant_id, "status": {"$in": ["active", "disabled"]}})

    async def set_status(self, tenant_id: str, voice_id: str, status: str, by: str) -> dict | None:
        assert status in STATUSES
        now = utcnow()
        upd = {"status": status, "updated_at": now, f"{status}_by": by, f"{status}_at": now}
        if status == "deleted":
            upd["name_lower"] = f"~deleted~{voice_id}"     # frees the name; the tombstone keeps consent_ref and ids for the audit trail
        return await self.db["voices"].find_one_and_update({"_id": voice_id, "tenant_id": tenant_id, "status": {"$ne": "deleted"}}, {"$set": upd}, return_document=ReturnDocument.AFTER)

    async def read_clip(self, voice: dict) -> bytes:
        stream = await self.bucket.open_download_stream(voice["gridfs_id"])
        return await stream.read()

    async def erase_clip(self, voice: dict) -> None:
        """Delete the audio. The record stays as a tombstone (no audio)."""
        if voice.get("gridfs_id") is not None:
            await self.bucket.delete(voice["gridfs_id"])
            await self.db["voices"].update_one({"_id": voice["_id"], "tenant_id": voice["tenant_id"]}, {"$set": {"gridfs_id": None}})

    # ------------------------------------------------------------------ usage limits
    async def claim(self, tenant_id: str, kind: str, limit: int, window: timedelta) -> bool:
        """Record one event of `kind` and say whether the tenant is still within `limit` events per `window`. An over-limit event is not kept.
        (Insert first, then count: two requests at once cannot both slip under the limit.)"""
        now = utcnow()
        r = await self.db["voice_usage"].insert_one({"tenant_id": tenant_id, "kind": kind, "ts": now, "purge_at": now + timedelta(days=2)})
        n = await self.db["voice_usage"].count_documents({"tenant_id": tenant_id, "kind": kind, "ts": {"$gte": now - window}})
        if n > limit:
            await self.db["voice_usage"].delete_one({"_id": r.inserted_id})
            return False
        return True

    async def usage(self, tenant_id: str, kind: str, window: timedelta) -> int:
        return await self.db["voice_usage"].count_documents({"tenant_id": tenant_id, "kind": kind, "ts": {"$gte": utcnow() - window}})

    # ------------------------------------------------------------------ spend counter (micro-US-dollars per UTC month)
    @staticmethod
    def period() -> str:
        return utcnow().strftime("%Y-%m")

    async def spend_reserve(self, tenant_id: str, micro: int, cap_micro: int) -> str | None:
        """Add `micro` to this month's counter only if the result stays within `cap_micro` (atomic). Returns the period it was added to, or None when refused."""
        period = self.period()
        if micro > cap_micro:
            return None
        coll = self.db["voice_spend"]
        try:
            await coll.update_one({"tenant_id": tenant_id, "period": period}, {"$setOnInsert": {"micro_usd": 0}}, upsert=True)
        except DuplicateKeyError:
            pass                                            # created by a concurrent request: fine
        r = await coll.update_one({"tenant_id": tenant_id, "period": period, "micro_usd": {"$lte": cap_micro - micro}},
                                  {"$inc": {"micro_usd": micro}, "$set": {"updated_at": utcnow()}})
        return period if r.modified_count == 1 else None

    async def spend_adjust(self, tenant_id: str, period: str, delta_micro: int) -> None:
        """Replace a reservation by the measured cost (negative delta = refund)."""
        if delta_micro:
            await self.db["voice_spend"].update_one({"tenant_id": tenant_id, "period": period}, {"$inc": {"micro_usd": delta_micro}, "$set": {"updated_at": utcnow()}})

    async def spend_get(self, tenant_id: str) -> int:
        doc = await self.db["voice_spend"].find_one({"tenant_id": tenant_id, "period": self.period()})
        return int(doc["micro_usd"]) if doc else 0
