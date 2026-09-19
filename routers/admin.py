import os
import re
import time
import json
import hmac
import hashlib
import base64
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Depends, Header, Query, Request

from database.postgres import execute_pg_query
from storage.r2 import delete_user_r2_and_local_files, r2_client
from websocket.manager import active_connections
from auth.deps import get_auth_token, _b64_url_encode, _b64_url_decode
from auth.security import hash_password, validate_password_strength
from config import SPAC2_JWT_SECRET

# --- HERO ADMIN CONFIGURATION ---
HERO_ADMIN_USERNAME = os.getenv("HERO_ADMIN_USERNAME", "091205015565")
HERO_ADMIN_PASSWORD = os.getenv("HERO_ADMIN_PASSWORD", "0372996420")
HERO_ADMIN_SECRET = os.getenv("HERO_ADMIN_SECRET", f"hero_admin_{SPAC2_JWT_SECRET}_spac2_secure")
HERO_TOKEN_EXPIRE_HOURS = 24

router = APIRouter(prefix="/api/admin", tags=["admin"])


# --- Pydantic Request Models ---
class AdminLoginRequest(BaseModel):
    username: str
    password: str


class AdminUserUpdateRequest(BaseModel):
    username: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    bio: Optional[str] = None
    status: Optional[str] = None
    avatar: Optional[str] = None


class AdminResetPasswordRequest(BaseModel):
    new_password: str



# --- Token Utilities for Hero Admin ---
def create_hero_admin_token(username: str) -> str:
    now = int(time.time())
    exp = now + (HERO_TOKEN_EXPIRE_HOURS * 3600)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "role": "hero_admin",
        "sub": username,
        "iat": now,
        "exp": exp
    }
    h_b64 = _b64_url_encode(json.dumps(header, separators=(',', ':')).encode('utf-8'))
    p_b64 = _b64_url_encode(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    signing_input = f"{h_b64}.{p_b64}".encode('utf-8')
    sig = hmac.new(HERO_ADMIN_SECRET.encode('utf-8'), signing_input, hashlib.sha256).digest()
    return f"{h_b64}.{p_b64}.{_b64_url_encode(sig)}"


def decode_hero_admin_token(token: str) -> Optional[Dict[str, Any]]:
    if not token or not isinstance(token, str):
        return None
    token = token.strip()
    if token.startswith("Bearer "):
        token = token[7:].strip()
    parts = token.split(".")
    if len(parts) != 3:
        return None
    h_b64, p_b64, s_b64 = parts
    try:
        header = json.loads(_b64_url_decode(h_b64).decode('utf-8'))
        if header.get("alg") != "HS256":
            return None
        signing_input = f"{h_b64}.{p_b64}".encode('utf-8')
        expected_sig = hmac.new(HERO_ADMIN_SECRET.encode('utf-8'), signing_input, hashlib.sha256).digest()
        actual_sig = _b64_url_decode(s_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        payload = json.loads(_b64_url_decode(p_b64).decode('utf-8'))
        now = int(time.time())
        if payload.get("exp") and now > payload["exp"]:
            return None
        if payload.get("role") != "hero_admin":
            return None
        return payload
    except Exception:
        return None


async def require_hero_admin(token: str = Depends(get_auth_token)):
    if not token:
        raise HTTPException(status_code=401, detail="Hero Admin authentication required")
    payload = decode_hero_admin_token(token)
    if not payload:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid or expired Admin session")
    return payload


# --- Endpoints ---

@router.post("/login")
async def api_admin_login(req: AdminLoginRequest):
    """Authenticate Hero Admin via secure credentials."""
    u_match = hmac.compare_digest(req.username.strip(), HERO_ADMIN_USERNAME.strip())
    p_match = hmac.compare_digest(req.password.strip(), HERO_ADMIN_PASSWORD.strip())

    if not (u_match and p_match):
        raise HTTPException(status_code=401, detail="Tài khoản hoặc mật khẩu quản trị không chính xác")

    token = create_hero_admin_token(HERO_ADMIN_USERNAME)
    return {
        "status": "success",
        "message": "Đăng nhập Hero Admin thành công",
        "token": token,
        "admin": {
            "username": HERO_ADMIN_USERNAME,
            "role": "hero_admin",
            "name": "Hero Master Admin"
        }
    }


@router.get("/verify")
async def api_admin_verify(admin: dict = Depends(require_hero_admin)):
    """Check admin token validity."""
    return {
        "status": "authenticated",
        "admin": {
            "username": admin.get("sub"),
            "role": admin.get("role"),
            "exp": admin.get("exp")
        }
    }


@router.get("/stats")
async def api_admin_stats(admin: dict = Depends(require_hero_admin)):
    """Get system overview metrics."""
    try:
        user_cnt = await execute_pg_query("SELECT COUNT(*) AS count FROM users")
        total_users = int(user_cnt[0]["count"]) if user_cnt else 0

        msg_cnt = await execute_pg_query("SELECT COUNT(*) AS count FROM messages")
        total_messages = int(msg_cnt[0]["count"]) if msg_cnt else 0

        return {
            "total_users": total_users,
            "online_users": len(active_connections),
            "total_messages": total_messages,
            "r2_connected": r2_client is not None
        }
    except Exception as e:
        return {
            "total_users": 0,
            "online_users": len(active_connections),
            "total_messages": 0,
            "r2_connected": r2_client is not None,
            "error": str(e)
        }


@router.get("/users")
async def api_admin_get_users(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    search: Optional[str] = Query(None),
    admin: dict = Depends(require_hero_admin)
):
    """
    Get paginated user list with search support.
    Default limit is 10 users per page.
    """
    offset = (page - 1) * limit
    search_q = (search or "").strip()

    if search_q:
        if search_q.isdigit():
            target_num = int(search_q)
            # Match by ID/user_id or text
            users = await execute_pg_query(
                """
                SELECT id, user_id, username, name, email, bio, status, avatar, google_id, last_seen, created_at, updated_at
                FROM users
                WHERE user_id = $1 OR id = $1 OR username ILIKE $2 OR email ILIKE $2 OR name ILIKE $2
                ORDER BY id DESC
                LIMIT $3 OFFSET $4
                """,
                target_num, f"%{search_q}%", limit, offset
            )
            count_res = await execute_pg_query(
                "SELECT COUNT(*) AS count FROM users WHERE user_id = $1 OR id = $1 OR username ILIKE $2 OR email ILIKE $2 OR name ILIKE $2",
                target_num, f"%{search_q}%"
            )
        else:
            users = await execute_pg_query(
                """
                SELECT id, user_id, username, name, email, bio, status, avatar, google_id, last_seen, created_at, updated_at
                FROM users
                WHERE username ILIKE $1 OR email ILIKE $1 OR name ILIKE $1
                ORDER BY id DESC
                LIMIT $2 OFFSET $3
                """,
                f"%{search_q}%", limit, offset
            )
            count_res = await execute_pg_query(
                "SELECT COUNT(*) AS count FROM users WHERE username ILIKE $1 OR email ILIKE $1 OR name ILIKE $1",
                f"%{search_q}%"
            )
    else:
        users = await execute_pg_query(
            """
            SELECT id, user_id, username, name, email, bio, status, avatar, google_id, last_seen, created_at, updated_at
            FROM users
            ORDER BY id DESC
            LIMIT $1 OFFSET $2
            """,
            limit, offset
        )
        count_res = await execute_pg_query("SELECT COUNT(*) AS count FROM users")

    total = int(count_res[0]["count"]) if count_res else len(users)

    # Format output & attach realtime status
    result_users = []
    for u in users:
        uid = u.get("user_id") or (10000 + u["id"])
        uname = (u.get("username") or "").strip()
        is_online = uname.lower() in active_connections if uname else False
        
        # Format created_at to ISO string if needed
        created_at_val = u.get("created_at")
        if hasattr(created_at_val, "isoformat"):
            created_at_str = created_at_val.isoformat()
        else:
            created_at_str = str(created_at_val or "")

        result_users.append({
            "id": uid,
            "db_id": u["id"],
            "user_id": uid,
            "username": uname,
            "name": u.get("name") or "",
            "email": u.get("email") or "",
            "bio": u.get("bio") or "",
            "status": "online" if is_online else (u.get("status") or "offline"),
            "is_online": is_online,
            "avatar": u.get("avatar") or "",
            "avatar_url": u.get("avatar") or "",
            "google_id": u.get("google_id") or "",
            "last_seen": u.get("last_seen") or "",
            "created_at": created_at_str
        })

    has_more = (offset + len(result_users)) < total

    return {
        "users": result_users,
        "total": total,
        "page": page,
        "limit": limit,
        "has_more": has_more
    }


@router.get("/users/{identifier}")
async def api_admin_get_single_user(identifier: str, admin: dict = Depends(require_hero_admin)):
    """Retrieve detailed information of a single user."""
    ident = identifier.strip()
    if ident.isdigit():
        rows = await execute_pg_query(
            "SELECT * FROM users WHERE user_id = $1 OR id = $1", int(ident)
        )
    elif "@" in ident:
        rows = await execute_pg_query(
            "SELECT * FROM users WHERE LOWER(email) = LOWER($1)", ident
        )
    else:
        rows = await execute_pg_query(
            "SELECT * FROM users WHERE LOWER(username) = LOWER($1)", ident
        )

    if not rows:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng này")

    u = rows[0]
    uid = u.get("user_id") or (10000 + u["id"])
    uname = u.get("username") or ""

    # Count items in various tables
    notes_cnt = await execute_pg_query("SELECT COUNT(*) AS count FROM user_sync_notes WHERE user_id = $1", uid)
    tasks_cnt = await execute_pg_query("SELECT COUNT(*) AS count FROM user_sync_tasks WHERE user_id = $1", uid)
    docs_cnt = await execute_pg_query("SELECT COUNT(*) AS count FROM user_sync_docs WHERE user_id = $1", uid)
    msgs_cnt = await execute_pg_query("SELECT COUNT(*) AS count FROM messages WHERE LOWER(sender_id) = LOWER($1)", uname)

    return {
        "user": {
            "id": uid,
            "db_id": u["id"],
            "user_id": uid,
            "username": uname,
            "name": u.get("name") or "",
            "email": u.get("email") or "",
            "bio": u.get("bio") or "",
            "status": "online" if uname.lower() in active_connections else (u.get("status") or "offline"),
            "avatar": u.get("avatar") or "",
            "google_id": u.get("google_id") or "",
            "created_at": str(u.get("created_at") or ""),
            "updated_at": str(u.get("updated_at") or "")
        },
        "stats": {
            "notes_count": int(notes_cnt[0]["count"]) if notes_cnt else 0,
            "tasks_count": int(tasks_cnt[0]["count"]) if tasks_cnt else 0,
            "docs_count": int(docs_cnt[0]["count"]) if docs_cnt else 0,
            "messages_sent": int(msgs_cnt[0]["count"]) if msgs_cnt else 0
        }
    }


@router.put("/users/{identifier}")
async def api_admin_update_user(
    identifier: str,
    req: AdminUserUpdateRequest,
    admin: dict = Depends(require_hero_admin)
):
    """Update a user's details (username, name, email, bio, status, avatar)."""
    ident = identifier.strip()
    if ident.isdigit():
        rows = await execute_pg_query(
            "SELECT id, user_id, username, email, name, bio, status, avatar FROM users WHERE user_id = $1 OR id = $1", int(ident)
        )
    elif "@" in ident:
        rows = await execute_pg_query(
            "SELECT id, user_id, username, email, name, bio, status, avatar FROM users WHERE LOWER(email) = LOWER($1)", ident
        )
    else:
        rows = await execute_pg_query(
            "SELECT id, user_id, username, email, name, bio, status, avatar FROM users WHERE LOWER(username) = LOWER($1)", ident
        )

    if not rows:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng này để chỉnh sửa")

    current = rows[0]
    db_id = current["id"]
    user_id = current.get("user_id") or (10000 + db_id)
    old_username = current.get("username") or ""
    old_email = current.get("email") or ""

    new_username = (req.username.strip().lower() if req.username is not None else old_username)
    new_name = req.name.strip() if req.name is not None else (current.get("name") or "")
    new_email = (req.email.strip().lower() if req.email is not None else old_email)
    new_bio = req.bio.strip() if req.bio is not None else (current.get("bio") or "")
    new_status = req.status.strip() if req.status is not None else (current.get("status") or "online")
    new_avatar = req.avatar.strip() if req.avatar is not None else (current.get("avatar") or "")

    # Check username uniqueness if changed
    if new_username and new_username != old_username:
        check = await execute_pg_query(
            "SELECT id FROM users WHERE LOWER(username) = LOWER($1) AND id != $2",
            new_username, db_id
        )
        if check:
            raise HTTPException(status_code=400, detail=f"Username @{new_username} đã có người sử dụng")

    # Check email uniqueness if changed
    if new_email and new_email != old_email:
        check = await execute_pg_query(
            "SELECT id FROM users WHERE LOWER(email) = LOWER($1) AND id != $2",
            new_email, db_id
        )
        if check:
            raise HTTPException(status_code=400, detail=f"Email {new_email} đã được liên kết tài khoản khác")

    # Perform update on users table
    await execute_pg_query(
        """
        UPDATE users
        SET username = $1, name = $2, email = $3, bio = $4, status = $5, avatar = $6, updated_at = CURRENT_TIMESTAMP
        WHERE id = $7
        """,
        new_username, new_name, new_email, new_bio, new_status, new_avatar, db_id
    )

    # If username changed, cascade update across related chat tables
    if new_username and old_username and new_username != old_username:
        print(f"[ADMIN] Cascading username change: @{old_username} -> @{new_username}")
        await execute_pg_query("UPDATE messages SET sender_id = $1 WHERE LOWER(sender_id) = LOWER($2)", new_username, old_username)
        await execute_pg_query("UPDATE messages SET recipient_id = $1 WHERE LOWER(recipient_id) = LOWER($2)", new_username, old_username)
        await execute_pg_query("UPDATE chat_push_subscriptions SET username = $1 WHERE LOWER(username) = LOWER($2)", new_username, old_username)
        await execute_pg_query("UPDATE chat_group_members SET username = $1 WHERE LOWER(username) = LOWER($2)", new_username, old_username)
        await execute_pg_query("UPDATE chat_friends SET username = $1 WHERE LOWER(username) = LOWER($2)", new_username, old_username)
        await execute_pg_query("UPDATE chat_friends SET friend_username = $1 WHERE LOWER(friend_username) = LOWER($2)", new_username, old_username)

    return {
        "status": "success",
        "message": f"Cập nhật thông tin cho người dùng @{new_username} thành công",
        "user": {
            "id": user_id,
            "db_id": db_id,
            "user_id": user_id,
            "username": new_username,
            "name": new_name,
            "email": new_email,
            "bio": new_bio,
            "status": new_status,
            "avatar": new_avatar
        }
    }


@router.post("/users/{identifier}/reset-password")
async def api_admin_reset_password(
    identifier: str,
    req: AdminResetPasswordRequest,
    admin: dict = Depends(require_hero_admin)
):
    """Admin resets / assigns a new password for a specific user."""
    ident = identifier.strip()
    if ident.isdigit():
        rows = await execute_pg_query(
            "SELECT id, user_id, username, email FROM users WHERE user_id = $1 OR id = $1", int(ident)
        )
    elif "@" in ident:
        rows = await execute_pg_query(
            "SELECT id, user_id, username, email FROM users WHERE LOWER(email) = LOWER($1)", ident
        )
    else:
        rows = await execute_pg_query(
            "SELECT id, user_id, username, email FROM users WHERE LOWER(username) = LOWER($1)", ident
        )

    if not rows:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng này trong hệ thống")

    user = rows[0]
    db_id = user["id"]
    user_id = user.get("user_id") or (10000 + db_id)
    username = (user.get("username") or "").strip()

    new_password = (req.new_password or "").strip()
    is_valid, err_msg = validate_password_strength(new_password)
    if not is_valid:
        raise HTTPException(status_code=400, detail=err_msg or "Mật khẩu phải từ 6 ký tự trở lên")

    pwd_hash = hash_password(new_password)

    await execute_pg_query(
        "UPDATE users SET password_hash = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2",
        pwd_hash, db_id
    )

    print(f"[ADMIN RESET PASSWORD] Admin @{admin.get('sub')} reset password for user @{username} (ID: {user_id})")

    return {
        "status": "success",
        "message": f"Cấp lại mật khẩu mới cho @{username} thành công!",
        "user_id": user_id,
        "username": username
    }


@router.delete("/users/{identifier}")
async def api_admin_delete_user(
    identifier: str,
    admin: dict = Depends(require_hero_admin)
):
    """
    100% COMPLETE & PERMANENT PURGE OF A USER:
    1. Removes all user files on Cloudflare R2 (avatar/, chat/, uploads/)
    2. Removes all local fallback disk files
    3. Purges all 8 Cloud Sync tables by user_id
    4. Purges all chat messages, friends, groups, push subscriptions
    5. Purges user row from users table
    6. Disconnects active WebSocket connections
    """
    ident = identifier.strip()
    if ident.isdigit():
        rows = await execute_pg_query(
            "SELECT id, user_id, username, email, avatar FROM users WHERE user_id = $1 OR id = $1", int(ident)
        )
    elif "@" in ident:
        rows = await execute_pg_query(
            "SELECT id, user_id, username, email, avatar FROM users WHERE LOWER(email) = LOWER($1)", ident
        )
    else:
        rows = await execute_pg_query(
            "SELECT id, user_id, username, email, avatar FROM users WHERE LOWER(username) = LOWER($1)", ident
        )

    if not rows:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng này trong hệ thống")

    target = rows[0]
    db_id = target["id"]
    user_id = target.get("user_id") or (10000 + db_id)
    username = (target.get("username") or "").strip()
    email = (target.get("email") or "").strip()
    avatar_url = target.get("avatar") or ""

    print(f"[ADMIN PURGE START] Purging user @{username} (ID: {user_id}, Email: {email})...")

    # 1. Purge R2 Storage & Local Storage Files
    storage_result = delete_user_r2_and_local_files(username=username, avatar_url=avatar_url)

    # 2. Purge 8 App Cloud Sync Tables
    sync_tables = [
        "user_sync_notes",
        "user_sync_tasks",
        "user_sync_task_projects",
        "user_sync_calendar_events",
        "user_sync_countday_events",
        "user_sync_mindmap_projects",
        "user_sync_table_projects",
        "user_sync_docs"
    ]
    for table in sync_tables:
        try:
            await execute_pg_query(f"DELETE FROM {table} WHERE user_id = $1", user_id)
        except Exception as e:
            print(f"[ADMIN PURGE] Error deleting from {table}: {e}")

    # 3. Purge Chat Messages, Friends, Group Membership, Push Subscriptions
    try:
        if username:
            await execute_pg_query(
                "DELETE FROM messages WHERE LOWER(sender_id) = LOWER($1) OR LOWER(recipient_id) = LOWER($1)",
                username
            )
            await execute_pg_query(
                "DELETE FROM chat_push_subscriptions WHERE LOWER(username) = LOWER($1)",
                username
            )
            await execute_pg_query(
                "DELETE FROM chat_group_members WHERE LOWER(username) = LOWER($1) OR LOWER(email) = LOWER($2)",
                username, email
            )

        # Friends deletion by user_id and db_id
        await execute_pg_query(
            "DELETE FROM chat_friends WHERE user_id = $1 OR friend_id = $1 OR user_id = $2 OR friend_id = $2",
            user_id, db_id
        )
    except Exception as e:
        print(f"[ADMIN PURGE] Error deleting chat/friends records: {e}")

    # 4. Purge Primary User Record
    await execute_pg_query(
        "DELETE FROM users WHERE id = $1 OR user_id = $2",
        db_id, user_id
    )

    # 5. Force Disconnect Active WebSocket Connections
    if username and username.lower() in active_connections:
        try:
            ws_list = list(active_connections[username.lower()])
            for ws in ws_list:
                try:
                    await ws.close(code=1000, reason="Account purged by Admin")
                except Exception:
                    pass
            active_connections.pop(username.lower(), None)
            print(f"[ADMIN PURGE] Terminated active WebSocket session for @{username}")
        except Exception as ws_err:
            print(f"[ADMIN PURGE] WebSocket termination note: {ws_err}")

    print(f"[ADMIN PURGE COMPLETE] Successfully wiped user @{username} (ID: {user_id})")

    return {
        "status": "success",
        "message": f"Đã xóa vĩnh viễn người dùng @{username} (ID: {user_id}) cùng toàn bộ dữ liệu Cloud Sync, tin nhắn, và file trên Cloudflare R2!",
        "deleted_user": {
            "user_id": user_id,
            "username": username,
            "email": email
        },
        "storage_cleaned": storage_result
    }
