"""
CloudDrop — Short-Link Manager (Write-Back Cache: Redis → PostgreSQL)

Implements Write-Back (Write-Behind) caching:
    WRITE PATH:  App → Redis SET + dirty marker → Background thread flushes to PostgreSQL
    READ PATH:   App → Redis GET (hit?) → miss → PostgreSQL SELECT → populate Redis cache

Each link has a TTL of 24 hours in Redis, matching the S3 lifecycle policy.
When download counter reaches 0, the Redis key AND the S3 object are deleted.
The background sync thread (db_sync.py) flushes dirty entries to PostgreSQL.
"""

import json
import string
import secrets
import logging
import redis

from config import settings

logger = logging.getLogger("clouddrop.shortener")

# ── Base62 Alphabet ─────────────────────────────────────────────────────────

BASE62_ALPHABET = string.ascii_letters + string.digits  # a-zA-Z0-9

# ── Redis Client ────────────────────────────────────────────────────────────

_redis_client = None


def _get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        try:
            kwargs = {
                "host": settings.REDIS_HOST,
                "port": settings.REDIS_PORT,
                "db": settings.REDIS_DB,
                "decode_responses": True,
                "socket_connect_timeout": 5,
                "socket_timeout": 5,
            }
            if settings.REDIS_PASSWORD:
                kwargs["password"] = settings.REDIS_PASSWORD
            
            if settings.REDIS_USE_TLS:
                kwargs["ssl"] = True
                kwargs["ssl_cert_reqs"] = "none"  # Bypass strict AWS CA verification natively

            # AWS ElastiCache often defaults to 'Encryption in Transit' (TLS)
            # If we don't use TLS but the server expects it, it hangs silently.
            if settings.REDIS_USE_TLS:
                r = redis.Redis(**kwargs)
                r.ping()
                _redis_client = r
                logger.info("Connected to Redis using explicit TLS settings.")
            else:
                # Try plaintext first, fallback to TLS if the server yells at us
                try:
                    r = redis.Redis(**kwargs)
                    r.ping()
                    _redis_client = r
                    logger.info("Connected to Redis using plaintext.")
                except Exception as e:
                    logger.warning(f"Plaintext connection failed ({e}), trying TLS as fallback.")
                    kwargs["ssl"] = True
                    kwargs["ssl_cert_reqs"] = "none"
                    r = redis.Redis(**kwargs)
                    r.ping()
                    _redis_client = r
                    logger.info("Connected to Redis using TLS fallback.")
        except Exception as e:
            logger.error(f"FATAL: All Redis connection attempts failed: {e}")
            raise
    return _redis_client


# ── Short ID Generation ─────────────────────────────────────────────────────

def generate_short_id(length: int = 8) -> str:
    """Generate a cryptographically random Base62 short ID."""
    return "".join(secrets.choice(BASE62_ALPHABET) for _ in range(length))


# ── Write-Back Dirty Markers ────────────────────────────────────────────────
# When we write to Redis, we also set a "dirty:{short_id}" key that tells
# the background sync thread (db_sync.py) this entry needs flushing to PostgreSQL.

DIRTY_PREFIX = "dirty:"


def _mark_dirty(short_id: str, action: str, extra: dict = None):
    """Mark a Redis entry as dirty (needs flushing to PostgreSQL)."""
    r = _get_redis()
    meta = {"action": action}
    if extra:
        meta.update(extra)
    # Dirty marker TTL = 1 hour (safety net — if sync fails repeatedly, don't keep forever)
    r.setex(f"{DIRTY_PREFIX}{short_id}", 3600, json.dumps(meta))


# ── Link CRUD ────────────────────────────────────────────────────────────────

def create_link(
    short_id: str,
    s3_key: str,
    original_filename: str,
    content_type: str,
    max_downloads: int | None = None,
    encrypted_dek_b64: str = None,
    iv_b64: str = None,
    uploader_email: str = None,
    file_size_bytes: int = None,
) -> dict:
    """
    Store a new short-link mapping in Redis with a 24-hour TTL.
    Sets a dirty marker so the background thread flushes to PostgreSQL.

    Write-Back pattern:
        1. Redis SET (immediate, fast)
        2. Dirty marker SET (tells sync thread to flush to DB)
        3. Sync thread flushes to PostgreSQL within DB_SYNC_INTERVAL_SECONDS
    """
    r = _get_redis()

    if max_downloads is None:
        max_downloads = settings.MAX_DOWNLOADS

    link_data = {
        "s3_key": s3_key,
        "original_filename": original_filename,
        "content_type": content_type,
        "downloads_left": max_downloads,
        "max_downloads": max_downloads,
        "encrypted_dek_b64": encrypted_dek_b64,
        "iv_b64": iv_b64,
        "uploader_email": uploader_email,
        "file_size_bytes": file_size_bytes,
    }

    # Step 1: Write to Redis (immediate)
    r.setex(
        name=f"link:{short_id}",
        time=settings.LINK_TTL_SECONDS,
        value=json.dumps(link_data),
    )

    # Step 2: Mark as dirty for Write-Back flush
    _mark_dirty(short_id, "create", {
        "original_filename": original_filename,
        "file_size_bytes": file_size_bytes,
    })

    logger.info(f"Created link: {short_id} → s3://{settings.S3_BUCKET}/{s3_key} [Write-Back: pending DB flush]")
    return link_data


def get_link(short_id: str) -> dict | None:
    """
    Retrieve link metadata. Cache-Aside read path:
        1. Check Redis cache → HIT → return
        2. MISS → Check PostgreSQL → populate Redis cache → return
        3. Not in either → return None
    """
    r = _get_redis()

    # Step 1: Check Redis cache
    raw = r.get(f"link:{short_id}")
    if raw is not None:
        data = json.loads(raw)
        # Attach remaining TTL for display
        ttl = r.ttl(f"link:{short_id}")
        data["ttl_seconds"] = max(ttl, 0)
        return data

    # Step 2: Cache miss — check PostgreSQL
    try:
        from database import db_get_link
        db_data = db_get_link(short_id)
        if db_data is None:
            return None

        # Populate Redis cache from PostgreSQL (Cache-Aside)
        r.setex(
            name=f"link:{short_id}",
            time=settings.LINK_TTL_SECONDS,
            value=json.dumps(db_data),
        )
        ttl = r.ttl(f"link:{short_id}")
        db_data["ttl_seconds"] = max(ttl, 0)
        logger.info(f"Cache miss for {short_id} — populated from PostgreSQL")
        return db_data
    except Exception as e:
        logger.warning(f"PostgreSQL fallback failed for {short_id}: {e}")
        return None


def decrement_downloads(short_id: str) -> int:
    """
    Decrement the download counter. Write-Back pattern:
        1. Update Redis immediately
        2. Mark dirty for background flush to PostgreSQL
    """
    r = _get_redis()
    raw = r.get(f"link:{short_id}")
    if raw is None:
        return -1

    data = json.loads(raw)
    data["downloads_left"] = max(0, data["downloads_left"] - 1)
    remaining = data["downloads_left"]

    if remaining <= 0:
        # Link exhausted — delete from Redis
        r.delete(f"link:{short_id}")
        _mark_dirty(short_id, "exhaust", {
            "original_filename": data.get("original_filename", ""),
        })
        logger.info(f"Link {short_id} exhausted — removed from Redis [Write-Back: pending DB flush]")
    else:
        # Preserve original TTL, update data
        ttl = r.ttl(f"link:{short_id}")
        if ttl > 0:
            r.setex(f"link:{short_id}", ttl, json.dumps(data))
        else:
            r.set(f"link:{short_id}", json.dumps(data))

        _mark_dirty(short_id, "decrement", {
            "original_filename": data.get("original_filename", ""),
        })

    return remaining


def is_slug_available(slug: str) -> bool:
    """Check if a custom slug is available (check Redis and PostgreSQL)."""
    r = _get_redis()
    # Check Redis first
    if r.exists(f"link:{slug}"):
        return False
    # Also check PostgreSQL in case it was flushed but expired from Redis
    try:
        from database import db_get_link
        if db_get_link(slug) is not None:
            return False
    except Exception:
        pass
    return True


# ── User Upload History ─────────────────────────────────────────────────────

USER_UPLOADS_PREFIX = "user_uploads:"


def track_user_upload(email: str, short_id: str, original_filename: str, max_downloads: int):
    """Track an upload for a logged-in user (Redis cache + audit via Write-Back)."""
    r = _get_redis()
    upload_entry = json.dumps({
        "short_id": short_id,
        "filename": original_filename,
        "max_downloads": max_downloads,
    })
    r.lpush(f"{USER_UPLOADS_PREFIX}{email}", upload_entry)
    # Keep only last 50 uploads per user
    r.ltrim(f"{USER_UPLOADS_PREFIX}{email}", 0, 49)
    # Set TTL to 7 days for upload history
    r.expire(f"{USER_UPLOADS_PREFIX}{email}", 604800)
    logger.info(f"Tracked upload {short_id} for user {email}")


def get_user_uploads(email: str) -> list[dict]:
    """
    Get upload history for a user. Cache-Aside:
        1. Check Redis list → if entries exist, use them
        2. If empty → query PostgreSQL read replica → repopulate Redis
    """
    r = _get_redis()
    raw_list = r.lrange(f"{USER_UPLOADS_PREFIX}{email}", 0, -1)

    if raw_list:
        # Use Redis cached list
        uploads = []
        for raw in raw_list:
            entry = json.loads(raw)
            link = get_link(entry["short_id"])
            if link is not None:
                entry["downloads_left"] = link["downloads_left"]
                entry["active"] = True
            else:
                entry["downloads_left"] = 0
                entry["active"] = False
            uploads.append(entry)
        return uploads

    # Cache miss — try PostgreSQL
    try:
        from database import db_get_user_uploads
        db_uploads = db_get_user_uploads(email)
        if db_uploads:
            # Repopulate Redis cache
            for upload in db_uploads:
                entry = json.dumps({
                    "short_id": upload["short_id"],
                    "filename": upload["filename"],
                    "max_downloads": upload["max_downloads"],
                })
                r.rpush(f"{USER_UPLOADS_PREFIX}{email}", entry)
            r.expire(f"{USER_UPLOADS_PREFIX}{email}", 604800)
            logger.info(f"Upload history cache miss for {email} — populated from PostgreSQL")
        return db_uploads
    except Exception as e:
        logger.warning(f"PostgreSQL fallback failed for user uploads {email}: {e}")
        return []
