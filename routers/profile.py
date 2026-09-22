import re
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, Request, Response, Header
from database.postgres import execute_pg_query
from auth.deps import get_auth_token, verify_google_token, create_spac2_token, decode_spac2_token, create_text2_token, decode_text2_token
from models.schemas import UserProfile
from services.user_service import save_profile, get_profile, get_email_by_username
from websocket.manager import active_connections
from config import DEFAULT_AVATAR_URL
from storage.r2 import (
    get_user_files_storage,
    delete_user_folder_files,
    format_bytes,
)

router = APIRouter(prefix="/api", tags=["profile"])


async def get_authenticated_user(
    token: Optional[str] = Depends(get_auth_token),
    x_user_id: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_user_name: Optional[str] = Header(None)
) -> dict:
    """Resolve the currently authenticated user from Spac2 unified session token or headers."""
    if token:
        payload = decode_spac2_token(token)
        if payload:
            lookup = payload.get("email") or payload.get("username") or str(payload.get("user_id") or "")
            if lookup:
                p = await get_profile(lookup)
                if p:
                    return p

    for ident in [x_user_email, x_user_id, x_user_name]:
        if ident and ident.strip():
            p = await get_profile(ident.strip())
            if p:
                return p

    if token and len(token) < 200:
        p = await get_profile(token)
        if p:
            return p

    raise HTTPException(status_code=401, detail="Unauthorized: No active profile session found")


@router.get("/profile")
async def api_get_my_profile(
    user: dict = Depends(get_authenticated_user)
):
    """Retrieve current authenticated user profile across Spac2 Ecosystem."""
    return user


@router.get("/profile/storage")
async def api_get_user_storage(
    user: dict = Depends(get_authenticated_user)
):
    """
    On-demand calculation of user cloud storage across all sync apps and media.
    Only executed when explicitly requested by user.
    """
    user_id = user["user_id"]
    username = (user.get("username") or "").strip().lower()
    avatar_url = user.get("avatar") or ""

    files_storage = get_user_files_storage(username, avatar_url)
    breakdown_files = files_storage.get("breakdown", {})

    apps_list = []

    # 1. Notes
    notes_bytes = 0
    notes_cnt = 0
    try:
        res = await execute_pg_query(
            "SELECT COUNT(*) AS count, COALESCE(SUM(LENGTH(COALESCE(title, '')) + LENGTH(COALESCE(content, '')) + 40), 0) AS bytes FROM user_sync_notes WHERE user_id = $1",
            user_id
        )
        if res:
            notes_cnt = int(res[0].get("count") or 0)
            notes_bytes = int(res[0].get("bytes") or 0)
    except Exception:
        pass
    apps_list.append({
        "app_key": "notes",
        "name": "Notes",
        "icon": "ph-note",
        "count": notes_cnt,
        "count_label": f"{notes_cnt} notes",
        "bytes": notes_bytes,
        "formatted": format_bytes(notes_bytes)
    })

    # 2. Tasks
    tasks_bytes = 0
    tasks_cnt = 0
    try:
        res1 = await execute_pg_query(
            "SELECT COUNT(*) AS count, COALESCE(SUM(LENGTH(COALESCE(title, '')) + LENGTH(COALESCE(note, '')) + 40), 0) AS bytes FROM user_sync_tasks WHERE user_id = $1",
            user_id
        )
        res2 = await execute_pg_query(
            "SELECT COUNT(*) AS count, COALESCE(SUM(LENGTH(COALESCE(name, '')) + 30), 0) AS bytes FROM user_sync_task_projects WHERE user_id = $1",
            user_id
        )
        t_cnt = (int(res1[0]["count"]) if res1 else 0) + (int(res2[0]["count"]) if res2 else 0)
        t_bytes = (int(res1[0]["bytes"]) if res1 else 0) + (int(res2[0]["bytes"]) if res2 else 0)
        tasks_cnt = t_cnt
        tasks_bytes = t_bytes
    except Exception:
        pass
    apps_list.append({
        "app_key": "tasks",
        "name": "Tasks & Projects",
        "icon": "ph-check-square",
        "count": tasks_cnt,
        "count_label": f"{tasks_cnt} items",
        "bytes": tasks_bytes,
        "formatted": format_bytes(tasks_bytes)
    })

    # 3. Docs
    docs_bytes = 0
    docs_cnt = 0
    try:
        res = await execute_pg_query(
            "SELECT COUNT(*) AS count, COALESCE(SUM(LENGTH(COALESCE(title, '')) + LENGTH(COALESCE(body, '')) + LENGTH(COALESCE(preview_text, '')) + 100), 0) AS bytes FROM user_sync_docs WHERE user_id = $1",
            user_id
        )
        if res:
            docs_cnt = int(res[0].get("count") or 0)
            docs_bytes = int(res[0].get("bytes") or 0)
    except Exception:
        pass
    apps_list.append({
        "app_key": "docs",
        "name": "Documents",
        "icon": "ph-file-text",
        "count": docs_cnt,
        "count_label": f"{docs_cnt} docs",
        "bytes": docs_bytes,
        "formatted": format_bytes(docs_bytes)
    })

    # 4. Mindmap
    mm_bytes = 0
    mm_cnt = 0
    try:
        res = await execute_pg_query(
            "SELECT COUNT(*) AS count, COALESCE(SUM(LENGTH(COALESCE(name, '')) + LENGTH(COALESCE(data::text, '')) + 50), 0) AS bytes FROM user_sync_mindmap_projects WHERE user_id = $1",
            user_id
        )
        if res:
            mm_cnt = int(res[0].get("count") or 0)
            mm_bytes = int(res[0].get("bytes") or 0)
    except Exception:
        pass
    apps_list.append({
        "app_key": "mindmap",
        "name": "Mindmaps",
        "icon": "ph-tree-structure",
        "count": mm_cnt,
        "count_label": f"{mm_cnt} maps",
        "bytes": mm_bytes,
        "formatted": format_bytes(mm_bytes)
    })

    # 5. Table
    tbl_bytes = 0
    tbl_cnt = 0
    try:
        res = await execute_pg_query(
            "SELECT COUNT(*) AS count, COALESCE(SUM(LENGTH(COALESCE(name, '')) + LENGTH(COALESCE(data::text, '')) + 50), 0) AS bytes FROM user_sync_table_projects WHERE user_id = $1",
            user_id
        )
        if res:
            tbl_cnt = int(res[0].get("count") or 0)
            tbl_bytes = int(res[0].get("bytes") or 0)
    except Exception:
        pass
    apps_list.append({
        "app_key": "table",
        "name": "Spreadsheets",
        "icon": "ph-table",
        "count": tbl_cnt,
        "count_label": f"{tbl_cnt} sheets",
        "bytes": tbl_bytes,
        "formatted": format_bytes(tbl_bytes)
    })

    # 6. Calendar
    cal_bytes = 0
    cal_cnt = 0
    try:
        res = await execute_pg_query(
            "SELECT COUNT(*) AS count, COALESCE(SUM(LENGTH(COALESCE(title, '')) + LENGTH(COALESCE(description, '')) + 40), 0) AS bytes FROM user_sync_calendar_events WHERE user_id = $1",
            user_id
        )
        if res:
            cal_cnt = int(res[0].get("count") or 0)
            cal_bytes = int(res[0].get("bytes") or 0)
    except Exception:
        pass
    apps_list.append({
        "app_key": "calendar",
        "name": "Calendar",
        "icon": "ph-calendar-blank",
        "count": cal_cnt,
        "count_label": f"{cal_cnt} events",
        "bytes": cal_bytes,
        "formatted": format_bytes(cal_bytes)
    })

    # 7. Countday
    cd_bytes = 0
    cd_cnt = 0
    try:
        res = await execute_pg_query(
            "SELECT COUNT(*) AS count, COALESCE(SUM(LENGTH(COALESCE(title, '')) + 30), 0) AS bytes FROM user_sync_countday_events WHERE user_id = $1",
            user_id
        )
        if res:
            cd_cnt = int(res[0].get("count") or 0)
            cd_bytes = int(res[0].get("bytes") or 0)
    except Exception:
        pass
    apps_list.append({
        "app_key": "countday",
        "name": "Countdowns",
        "icon": "ph-hourglass-medium",
        "count": cd_cnt,
        "count_label": f"{cd_cnt} events",
        "bytes": cd_bytes,
        "formatted": format_bytes(cd_bytes)
    })

    # 8. Chat & Media (Messages + R2 Chat Files)
    chat_file_bytes = int(breakdown_files.get("chat_bytes") or 0)
    chat_msg_bytes = 0
    chat_msg_cnt = 0
    try:
        if username:
            res = await execute_pg_query(
                "SELECT COUNT(*) AS count, COALESCE(SUM(LENGTH(COALESCE(content, '')) + 50), 0) AS bytes FROM messages WHERE LOWER(sender_id) = LOWER($1) OR LOWER(recipient_id) = LOWER($1)",
                username
            )
            if res:
                chat_msg_cnt = int(res[0].get("count") or 0)
                chat_msg_bytes = int(res[0].get("bytes") or 0)
    except Exception:
        pass
    total_chat_bytes = chat_msg_bytes + chat_file_bytes
    apps_list.append({
        "app_key": "chat",
        "name": "Chat Messages & Media",
        "icon": "ph-chats",
        "count": chat_msg_cnt,
        "count_label": f"{chat_msg_cnt} messages & media",
        "bytes": total_chat_bytes,
        "formatted": format_bytes(total_chat_bytes)
    })

    # 9. Uploads
    uploads_bytes = int(breakdown_files.get("uploads_bytes") or 0)
    apps_list.append({
        "app_key": "uploads",
        "name": "Uploaded Files",
        "icon": "ph-upload-simple",
        "count": 0,
        "count_label": "Cloud files",
        "bytes": uploads_bytes,
        "formatted": format_bytes(uploads_bytes)
    })

    total_storage_bytes = sum(item["bytes"] for item in apps_list)

    return {
        "status": "success",
        "total_bytes": total_storage_bytes,
        "total_formatted": format_bytes(total_storage_bytes),
        "apps": apps_list
    }


@router.delete("/profile/storage/{app_key}")
async def api_delete_user_app_data(
    app_key: str,
    user: dict = Depends(get_authenticated_user)
):
    """
    Delete all database rows and Cloudflare R2 / local files for a specific application.
    """
    user_id = user["user_id"]
    username = (user.get("username") or "").strip().lower()
    app = (app_key or "").strip().lower()

    valid_apps = ["notes", "tasks", "docs", "mindmap", "table", "calendar", "countday", "chat", "uploads"]
    if app not in valid_apps:
        raise HTTPException(status_code=400, detail="Invalid application identifier")

    deleted_info = {}

    try:
        if app == "notes":
            await execute_pg_query("DELETE FROM user_sync_notes WHERE user_id = $1", user_id)
            deleted_info["app"] = "Notes"
        elif app == "tasks":
            await execute_pg_query("DELETE FROM user_sync_tasks WHERE user_id = $1", user_id)
            await execute_pg_query("DELETE FROM user_sync_task_projects WHERE user_id = $1", user_id)
            deleted_info["app"] = "Tasks & Projects"
        elif app == "docs":
            await execute_pg_query("DELETE FROM user_sync_docs WHERE user_id = $1", user_id)
            deleted_info["app"] = "Documents"
        elif app == "mindmap":
            await execute_pg_query("DELETE FROM user_sync_mindmap_projects WHERE user_id = $1", user_id)
            deleted_info["app"] = "Mindmaps"
        elif app == "table":
            await execute_pg_query("DELETE FROM user_sync_table_projects WHERE user_id = $1", user_id)
            deleted_info["app"] = "Spreadsheets"
        elif app == "calendar":
            await execute_pg_query("DELETE FROM user_sync_calendar_events WHERE user_id = $1", user_id)
            deleted_info["app"] = "Calendar"
        elif app == "countday":
            await execute_pg_query("DELETE FROM user_sync_countday_events WHERE user_id = $1", user_id)
            deleted_info["app"] = "Countdowns"
        elif app == "chat":
            if username:
                await execute_pg_query(
                    "DELETE FROM messages WHERE LOWER(sender_id) = LOWER($1) OR LOWER(recipient_id) = LOWER($1)",
                    username
                )
            r2_res = delete_user_folder_files(username, "chat")
            deleted_info["app"] = "Chat Messages & Media"
            deleted_info["files_cleaned"] = r2_res
        elif app == "uploads":
            r2_res = delete_user_folder_files(username, "uploads")
            deleted_info["app"] = "Uploaded Files"
            deleted_info["files_cleaned"] = r2_res

        return {
            "status": "success",
            "message": f"Successfully deleted all data for {deleted_info.get('app', app)} from Database and R2!",
            "details": deleted_info
        }
    except Exception as e:
        print(f"[APP DATA PURGE ERROR] Failed to delete {app} for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete {app} data: {str(e)}")


@router.post("/profile")
async def api_save_profile(
    profile: UserProfile, 
    request: Request, 
    token: Optional[str] = Depends(get_auth_token),
    x_user_email: Optional[str] = Header(None)
):
    # 1. Resolve email from JWT token or header if missing
    auth_email = ""
    token_username = ""
    if token:
        payload = decode_spac2_token(token)
        if payload:
            auth_email = (payload.get("email") or "").strip().lower()
            token_username = (payload.get("username") or "").strip().lower()

    if not auth_email and x_user_email:
        auth_email = x_user_email.strip().lower()

    if not auth_email and profile.email:
        auth_email = profile.email.strip().lower()

    if not auth_email:
        raise HTTPException(status_code=401, detail="Unauthorized: No active authentication session found")

    if not profile.email:
        profile.email = auth_email

    # Map avatar_url to avatar if needed
    if profile.avatar_url and not profile.avatar:
        profile.avatar = profile.avatar_url
    elif profile.avatar and not profile.avatar_url:
        profile.avatar_url = profile.avatar

    username = profile.username.strip().lower()
    if not username:
        username = token_username or re.sub(r"[^a-z0-9_]", "", profile.email.split("@")[0].lower()) or "user"
        profile.username = username

    if not await verify_google_token(token, profile.email):
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or expired authentication session")

    try:
        base_url = str(request.base_url).rstrip("/")
        updated_profile, is_new = await save_profile(profile.dict(), base_url)
        
        # Generate official Spac2 Unified Ecosystem Token (JWT)
        user_uid = updated_profile.get("user_id") or updated_profile.get("id") or 0
        spac2_token = create_spac2_token(
            user_id=user_uid,
            username=updated_profile.get("username", username),
            email=updated_profile.get("email", profile.email)
        )

        return {
            "status": "success",
            "message": f"Profile for @{username} synced.",
            "token": spac2_token,
            "profile": updated_profile,
            "is_new": is_new
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Error saving profile: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save profile: {str(e)}")


@router.get("/profile/{username}")
async def api_get_profile(username: str):

    username = username.strip().lower()
    profile = await get_profile(username)
    if profile:
        return profile
    raise HTTPException(status_code=404, detail="Profile not found")


@router.get("/search_user")
async def api_search_user(q: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    q = q.strip().lower()
    if not q:
        return []
    try:
        if q.isdigit():
            users = await execute_pg_query(
                "SELECT id, user_id, username, name, bio, email, status, avatar, last_seen FROM users WHERE user_id = $1 OR id = $1 OR username ILIKE $2 OR email ILIKE $2 OR name ILIKE $2 LIMIT 10",
                int(q), f"%{q}%"
            )
        else:
            users = await execute_pg_query(
                "SELECT id, user_id, username, name, bio, email, status, avatar, last_seen FROM users WHERE username ILIKE $1 OR email ILIKE $1 OR name ILIKE $1 LIMIT 10",
                f"%{q}%"
            )
        for u in users:
            uid = u.get("user_id") or (10000 + u["id"])
            u["id"] = uid
            u["user_id"] = uid
            uname = u.get("username", "").strip().lower()
            u["status"] = "online" if uname in active_connections else "offline"
            avatar_val = u.get("avatar") or DEFAULT_AVATAR_URL
            u["avatar"] = avatar_val
            u["avatar_url"] = avatar_val
        return users
    except Exception as e:
        print(f"Error searching users: {e}")
        return []
