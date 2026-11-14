import asyncio
import time
from datetime import datetime, timezone
from database.postgres import execute_pg_query
from websocket.manager import (
    active_connections,
    connection_metadata,
    broadcast_user_status,
    cleanup_socket_subscriptions,
)
from services.memory_cleaner import (
    purge_application_caches,
    perform_full_garbage_collection,
    adaptive_memory_watchdog,
)


async def cleanup_expired_sync_tombstones():
    """Purge soft-deleted sync tombstones older than 30 days, stale sync batches older than 7 days, and run Maximum GC."""
    try:
        now_ts = int(time.time() * 1000)
        retention_ms = 30 * 24 * 60 * 60 * 1000  # 30 days retention
        cutoff_ts = now_ts - retention_ms

        sync_tables = [
            "user_sync_table_projects",
            "user_sync_notes",
            "user_sync_mindmap_projects",
            "user_sync_tasks",
            "user_sync_task_projects",
            "user_sync_calendar_events",
            "user_sync_countday_events"
        ]

        for tbl in sync_tables:
            try:
                await execute_pg_query(f"DELETE FROM {tbl} WHERE is_deleted = TRUE AND updated_at < $1", cutoff_ts)
            except Exception:
                pass

        # Purge idempotency batches older than 7 days
        batch_cutoff_ts = now_ts - (7 * 24 * 60 * 60 * 1000)
        try:
            await execute_pg_query("DELETE FROM user_sync_batches WHERE created_at < $1", batch_cutoff_ts)
        except Exception:
            pass

        # Reclaim memory and trim OS heap pages
        perform_full_garbage_collection(force_trim=True)
    except Exception as e:
        print(f"[GC] Error in sync tombstone garbage collection: {e}")


async def heartbeat_check_loop():
    """
    Background worker that:
    1. Monitors and terminates stale WebSocket connections (>120s inactivity).
    2. Runs continuous Adaptive Memory Watchdog (triggers deep GC on high RAM spikes).
    3. Prunes in-memory caches every 60s.
    4. Runs periodic Maximum GC + OS Memory Trimming every 10 minutes (600s).
    5. Cleans expired database tombstones once every hour (3600s).
    """
    last_db_gc_time = 0
    last_deep_gc_time = 0
    last_cache_prune_time = 0
    last_watchdog_time = 0

    while True:
        await asyncio.sleep(15)
        now = time.time()

        # 1. Adaptive Memory Watchdog check every 30s
        if now - last_watchdog_time > 30:
            last_watchdog_time = now
            try:
                adaptive_memory_watchdog(max_process_rss_mb=350.0, max_system_memory_percent=85.0)
            except Exception as e:
                print(f"[MemoryWatchdog] Error during watchdog evaluation: {e}")

        # 2. Prune in-memory caches (Auth OTPs, rate-limit keys, WebRTC buffers) every 60s
        if now - last_cache_prune_time > 60:
            last_cache_prune_time = now
            try:
                purge_application_caches()
            except Exception as e:
                print(f"[GC] Error purging application caches: {e}")

        # 3. Proactive deep GC & OS memory trim every 10 minutes (600s)
        if now - last_deep_gc_time > 600:
            last_deep_gc_time = now
            try:
                perform_full_garbage_collection(force_trim=True)
            except Exception as e:
                print(f"[GC] Error in proactive deep GC: {e}")

        # 4. Run DB tombstone purge once every hour
        if now - last_db_gc_time > 3600:
            last_db_gc_time = now
            asyncio.create_task(cleanup_expired_sync_tombstones())

        # 5. Allow up to 120s of inactivity so mobile background tabs & heavy uploads aren't killed prematurely
        stale_websockets = [
            ws for ws, meta in list(connection_metadata.items())
            if now - meta.get("last_seen", now) > 120
        ]

        for ws in stale_websockets:
            print("[HEARTBEAT] Closing stale websocket connection due to inactivity (>120s)")
            cleanup_socket_subscriptions(ws)
            try:
                await ws.close(code=1001)
            except Exception:
                pass
            connection_metadata.pop(ws, None)

            for username, ws_set in list(active_connections.items()):
                if ws in ws_set:
                    ws_set.discard(ws)
                    if not ws_set:
                        active_connections.pop(username, None)
                        try:
                            now_iso = datetime.now(timezone.utc).isoformat()
                            await execute_pg_query(
                                "UPDATE users SET status = $1, last_seen = $2 WHERE username = $3",
                                "offline", now_iso, username
                            )
                            await broadcast_user_status(username, "offline", now_iso)
                        except Exception as e:
                            print(f"Failed to update status on stale disconnect in heartbeat for @{username}: {e}")
