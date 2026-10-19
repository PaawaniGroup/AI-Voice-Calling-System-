import uuid
from datetime import datetime, timezone


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    # Mongo stores millisecond precision UTC datetimes; keep tz-aware in Python.
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def aware(dt: datetime | None) -> datetime | None:
    """pymongo returns naive UTC datetimes unless tz_aware=True; normalise."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
