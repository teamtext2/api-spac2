from __future__ import annotations
import time
from typing import Optional, Dict, Any
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
    visibility: str  # 'private' | 'link_read' (public link view-only)


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
    Standard Read-Only Resource Fetching Endpoint.
    - Owner -> Full access ('owner')
    - Shared Link (link_read / public) -> Read-only ('viewer')
    - Private & Non-Owner -> 404 (Masking)
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
            raise HTTPException(status_code=404, detail="NOT_FOUND")

        item = rows[0]
        is_deleted = bool(item.get("is_deleted"))
        if is_deleted:
            raise HTTPException(status_code=404, detail="DELETED")

        owner_id = int(item.get(user_col) or 0)
        raw_visibility = str(item.get("visibility") or "private").lower().strip()

        # 1. Owner check
        if requester_id and requester_id == owner_id:
            return {
                "status": "success",
                "role": "owner",
                "visibility": "link_read" if raw_visibility in ("link_read", "public", "link_edit") else "private",
                "data": item
            }

        # 2. Public / Shared link check (Strictly View-Only)
        if raw_visibility in ("link_read", "public", "link_edit"):
            return {
                "status": "success",
                "role": "viewer",
                "visibility": "link_read",
                "data": item
            }

        # 3. Private resource -> 403 Forbidden for non-owners
        raise HTTPException(status_code=403, detail="PERMISSION_DENIED")

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ResourceEndpoint] Error fetching {app_code}/{resource_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


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
    Simplified Share Switch: Toggle between 'link_read' (public view link) and 'private' (disabled).
    Only the resource owner can toggle this.
    """
    inject_resource_headers(response)
    app_code = app_code.strip().lower()
    cfg = RESOURCE_CONFIGS.get(app_code)
    if not cfg:
        raise HTTPException(status_code=404, detail="Resource not found")

    table = cfg["table"]
    id_col = cfg["id_col"]
    user_col = cfg["user_col"]

    raw_vis = payload.visibility.strip().lower()
    new_vis = "link_read" if raw_vis in ("link_read", "public", "link_edit", "true", "1") else "private"

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
        up_rows = await execute_pg_query(
            f"UPDATE {table} SET visibility = $1, rev = rev + 1, updated_at = $2 WHERE {id_col} = $3 RETURNING rev",
            new_vis, now_ts, resource_id
        )
        new_rev = int(up_rows[0].get("rev") or 1) if up_rows else 1

        return {
            "status": "success",
            "app_code": app_code,
            "resource_id": resource_id,
            "visibility": new_vis,
            "rev": new_rev,
            "updated_at": now_ts
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ResourceEndpoint] Error setting visibility for {app_code}/{resource_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
