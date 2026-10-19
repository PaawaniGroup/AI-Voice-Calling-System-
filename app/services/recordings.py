"""Recording policy, FreeSWITCH recording control, metadata, storage upload and authorised access.

Safe by default: a recording starts only when ALL of these hold: global recording_enabled, a consent mode,
a configured storage backend, and the tenant policy `recording` == "on". Metadata is marked `available`
only after the uploaded object is verified (size > 0 and head-checked for S3).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from pathlib import Path

from app.core.errors import Conflict, NotFound, Unavailable
from app.core.ids import new_id, utcnow
from app.observability import metrics as m
from app.telephony import dialstrings as ds
from config.settings import Settings

log = logging.getLogger("mme_telephony.recordings")


class StorageBackend:
    async def put(self, local_path: str, key: str) -> int: ...
    async def exists(self, key: str) -> int | None: ...
    async def presign(self, key: str, ttl: int) -> str: ...
    async def delete(self, key: str) -> None: ...


class LocalStorage(StorageBackend):
    """Development/test backend. Never returns permanent public URLs (only an opaque internal reference)."""

    def __init__(self, root: str):
        self.root = Path(root)

    def _p(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("unsafe key")
        return p

    async def put(self, local_path: str, key: str) -> int:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(lambda: p.write_bytes(Path(local_path).read_bytes()))
        return p.stat().st_size

    async def exists(self, key: str) -> int | None:
        p = self._p(key)
        return p.stat().st_size if p.exists() else None

    async def presign(self, key: str, ttl: int) -> str:
        raise Unavailable("local storage cannot issue download URLs", code="download_unsupported")

    async def delete(self, key: str) -> None:
        self._p(key).unlink(missing_ok=True)


class S3Storage(StorageBackend):
    def __init__(self, s: Settings):
        import boto3  # optional dependency, only imported when S3 is configured
        from botocore.config import Config

        self.s = s
        self.c = boto3.client("s3", region_name=s.s3_region,
                              config=Config(connect_timeout=5, read_timeout=s.s3_upload_timeout_s, retries={"max_attempts": s.s3_upload_retries}))

    def _extra(self) -> dict:
        if self.s.s3_kms_key_id:
            return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self.s.s3_kms_key_id}
        return {"ServerSideEncryption": "AES256"}

    async def put(self, local_path: str, key: str) -> int:
        await asyncio.to_thread(lambda: self.c.upload_file(local_path, self.s.s3_bucket, key, ExtraArgs=self._extra()))
        return (await self.exists(key)) or 0

    async def exists(self, key: str) -> int | None:
        try:
            r = await asyncio.to_thread(lambda: self.c.head_object(Bucket=self.s.s3_bucket, Key=key))
            return int(r["ContentLength"])
        except Exception:
            return None

    async def presign(self, key: str, ttl: int) -> str:
        return await asyncio.to_thread(lambda: self.c.generate_presigned_url(
            "get_object", Params={"Bucket": self.s.s3_bucket, "Key": key, "ResponseContentDisposition": "attachment"}, ExpiresIn=ttl))

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(lambda: self.c.delete_object(Bucket=self.s.s3_bucket, Key=key))


def make_storage(s: Settings) -> StorageBackend | None:
    if s.storage_backend == "local":
        return LocalStorage(s.fs_recordings_dir + "/_store")
    if s.storage_backend == "s3":
        return S3Storage(s)
    return None


def recording_allowed(s: Settings, policy: dict | None) -> tuple[bool, str]:
    if not s.recording_enabled:
        return False, "recording_disabled_globally"
    if not s.recording_consent_mode:
        return False, "consent_mode_missing"
    if s.storage_backend == "none":
        return False, "storage_not_configured"
    if (policy or {}).get("recording", "off") != "on":
        return False, "tenant_policy_off"
    return True, "ok"


async def start_recording_if_allowed(ctrl, call_id: str) -> None:
    call = await ctrl.repos.get_call(call_id)
    if not call or not call.get("fs_uuid") or ctrl.esl_cmd is None:
        return
    policy = await ctrl.repos.get_policy(call["tenant_id"])
    ok, why = recording_allowed(ctrl.s, policy)
    if not ok:
        return
    rid = new_id("rec")
    path = f"{ctrl.s.fs_recordings_dir.rstrip('/')}/{call['tenant_id']}/{rid}.wav"
    try:  # best effort: in production the spool directory is provisioned for the FreeSWITCH user
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    now = utcnow()
    doc = {"_id": rid, "tenant_id": call["tenant_id"], "call_id": call_id, "status": "recording",
           "local_path": path, "created_at": now, "started_at": now, "consent_mode": ctrl.s.recording_consent_mode,
           "retention_until": now + timedelta(days=ctrl.s.recording_retention_days), "storage": ctrl.s.storage_backend}
    await ctrl.repos.db["recordings"].insert_one(doc)
    try:
        await ctrl.esl_cmd.api(ds.record_start_command(call["fs_uuid"], path, ctrl.s.recording_max_seconds))
        await ctrl.repos.update_call(call_id, {}, add_to_set={"recording_ids": rid})
    except Exception as e:
        m.RECORDING_FAILURES.inc()
        await ctrl.repos.db["recordings"].update_one({"_id": rid}, {"$set": {"status": "failed", "error": type(e).__name__}})


async def finalize_recordings(repos, storage: StorageBackend | None, s: Settings) -> int:
    """Upload completed local recordings and verify them. Safe to run repeatedly."""
    n = 0
    if storage is None:
        return 0
    async for r in repos.db["recordings"].find({"status": {"$in": ["recording", "uploading"]}}).limit(50):
        call = await repos.get_call(r["call_id"])
        if not call or call["state"] not in ("completed", "failed"):
            continue
        p = Path(r["local_path"])
        if not p.exists() or p.stat().st_size == 0:
            await repos.db["recordings"].update_one({"_id": r["_id"]}, {"$set": {"status": "failed", "error": "no_audio_file"}})
            m.RECORDING_FAILURES.inc()
            continue
        key = f"{s.s3_prefix.rstrip('/')}/{r['tenant_id']}/{r['_id']}.wav"
        try:
            size = await storage.put(str(p), key)
            verified = await storage.exists(key)
            if not verified or verified != size or size <= 0:
                raise RuntimeError("verification_failed")
        except Exception as e:
            m.RECORDING_FAILURES.inc()
            await repos.db["recordings"].update_one({"_id": r["_id"]}, {"$set": {"status": "upload_failed", "error": type(e).__name__}})
            continue
        await repos.db["recordings"].update_one(
            {"_id": r["_id"]},
            {"$set": {"status": "available", "storage_key": key, "size_bytes": size, "duration_s": call.get("connected_duration_s"),
                      "verified_at": utcnow()}})
        try:
            os.remove(p)
        except OSError:
            pass
        n += 1
    return n


async def download_url(repos, storage: StorageBackend | None, s: Settings, tenant_id: str, recording_id: str) -> str:
    r = await repos.db["recordings"].find_one({"_id": recording_id, "tenant_id": tenant_id})
    if not r:
        raise NotFound("recording not found")
    if r["status"] != "available" or r.get("deleted"):
        raise Conflict("recording not available", code="recording_not_available")
    if storage is None:
        raise Unavailable("storage not configured", code="storage_not_configured")
    return await storage.presign(r["storage_key"], s.recording_url_ttl_s)


async def purge_expired(repos, storage: StorageBackend | None) -> int:
    n = 0
    async for r in repos.db["recordings"].find({"retention_until": {"$lt": utcnow()}, "deleted": {"$ne": True}}).limit(100):
        if storage and r.get("storage_key"):
            await storage.delete(r["storage_key"])
        await repos.db["recordings"].update_one({"_id": r["_id"]}, {"$set": {"deleted": True, "status": "deleted", "deleted_at": utcnow()}})
        n += 1
    return n
