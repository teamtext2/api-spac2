from __future__ import annotations
import json
import hashlib
import time
from typing import Optional, List, Dict, Any, Union
from fastapi import APIRouter, HTTPException, Depends, Query, Header, Response
from pydantic import BaseModel

from auth.deps import get_auth_token, decode_spac2_token, create_spac2_token, decode_text2_token, create_text2_token
from database.postgres import execute_pg_query

router = APIRouter(prefix="/api/sync/countday", tags=["sync_countday"])


# --- Pydantic Models ---
class CountdayEventSyncItem(BaseModel):
    id: str
    title: Optional[str] = ""
    date: Optional[str] = ""
    target_date: Optional[str] = None  # Alias support
    order_index: Optional[int] = 0
    order: Optional[int] = None  # Alias support
    is_deleted: Optional[bool] = False


class CountdayKeepSyncRequest(BaseModel):
    since_rev: Optional[int] = 0
    items: Optional[List[CountdayEventSyncItem]] = []


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

    # 1. Primary: Verify Spac2 JWT Token
    if clean_token:
        payload = decode_spac2_token(clean_token)
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
                    print(f"[ResolveUser] Auto-provision countday user notice: {prov_err}")
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
                    new_token = create_spac2_token(
                        user_id=uid,
                        username=row.get("username") or clean_username,
                        email=row.get("email") or clean_email or f"{clean_username}@spac2.com"
                    )
                    response.headers["X-New-Spac2-Token"] = new_token
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

    raise HTTPException(status_code=401, detail="Unauthorized: No valid Spac2 session or token found")


def _format_countday_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items_list = []
    seen_ids = set()
    seen_sigs = set()

    for r in rows:
        event_id = str(r.get("event_id") or "").strip()
        if not event_id or event_id in seen_ids:
            continue

        date_str = str(r.get("target_date") or "").strip()
        title = (r.get("title") or "").strip()
        order_index = int(r.get("order_index") or 0)
        is_deleted = bool(r.get("is_deleted"))

        # Deduplication signature
        if not is_deleted and (title or date_str):
            sig = f"{date_str}|||{title.lower()}"
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)

        seen_ids.add(event_id)

        items_list.append({
            "id": event_id,
            "title": title,
            "date": date_str,
            "target_date": date_str,
            "order_index": order_index,
            "order": order_index,
            "rev": int(r.get("rev") or 1),
            "updatedAt": int(r.get("updated_at") or 0),
            "is_deleted": is_deleted
        })
    return items_list


@router.get("")
async def get_countday_delta_sync(
    response: Response,
    since_rev: int = Query(0),
    token: str = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
):
    """Google Keep / Countday-Style Lightweight Delta Pull.
    Returns countdown events created/updated/deleted since since_rev.
    """
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)

    try:
        rev_res = await execute_pg_query(
            "SELECT COALESCE(MAX(rev), 0) AS current_rev FROM user_sync_countday_events WHERE user_id = $1",
            user_id
        )
        current_rev = int(rev_res[0]["current_rev"]) if rev_res and rev_res[0].get("current_rev") else 0

        if since_rev > 0 and since_rev == current_rev:
            return {
                "status": "success",
                "current_rev": current_rev,
                "items": []
            }

        if since_rev == 0 or since_rev > current_rev:
            # Full active events pull
            rows = await execute_pg_query(
                "SELECT event_id, title, target_date, order_index, rev, is_deleted, updated_at "
                "FROM user_sync_countday_events "
                "WHERE user_id = $1 AND is_deleted = FALSE "
                "ORDER BY order_index ASC, rev ASC",
                user_id
            )
        else:
            # Delta pull
            rows = await execute_pg_query(
                "SELECT event_id, title, target_date, order_index, rev, is_deleted, updated_at "
                "FROM user_sync_countday_events "
                "WHERE user_id = $1 AND rev > $2 "
                "ORDER BY order_index ASC, rev ASC",
                user_id, since_rev
            )

        return {
            "status": "success",
            "current_rev": current_rev,
            "items": _format_countday_rows(rows)
        }
    except Exception as e:
        print(f"[KeepSyncCountday] Delta fetch error for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch delta countday events: {str(e)}")


@router.post("")
async def sync_keep_countday_batch(
    payload: CountdayKeepSyncRequest,
    response: Response,
    token: str = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
):
    """Google Keep / Countday Single-Trip Atomic Full Sync (Push + Pull).
    1. Saves all client mutations (upserts/deletes) with next revision.
    2. Atomically queries and returns remote events updated by other devices since since_rev.
    """
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)

    try:
        # 1. Get current max revision
        rev_res = await execute_pg_query(
            "SELECT COALESCE(MAX(rev), 0) AS current_rev FROM user_sync_countday_events WHERE user_id = $1",
            user_id
        )
        current_max_rev = int(rev_res[0]["current_rev"]) if rev_res and rev_res[0].get("current_rev") else 0
        
        synced_event_ids = []
        next_rev = current_max_rev

        # 2. Push client mutations (if any)
        if payload.items and len(payload.items) > 0:
            now_ts = int(time.time() * 1000)
            next_rev = current_max_rev + 1
            for item in payload.items:
                event_id = str(item.id).strip()
                if not event_id:
                    continue

                item_title = (item.title or "").strip()
                item_date = (item.date or item.target_date or "").strip()
                order_index = int(item.order_index if item.order_index is not None else (item.order or 0))

                # Backend Smart Deduplication: reuse existing event_id if duplicate signature
                if not item.is_deleted and (item_title and item_date):
                    try:
                        existing_dup = await execute_pg_query(
                            "SELECT event_id FROM user_sync_countday_events WHERE user_id = $1 AND title = $2 AND target_date = $3 AND is_deleted = FALSE LIMIT 1",
                            user_id, item_title, item_date
                        )
                        if existing_dup and existing_dup[0].get("event_id"):
                            event_id = str(existing_dup[0]["event_id"])
                    except Exception:
                        pass

                synced_event_ids.append(event_id)

                if item.is_deleted:
                    await execute_pg_query(
                        "INSERT INTO user_sync_countday_events (user_id, event_id, title, target_date, order_index, rev, is_deleted, updated_at) "
                        "VALUES ($1, $2, '', '', 0, $3, TRUE, $4) "
                        "ON CONFLICT (user_id, event_id) DO UPDATE SET "
                        "title = '', target_date = '', is_deleted = TRUE, rev = EXCLUDED.rev, updated_at = EXCLUDED.updated_at",
                        user_id, event_id, next_rev, now_ts
                    )
                else:
                    await execute_pg_query(
                        "INSERT INTO user_sync_countday_events (user_id, event_id, title, target_date, order_index, rev, is_deleted, updated_at) "
                        "VALUES ($1, $2, $3, $4, $5, $6, FALSE, $7) "
                        "ON CONFLICT (user_id, event_id) DO UPDATE SET "
                        "title = EXCLUDED.title, target_date = EXCLUDED.target_date, order_index = EXCLUDED.order_index, "
                        "rev = EXCLUDED.rev, is_deleted = FALSE, updated_at = EXCLUDED.updated_at",
                        user_id, event_id, item_title, item_date, order_index, next_rev, now_ts
                    )

        # 3. Pull remote updates (Atomic Pull)
        since_rev = payload.since_rev if payload.since_rev is not None else 0
        remote_items = []

        if since_rev == 0 or since_rev > next_rev:
            # First sync on this device or client revision is ahead: return all active events on server
            rows = await execute_pg_query(
                "SELECT event_id, title, target_date, order_index, rev, is_deleted, updated_at "
                "FROM user_sync_countday_events "
                "WHERE user_id = $1 AND is_deleted = FALSE "
                "ORDER BY order_index ASC, rev ASC",
                user_id
            )
            remote_items = _format_countday_rows(rows)
        elif since_rev < next_rev:
            # Return events updated by other devices (rev > since_rev)
            rows = await execute_pg_query(
                "SELECT event_id, title, target_date, order_index, rev, is_deleted, updated_at "
                "FROM user_sync_countday_events "
                "WHERE user_id = $1 AND rev > $2 "
                "ORDER BY order_index ASC, rev ASC",
                user_id, since_rev
            )
            remote_items = _format_countday_rows(rows)

        return {
            "status": "success",
            "current_rev": next_rev,
            "synced_count": len(synced_event_ids),
            "items": remote_items
        }
    except Exception as e:
        print(f"[KeepSyncCountday] Sync error for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Keep Sync Countday Error: {str(e)}")
