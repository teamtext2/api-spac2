from __future__ import annotations
import asyncio
import json
import hashlib
import uuid
import time
from typing import Optional, Dict, Any, List, Callable, Awaitable
from fastapi import HTTPException
from database.postgres import get_pg_pool, execute_pg_query, _current_pg_conn
from database.sqlite import execute_sqlite_query, sqlite_transaction

# Official Spac2 V2 Domain Mapping
DOMAIN_TABLES: Dict[str, List[str]] = {
    "task": ["user_sync_tasks", "user_sync_task_projects"],
    "doc": ["user_sync_docs"],
    "note": ["user_sync_notes"],
    "calendar": ["user_sync_calendar_events"],
    "mindmap": ["user_sync_mindmap_projects"],
    "table": ["user_sync_table_projects"],
    "countday": ["user_sync_countday_events"],
}


def calculate_payload_hash(payload_data: Any) -> str:
    """Calculate deterministic SHA-256 hash for idempotency integrity check."""
    try:
        if hasattr(payload_data, "dict"):
            raw_dict = payload_data.dict()
        elif hasattr(payload_data, "model_dump"):
            raw_dict = payload_data.model_dump()
        elif isinstance(payload_data, dict):
            raw_dict = payload_data
        else:
            raw_dict = {"data": str(payload_data)}
        
        # Exclude client transient fields if needed, sort keys deterministically
        normalized_json = json.dumps(raw_dict, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(normalized_json.encode("utf-8")).hexdigest()
    except Exception:
        return hashlib.sha256(str(payload_data).encode("utf-8")).hexdigest()


async def get_domain_max_rev(user_id: int, app_code: str) -> int:
    """
    Get the current maximum revision for a user in a domain.
    Reads from user_sync_counters with fallback to historical table MAX(rev).
    """
    app_code = app_code.strip().lower()
    
    # 1. Try reading from user_sync_counters
    try:
        rows = await execute_pg_query(
            "SELECT current_rev FROM user_sync_counters WHERE user_id = $1 AND app_code = $2",
            user_id, app_code
        )
        if rows and rows[0].get("current_rev") is not None:
            return int(rows[0]["current_rev"])
    except Exception as e:
        print(f"[SyncEngine] Warning reading counter for user {user_id} app {app_code}: {e}")

    # 2. Fallback to historical tables if counter row does not exist yet
    tables = DOMAIN_TABLES.get(app_code, [])
    if not tables:
        return 0

    try:
        union_queries = " UNION ALL ".join([f"SELECT COALESCE(MAX(rev), 0) AS rev FROM {t} WHERE user_id = $1" for t in tables])
        sql = f"SELECT COALESCE(MAX(rev), 0) AS max_rev FROM ({union_queries}) AS combined"
        rev_res = await execute_pg_query(sql, user_id)
        if rev_res and rev_res[0].get("max_rev") is not None:
            max_val = int(rev_res[0]["max_rev"])
            # Seed the counter for future queries
            try:
                await execute_pg_query(
                    """
                    INSERT INTO user_sync_counters (user_id, app_code, current_rev, updated_at)
                    VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
                    ON CONFLICT (user_id, app_code)
                    DO UPDATE SET current_rev = GREATEST(user_sync_counters.current_rev, EXCLUDED.current_rev), updated_at = CURRENT_TIMESTAMP
                    """,
                    user_id, app_code, max_val
                )
            except Exception:
                pass
            return max_val
    except Exception as e:
        print(f"[SyncEngine] Error querying historical MAX(rev) for user {user_id} app {app_code}: {e}")

    return 0


_sqlite_domain_locks: Dict[str, asyncio.Lock] = {}


def _get_sqlite_domain_lock(user_id: int, app_code: str) -> asyncio.Lock:
    key = f"{user_id}:{app_code.lower().strip()}"
    if key not in _sqlite_domain_locks:
        _sqlite_domain_locks[key] = asyncio.Lock()
    return _sqlite_domain_locks[key]


async def execute_sync_batch_atomic(
    user_id: int,
    app_code: str,
    sync_batch_id: Optional[str],
    payload_data: Any,
    has_mutations: bool,
    write_callback: Callable[[int, int], Awaitable[Any]]
) -> Dict[str, Any]:
    """
    Spac2 V2 Atomic Monotonic Sync Batch Engine with Idempotency & Concurrency Safety.
    
    Flow:
    1. If no mutations: return current_rev.
    2. Check Idempotency: If sync_batch_id exists, verify payload_hash:
       - Match: return cached result with deduplicated=True.
       - Conflict: raise HTTP 409 Conflict if same batch_id sent with different payload.
    3. In single atomic transaction:
       - Upsert counter (locks user_id + app_code row, returns monotonic next_rev).
       - Executes write_callback(next_rev, now_ts) using transactional connection.
       - Records sync_batch_id + payload_hash in user_sync_batches.
    4. Legacy V1 Support: If sync_batch_id is omitted, a random UUID is assigned.
       Note: Legacy clients without sync_batch_id are fully functional for sync,
       but cannot guarantee zero-mutation idempotency on network retries without client tokens.
    """
    app_code = app_code.strip().lower()
    
    if not has_mutations:
        current_rev = await get_domain_max_rev(user_id, app_code)
        return {
            "status": "success",
            "current_rev": current_rev,
            "next_rev": current_rev,
            "deduplicated": False
        }

    # Generate or normalize sync_batch_id
    is_client_batch_id = bool(sync_batch_id and str(sync_batch_id).strip())
    if not is_client_batch_id:
        batch_uuid = uuid.uuid4()
    else:
        try:
            batch_uuid = uuid.UUID(str(sync_batch_id).strip())
        except (ValueError, TypeError):
            # Fallback for non-UUID strings: deterministic v5 UUID based on string
            batch_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, str(sync_batch_id).strip())

    payload_hash = calculate_payload_hash(payload_data)
    now_ts = int(time.time() * 1000)
    pool = await get_pg_pool()

    if pool:
        async with pool.acquire() as conn:
            # 1. Idempotency Check (Only for client-provided sync_batch_id)
            if is_client_batch_id:
                try:
                    existing = await conn.fetchrow(
                        "SELECT rev, payload_hash FROM user_sync_batches WHERE user_id = $1 AND app_code = $2 AND sync_batch_id = $3",
                        user_id, app_code, batch_uuid
                    )
                    if existing:
                        if existing["payload_hash"] == payload_hash:
                            # Exact identical retry: Return previous rev gracefully
                            return {
                                "status": "success",
                                "current_rev": int(existing["rev"]),
                                "next_rev": int(existing["rev"]),
                                "deduplicated": True
                            }
                        else:
                            # Same ID but mismatched payload: Replay collision / tampering
                            raise HTTPException(
                                status_code=409,
                                detail=f"Conflict: sync_batch_id '{batch_uuid}' already executed with different payload."
                            )
                except HTTPException:
                    raise
                except Exception as idemp_err:
                    print(f"[SyncEngine] Idempotency check warning: {idemp_err}")

            # 2. Atomic Transaction Execution
            async with conn.transaction():
                tok = _current_pg_conn.set(conn)
                try:
                    # Atomic Revision Allocation with Row-level Lock
                    next_rev = await conn.fetchval(
                        """
                        INSERT INTO user_sync_counters (user_id, app_code, current_rev, updated_at)
                        VALUES ($1, $2, 1, CURRENT_TIMESTAMP)
                        ON CONFLICT (user_id, app_code)
                        DO UPDATE SET current_rev = user_sync_counters.current_rev + 1, updated_at = CURRENT_TIMESTAMP
                        RETURNING current_rev;
                        """,
                        user_id, app_code
                    )
                    next_rev = int(next_rev) if next_rev is not None else 1

                    # Execute domain-specific write operations within transactional scope
                    await write_callback(next_rev, now_ts)

                    # Record batch for idempotency tracking
                    await conn.execute(
                        """
                        INSERT INTO user_sync_batches (user_id, app_code, sync_batch_id, payload_hash, rev, created_at)
                        VALUES ($1, $2, $3, $4, $5, CURRENT_TIMESTAMP)
                        ON CONFLICT (user_id, app_code, sync_batch_id) DO NOTHING;
                        """,
                        user_id, app_code, batch_uuid, payload_hash, next_rev
                    )
                finally:
                    _current_pg_conn.reset(tok)

            return {
                "status": "success",
                "current_rev": next_rev,
                "next_rev": next_rev,
                "deduplicated": False
            }

    # --- SQLite / Mock Fallback Execution with In-Process Lock ---
    lock = _get_sqlite_domain_lock(user_id, app_code)
    async with lock:
        if is_client_batch_id:
            existing_batch = execute_sqlite_query(
                "SELECT rev, payload_hash FROM user_sync_batches WHERE user_id = ? AND app_code = ? AND sync_batch_id = ?",
                user_id, app_code, str(batch_uuid)
            )
            if existing_batch and len(existing_batch) > 0:
                if existing_batch[0]["payload_hash"] == payload_hash:
                    return {
                        "status": "success",
                        "current_rev": int(existing_batch[0]["rev"]),
                        "next_rev": int(existing_batch[0]["rev"]),
                        "deduplicated": True
                    }
                else:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Conflict: sync_batch_id '{batch_uuid}' already executed with different payload."
                    )

        with sqlite_transaction():
            curr_rev = await get_domain_max_rev(user_id, app_code)
            next_rev = curr_rev + 1
            await write_callback(next_rev, now_ts)
            
            execute_sqlite_query(
                """
                INSERT INTO user_sync_counters (user_id, app_code, current_rev, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, app_code) DO UPDATE SET current_rev = ?, updated_at = CURRENT_TIMESTAMP
                """,
                user_id, app_code, next_rev, next_rev
            )
            execute_sqlite_query(
                """
                INSERT INTO user_sync_batches (user_id, app_code, sync_batch_id, payload_hash, rev, created_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, app_code, sync_batch_id) DO NOTHING
                """,
                user_id, app_code, str(batch_uuid), payload_hash, next_rev
            )

        return {
            "status": "success",
            "current_rev": next_rev,
            "next_rev": next_rev,
            "deduplicated": False
        }

