"""
CloudDrop — Write-Back Sync Thread (Redis → PostgreSQL)

Runs a background daemon thread that periodically flushes "dirty" Redis
entries to PostgreSQL. This implements the Write-Back (Write-Behind)
caching strategy:

    1. App writes to Redis immediately (fast, ~0.5ms)
    2. App sets a "dirty:{short_id}" marker in Redis
    3. This thread wakes every DB_SYNC_INTERVAL_SECONDS (default: 10s)
    4. Scans for all "dirty:*" keys
    5. Batch-inserts/updates the corresponding link data into PostgreSQL
    6. Removes the "dirty:*" markers after successful flush

If the flush fails for a specific entry, the dirty marker is kept so
it will be retried on the next cycle.
"""

import json
import threading
import logging
import time

from config import settings

logger = logging.getLogger("clouddrop.db_sync")

# Prefix used to mark entries that need flushing to PostgreSQL
DIRTY_PREFIX = "dirty:"

_sync_thread = None
_stop_event = threading.Event()


def _flush_dirty_entries():
    """Scan Redis for dirty markers and flush to PostgreSQL."""
    # Import here to avoid circular imports at module level
    from shortener import _get_redis
    from database import db_create_link, db_decrement_downloads, db_log_action, db_get_link

    try:
        r = _get_redis()
    except Exception as e:
        logger.error(f"Sync: Cannot connect to Redis: {e}")
        return

    # Scan for all dirty:* keys
    cursor = 0
    dirty_keys = []
    while True:
        cursor, keys = r.scan(cursor=cursor, match=f"{DIRTY_PREFIX}*", count=100)
        dirty_keys.extend(keys)
        if cursor == 0:
            break

    if not dirty_keys:
        return

    logger.info(f"Sync: Found {len(dirty_keys)} dirty entries to flush")

    flushed = 0
    for dirty_key in dirty_keys:
        try:
            # dirty_key format: "dirty:{short_id}"
            short_id = dirty_key[len(DIRTY_PREFIX):]

            # Get the dirty action metadata
            dirty_meta_raw = r.get(dirty_key)
            if dirty_meta_raw is None:
                # Already cleaned up by another process
                continue

            dirty_meta = json.loads(dirty_meta_raw)
            action = dirty_meta.get("action", "create")

            if action == "create":
                # Flush a new link creation to PostgreSQL
                link_raw = r.get(f"link:{short_id}")
                if link_raw is None:
                    # Link already expired from Redis — skip
                    r.delete(dirty_key)
                    continue

                link_data = json.loads(link_raw)
                try:
                    db_create_link(
                        short_id=short_id,
                        s3_key=link_data["s3_key"],
                        original_filename=link_data["original_filename"],
                        content_type=link_data["content_type"],
                        max_downloads=link_data["max_downloads"],
                        encrypted_dek_b64=link_data.get("encrypted_dek_b64"),
                        iv_b64=link_data.get("iv_b64"),
                        uploader_email=link_data.get("uploader_email"),
                    )
                    # Log the upload action
                    db_log_action(
                        short_id=short_id,
                        action="upload",
                        uploader_email=link_data.get("uploader_email"),
                        original_filename=link_data["original_filename"],
                        file_size_bytes=link_data.get("file_size_bytes"),
                    )
                except Exception as e:
                    # Check if it's a duplicate (already flushed by a previous cycle)
                    if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                        logger.debug(f"Sync: Link {short_id} already in DB (duplicate), removing dirty marker")
                    else:
                        logger.error(f"Sync: Failed to flush link {short_id}: {e}")
                        continue  # Keep dirty marker for retry

            elif action == "decrement":
                # Flush a download counter decrement
                try:
                    db_decrement_downloads(short_id)
                    db_log_action(
                        short_id=short_id,
                        action="download",
                        original_filename=dirty_meta.get("original_filename", ""),
                    )
                except Exception as e:
                    logger.error(f"Sync: Failed to flush decrement for {short_id}: {e}")
                    continue  # Keep dirty marker for retry

            elif action == "exhaust":
                # Flush a link exhaustion (downloads_left reached 0)
                try:
                    db_decrement_downloads(short_id)
                    db_log_action(
                        short_id=short_id,
                        action="exhausted",
                        original_filename=dirty_meta.get("original_filename", ""),
                    )
                except Exception as e:
                    logger.error(f"Sync: Failed to flush exhaustion for {short_id}: {e}")
                    continue

            # Successfully flushed — remove the dirty marker
            r.delete(dirty_key)
            flushed += 1

        except Exception as e:
            logger.error(f"Sync: Unexpected error processing {dirty_key}: {e}")
            continue

    if flushed > 0:
        logger.info(f"Sync: Flushed {flushed}/{len(dirty_keys)} entries to PostgreSQL")


def _sync_loop():
    """Main loop for the background sync thread."""
    interval = settings.DB_SYNC_INTERVAL_SECONDS
    logger.info(f"Write-Back sync thread started (interval={interval}s)")

    while not _stop_event.is_set():
        try:
            _flush_dirty_entries()
        except Exception as e:
            logger.error(f"Sync: Unhandled error in flush cycle: {e}")

        # Sleep in small increments so we can respond to stop quickly
        for _ in range(interval * 2):
            if _stop_event.is_set():
                break
            time.sleep(0.5)

    logger.info("Write-Back sync thread stopped.")


def start_sync_thread():
    """Start the background Write-Back sync thread (daemon)."""
    global _sync_thread
    if _sync_thread is not None and _sync_thread.is_alive():
        logger.warning("Sync thread already running")
        return

    _stop_event.clear()
    _sync_thread = threading.Thread(target=_sync_loop, daemon=True, name="db-sync")
    _sync_thread.start()
    logger.info("Write-Back sync thread launched")


def stop_sync_thread():
    """Signal the sync thread to stop and wait for it."""
    _stop_event.set()
    if _sync_thread is not None:
        _sync_thread.join(timeout=15)
        logger.info("Write-Back sync thread joined")
