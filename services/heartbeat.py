import asyncio
import time
from datetime import datetime, timezone
from database.postgres import execute_pg_query
from websocket.manager import active_connections, connection_metadata, broadcast_user_status


async def cleanup_expired_sync_tombstones():
    """Purge soft-deleted sync tombstones older than 30 days to free DB storage."""
    try:
        now_ts = int(time.time() * 1000)
        retention_ms = 30 * 24 * 60 * 60 * 1000 # 30 days retention
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
    except Exception as e:
        print(f"[GC] Error in sync tombstone garbage collection: {e}")


async def heartbeat_check_loop():
    """Background task that closes stale WebSocket connections, marks users offline, and purges expired sync tombstones."""
    last_gc_time = 0

    while True:
        await asyncio.sleep(10)
        now = time.time()

        # Run tombstone GC once every hour
        if now - last_gc_time > 3600:
            last_gc_time = now
            asyncio.create_task(cleanup_expired_sync_tombstones())

        stale_websockets = [
            ws for ws, meta in list(connection_metadata.items())
            if now - meta.get("last_seen", now) > 60
        ]

        for ws in stale_websockets:
            print("[HEARTBEAT] Closing stale websocket connection due to inactivity")
            try:
                await ws.close(code=1001)
            except Exception:
                pass
            if ws in connection_metadata:
                del connection_metadata[ws]

            for username, ws_set in list(active_connections.items()):
                if ws in ws_set:
                    ws_set.discard(ws)
                    if not ws_set:
                        del active_connections[username]
                        try:
                            now_iso = datetime.now(timezone.utc).isoformat()
                            await execute_pg_query(
                                "UPDATE users SET status = $1, last_seen = $2 WHERE username = $3",
                                "offline", now_iso, username
                            )
                            await broadcast_user_status(username, "offline", now_iso)
                        except Exception as e:
                            print(f"Failed to update status on stale disconnect in heartbeat for @{username}: {e}")
