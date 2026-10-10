from __future__ import annotations
import json
import hashlib
import time
from typing import Optional, List, Dict, Any, Union
from fastapi import APIRouter, HTTPException, Depends, Query, Header, Response
from pydantic import BaseModel

from auth.deps import get_auth_token, decode_spac2_token, create_spac2_token, decode_text2_token, create_text2_token
from database.postgres import execute_pg_query
from sync.engine import get_domain_max_rev, execute_sync_batch_atomic, resolve_canonical_id

router = APIRouter(prefix="/api/sync/mindmap", tags=["sync_mindmap"])

_table_initialized = False


async def _ensure_mindmap_table():
    global _table_initialized
    if _table_initialized:
        return
    try:
        await execute_pg_query("""
            CREATE TABLE IF NOT EXISTS user_sync_mindmap_projects (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                project_id VARCHAR(100) NOT NULL,
                name TEXT DEFAULT '',
                data JSONB DEFAULT '{"nodes": [], "edges": [], "transform": {"x": 0, "y": 0, "scale": 1}}',
                created_at_str TEXT DEFAULT '',
                rev BIGINT NOT NULL DEFAULT 1,
                is_deleted BOOLEAN DEFAULT FALSE,
                deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at BIGINT DEFAULT 0,
                CONSTRAINT uq_sync_mindmap_project_id UNIQUE (project_id)
            );
        """)
        try:
            await execute_pg_query("ALTER TABLE user_sync_mindmap_projects DROP COLUMN IF EXISTS visibility CASCADE;")
            await execute_pg_query("ALTER TABLE user_sync_mindmap_projects ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL;")
            await execute_pg_query("CREATE INDEX IF NOT EXISTS idx_sync_mindmap_user_rev ON user_sync_mindmap_projects(user_id, rev);")
            await execute_pg_query("CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_mindmap_global_project_id ON user_sync_mindmap_projects(project_id);")
        except Exception:
            pass
        _table_initialized = True
    except Exception as e:
        print(f"[MINDMAP SYNC] Warning initializing user_sync_mindmap_projects table: {e}")


# --- Pydantic Models ---
class MindmapProjectSyncItem(BaseModel):
    id: str
    name: Optional[str] = ""
    data: Optional[Union[Dict[str, Any], str]] = None
    createdAt: Optional[Union[str, int, float]] = ""
    updatedAt: Optional[Union[str, int, float]] = None
    created_at: Optional[Union[str, int, float]] = None
    updated_at: Optional[Union[str, int, float]] = None
    rev: Optional[int] = 1
    base_rev: Optional[int] = None
    visibility: Optional[str] = None
    is_deleted: Optional[bool] = False


class MindmapKeepSyncRequest(BaseModel):
    sync_batch_id: Optional[str] = None
    since_rev: Optional[int] = 0
    projects: Optional[List[MindmapProjectSyncItem]] = []
    items: Optional[List[MindmapProjectSyncItem]] = []


def _clean_str(val: Any) -> str:
    if isinstance(val, str):
        return val.strip()
    return ""


def _inject_mindmap_headers(response: Response):
    """Inject strict anti-caching and noindex headers for all mindmap sync endpoints."""
    response.headers["Cache-Control"] = "private, no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"


async def _resolve_user_id(
    token: Any = None,
    x_user_id: Any = None,
    x_user_email: Any = None,
    x_user_name: Any = None,
    response: Optional[Response] = None
) -> int:
    """Seamlessly resolve a stable, permanent user_id across all devices."""
    clean_token = _clean_str(token)
    clean_id = _clean_str(x_user_id)
    clean_email = _clean_str(x_user_email).lower()
    clean_username = _clean_str(x_user_name).lower()

    # 1. Primary: Verify Spac2 JWT Token
    if clean_token:
        payload = decode_spac2_token(clean_token)
        if payload and payload.get("user_id"):
            return int(payload["user_id"])

    # 2. Secondary: Direct X-User-Id Header
    if clean_id and clean_id.isdigit() and int(clean_id) > 0:
        uid_val = int(clean_id)
        if uid_val < 10000:
            try:
                rows = await execute_pg_query("SELECT id, user_id FROM users WHERE id = $1", uid_val)
                if rows and rows[0].get("id"):
                    return int(rows[0].get("user_id") or (10000 + rows[0]["id"]))
            except Exception:
                pass
            return 10000 + uid_val
        return uid_val

    # 3. Tertiary: Lookup in central users table by email
    if clean_email:
        try:
            rows = await execute_pg_query(
                "SELECT id, user_id, username, email FROM users WHERE LOWER(email) = $1", 
                clean_email
            )
            if rows and len(rows) > 0 and rows[0].get("id"):
                row = rows[0]
                db_id = row["id"]
                uid = int(row.get("user_id") or (10000 + db_id))
                
                # Ensure user_id column in DB is populated
                if not row.get("user_id"):
                    try:
                        await execute_pg_query("UPDATE users SET user_id = $1 WHERE id = $2", uid, db_id)
                    except Exception:
                        pass

                if response is not None:
                    new_token = create_spac2_token(
                        user_id=uid,
                        username=row.get("username") or clean_username or clean_email.split("@")[0],
                        email=row.get("email") or clean_email
                    )
                    response.headers["X-New-Spac2-Token"] = new_token
                return uid
            else:
                # Auto-provision user in central table so all devices share the exact same user_id
                uname = clean_username or clean_email.split("@")[0]
                try:
                    await execute_pg_query(
                        "INSERT INTO users (username, email, user_id, name, created_at, updated_at) "
                        "VALUES ($1, $2, (SELECT COALESCE(MAX(user_id), 10000) + 1 FROM users), $3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                        "ON CONFLICT (email) DO NOTHING",
                        uname, clean_email, uname
                    )
                    prov_rows = await execute_pg_query("SELECT id, user_id, username, email FROM users WHERE LOWER(email) = $1", clean_email)
                    if prov_rows and prov_rows[0].get("id"):
                        prov_row = prov_rows[0]
                        uid = int(prov_row.get("user_id") or (10000 + prov_row["id"]))
                        if response is not None:
                            new_token = create_spac2_token(
                                user_id=uid,
                                username=prov_row.get("username") or uname,
                                email=clean_email
                            )
                            response.headers["X-New-Spac2-Token"] = new_token
                        return uid
                except Exception as prov_err:
                    print(f"[ResolveUser] Auto-provision mindmap user notice: {prov_err}")
        except Exception as err:
            print(f"[ResolveUser] Email lookup warning: {err}")

    # 4. Quaternary: Lookup by username
    if clean_username:
        try:
            rows = await execute_pg_query(
                "SELECT id, user_id, username, email FROM users WHERE LOWER(username) = $1", 
                clean_username
            )
            if rows and len(rows) > 0 and rows[0].get("id"):
                row = rows[0]
                uid = int(row.get("user_id") or (10000 + row["id"]))
                if response is not None:
                    new_token = create_spac2_token(
                        user_id=uid,
                        username=row.get("username") or clean_username,
                        email=row.get("email") or clean_email or f"{clean_username}@spac2.com"
                    )
                    response.headers["X-New-Spac2-Token"] = new_token
                return uid
        except Exception as err:
            print(f"[ResolveUser] Username lookup warning: {err}")

    # 5. Deterministic permanent fallback hash from email
    if clean_email:
        email_hash_int = int(hashlib.sha256(clean_email.encode("utf-8")).hexdigest()[:8], 16)
        stable_id = 50000 + (email_hash_int % 40000)
        return stable_id

    raise HTTPException(status_code=401, detail="Unauthorized: No valid Spac2 session or token found")


def _format_mindmap_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    projects_list = []
    seen_ids = set()

    for r in rows:
        proj_id = str(r.get("project_id") or "").strip()
        if not proj_id or proj_id in seen_ids:
            continue

        raw_data = r.get("data")
        parsed_data = {"nodes": [], "edges": [], "transform": {"x": 0, "y": 0, "scale": 1}}
        if isinstance(raw_data, str):
            try:
                parsed_data = json.loads(raw_data)
            except Exception:
                pass
        elif isinstance(raw_data, dict):
            parsed_data = raw_data

        name = (r.get("name") or "").strip()
        created_at_str = str(r.get("created_at_str") or "").strip()
        updated_at_ts = int(r.get("updated_at") or 0)
        rev = int(r.get("rev") or 1)
        is_deleted = bool(r.get("is_deleted"))

        seen_ids.add(proj_id)

        projects_list.append({
            "id": proj_id,
            "name": name,
            "data": parsed_data,
            "createdAt": created_at_str or (str(r.get("created_at")) if r.get("created_at") else ""),
            "updatedAt": updated_at_ts,
            "updated_at": updated_at_ts,
            "rev": rev,
            "is_deleted": is_deleted
        })
    return projects_list


@router.get("")
async def get_mindmap_delta_sync(
    response: Response,
    since_rev: int = Query(0),
    token: str = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
):
    """Google Keep / Mindmap-Style Lightweight Delta Pull.
    Returns projects created/updated/deleted since since_rev.
    """
    _inject_mindmap_headers(response)
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)
    await _ensure_mindmap_table()

    try:
        current_rev = await get_domain_max_rev(user_id, "mindmap")

        if since_rev > 0 and since_rev == current_rev:
            return {
                "status": "success",
                "current_rev": current_rev,
                "projects": [],
                "items": []
            }

        if since_rev == 0 or since_rev > current_rev:
            # Full active projects pull
            rows = await execute_pg_query(
                "SELECT project_id, name, data, created_at_str, rev, is_deleted, created_at, updated_at "
                "FROM user_sync_mindmap_projects "
                "WHERE user_id = $1 AND is_deleted = FALSE "
                "ORDER BY updated_at DESC, rev ASC",
                user_id
            )
        else:
            # Delta pull
            rows = await execute_pg_query(
                "SELECT project_id, name, data, created_at_str, rev, is_deleted, created_at, updated_at "
                "FROM user_sync_mindmap_projects "
                "WHERE user_id = $1 AND rev > $2 "
                "ORDER BY updated_at DESC, rev ASC",
                user_id, since_rev
            )

        formatted_projects = _format_mindmap_rows(rows)
        return {
            "status": "success",
            "current_rev": current_rev,
            "projects": formatted_projects,
            "items": formatted_projects
        }
    except Exception as e:
        print(f"[KeepSyncMindmap] Delta fetch error for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch delta mindmap projects: {str(e)}")


@router.post("")
async def sync_keep_mindmap_batch(
    payload: MindmapKeepSyncRequest,
    response: Response,
    token: str = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
):
    """Google Keep / Mindmap Single-Trip Atomic Full Sync (Push + Pull).
    1. Saves all client mutations (upserts/deletes) atomically with next revision and OCC checks.
    2. Soft Delete Tombstone: cleans node/edge graph data while preserving metadata for 30-day delta propagation.
    3. Auto-purges soft-deleted tombstones older than 30 days.
    4. Atomically queries and returns remote projects updated by other devices since since_rev.
    """
    _inject_mindmap_headers(response)
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)
    await _ensure_mindmap_table()

    try:
        incoming_items = (payload.projects or []) + (payload.items or [])
        unique_incoming = {}
        for item in incoming_items:
            if item and item.id:
                unique_incoming[str(item.id).strip()] = item

        has_mutations = bool(len(unique_incoming) > 0)
        mutation_results: List[Dict[str, Any]] = []

        async def _write_mindmap_mutations(next_rev: int, now_ts: int):
            if len(unique_incoming) > 0:
                for raw_proj_id, item in unique_incoming.items():
                    if not raw_proj_id:
                        continue

                    proj_id = await resolve_canonical_id(user_id, "mindmap", raw_proj_id)
                    if not proj_id:
                        continue

                    # 1. Ownership & Permissions lookup
                    existing_rows = await execute_pg_query(
                        "SELECT user_id, rev, name, data, created_at_str, is_deleted, updated_at "
                        "FROM user_sync_mindmap_projects WHERE project_id = $1 LIMIT 1",
                        proj_id
                    )

                    is_owner = True
                    target_owner_id = user_id
                    existing_row = None

                    if existing_rows and len(existing_rows) > 0:
                        existing_row = existing_rows[0]
                        owner_id = int(existing_row["user_id"])
                        is_owner = (owner_id == user_id)
                        target_owner_id = owner_id

                        if not is_owner:
                            # Strict Owner-Only Policy: Non-owners cannot mutate another user's project
                            mutation_results.append({
                                "id": raw_proj_id,
                                "status": "REJECT",
                                "error": "PERMISSION_DENIED"
                            })
                            continue

                    # 2. Strict Optimistic Concurrency Control (OCC) - DO NOT overwrite stale server data
                    if existing_row and not item.is_deleted:
                        server_rev = int(existing_row.get("rev") or 1)
                        client_base_rev = item.base_rev if item.base_rev is not None else item.rev
                        if client_base_rev is not None and client_base_rev > 0 and client_base_rev < server_rev:
                            raw_sdata = existing_row.get("data")
                            parsed_sdata = {"nodes": [], "edges": [], "transform": {"x": 0, "y": 0, "scale": 1}}
                            if isinstance(raw_sdata, str):
                                try:
                                    parsed_sdata = json.loads(raw_sdata)
                                except Exception:
                                    pass
                            elif isinstance(raw_sdata, dict):
                                parsed_sdata = raw_sdata

                            mutation_results.append({
                                "id": raw_proj_id,
                                "status": "CONFLICT",
                                "server_rev": server_rev,
                                "server_name": existing_row.get("name") or "",
                                "server_data": parsed_sdata,
                                "server_updated_at": int(existing_row.get("updated_at") or now_ts),
                                "error": "OCC_VERSION_MISMATCH"
                            })
                            continue

                    item_name = (item.name or "").strip()
                    item_data = item.data if item.data is not None else {"nodes": [], "edges": [], "transform": {"x": 0, "y": 0, "scale": 1}}
                    if isinstance(item_data, str):
                        data_json_str = item_data
                    else:
                        data_json_str = json.dumps(item_data)

                    created_at_str = str(item.createdAt or item.created_at or "").strip()
                    item_updated_at = int(item.updatedAt or item.updated_at or now_ts)

                    if item.is_deleted:
                        if is_owner:
                            await execute_pg_query(
                                "INSERT INTO user_sync_mindmap_projects ("
                                "   user_id, project_id, name, data, created_at_str, rev, is_deleted, deleted_at, updated_at"
                                ") VALUES ($1, $2, '', '{\"nodes\":[],\"edges\":[],\"transform\":{\"x\":0,\"y\":0,\"scale\":1}}'::jsonb, '', $3, TRUE, CURRENT_TIMESTAMP, $4) "
                                "ON CONFLICT (project_id) DO UPDATE SET "
                                "   name = '', data = '{\"nodes\":[],\"edges\":[],\"transform\":{\"x\":0,\"y\":0,\"scale\":1}}'::jsonb, "
                                "   is_deleted = TRUE, deleted_at = CURRENT_TIMESTAMP, rev = EXCLUDED.rev, updated_at = EXCLUDED.updated_at "
                                "WHERE user_sync_mindmap_projects.user_id = EXCLUDED.user_id",
                                target_owner_id, proj_id, next_rev, now_ts
                            )
                            mutation_results.append({"id": raw_proj_id, "status": "ACK", "rev": next_rev})
                        else:
                            mutation_results.append({
                                "id": raw_proj_id,
                                "status": "REJECT",
                                "error": "ONLY_OWNER_CAN_DELETE"
                            })
                    else:
                        await execute_pg_query(
                            "INSERT INTO user_sync_mindmap_projects ("
                            "   user_id, project_id, name, data, created_at_str, rev, is_deleted, deleted_at, updated_at"
                            ") VALUES ($1, $2, $3, $4::jsonb, $5, $6, FALSE, NULL, $7) "
                            "ON CONFLICT (project_id) DO UPDATE SET "
                            "   name = EXCLUDED.name, data = EXCLUDED.data, created_at_str = EXCLUDED.created_at_str, "
                            "   rev = EXCLUDED.rev, is_deleted = FALSE, deleted_at = NULL, updated_at = EXCLUDED.updated_at "
                            "WHERE user_sync_mindmap_projects.user_id = EXCLUDED.user_id AND user_sync_mindmap_projects.is_deleted = FALSE",
                            target_owner_id, proj_id, item_name, data_json_str, created_at_str, next_rev, item_updated_at
                        )
                        mutation_results.append({"id": raw_proj_id, "status": "ACK", "rev": next_rev})

            # Auto-purge soft-deleted tombstones older than 30 days
            try:
                thirty_days_ago_ts = now_ts - (30 * 86400 * 1000)
                await execute_pg_query(
                    "DELETE FROM user_sync_mindmap_projects WHERE user_id = $1 AND is_deleted = TRUE AND updated_at < $2",
                    user_id, thirty_days_ago_ts
                )
            except Exception as purge_err:
                print(f"[KeepSyncMindmap] 30-day tombstone cleanup notice: {purge_err}")

        # Atomic commit with idempotency & concurrency protection
        sync_result = await execute_sync_batch_atomic(
            user_id=user_id,
            app_code="mindmap",
            sync_batch_id=payload.sync_batch_id,
            payload_data=payload,
            has_mutations=has_mutations,
            write_callback=_write_mindmap_mutations
        )

        effective_rev = sync_result["current_rev"]

        # Pull remote updates (Atomic Pull)
        since_rev = payload.since_rev if payload.since_rev is not None else 0
        remote_projects = []

        if since_rev == 0 or since_rev > effective_rev:
            rows = await execute_pg_query(
                "SELECT project_id, name, data, created_at_str, rev, is_deleted, created_at, updated_at "
                "FROM user_sync_mindmap_projects "
                "WHERE user_id = $1 AND is_deleted = FALSE "
                "ORDER BY updated_at DESC, rev ASC",
                user_id
            )
            remote_projects = _format_mindmap_rows(rows)
        elif since_rev < effective_rev:
            rows = await execute_pg_query(
                "SELECT project_id, name, data, created_at_str, rev, is_deleted, created_at, updated_at "
                "FROM user_sync_mindmap_projects "
                "WHERE user_id = $1 AND rev > $2 "
                "ORDER BY updated_at DESC, rev ASC",
                user_id, since_rev
            )
            remote_projects = _format_mindmap_rows(rows)

        ack_count = len([r for r in mutation_results if r.get("status") == "ACK"])

        # Broadcast lightweight invalidation ping to subscribed clients
        if ack_count > 0:
            for res_item in mutation_results:
                if res_item.get("status") == "ACK":
                    proj_id_val = res_item.get("id")
                    if proj_id_val:
                        try:
                            import asyncio
                            from websocket.manager import broadcast_resource_invalidation
                            asyncio.create_task(broadcast_resource_invalidation(
                                app="mindmap",
                                resource_id=str(proj_id_val),
                                rev=effective_rev,
                                actor_email=_clean_str(x_user_email)
                            ))
                        except Exception as ping_err:
                            print(f"[WS Ping Notice] {ping_err}")

        return {
            "status": "success",
            "current_rev": effective_rev,
            "deduplicated": sync_result.get("deduplicated", False),
            "synced_count": ack_count,
            "results": mutation_results,
            "projects": remote_projects,
            "items": remote_projects
        }
    except Exception as e:
        print(f"[KeepSyncMindmap] Sync error for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Keep Sync Mindmap Error: {str(e)}")
