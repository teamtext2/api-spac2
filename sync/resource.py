from __future__ import annotations
import time
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, HTTPException, Depends, Header, Response
from pydantic import BaseModel

from auth.deps import get_auth_token, decode_spac2_token
from database.postgres import execute_pg_query

router = APIRouter(prefix="/api/sync/resource", tags=["sync_resource"])

# App domain to table & ID column configuration
RESOURCE_CONFIGS: Dict[str, Dict[str, str]] = {
    "doc": {"table": "user_sync_docs", "id_col": "doc_id", "user_col": "user_id"},
    "note": {"table": "user_sync_notes", "id_col": "note_id", "user_col": "user_id"},
    "task": {"table": "user_sync_tasks", "id_col": "task_id", "user_col": "user_id"},
    "mindmap": {"table": "user_sync_mindmap_projects", "id_col": "project_id", "user_col": "user_id"},
    "table": {"table": "user_sync_table_projects", "id_col": "project_id", "user_col": "user_id"},
    "calendar": {"table": "user_sync_calendar_events", "id_col": "event_id", "user_col": "user_id"},
    "countday": {"table": "user_sync_countday_events", "id_col": "event_id", "user_col": "user_id"},
}


class SetVisibilityRequest(BaseModel):
    visibility: str  # 'private' | 'link_read' | 'link_edit' | 'restricted'


class UpdateResourceContentRequest(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    preview_text: Optional[str] = None
    previewText: Optional[str] = None
    word_count: Optional[int] = None
    wordCount: Optional[int] = None
    tabs: Optional[Union[List[Dict[str, Any]], str]] = None
    active_tab_id: Optional[str] = None
    activeTabId: Optional[str] = None
    target: Optional[int] = None


def inject_resource_headers(response: Response) -> None:
    """Inject strict anti-caching and noindex headers for all resource endpoints."""
    response.headers["Cache-Control"] = "private, no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"


def _clean_str(val: Any) -> str:
    if isinstance(val, str):
        return val.strip()
    return ""


async def resolve_optional_user_id(
    token: Any = None,
    x_user_id: Any = None,
    x_user_email: Any = None,
) -> Optional[int]:
    """Extract authenticated user_id if present, without failing for anonymous visitors."""
    clean_token = _clean_str(token)
    if clean_token:
        payload = decode_spac2_token(clean_token)
        if payload and payload.get("user_id"):
            try:
                return int(payload["user_id"])
            except (ValueError, TypeError):
                pass

    clean_id = _clean_str(x_user_id)
    if clean_id and clean_id.isdigit() and int(clean_id) > 0:
        return int(clean_id)

    clean_email = _clean_str(x_user_email).lower()
    if clean_email:
        try:
            rows = await execute_pg_query("SELECT id, user_id FROM users WHERE LOWER(email) = $1", clean_email)
            if rows and len(rows) > 0 and rows[0].get("id"):
                return int(rows[0].get("user_id") or (10000 + rows[0]["id"]))
        except Exception:
            pass

    return None


async def _ensure_activity_table():
    try:
        await execute_pg_query("""
            CREATE TABLE IF NOT EXISTS user_resource_activity (
                id BIGSERIAL PRIMARY KEY,
                app_code VARCHAR(30) NOT NULL,
                resource_id VARCHAR(100) NOT NULL,
                user_id BIGINT NOT NULL,
                action VARCHAR(20) NOT NULL DEFAULT 'view',
                last_active TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_resource_activity UNIQUE (app_code, resource_id, user_id, action)
            );
            CREATE INDEX IF NOT EXISTS idx_res_act_lookup ON user_resource_activity(app_code, resource_id, last_active);
        """)
    except Exception as e:
        print(f"[ResourceActivity] Table init notice: {e}")


async def _get_resource_collaborators(app_code: str, resource_id: str, owner_id: int) -> List[Dict[str, Any]]:
    await _ensure_activity_table()
    try:
        # 1. Auto-purge transient activity records older than 30 minutes to keep database compact & zero-cost
        await execute_pg_query(
            "DELETE FROM user_resource_activity WHERE last_active < NOW() - INTERVAL '30 minutes'"
        )

        # 2. Query only activity within the last 30 minutes
        rows = await execute_pg_query("""
            SELECT a.user_id, a.action, EXTRACT(EPOCH FROM a.last_active) * 1000 AS last_active_ts,
                   COALESCE(u.name, u.username, 'Collaborator') AS name,
                   COALESCE(u.email, '') AS email,
                   COALESCE(u.username, '') AS username
            FROM user_resource_activity a
            LEFT JOIN users u ON (u.user_id = a.user_id OR u.id = a.user_id)
            WHERE a.app_code = $1 AND a.resource_id = $2 AND a.last_active >= NOW() - INTERVAL '30 minutes'
            ORDER BY a.last_active DESC
            LIMIT 15
        """, app_code, resource_id)

        # 3. Always look up and place document Owner at top
        owner_rows = await execute_pg_query("""
            SELECT COALESCE(name, username, 'Owner') AS name, COALESCE(email, '') AS email, COALESCE(username, '') AS username
            FROM users WHERE user_id = $1 OR id = $1 LIMIT 1
        """, owner_id)
        owner_name = owner_rows[0].get("name") if (owner_rows and len(owner_rows) > 0) else "Owner"
        owner_email = owner_rows[0].get("email") if (owner_rows and len(owner_rows) > 0) else ""
        owner_username = owner_rows[0].get("username") if (owner_rows and len(owner_rows) > 0) else ""

        collabs = [{
            "user_id": owner_id,
            "name": owner_name,
            "email": owner_email,
            "username": owner_username,
            "action": "owner",
            "is_owner": True,
            "last_active": int(time.time() * 1000)
        }]

        for r in (rows or []):
            uid = int(r.get("user_id") or 0)
            if uid == owner_id:
                continue
            collabs.append({
                "user_id": uid,
                "name": r.get("name") or "Collaborator",
                "email": r.get("email") or "",
                "username": r.get("username") or "",
                "action": r.get("action") or "view",
                "is_owner": False,
                "last_active": int(r.get("last_active_ts") or 0)
            })
        return collabs
    except Exception as e:
        print(f"[ResourceCollaborators] Fetch notice: {e}")
        return []


@router.get("/{app_code}/{resource_id}")
async def get_resource_by_id(
    app_code: str,
    resource_id: str,
    response: Response,
    token: Optional[str] = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """
    Spac2 Resource Contract v1.4 - Read Resource & Collaborators
    """
    inject_resource_headers(response)
    app_code = app_code.strip().lower()
    cfg = RESOURCE_CONFIGS.get(app_code)
    if not cfg:
        raise HTTPException(status_code=404, detail="Resource not found")

    table = cfg["table"]
    id_col = cfg["id_col"]
    user_col = cfg["user_col"]

    requester_id = await resolve_optional_user_id(token, x_user_id, x_user_email)

    try:
        rows = await execute_pg_query(
            f"SELECT * FROM {table} WHERE {id_col} = $1 LIMIT 1",
            resource_id
        )
        if not rows or len(rows) == 0:
            raise HTTPException(status_code=404, detail="Resource not found")

        item = rows[0]
        is_deleted = bool(item.get("is_deleted"))
        if is_deleted:
            raise HTTPException(status_code=404, detail="Resource not found")

        owner_id = int(item.get(user_col) or 0)
        visibility = str(item.get("visibility") or "private").lower().strip()

        # Log viewing activity for authenticated users
        if requester_id:
            await _ensure_activity_table()
            try:
                await execute_pg_query("""
                    INSERT INTO user_resource_activity (app_code, resource_id, user_id, action, last_active)
                    VALUES ($1, $2, $3, 'view', CURRENT_TIMESTAMP)
                    ON CONFLICT (app_code, resource_id, user_id, action) DO UPDATE SET last_active = CURRENT_TIMESTAMP
                """, app_code, resource_id, requester_id)
            except Exception:
                pass

        collaborators = await _get_resource_collaborators(app_code, resource_id, owner_id)

        # 1. Owner check
        if requester_id and requester_id == owner_id:
            return {
                "status": "success",
                "role": "owner",
                "visibility": visibility,
                "collaborators": collaborators,
                "data": item
            }

        # 2. Link Edit check (Public collaboration)
        if visibility == "link_edit":
            role = "editor" if requester_id else "viewer"
            return {
                "status": "success",
                "role": role,
                "visibility": "link_edit",
                "requires_login_to_edit": not bool(requester_id),
                "collaborators": collaborators,
                "data": item
            }

        # 3. Link Read check (Public view only)
        if visibility == "link_read":
            return {
                "status": "success",
                "role": "viewer",
                "visibility": "link_read",
                "requires_login_to_edit": False,
                "collaborators": collaborators,
                "data": item
            }

        # 4. Restricted ACL check
        if visibility == "restricted" and requester_id:
            acl_rows = await execute_pg_query(
                "SELECT permission FROM user_resource_acls WHERE app_code = $1 AND resource_id = $2 AND user_id = $3",
                app_code, resource_id, requester_id
            )
            if acl_rows and len(acl_rows) > 0:
                perm = acl_rows[0].get("permission") or "read"
                return {
                    "status": "success",
                    "role": "editor" if perm in ("write", "admin") else "viewer",
                    "visibility": "restricted",
                    "collaborators": collaborators,
                    "data": item
                }

        # 5. Information Disclosure Masking: Always 404 for unauthorized access
        raise HTTPException(status_code=404, detail="Resource not found")

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ResourceEndpoint] Error fetching {app_code}/{resource_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/{app_code}/{resource_id}/collaborators")
async def get_collaborators_endpoint(
    app_code: str,
    resource_id: str,
    response: Response,
    token: Optional[str] = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """Retrieve real-time active collaborators and viewer list for document."""
    inject_resource_headers(response)
    app_code = app_code.strip().lower()
    cfg = RESOURCE_CONFIGS.get(app_code)
    if not cfg:
        raise HTTPException(status_code=404, detail="Resource not found")

    table = cfg["table"]
    id_col = cfg["id_col"]
    user_col = cfg["user_col"]

    rows = await execute_pg_query(f"SELECT {user_col} FROM {table} WHERE {id_col} = $1 LIMIT 1", resource_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Resource not found")

    owner_id = int(rows[0].get(user_col) or 0)
    collabs = await _get_resource_collaborators(app_code, resource_id, owner_id)
    return {
        "status": "success",
        "resource_id": resource_id,
        "collaborators": collabs
    }


@router.patch("/{app_code}/{resource_id}/visibility")
async def update_resource_visibility(
    app_code: str,
    resource_id: str,
    payload: SetVisibilityRequest,
    response: Response,
    token: Optional[str] = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """
    Spac2 Resource Contract v1.4 - Toggle Visibility with Atomic Rev Increment
    """
    inject_resource_headers(response)
    app_code = app_code.strip().lower()
    cfg = RESOURCE_CONFIGS.get(app_code)
    if not cfg:
        raise HTTPException(status_code=404, detail="Resource not found")

    table = cfg["table"]
    id_col = cfg["id_col"]
    user_col = cfg["user_col"]

    new_vis = payload.visibility.strip().lower()
    if new_vis not in ("private", "link_read", "link_edit", "restricted"):
        raise HTTPException(status_code=400, detail="Invalid visibility state.")

    requester_id = await resolve_optional_user_id(token, x_user_id, x_user_email)
    if not requester_id:
        raise HTTPException(status_code=401, detail="Authentication required to manage sharing settings.")

    try:
        rows = await execute_pg_query(
            f"SELECT {user_col}, is_deleted FROM {table} WHERE {id_col} = $1 LIMIT 1",
            resource_id
        )
        if not rows or len(rows) == 0:
            raise HTTPException(status_code=404, detail="Resource not found")

        item = rows[0]
        if bool(item.get("is_deleted")):
            raise HTTPException(status_code=404, detail="Resource not found")

        owner_id = int(item.get(user_col) or 0)
        if requester_id != owner_id:
            raise HTTPException(status_code=403, detail="Forbidden: Only the owner can change resource visibility.")

        now_ts = int(time.time() * 1000)
        await execute_pg_query(
            f"UPDATE {table} SET visibility = $1, rev = rev + 1, updated_at = $2 WHERE {id_col} = $3",
            new_vis, now_ts, resource_id
        )

        return {
            "status": "success",
            "app_code": app_code,
            "resource_id": resource_id,
            "visibility": new_vis,
            "updated_at": now_ts
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ResourceEndpoint] Error setting visibility for {app_code}/{resource_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
