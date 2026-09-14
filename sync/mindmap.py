from __future__ import annotations
import json
import hashlib
import time
from typing import Optional, List, Dict, Any, Union
from fastapi import APIRouter, HTTPException, Depends, Query, Header, Response
from pydantic import BaseModel

from auth.deps import get_auth_token, decode_text2_token, create_text2_token
from database.postgres import execute_pg_query

router = APIRouter(prefix="/api/sync/mindmap", tags=["sync_mindmap"])


# --- Pydantic Models ---
class MindmapProjectSyncItem(BaseModel):
    id: str
    name: Optional[str] = ""
    data: Optional[Union[Dict[str, Any], str]] = None
    createdAt: Optional[Union[str, int, float]] = ""
    updatedAt: Optional[Union[str, int, float]] = None
    created_at: Optional[Union[str, int, float]] = None
    updated_at: Optional[Union[str, int, float]] = None
    is_deleted: Optional[bool] = False


class MindmapKeepSyncRequest(BaseModel):
    since_rev: Optional[int] = 0
    projects: Optional[List[MindmapProjectSyncItem]] = []
    items: Optional[List[MindmapProjectSyncItem]] = []


def _clean_str(val: Any) -> str:
    if isinstance(val, str):
        return val.strip()
    return ""


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

    # 1. Primary: Verify Text2 JWT Token
    if clean_token:
        payload = decode_text2_token(clean_token)
        if payload and payload.get("user_id"):
            return int(payload["user_id"])

    # 2. Secondary: Lookup in central users table by email
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
                    new_token = create_text2_token(
                        user_id=uid,
                        username=row.get("username") or clean_username or clean_email.split("@")[0],
                        email=row.get("email") or clean_email
                    )
                    response.headers["X-New-Text2-Token"] = new_token
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
                            new_token = create_text2_token(
                                user_id=uid,
                                username=prov_row.get("username") or uname,
                                email=clean_email
                            )
                            response.headers["X-New-Text2-Token"] = new_token
                        return uid
                except Exception as prov_err:
                    print(f"[ResolveUser] Auto-provision mindmap user notice: {prov_err}")
        except Exception as err:
            print(f"[ResolveUser] Email lookup warning: {err}")

    # 3. Tertiary: Lookup by username
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
                    new_token = create_text2_token(
                        user_id=uid,
                        username=row.get("username") or clean_username,
                        email=row.get("email") or clean_email or f"{clean_username}@text2.co"
                    )
                    response.headers["X-New-Text2-Token"] = new_token
                return uid
        except Exception as err:
            print(f"[ResolveUser] Username lookup warning: {err}")

    # 4. Direct X-User-Id header if provided
    if x_user_id and str(x_user_id).isdigit() and int(x_user_id) > 0:
        uid_val = int(x_user_id)
        if uid_val < 10000:
            try:
                rows = await execute_pg_query("SELECT id, user_id FROM users WHERE id = $1", uid_val)
                if rows and rows[0].get("id"):
                    return int(rows[0].get("user_id") or (10000 + rows[0]["id"]))
            except Exception:
                pass
            return 10000 + uid_val
        return uid_val

    # 5. Deterministic permanent fallback hash from email
    if clean_email:
        email_hash_int = int(hashlib.sha256(clean_email.encode("utf-8")).hexdigest()[:8], 16)
        stable_id = 50000 + (email_hash_int % 40000)
        return stable_id

    raise HTTPException(status_code=401, detail="Unauthorized: No valid Text2 session or token found")


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
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)

    try:
        rev_res = await execute_pg_query(
            "SELECT COALESCE(MAX(rev), 0) AS current_rev FROM user_sync_mindmap_projects WHERE user_id = $1",
            user_id
        )
        current_rev = int(rev_res[0]["current_rev"]) if rev_res and rev_res[0].get("current_rev") else 0

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
    1. Saves all client mutations (upserts/deletes) with next revision.
    2. Atomically queries and returns remote projects updated by other devices since since_rev.
    """
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)

    try:
        # 1. Get current max revision
        rev_res = await execute_pg_query(
            "SELECT COALESCE(MAX(rev), 0) AS current_rev FROM user_sync_mindmap_projects WHERE user_id = $1",
            user_id
        )
        current_max_rev = int(rev_res[0]["current_rev"]) if rev_res and rev_res[0].get("current_rev") else 0
        
        synced_project_ids = []
        next_rev = current_max_rev

        # Combine payload.projects and payload.items if both exist
        incoming_items = (payload.projects or []) + (payload.items or [])
        # Deduplicate incoming items by id
        unique_incoming = {}
        for item in incoming_items:
            if item and item.id:
                unique_incoming[str(item.id).strip()] = item

        # 2. Push client mutations (if any)
        if len(unique_incoming) > 0:
            now_ts = int(time.time() * 1000)
            next_rev = current_max_rev + 1
            for proj_id, item in unique_incoming.items():
                if not proj_id:
                    continue

                item_name = (item.name or "").strip()
                item_data = item.data if item.data is not None else {"nodes": [], "edges": [], "transform": {"x": 0, "y": 0, "scale": 1}}
                if isinstance(item_data, str):
                    data_json_str = item_data
                else:
                    data_json_str = json.dumps(item_data)

                created_at_str = str(item.createdAt or item.created_at or "").strip()

                synced_project_ids.append(proj_id)

                if item.is_deleted:
                    await execute_pg_query(
                        "INSERT INTO user_sync_mindmap_projects (user_id, project_id, name, data, created_at_str, rev, is_deleted, updated_at) "
                        "VALUES ($1, $2, '', '{}'::jsonb, '', $3, TRUE, $4) "
                        "ON CONFLICT (user_id, project_id) DO UPDATE SET "
                        "name = '', data = '{}'::jsonb, is_deleted = TRUE, rev = EXCLUDED.rev, updated_at = EXCLUDED.updated_at",
                        user_id, proj_id, next_rev, now_ts
                    )
                else:
                    await execute_pg_query(
                        "INSERT INTO user_sync_mindmap_projects (user_id, project_id, name, data, created_at_str, rev, is_deleted, updated_at) "
                        "VALUES ($1, $2, $3, $4::jsonb, $5, $6, FALSE, $7) "
                        "ON CONFLICT (user_id, project_id) DO UPDATE SET "
                        "name = EXCLUDED.name, data = EXCLUDED.data, created_at_str = EXCLUDED.created_at_str, "
                        "rev = EXCLUDED.rev, is_deleted = FALSE, updated_at = EXCLUDED.updated_at",
                        user_id, proj_id, item_name, data_json_str, created_at_str, next_rev, now_ts
                    )

        # 3. Pull remote updates (Atomic Pull)
        since_rev = payload.since_rev if payload.since_rev is not None else 0
        remote_projects = []

        if since_rev == 0 or since_rev > next_rev:
            # First sync on this device or client revision is ahead: return all active projects on server
            rows = await execute_pg_query(
                "SELECT project_id, name, data, created_at_str, rev, is_deleted, created_at, updated_at "
                "FROM user_sync_mindmap_projects "
                "WHERE user_id = $1 AND is_deleted = FALSE "
                "ORDER BY updated_at DESC, rev ASC",
                user_id
            )
            remote_projects = _format_mindmap_rows(rows)
        elif since_rev < next_rev:
            # Return projects updated by other devices (rev > since_rev)
            rows = await execute_pg_query(
                "SELECT project_id, name, data, created_at_str, rev, is_deleted, created_at, updated_at "
                "FROM user_sync_mindmap_projects "
                "WHERE user_id = $1 AND rev > $2 "
                "ORDER BY updated_at DESC, rev ASC",
                user_id, since_rev
            )
            remote_projects = _format_mindmap_rows(rows)

        return {
            "status": "success",
            "current_rev": next_rev,
            "synced_count": len(synced_project_ids),
            "projects": remote_projects,
            "items": remote_projects
        }
    except Exception as e:
        print(f"[KeepSyncMindmap] Sync error for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Keep Sync Mindmap Error: {str(e)}")
