from __future__ import annotations
import json
import hashlib
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, Query, Header, Response
from pydantic import BaseModel

from auth.deps import get_auth_token, decode_spac2_token, create_spac2_token, decode_text2_token, create_text2_token
from database.postgres import execute_pg_query

router = APIRouter(prefix="/api/sync/note", tags=["sync_note"])


class NoteSyncItem(BaseModel):
    id: str
    title: Optional[str] = ""
    content: Optional[str] = ""
    color: Optional[Dict[str, Any]] = None
    isSaved: Optional[bool] = False
    date: Optional[str] = ""
    is_deleted: Optional[bool] = False


class NoteKeepSyncRequest(BaseModel):
    since_rev: Optional[int] = 0
    items: Optional[List[NoteSyncItem]] = []


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
                    print(f"[ResolveUser] Auto-provision note user notice: {prov_err}")
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


def _is_dark_color(hex_color: str) -> bool:
    if not hex_color or not isinstance(hex_color, str):
        return False
    c = hex_color.replace("#", "").strip()
    if len(c) == 3:
        c = "".join([x + x for x in c])
    if len(c) != 6:
        return False
    try:
        r = int(c[0:2], 16)
        g = int(c[2:4], 16)
        b = int(c[4:6], 16)
        yiq = (r * 299 + g * 587 + b * 114) / 1000
        return yiq < 140
    except Exception:
        return False


def _normalize_color(color_val: Any) -> Dict[str, str]:
    if not color_val:
        return {"bg": "#FDE047", "text": "#000000"}
    
    cur = color_val
    for _ in range(3):
        if isinstance(cur, str):
            cur_s = cur.strip()
            if cur_s.startswith("{"):
                try:
                    cur = json.loads(cur_s)
                except Exception:
                    break
            elif cur_s.startswith('"') and cur_s.endswith('"'):
                try:
                    cur = json.loads(cur_s)
                except Exception:
                    break
            elif cur_s.startswith("#"):
                return {"bg": cur_s, "text": "#FFFFFF" if _is_dark_color(cur_s) else "#000000"}
            else:
                break
        else:
            break

    if isinstance(cur, dict):
        bg = str(cur.get("bg") or "#FDE047")
        text = str(cur.get("text") or ("#FFFFFF" if _is_dark_color(bg) else "#000000"))
        return {"bg": bg, "text": text}

    return {"bg": "#FDE047", "text": "#000000"}


def _format_note_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items_list = []
    seen_ids = set()
    seen_sigs = set()

    for r in rows:
        note_id = str(r.get("note_id") or "").strip()
        if not note_id or note_id in seen_ids:
            continue

        title = (r.get("title") or "").strip()
        content = (r.get("content") or "").strip()
        date_str = (r.get("date") or "").strip()
        is_deleted = bool(r.get("is_deleted"))

        # Content signature for deduplicating identical clones
        if not is_deleted and (title or content):
            time_key = date_str[:16] if len(date_str) >= 16 else ""
            sig = f"{title.lower()}|||{content.lower()}|||{time_key}"
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)

        seen_ids.add(note_id)
        color_val = _normalize_color(r.get("color"))

        items_list.append({
            "id": note_id,
            "title": title,
            "content": content,
            "color": color_val,
            "isSaved": bool(r.get("is_saved")),
            "date": date_str,
            "rev": int(r.get("rev") or 1),
            "is_deleted": is_deleted
        })
    return items_list


@router.get("")
async def get_note_delta_sync(
    response: Response,
    since_rev: int = Query(0),
    token: str = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
):
    """Google Keep-Style Lightweight Delta Pull.
    Returns notes created/updated/deleted since since_rev.
    """
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)

    try:
        rev_res = await execute_pg_query(
            "SELECT COALESCE(MAX(rev), 0) AS current_rev FROM user_sync_notes WHERE user_id = $1",
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
            # Full active notes pull
            rows = await execute_pg_query(
                "SELECT note_id, title, content, color, is_saved, date, rev, is_deleted "
                "FROM user_sync_notes "
                "WHERE user_id = $1 AND is_deleted = FALSE "
                "ORDER BY rev ASC",
                user_id
            )
        else:
            # Delta pull
            rows = await execute_pg_query(
                "SELECT note_id, title, content, color, is_saved, date, rev, is_deleted "
                "FROM user_sync_notes "
                "WHERE user_id = $1 AND rev > $2 "
                "ORDER BY rev ASC",
                user_id, since_rev
            )

        return {
            "status": "success",
            "current_rev": current_rev,
            "items": _format_note_rows(rows)
        }
    except Exception as e:
        print(f"[KeepSyncNote] Delta fetch error for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch delta notes: {str(e)}")


@router.post("")
async def sync_keep_notes_batch(
    payload: NoteKeepSyncRequest,
    response: Response,
    token: str = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
):
    """Google Keep-Style Single-Trip Atomic Full Sync (Push + Pull).
    1. Saves all client mutations (upserts/deletes) with next revision.
    2. Atomically queries and returns remote notes updated by other devices since since_rev.
    """
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)

    try:
        # 1. Get current max revision
        rev_res = await execute_pg_query(
            "SELECT COALESCE(MAX(rev), 0) AS current_rev FROM user_sync_notes WHERE user_id = $1",
            user_id
        )
        current_max_rev = int(rev_res[0]["current_rev"]) if rev_res and rev_res[0].get("current_rev") else 0
        
        synced_note_ids = []
        next_rev = current_max_rev

        # 2. Push client mutations (if any)
        if payload.items and len(payload.items) > 0:
            import time
            now_ts = int(time.time() * 1000)
            next_rev = current_max_rev + 1
            for item in payload.items:
                note_id = str(item.id).strip()
                if not note_id or (note_id == '1' and item.title == 'Start taking note'):
                    continue

                item_title = (item.title or "").strip()
                item_content = (item.content or "").strip()
                normalized_color = _normalize_color(item.color)
                color_json = json.dumps(normalized_color)
                synced_note_ids.append(note_id)

                if item.is_deleted:
                    await execute_pg_query(
                        "INSERT INTO user_sync_notes (user_id, note_id, title, content, color, is_saved, date, rev, is_deleted, updated_at) "
                        "VALUES ($1, $2, '', '', '{}'::jsonb, FALSE, '', $3, TRUE, $4) "
                        "ON CONFLICT (user_id, note_id) DO UPDATE SET "
                        "title = '', content = '', is_deleted = TRUE, rev = EXCLUDED.rev, updated_at = EXCLUDED.updated_at",
                        user_id, note_id, next_rev, now_ts
                    )
                else:

                    await execute_pg_query(
                        "INSERT INTO user_sync_notes (user_id, note_id, title, content, color, is_saved, date, rev, is_deleted, updated_at) "
                        "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, FALSE, $9) "
                        "ON CONFLICT (user_id, note_id) DO UPDATE SET "
                        "title = EXCLUDED.title, content = EXCLUDED.content, color = EXCLUDED.color, "
                        "is_saved = EXCLUDED.is_saved, date = EXCLUDED.date, "
                        "rev = EXCLUDED.rev, is_deleted = FALSE, updated_at = EXCLUDED.updated_at",
                        user_id, note_id, item.title or "", item.content or "",
                        color_json, bool(item.isSaved), item.date or "", next_rev, now_ts
                    )

        # 3. Pull remote updates (Atomic Pull)
        since_rev = payload.since_rev if payload.since_rev is not None else 0
        remote_items = []

        if since_rev == 0 or since_rev > next_rev:
            # First sync on this device or client revision is ahead: return all active notes on server
            rows = await execute_pg_query(
                "SELECT note_id, title, content, color, is_saved, date, rev, is_deleted "
                "FROM user_sync_notes "
                "WHERE user_id = $1 AND is_deleted = FALSE "
                "ORDER BY rev ASC",
                user_id
            )
            remote_items = _format_note_rows(rows)
        elif since_rev < next_rev:
            # Return notes updated by other devices (rev > since_rev)
            rows = await execute_pg_query(
                "SELECT note_id, title, content, color, is_saved, date, rev, is_deleted "
                "FROM user_sync_notes "
                "WHERE user_id = $1 AND rev > $2 "
                "ORDER BY rev ASC",
                user_id, since_rev
            )
            remote_items = _format_note_rows(rows)

        return {
            "status": "success",
            "current_rev": next_rev,
            "synced_count": len(synced_note_ids),
            "items": remote_items
        }
    except Exception as e:
        print(f"[KeepSyncNote] Sync error for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Keep Sync Error: {str(e)}")
