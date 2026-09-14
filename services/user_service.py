import re
import time
from datetime import datetime, timezone
from fastapi import HTTPException

from database.postgres import execute_pg_query
from storage.r2 import process_and_upload_avatar, delete_r2_object
from websocket.manager import active_connections
from config import LOCAL_AVATARS_DIR, R2_CDN_BASE
import os


async def get_email_by_username(username: str) -> str:
    from websocket.manager import get_email_by_username as mgr_get_email
    return await mgr_get_email(username)


async def save_profile(profile_data: dict, base_url: str = ""):
    """Upsert a user profile into the central users table. Handles login sync, username uniqueness, and avatar processing."""
    is_login = profile_data.get("is_login", False)
    email = profile_data.get("email").strip().lower()

    existing_users = await execute_pg_query(
        "SELECT id, user_id, username, name, bio, status, avatar, google_id FROM users WHERE email = $1", email
    )

    if existing_users and is_login:
        merged = await _merge_login_profile(existing_users, profile_data, email, base_url)
        return merged, False

    is_new = not bool(existing_users)
    updated = await _upsert_profile(existing_users, profile_data, email, base_url)
    return updated, is_new


async def _merge_login_profile(existing_users, profile_data, email, base_url):
    """Fill in missing profile fields during a Google login sync."""
    u = existing_users[0]
    db_id = u["id"]
    user_id = u.get("user_id") or (10000 + db_id)

    db_username = (u["username"] or "").strip()
    db_name = (u["name"] or "").strip()
    db_bio = (u["bio"] or "").strip()
    db_status = (u["status"] or "online").strip()
    db_avatar = (u["avatar"] or "").strip()
    db_google_id = (u["google_id"] or "").strip()

    g_username = profile_data.get("username", "").strip().lower()
    g_name = profile_data.get("name", "").strip()
    g_bio = profile_data.get("bio", "").strip()
    g_status = profile_data.get("status", "online").strip()
    g_avatar = profile_data.get("avatar", "").strip()
    g_google_id = profile_data.get("google_id", "").strip()

    updated = False

    if not u.get("user_id"):
        await execute_pg_query("UPDATE users SET user_id = $1 WHERE id = $2", user_id, db_id)

    if not db_username:
        candidate_username = g_username or re.sub(r"[^a-z0-9]", "", email.split("@")[0].lower()) or f"user{int(time.time())}"
        username_check = await execute_pg_query("SELECT id FROM users WHERE username = $1", candidate_username)
        if username_check:
            base_username = candidate_username
            counter = 1
            while True:
                candidate = f"{base_username}{counter}"
                check = await execute_pg_query("SELECT id FROM users WHERE username = $1", candidate)
                if not check:
                    candidate_username = candidate
                    break
                counter += 1
        db_username = candidate_username
        updated = True

    if not db_name and g_name:
        db_name = g_name
        updated = True
    if not db_bio and g_bio:
        db_bio = g_bio
        updated = True
    if not db_status and g_status:
        db_status = g_status
        updated = True
    if not db_avatar and g_avatar:
        if g_avatar.startswith("data:image/") or (g_avatar.startswith("http") and not g_avatar.startswith(f"{R2_CDN_BASE}/")):
            db_avatar = process_and_upload_avatar(db_username, g_avatar, base_url)
        else:
            db_avatar = g_avatar
        updated = True
    if not db_google_id and g_google_id:
        db_google_id = g_google_id
        updated = True

    if updated:
        await execute_pg_query(
            "UPDATE users SET username = $1, name = $2, bio = $3, status = $4, avatar = $5, google_id = $6, user_id = $7, updated_at = CURRENT_TIMESTAMP WHERE email = $8",
            db_username, db_name, db_bio, db_status, db_avatar, db_google_id, user_id, email
        )
        print(f"Profile for @{db_username} (ID: {user_id}) merged and updated in PostgreSQL users table during login.")

    return {
        "id": user_id,
        "user_id": user_id,
        "username": db_username,
        "name": db_name,
        "bio": db_bio,
        "email": email,
        "status": db_status,
        "avatar": db_avatar,
        "google_id": db_google_id
    }


async def _upsert_profile(existing_users, profile_data, email, base_url):
    """Create or update a user profile (non-login sync path)."""
    username = profile_data.get("username").strip().lower()
    name = profile_data.get("name", "").strip()
    bio = profile_data.get("bio", "").strip()
    status = profile_data.get("status", "online").strip()
    google_id = profile_data.get("google_id", "").strip()
    avatar = profile_data.get("avatar", "").strip()

    old_username = None
    old_avatar = None
    user_id = None

    if existing_users:
        old_username = existing_users[0]["username"]
        old_avatar = existing_users[0]["avatar"]
        user_id = existing_users[0].get("user_id") or (10000 + existing_users[0]["id"])

    # Username uniqueness check
    is_login = profile_data.get("is_login", False)
    if old_username and old_username != username:
        username_check = await execute_pg_query("SELECT id FROM users WHERE username = $1", username)
        if username_check:
            raise HTTPException(status_code=400, detail="Username is already taken by another user")
    elif not old_username:
        username_check = await execute_pg_query("SELECT id FROM users WHERE username = $1", username)
        if username_check:
            if is_login:
                base_username = username
                counter = 1
                while True:
                    candidate = f"{base_username}{counter}"
                    check = await execute_pg_query("SELECT id FROM users WHERE username = $1", candidate)
                    if not check:
                        username = candidate
                        break
                    counter += 1
            else:
                raise HTTPException(status_code=400, detail="Username is already taken by another user")

    # Process avatar and upload to Cloudflare R2
    avatar_url = avatar
    if avatar != old_avatar:
        if avatar.startswith("data:image/") or (avatar.startswith("http") and not avatar.startswith(f"{R2_CDN_BASE}/")):
            avatar_url = process_and_upload_avatar(username, avatar, base_url)

        # Clean up old avatar from R2
        if old_avatar and old_avatar.startswith(f"{R2_CDN_BASE}/"):
            old_key = old_avatar.replace(f"{R2_CDN_BASE}/", "")
            delete_r2_object(old_key)

        # Clean up old local avatar
        if old_avatar and "/data/avatars/" in old_avatar:
            try:
                local_filename = old_avatar.split("/")[-1]
                local_path = os.path.join(LOCAL_AVATARS_DIR, local_filename)
                if os.path.exists(local_path):
                    os.remove(local_path)
                    print(f"Old local avatar {local_filename} deleted.")
            except Exception as e:
                print(f"Failed to delete old local avatar: {e}")

    if existing_users:
        db_id = existing_users[0]["id"]
        # Update central users table
        await execute_pg_query(
            "UPDATE users SET username = $1, name = $2, bio = $3, status = $4, avatar = $5, google_id = $6, user_id = $7, updated_at = CURRENT_TIMESTAMP WHERE email = $8",
            username, name, bio, status, avatar_url, google_id, user_id, email
        )
        print(f"Profile for @{username} (ID: {user_id}) updated in PostgreSQL users table.")

        if old_username and old_username != username:
            print(f"Username changed from @{old_username} to @{username}. Cascading updates across chat tables...")
            await execute_pg_query("UPDATE messages SET sender_id = $1 WHERE LOWER(sender_id) = LOWER($2)", username, old_username)
            await execute_pg_query("UPDATE messages SET recipient_id = $1 WHERE LOWER(recipient_id) = LOWER($2)", username, old_username)
            await execute_pg_query("UPDATE chat_push_subscriptions SET username = $1 WHERE LOWER(username) = LOWER($2)", username, old_username)
            await execute_pg_query("UPDATE chat_group_members SET username = $1 WHERE LOWER(username) = LOWER($2)", username, old_username)
            await execute_pg_query("UPDATE chat_friends SET username = $1 WHERE LOWER(username) = LOWER($2)", username, old_username)
            await execute_pg_query("UPDATE chat_friends SET friend_username = $1 WHERE LOWER(friend_username) = LOWER($2)", username, old_username)
    else:
        next_uid_res = await execute_pg_query("SELECT nextval('users_user_id_seq') AS next_uid")
        user_id = next_uid_res[0]["next_uid"] if next_uid_res else 10001
        
        # Insert into central users table
        await execute_pg_query(
            "INSERT INTO users (user_id, username, name, bio, email, status, avatar, google_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (email) DO UPDATE SET username = EXCLUDED.username, avatar = EXCLUDED.avatar",
            user_id, username, name, bio, email, status, avatar_url, google_id
        )
        print(f"Profile for @{username} (ID: {user_id}) inserted into PostgreSQL users table.")

    return {
        "id": user_id,
        "user_id": user_id,
        "username": username,
        "name": name,
        "bio": bio,
        "email": email,
        "status": status,
        "avatar": avatar_url,
        "google_id": google_id
    }


async def get_profile(identifier: str):
    """Retrieve a public user profile by username, email, or user_id from the central users table."""
    if not identifier:
        return None
    ident = str(identifier).strip().lower()
    
    if ident.isdigit():
        users = await execute_pg_query(
            "SELECT id, user_id, username, name, bio, email, status, avatar, google_id, last_seen FROM users WHERE user_id = $1 OR id = $1",
            int(ident)
        )
    elif "@" in ident:
        users = await execute_pg_query(
            "SELECT id, user_id, username, name, bio, email, status, avatar, google_id, last_seen FROM users WHERE LOWER(email) = $1",
            ident
        )
    else:
        users = await execute_pg_query(
            "SELECT id, user_id, username, name, bio, email, status, avatar, google_id, last_seen FROM users WHERE LOWER(username) = $1",
            ident
        )

    if users:
        u = users[0]
        uid = u.get("user_id") or (10000 + u["id"])
        return {
            "id": uid,
            "user_id": uid,
            "name": u["name"] or "",
            "username": u["username"] or "",
            "bio": u["bio"] or "",
            "email": u["email"] or "",
            "status": "online" if u["username"] and u["username"].strip().lower() in active_connections else "offline",
            "avatar": u["avatar"] or "",
            "google_id": u["google_id"] or "",
            "last_seen": u["last_seen"] or ""
        }
    return None


# Backward-compatible aliases
save_profile_to_d1 = save_profile
get_profile_from_d1 = get_profile

