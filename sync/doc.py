from __future__ import annotations
import json
import hashlib
import time
from typing import Optional, List, Dict, Any, Union
from fastapi import APIRouter, HTTPException, Depends, Query, Header, Response
from pydantic import BaseModel

from auth.deps import get_auth_token, decode_spac2_token, create_spac2_token, decode_text2_token, create_text2_token
from database.postgres import execute_pg_query

router = APIRouter(prefix="/api/sync/doc", tags=["sync_doc"])


# --- Pydantic Models ---
class DocSyncItem(BaseModel):
    id: str
    title: Optional[str] = ""
    body: Optional[str] = ""
    previewText: Optional[str] = ""
    preview_text: Optional[str] = ""
    wordCount: Optional[int] = 0
    word_count: Optional[int] = 0
    pinned: Optional[bool] = False
    inTrash: Optional[bool] = False
    in_trash: Optional[bool] = False
    target: Optional[int] = 500
    tabs: Optional[Union[List[Dict[str, Any]], str]] = None
    activeTabId: Optional[str] = ""
    active_tab_id: Optional[str] = ""
    history: Optional[Union[List[Dict[str, Any]], str]] = None
    created: Optional[Union[int, float, str]] = None
    updated: Optional[Union[int, float, str]] = None
    created_at: Optional[Union[int, float, str]] = None
    updated_at: Optional[Union[int, float, str]] = None
    is_deleted: Optional[bool] = False


class DocKeepSyncRequest(BaseModel):
    since_rev: Optional[int] = 0
    items: Optional[List[DocSyncItem]] = []


_table_initialized = False


async def _ensure_doc_table():
    global _table_initialized
    if _table_initialized:
        return
    try:
        await execute_pg_query("""
            CREATE TABLE IF NOT EXISTS user_sync_docs (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                doc_id VARCHAR(100) NOT NULL,
                title TEXT DEFAULT '',
                body TEXT DEFAULT '',
                preview_text TEXT DEFAULT '',
                word_count INT DEFAULT 0,
                pinned BOOLEAN DEFAULT FALSE,
                in_trash BOOLEAN DEFAULT FALSE,
                target INT DEFAULT 500,
                tabs JSONB DEFAULT '[]',
                active_tab_id VARCHAR(100) DEFAULT 'tab-default',
                history JSONB DEFAULT '[]',
                rev BIGINT NOT NULL DEFAULT 1,
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at BIGINT DEFAULT 0,
                UNIQUE (user_id, doc_id)
            );
        """)
        try:
            await execute_pg_query("CREATE INDEX IF NOT EXISTS idx_sync_docs_user_rev ON user_sync_docs(user_id, rev);")
        except Exception:
            pass
        _table_initialized = True
    except Exception as e:
        print(f"[DOC SYNC] Warning initializing user_sync_docs table: {e}")


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
                    print(f"[ResolveUser] Auto-provision doc user notice: {prov_err}")
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
        email_hash_56bit = int(hashlib.sha256(f"spac2_doc_salt_{clean_email}".encode("utf-8")).hexdigest()[:14], 16)
        stable_id = 1000000000000 + (email_hash_56bit % 8000000000000)
        return stable_id

    raise HTTPException(status_code=401, detail="Unauthorized: No valid Spac2 session or user ID found")


def _normalize_json_field(val: Any, default_val: Any) -> Any:
    if val is None:
        return default_val
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        trimmed = val.strip()
        if not trimmed:
            return default_val
        try:
            return json.loads(trimmed)
        except Exception:
            return default_val
    return default_val


def _format_doc_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items_list = []
    seen_ids = set()

    for r in rows:
        doc_id = str(r.get("doc_id") or "").strip()
        if not doc_id or doc_id in seen_ids:
            continue
        seen_ids.add(doc_id)

        title = str(r.get("title") or "")
        body = str(r.get("body") or "")
        preview_text = str(r.get("preview_text") or "")
        word_count = int(r.get("word_count") or 0)
        pinned = bool(r.get("pinned"))
        in_trash = bool(r.get("in_trash"))
        target = int(r.get("target") or 500)
        tabs = _normalize_json_field(r.get("tabs"), [])
        active_tab_id = str(r.get("active_tab_id") or "")
        history = _normalize_json_field(r.get("history"), [])
        rev = int(r.get("rev") or 1)
        is_deleted = bool(r.get("is_deleted"))
        updated_at = int(r.get("updated_at") or 0)

        items_list.append({
            "id": doc_id,
            "title": title,
            "body": body,
            "previewText": preview_text,
            "preview_text": preview_text,
            "wordCount": word_count,
            "word_count": word_count,
            "pinned": pinned,
            "inTrash": in_trash,
            "in_trash": in_trash,
            "target": target,
            "tabs": tabs,
            "activeTabId": active_tab_id,
            "active_tab_id": active_tab_id,
            "history": history,
            "rev": rev,
            "is_deleted": is_deleted,
            "updated": updated_at,
            "updated_at": updated_at
        })
    return items_list


@router.get("")
async def get_doc_delta_sync(
    response: Response,
    since_rev: int = Query(0),
    token: str = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
):
    """Google Keep-Style Lightweight Delta Pull for Spac2 Doc.
    Returns documents created/updated/deleted since since_rev.
    """
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)
    await _ensure_doc_table()

    try:
        rev_res = await execute_pg_query(
            "SELECT COALESCE(MAX(rev), 0) AS current_rev FROM user_sync_docs WHERE user_id = $1",
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
            # Full active documents pull
            rows = await execute_pg_query(
                "SELECT doc_id, title, body, preview_text, word_count, pinned, in_trash, target, "
                "tabs, active_tab_id, history, rev, is_deleted, updated_at "
                "FROM user_sync_docs "
                "WHERE user_id = $1 AND is_deleted = FALSE "
                "ORDER BY rev ASC",
                user_id
            )
        else:
            # Delta pull
            rows = await execute_pg_query(
                "SELECT doc_id, title, body, preview_text, word_count, pinned, in_trash, target, "
                "tabs, active_tab_id, history, rev, is_deleted, updated_at "
                "FROM user_sync_docs "
                "WHERE user_id = $1 AND rev > $2 "
                "ORDER BY rev ASC",
                user_id, since_rev
            )

        return {
            "status": "success",
            "current_rev": current_rev,
            "items": _format_doc_rows(rows)
        }
    except Exception as e:
        print(f"[KeepSyncDoc] Delta fetch error for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch delta docs: {str(e)}")


@router.post("")
async def sync_keep_docs_batch(
    payload: DocKeepSyncRequest,
    response: Response,
    token: str = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
):
    """Google Keep-Style Single-Trip Atomic Full Sync (Push + Pull).
    1. Saves all client mutations (upserts/deletes) with next revision.
    2. Soft Delete Tombstone: frees heavy body/tabs content while retaining metadata for 30-day delta propagation.
    3. Auto-purges soft-deleted tombstones older than 30 days.
    4. Atomically queries and returns remote documents updated by other devices since since_rev.
    """
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)
    await _ensure_doc_table()

    try:
        # 1. Get current max revision
        rev_res = await execute_pg_query(
            "SELECT COALESCE(MAX(rev), 0) AS current_rev FROM user_sync_docs WHERE user_id = $1",
            user_id
        )
        current_max_rev = int(rev_res[0]["current_rev"]) if rev_res and rev_res[0].get("current_rev") else 0

        synced_doc_ids = []
        next_rev = current_max_rev

        now_ts = int(time.time() * 1000)

        # 2. Push client mutations (if any)
        if payload.items and len(payload.items) > 0:
            next_rev = current_max_rev + 1
            for item in payload.items:
                doc_id = str(item.id).strip()
                if not doc_id:
                    continue

                item_title = (item.title or "").strip()
                item_body = item.body or ""
                preview_text = item.previewText or item.preview_text or ""
                word_count = item.wordCount if item.wordCount is not None else (item.word_count or 0)
                pinned = bool(item.pinned)
                in_trash = bool(item.inTrash if item.inTrash is not None else item.in_trash)
                target = int(item.target or 500)
                active_tab_id = item.activeTabId or item.active_tab_id or ""

                tabs_val = _normalize_json_field(item.tabs, [])
                tabs_json = json.dumps(tabs_val)

                history_val = _normalize_json_field(item.history, [])
                history_json = json.dumps(history_val)

                item_updated_at = int(item.updated or item.updated_at or now_ts)
                synced_doc_ids.append(doc_id)

                if item.is_deleted:
                    # Soft Delete Tombstone: Clear heavy content, retain metadata for 30 days
                    await execute_pg_query(
                        "INSERT INTO user_sync_docs ("
                        "   user_id, doc_id, title, body, preview_text, word_count, pinned, in_trash, "
                        "   target, tabs, active_tab_id, history, rev, is_deleted, updated_at"
                        ") VALUES ($1, $2, '', '', '', 0, FALSE, FALSE, 500, '[]'::jsonb, '', '[]'::jsonb, $3, TRUE, $4) "
                        "ON CONFLICT (user_id, doc_id) DO UPDATE SET "
                        "   title = '', body = '', preview_text = '', word_count = 0, pinned = FALSE, in_trash = FALSE, "
                        "   tabs = '[]'::jsonb, history = '[]'::jsonb, is_deleted = TRUE, rev = EXCLUDED.rev, updated_at = EXCLUDED.updated_at",
                        user_id, doc_id, next_rev, now_ts
                    )
                else:
                    await execute_pg_query(
                        "INSERT INTO user_sync_docs ("
                        "   user_id, doc_id, title, body, preview_text, word_count, pinned, in_trash, "
                        "   target, tabs, active_tab_id, history, rev, is_deleted, updated_at"
                        ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12::jsonb, $13, FALSE, $14) "
                        "ON CONFLICT (user_id, doc_id) DO UPDATE SET "
                        "   title = EXCLUDED.title, body = EXCLUDED.body, preview_text = EXCLUDED.preview_text, "
                        "   word_count = EXCLUDED.word_count, pinned = EXCLUDED.pinned, in_trash = EXCLUDED.in_trash, "
                        "   target = EXCLUDED.target, tabs = EXCLUDED.tabs, active_tab_id = EXCLUDED.active_tab_id, "
                        "   history = EXCLUDED.history, rev = EXCLUDED.rev, is_deleted = FALSE, updated_at = EXCLUDED.updated_at",
                        user_id, doc_id, item_title, item_body, preview_text, word_count,
                        pinned, in_trash, target, tabs_json, active_tab_id, history_json,
                        next_rev, item_updated_at
                    )

        # 3. 30-Day Tombstone Auto-Purge: Clean tombstones older than 30 days
        try:
            thirty_days_ago_ts = now_ts - (30 * 86400 * 1000)
            await execute_pg_query(
                "DELETE FROM user_sync_docs WHERE user_id = $1 AND is_deleted = TRUE AND updated_at < $2",
                user_id, thirty_days_ago_ts
            )
        except Exception as purge_err:
            print(f"[KeepSyncDoc] 30-day tombstone cleanup notice: {purge_err}")

        # 4. Atomic Pull: Get remote updates
        since_rev = payload.since_rev if payload.since_rev is not None else 0
        remote_items = []

        if since_rev == 0 or since_rev > next_rev:
            # Full sync: all active documents
            rows = await execute_pg_query(
                "SELECT doc_id, title, body, preview_text, word_count, pinned, in_trash, target, "
                "tabs, active_tab_id, history, rev, is_deleted, updated_at "
                "FROM user_sync_docs "
                "WHERE user_id = $1 AND is_deleted = FALSE "
                "ORDER BY rev ASC",
                user_id
            )
            remote_items = _format_doc_rows(rows)
        elif since_rev < next_rev:
            # Delta sync
            rows = await execute_pg_query(
                "SELECT doc_id, title, body, preview_text, word_count, pinned, in_trash, target, "
                "tabs, active_tab_id, history, rev, is_deleted, updated_at "
                "FROM user_sync_docs "
                "WHERE user_id = $1 AND rev > $2 "
                "ORDER BY rev ASC",
                user_id, since_rev
            )
            remote_items = _format_doc_rows(rows)

        return {
            "status": "success",
            "current_rev": next_rev,
            "synced_count": len(synced_doc_ids),
            "items": remote_items
        }

    except Exception as e:
        print(f"[KeepSyncDoc] Sync error for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to sync docs: {str(e)}")
