from __future__ import annotations
import json
import hashlib
from typing import Optional, List, Dict, Any, Union
from fastapi import APIRouter, HTTPException, Depends, Query, Header, Response
from pydantic import BaseModel

from auth.deps import get_auth_token, decode_spac2_token, create_spac2_token, decode_text2_token, create_text2_token
from database.postgres import execute_pg_query
from sync.engine import get_domain_max_rev, execute_sync_batch_atomic, resolve_canonical_id

router = APIRouter(prefix="/api/sync/note", tags=["sync_note"])

_table_initialized = False


async def _ensure_note_table():
    global _table_initialized
    if _table_initialized:
        return
    try:
        await execute_pg_query("""
            CREATE TABLE IF NOT EXISTS user_sync_notes (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                note_id VARCHAR(100) NOT NULL,
                title TEXT DEFAULT '',
                content TEXT DEFAULT '',
                color JSONB DEFAULT '{}',
                is_saved BOOLEAN DEFAULT FALSE,
                date TEXT DEFAULT '',
                history JSONB DEFAULT '[]',
                rev BIGINT NOT NULL DEFAULT 1,
                visibility VARCHAR(20) NOT NULL DEFAULT 'private',
                is_deleted BOOLEAN DEFAULT FALSE,
                deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at BIGINT DEFAULT 0,
                CONSTRAINT uq_sync_notes_note_id UNIQUE (note_id)
            );
        """)
        try:
            await execute_pg_query("ALTER TABLE user_sync_notes ADD COLUMN IF NOT EXISTS rev BIGINT NOT NULL DEFAULT 1;")
            await execute_pg_query("ALTER TABLE user_sync_notes ADD COLUMN IF NOT EXISTS history JSONB DEFAULT '[]';")
            await execute_pg_query("ALTER TABLE user_sync_notes ADD COLUMN IF NOT EXISTS visibility VARCHAR(20) NOT NULL DEFAULT 'private';")
            await execute_pg_query("ALTER TABLE user_sync_notes ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL;")
            await execute_pg_query("CREATE INDEX IF NOT EXISTS idx_sync_notes_user_rev ON user_sync_notes(user_id, rev);")
            await execute_pg_query("CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_notes_global_note_id ON user_sync_notes(note_id);")
        except Exception:
            pass
        _table_initialized = True
    except Exception as e:
        print(f"[NOTE SYNC] Warning initializing user_sync_notes table: {e}")


class NoteSyncItem(BaseModel):
    id: str
    title: Optional[str] = ""
    content: Optional[str] = ""
    color: Optional[Union[Dict[str, Any], str]] = None
    isSaved: Optional[bool] = False
    date: Optional[str] = ""
    rev: Optional[int] = 1
    base_rev: Optional[int] = None
    visibility: Optional[str] = "private"
    history: Optional[Union[List[Dict[str, Any]], str]] = None
    updated: Optional[int] = None
    updated_at: Optional[int] = None
    is_deleted: Optional[bool] = False


class NoteKeepSyncRequest(BaseModel):
    sync_batch_id: Optional[str] = None
    since_rev: Optional[int] = 0
    items: Optional[List[NoteSyncItem]] = []


def _clean_str(val: Any) -> str:
    if isinstance(val, str):
        return val.strip()
    return ""


def _normalize_json_field(val: Any, default_val: Any) -> Any:
    if val is None:
        return default_val
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return default_val
        try:
            return json.loads(s)
        except Exception:
            return default_val
    return default_val


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
        visibility = str(r.get("visibility") or "private").lower().strip()
        history_val = _normalize_json_field(r.get("history"), [])

        items_list.append({
            "id": note_id,
            "title": title,
            "content": content,
            "color": color_val,
            "isSaved": bool(r.get("is_saved")),
            "date": date_str,
            "rev": int(r.get("rev") or 1),
            "visibility": visibility,
            "history": history_val,
            "updated": int(r.get("updated_at") or 0),
            "updated_at": int(r.get("updated_at") or 0),
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
    await _ensure_note_table()
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)

    try:
        current_rev = await get_domain_max_rev(user_id, "note")

        if since_rev > 0 and since_rev == current_rev:
            return {
                "status": "success",
                "current_rev": current_rev,
                "items": []
            }

        if since_rev == 0 or since_rev > current_rev:
            # Full active notes pull
            rows = await execute_pg_query(
                "SELECT note_id, title, content, color, is_saved, date, history, rev, visibility, is_deleted, updated_at "
                "FROM user_sync_notes "
                "WHERE user_id = $1 AND is_deleted = FALSE "
                "ORDER BY rev ASC",
                user_id
            )
        else:
            # Delta pull
            rows = await execute_pg_query(
                "SELECT note_id, title, content, color, is_saved, date, history, rev, visibility, is_deleted, updated_at "
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
    1. Saves all client mutations (upserts/deletes) atomically with next revision and OCC checks.
    2. Atomically queries and returns remote notes updated by other devices since since_rev.
    """
    await _ensure_note_table()
    user_id = await _resolve_user_id(token, x_user_id, x_user_email, x_user_name, response)

    try:
        has_mutations = bool(payload.items and len(payload.items) > 0)
        mutation_results: List[Dict[str, Any]] = []

        async def _write_note_mutations(next_rev: int, now_ts: int):
            if payload.items and len(payload.items) > 0:
                for item in payload.items:
                    raw_note_id = str(item.id).strip()
                    if not raw_note_id or (raw_note_id == '1' and item.title == 'Start taking note'):
                        continue

                    note_id = await resolve_canonical_id(user_id, "note", raw_note_id)
                    if not note_id:
                        continue

                    # 1. Ownership & Permissions lookup
                    existing_rows = await execute_pg_query(
                        "SELECT user_id, rev, title, content, color, is_saved, date, history, visibility, is_deleted, updated_at "
                        "FROM user_sync_notes WHERE note_id = $1 LIMIT 1",
                        note_id
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
                            # Verify Shared Edit permission (link_edit or ACL write/admin)
                            row_vis = str(existing_row.get("visibility") or "private").lower().strip()
                            can_edit = (row_vis == "link_edit")
                            if not can_edit and row_vis == "restricted":
                                acl_res = await execute_pg_query(
                                    "SELECT permission FROM user_resource_acls WHERE app_code = 'note' AND resource_id = $1 AND user_id = $2",
                                    note_id, user_id
                                )
                                if acl_res and acl_res[0].get("permission") in ("write", "admin"):
                                    can_edit = True

                            if not can_edit:
                                mutation_results.append({
                                    "id": raw_note_id,
                                    "status": "REJECT",
                                    "error": "PERMISSION_DENIED"
                                })
                                continue

                    # 2. Strict Optimistic Concurrency Control (OCC) - DO NOT overwrite stale server data
                    if existing_row and not item.is_deleted:
                        server_rev = int(existing_row.get("rev") or 1)
                        client_base_rev = item.base_rev if item.base_rev is not None else item.rev
                        if client_base_rev is not None and client_base_rev > 0 and client_base_rev < server_rev:
                            mutation_results.append({
                                "id": raw_note_id,
                                "status": "CONFLICT",
                                "server_rev": server_rev,
                                "server_updated_at": int(existing_row.get("updated_at") or now_ts),
                                "error": "OCC_VERSION_MISMATCH"
                            })
                            continue

                    item_title = (item.title or "").strip()
                    item_content = (item.content or "").strip()
                    normalized_color = _normalize_color(item.color)
                    color_json = json.dumps(normalized_color)
                    item_updated_at = int(item.updated or item.updated_at or now_ts)

                    # Visibility can only be updated by the owner
                    if is_owner:
                        item_visibility = str(item.visibility or (existing_row.get("visibility") if existing_row else "private")).lower().strip()
                        if item_visibility not in ("private", "link_read", "link_edit", "restricted"):
                            item_visibility = "private"
                    else:
                        item_visibility = str(existing_row.get("visibility") or "link_edit")

                    history_val = _normalize_json_field(item.history, [])
                    if not isinstance(history_val, list):
                        history_val = []
                    history_json = json.dumps(history_val[-30:]) # Retain latest 30 snapshots

                    if item.is_deleted:
                        if is_owner:
                            await execute_pg_query(
                                "INSERT INTO user_sync_notes ("
                                "   user_id, note_id, title, content, color, is_saved, date, history, rev, visibility, is_deleted, deleted_at, updated_at"
                                ") VALUES ($1, $2, '', '', '{}'::jsonb, FALSE, '', '[]'::jsonb, $3, 'private', TRUE, CURRENT_TIMESTAMP, $4) "
                                "ON CONFLICT (note_id) DO UPDATE SET "
                                "   title = '', content = '', is_deleted = TRUE, deleted_at = CURRENT_TIMESTAMP, rev = EXCLUDED.rev, updated_at = EXCLUDED.updated_at "
                                "WHERE user_sync_notes.user_id = EXCLUDED.user_id",
                                target_owner_id, note_id, next_rev, now_ts
                            )
                            mutation_results.append({"id": raw_note_id, "status": "ACK", "rev": next_rev})
                        else:
                            mutation_results.append({
                                "id": raw_note_id,
                                "status": "REJECT",
                                "error": "ONLY_OWNER_CAN_DELETE"
                            })
                    else:
                        await execute_pg_query(
                            "INSERT INTO user_sync_notes ("
                            "   user_id, note_id, title, content, color, is_saved, date, history, rev, visibility, is_deleted, deleted_at, updated_at"
                            ") VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8::jsonb, $9, $10, FALSE, NULL, $11) "
                            "ON CONFLICT (note_id) DO UPDATE SET "
                            "   title = EXCLUDED.title, content = EXCLUDED.content, color = EXCLUDED.color, "
                            "   is_saved = EXCLUDED.is_saved, date = EXCLUDED.date, history = EXCLUDED.history, "
                            "   rev = EXCLUDED.rev, visibility = EXCLUDED.visibility, is_deleted = FALSE, deleted_at = NULL, updated_at = EXCLUDED.updated_at "
                            "WHERE user_sync_notes.user_id = EXCLUDED.user_id",
                            target_owner_id, note_id, item_title, item_content,
                            color_json, bool(item.isSaved), item.date or "", history_json,
                            next_rev, item_visibility, item_updated_at
                        )
                        mutation_results.append({"id": raw_note_id, "status": "ACK", "rev": next_rev})

        # Atomic commit with idempotency & concurrency protection
        sync_result = await execute_sync_batch_atomic(
            user_id=user_id,
            app_code="note",
            sync_batch_id=payload.sync_batch_id,
            payload_data=payload,
            has_mutations=has_mutations,
            write_callback=_write_note_mutations
        )

        effective_rev = sync_result["current_rev"]

        # Pull remote updates (Atomic Pull)
        since_rev = payload.since_rev if payload.since_rev is not None else 0
        remote_items = []

        if since_rev == 0 or since_rev > effective_rev:
            rows = await execute_pg_query(
                "SELECT note_id, title, content, color, is_saved, date, history, rev, visibility, is_deleted, updated_at "
                "FROM user_sync_notes "
                "WHERE user_id = $1 AND is_deleted = FALSE "
                "ORDER BY rev ASC",
                user_id
            )
            remote_items = _format_note_rows(rows)
        elif since_rev < effective_rev:
            rows = await execute_pg_query(
                "SELECT note_id, title, content, color, is_saved, date, history, rev, visibility, is_deleted, updated_at "
                "FROM user_sync_notes "
                "WHERE user_id = $1 AND rev > $2 "
                "ORDER BY rev ASC",
                user_id, since_rev
            )
            remote_items = _format_note_rows(rows)

        ack_count = len([r for r in mutation_results if r.get("status") == "ACK"])

        # Broadcast lightweight invalidation ping to subscribed clients
        if ack_count > 0:
            for res_item in mutation_results:
                if res_item.get("status") == "ACK":
                    note_id_val = res_item.get("id")
                    if note_id_val:
                        try:
                            import asyncio
                            from websocket.manager import broadcast_resource_invalidation
                            asyncio.create_task(broadcast_resource_invalidation(
                                app="note",
                                resource_id=str(note_id_val),
                                rev=effective_rev,
                                actor_email=_clean_str(x_user_email)
                            ))
                        except Exception as ping_err:
                            print(f"[WS Ping Notice] {ping_err}")

        return {
            "status": "success",
            "current_rev": effective_rev,
            "deduplicated": sync_result.get("deduplicated", False),
            "results": mutation_results,
            "synced_count": ack_count,
            "items": remote_items
        }
    except Exception as e:
        print(f"[KeepSyncNote] Sync error for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Keep Sync Error: {str(e)}")
