import json
import uuid
import os
from fastapi import APIRouter, HTTPException, Depends, Request
from database.postgres import execute_pg_query
from auth.deps import get_auth_token, verify_google_token
from storage.r2 import process_and_upload_avatar, delete_r2_object, r2_client
from websocket.manager import active_connections, get_email_by_username, send_to_user_by_email
from config import LOCAL_AVATARS_DIR, R2_CDN_BASE, R2_BUCKET_NAME

router = APIRouter(prefix="/api", tags=["groups"])


async def perform_group_disband_cleanup(group_id: str, members_to_notify: list):
    """Delete all group data and notify members via WebSocket."""
    group_rows = await execute_pg_query("SELECT avatar FROM chat_groups WHERE id = $1", group_id)
    old_avatar = ""
    if group_rows:
        old_avatar = group_rows[0].get("avatar", "") or ""

    msg_rows = await execute_pg_query("SELECT content FROM messages WHERE recipient_id = $1", group_id)
    contents = [r["content"] for r in msg_rows]
    await _delete_message_attachments(contents)

    if old_avatar and old_avatar.startswith(f"{R2_CDN_BASE}/"):
        old_key = old_avatar.replace(f"{R2_CDN_BASE}/", "")
        delete_r2_object(old_key)

    if old_avatar and "/data/avatars/" in old_avatar:
        try:
            local_filename = old_avatar.split("/")[-1]
            local_path = os.path.join(LOCAL_AVATARS_DIR, local_filename)
            if os.path.exists(local_path):
                os.remove(local_path)
                print(f"Disband: Local group avatar {local_filename} deleted.")
        except Exception as e:
            print(f"Failed to delete local group avatar: {e}")

    await execute_pg_query("DELETE FROM messages WHERE recipient_id = $1", group_id)
    await execute_pg_query("DELETE FROM chat_group_members WHERE group_id = $1", group_id)
    await execute_pg_query("DELETE FROM chat_groups WHERE id = $1", group_id)

    broadcast_payload = json.dumps({"type": "group_disbanded", "group_id": group_id})
    for m in members_to_notify:
        if m in active_connections:
            for conn in list(active_connections[m]):
                try:
                    await conn.send_text(broadcast_payload)
                except Exception:
                    pass


async def _delete_message_attachments(contents: list):
    """Parse attachment URLs from message content and delete them from R2/local."""
    import re
    url_pattern = re.compile(r'\[Attachment: [^\]]+\] \((https?://[^\s\)]+|/[^\s\)]+)\)')
    LOCAL_UPLOADS = "./data/uploads"
    for content in contents:
        if not content:
            continue
        for match in url_pattern.finditer(content):
            file_url = match.group(1)
            if "uploads/" in file_url:
                key = "uploads/" + file_url.split("uploads/")[-1]
                if r2_client and R2_BUCKET_NAME:
                    try:
                        r2_client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
                        print(f"Deleted R2 attachment: {key}")
                    except Exception as re2:
                        print(f"Failed to delete R2 attachment {key}: {re2}")
            if "/data/uploads/" in file_url or "data/uploads" in file_url:
                local_filename = file_url.split("/")[-1]
                local_path = os.path.join(LOCAL_UPLOADS, local_filename)
                if os.path.exists(local_path):
                    try:
                        os.remove(local_path)
                        print(f"Deleted local fallback attachment: {local_path}")
                    except Exception as le:
                        print(f"Failed to delete local attachment {local_path}: {le}")


@router.post("/groups")
async def api_create_group(payload: dict, token: str = Depends(get_auth_token), request: Request = None):
    name = payload.get("name", "").strip()
    avatar_src = payload.get("avatar", "").strip()
    members = payload.get("members", [])
    creator = payload.get("creator", "").strip().lower()

    if not name or not creator:
        raise HTTPException(status_code=400, detail="Group name and creator are required")

    email = await get_email_by_username(creator)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    group_id = f"group_{uuid.uuid4().hex}"
    avatar_url = ""
    if avatar_src:
        base_url = str(request.base_url).rstrip("/") if request else ""
        avatar_url = process_and_upload_avatar(group_id, avatar_src, base_url)

    await execute_pg_query(
        "INSERT INTO chat_groups (id, name, avatar, created_by, created_by_email) VALUES ($1, $2, $3, $4, $5)",
        group_id, name, avatar_url, creator, email
    )

    unique_members = list(set(m.strip().lower() for m in members if m.strip()))
    if creator not in unique_members:
        unique_members.append(creator)

    added_member_emails = []
    for m in unique_members:
        user_row = await execute_pg_query("SELECT username, email FROM users WHERE LOWER(username) = LOWER($1)", m)
        if user_row:
            m_email = user_row[0]["email"].strip().lower()
            m_username = user_row[0]["username"].strip().lower()
            role = "creator" if m == creator else "member"
            await execute_pg_query(
                "INSERT INTO chat_group_members (group_id, email, username, role) VALUES ($1, $2, $3, $4) ON CONFLICT (group_id, email) DO NOTHING",
                group_id, m_email, m_username, role
            )
            added_member_emails.append(m_email)

    broadcast_payload = json.dumps({
        "type": "group_created",
        "group": {"id": group_id, "name": name, "avatar": avatar_url, "created_by": creator, "members": unique_members}
    })
    for m_email in added_member_emails:
        await send_to_user_by_email(m_email, broadcast_payload)

    return {"success": True, "group": {"id": group_id, "name": name, "avatar": avatar_url, "created_by": creator, "members": unique_members}}


@router.get("/group/{group_id}")
async def api_get_group(group_id: str):
    group_id = group_id.strip().lower()
    group_rows = await execute_pg_query(
        "SELECT id, name, avatar, created_by, created_by_email, created_at FROM chat_groups WHERE id = $1", group_id
    )
    if not group_rows:
        raise HTTPException(status_code=404, detail="Group not found")

    group = group_rows[0]
    member_rows = await execute_pg_query("SELECT username, email, role FROM chat_group_members WHERE group_id = $1", group_id)
    members = [r["username"] or r["email"] for r in member_rows]
    member_emails = [r["email"] for r in member_rows if r.get("email")]

    return {
        "id": group["id"],
        "name": group["name"],
        "avatar": group["avatar"],
        "created_by": group["created_by"],
        "created_by_email": group.get("created_by_email", ""),
        "created_at": group["created_at"],
        "members": members,
        "member_emails": member_emails
    }


@router.get("/groups/{username}")
async def api_get_user_groups(username: str, token: str = Depends(get_auth_token)):
    username = username.strip().lower()
    email = await get_email_by_username(username)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    rows = await execute_pg_query(
        """
        SELECT g.id, g.name, g.avatar, g.created_by, g.created_at
        FROM chat_groups g
        JOIN chat_group_members m ON g.id = m.group_id
        WHERE m.email = $1 OR m.username = $2
        """,
        email, username
    )

    groups = []
    for r in rows:
        member_rows = await execute_pg_query("SELECT username, email, role FROM chat_group_members WHERE group_id = $1", r["id"])
        members = [m["username"] or m["email"] for m in member_rows]
        member_emails = [m["email"] for m in member_rows if m.get("email")]
        groups.append({
            "id": r["id"], "name": r["name"], "avatar": r["avatar"],
            "created_by": r["created_by"], "created_at": r["created_at"],
            "members": members, "member_emails": member_emails
        })
    return groups


@router.post("/groups/{group_id}/leave")
async def api_leave_group(group_id: str, payload: dict, token: str = Depends(get_auth_token)):
    group_id = group_id.strip().lower()
    username = payload.get("username", "").strip().lower()

    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    email = await get_email_by_username(username)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    member_check = await execute_pg_query(
        "SELECT role FROM chat_group_members WHERE group_id = $1 AND (LOWER(email) = LOWER($2) OR LOWER(username) = LOWER($3))",
        group_id, email, username
    )
    if not member_check:
        raise HTTPException(status_code=400, detail="User is not a member of the group")

    await execute_pg_query(
        "DELETE FROM chat_group_members WHERE group_id = $1 AND (LOWER(email) = LOWER($2) OR LOWER(username) = LOWER($3))",
        group_id, email, username
    )

    member_rows = await execute_pg_query("SELECT email, username FROM chat_group_members WHERE group_id = $1", group_id)

    if not member_rows:
        await perform_group_disband_cleanup(group_id, [username])
        return {"success": True, "message": "Successfully left the group and group disbanded"}

    remaining_emails = [r["email"] for r in member_rows if r.get("email")]
    remaining_usernames = [r["username"] or "" for r in member_rows]

    broadcast_payload = json.dumps({
        "type": "group_member_left",
        "group_id": group_id,
        "user_email": email,
        "username": username,
        "members": remaining_usernames
    })

    for m_email in remaining_emails:
        await send_to_user_by_email(m_email, broadcast_payload)
    await send_to_user_by_email(email, broadcast_payload)

    return {"success": True, "message": "Successfully left the group"}


@router.post("/groups/{group_id}/add")
async def api_add_group_members(group_id: str, payload: dict, token: str = Depends(get_auth_token)):
    group_id = group_id.strip().lower()
    requester = payload.get("requester", "").strip().lower()
    new_members = payload.get("members", [])

    if not requester or not new_members:
        raise HTTPException(status_code=400, detail="Requester and new members are required")

    requester_email = await get_email_by_username(requester)
    if not requester_email or not await verify_google_token(token, requester_email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    requester_check = await execute_pg_query(
        "SELECT role FROM chat_group_members WHERE group_id = $1 AND (LOWER(email) = LOWER($2) OR LOWER(username) = LOWER($3))",
        group_id, requester_email, requester
    )
    if not requester_check:
        raise HTTPException(status_code=403, detail="Only group members can add new members")

    group_rows = await execute_pg_query("SELECT name, avatar, created_by FROM chat_groups WHERE id = $1", group_id)
    if not group_rows:
        raise HTTPException(status_code=404, detail="Group not found")
    group_info = group_rows[0]

    added_members_usernames = []
    added_members_emails = []
    for m in new_members:
        m = m.strip().lower()
        if not m:
            continue
        user_row = await execute_pg_query("SELECT username, email FROM users WHERE LOWER(username) = LOWER($1)", m)
        if user_row:
            m_email = user_row[0]["email"].strip().lower()
            m_username = user_row[0]["username"].strip().lower()
            await execute_pg_query(
                "INSERT INTO chat_group_members (group_id, email, username, role) VALUES ($1, $2, $3, 'member') ON CONFLICT (group_id, email) DO NOTHING",
                group_id, m_email, m_username
            )
            added_members_usernames.append(m_username)
            added_members_emails.append(m_email)

    member_rows = await execute_pg_query("SELECT username, email FROM chat_group_members WHERE group_id = $1", group_id)
    all_members_usernames = [r["username"] or r["email"] for r in member_rows]
    all_members_emails = [r["email"] for r in member_rows if r.get("email")]

    broadcast_payload = json.dumps({
        "type": "group_members_added",
        "group_id": group_id,
        "group": {"id": group_id, "name": group_info["name"], "avatar": group_info["avatar"], "created_by": group_info["created_by"]},
        "added_members": added_members_usernames,
        "added_member_emails": added_members_emails,
        "members": all_members_usernames
    })

    for m_email in all_members_emails:
        await send_to_user_by_email(m_email, broadcast_payload)

    return {"success": True, "added_members": added_members_usernames, "members": all_members_usernames}


@router.post("/groups/{group_id}/kick")
async def api_kick_group_member(group_id: str, payload: dict, token: str = Depends(get_auth_token)):
    group_id = group_id.strip().lower()
    requester = payload.get("requester", "").strip().lower()
    target_user = payload.get("target_user", "").strip().lower()

    if not requester or not target_user:
        raise HTTPException(status_code=400, detail="Requester and target_user are required")

    requester_email = await get_email_by_username(requester)
    if not requester_email or not await verify_google_token(token, requester_email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    group_rows = await execute_pg_query("SELECT created_by, created_by_email FROM chat_groups WHERE id = $1", group_id)
    if not group_rows:
        raise HTTPException(status_code=404, detail="Group not found")

    group_creator_email = group_rows[0].get("created_by_email", "") or ""
    group_creator = group_rows[0]["created_by"]
    if group_creator_email and group_creator_email != requester_email:
        raise HTTPException(status_code=403, detail="Only the group creator can kick members")
    elif not group_creator_email and group_creator != requester:
        raise HTTPException(status_code=403, detail="Only the group creator can kick members")

    if requester == target_user:
        raise HTTPException(status_code=400, detail="Creator cannot kick themselves")

    target_email = await get_email_by_username(target_user)

    if target_email:
        await execute_pg_query(
            "DELETE FROM chat_group_members WHERE group_id = $1 AND (LOWER(email) = LOWER($2) OR LOWER(username) = LOWER($3))",
            group_id, target_email, target_user
        )
    else:
        await execute_pg_query(
            "DELETE FROM chat_group_members WHERE group_id = $1 AND LOWER(username) = LOWER($2)",
            group_id, target_user
        )

    member_rows = await execute_pg_query("SELECT email, username FROM chat_group_members WHERE group_id = $1", group_id)

    if not member_rows:
        await perform_group_disband_cleanup(group_id, [requester, target_user])
        return {"success": True, "message": f"Successfully kicked {target_user} and group disbanded"}

    remaining_emails = [r["email"] for r in member_rows if r.get("email")]
    remaining_usernames = [r["username"] or "" for r in member_rows]

    broadcast_payload = json.dumps({
        "type": "group_member_kicked",
        "group_id": group_id,
        "target_user": target_user,
        "target_user_email": target_email or "",
        "members": remaining_usernames
    })

    for m_email in remaining_emails:
        await send_to_user_by_email(m_email, broadcast_payload)
    if target_email:
        await send_to_user_by_email(target_email, broadcast_payload)
    elif target_user in active_connections:
        for conn in list(active_connections[target_user]):
            try:
                await conn.send_text(broadcast_payload)
            except Exception:
                pass

    return {"success": True, "message": f"Successfully kicked {target_user}", "members": remaining_usernames}


@router.post("/groups/{group_id}/avatar")
async def api_update_group_avatar(group_id: str, payload: dict, token: str = Depends(get_auth_token), request: Request = None):
    group_id = group_id.strip().lower()
    avatar_src = payload.get("avatar", "").strip()
    requester = payload.get("requester", "").strip().lower()

    if not requester or not avatar_src:
        raise HTTPException(status_code=400, detail="Requester and avatar are required")

    email = await get_email_by_username(requester)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    group_rows = await execute_pg_query("SELECT avatar, created_by FROM chat_groups WHERE id = $1", group_id)
    if not group_rows:
        raise HTTPException(status_code=404, detail="Group not found")

    group_info = group_rows[0]
    if group_info["created_by"].lower() != requester:
        raise HTTPException(status_code=403, detail="Only the group creator can update the avatar")

    old_avatar = group_info.get("avatar", "")
    base_url = str(request.base_url).rstrip("/") if request else ""
    avatar_url = process_and_upload_avatar(group_id, avatar_src, base_url)

    await execute_pg_query("UPDATE chat_groups SET avatar = $1 WHERE id = $2", avatar_url, group_id)

    if old_avatar and old_avatar.startswith(f"{R2_CDN_BASE}/"):
        delete_r2_object(old_avatar.replace(f"{R2_CDN_BASE}/", ""))
    if old_avatar and "/data/avatars/" in old_avatar:
        try:
            local_filename = old_avatar.split("/")[-1]
            local_path = os.path.join(LOCAL_AVATARS_DIR, local_filename)
            if os.path.exists(local_path):
                os.remove(local_path)
        except Exception as e:
            print(f"Failed to delete old local group avatar: {e}")

    member_rows = await execute_pg_query("SELECT username FROM chat_group_members WHERE group_id = $1", group_id)
    members = [r["username"] for r in member_rows]

    broadcast_payload = json.dumps({"type": "group_avatar_updated", "group_id": group_id, "avatar": avatar_url, "members": members})
    for m in members:
        if m in active_connections:
            for conn in list(active_connections[m]):
                try:
                    await conn.send_text(broadcast_payload)
                except Exception:
                    pass

    return {"success": True, "avatar": avatar_url}


@router.post("/groups/{group_id}/rename")
async def api_rename_group(group_id: str, payload: dict, token: str = Depends(get_auth_token)):
    group_id = group_id.strip().lower()
    new_name = payload.get("name", "").strip()
    requester = payload.get("requester", "").strip().lower()

    if not requester or not new_name:
        raise HTTPException(status_code=400, detail="Requester and new name are required")

    email = await get_email_by_username(requester)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    group_rows = await execute_pg_query("SELECT created_by FROM chat_groups WHERE id = $1", group_id)
    if not group_rows:
        raise HTTPException(status_code=404, detail="Group not found")
    if group_rows[0]["created_by"].lower() != requester:
        raise HTTPException(status_code=403, detail="Only the group creator can rename the group")

    await execute_pg_query("UPDATE chat_groups SET name = $1 WHERE id = $2", new_name, group_id)

    member_rows = await execute_pg_query("SELECT username FROM chat_group_members WHERE group_id = $1", group_id)
    members = [r["username"] for r in member_rows if r.get("username")]

    broadcast_payload = json.dumps({"type": "group_name_updated", "group_id": group_id, "name": new_name})
    for m in members:
        if m in active_connections:
            for conn in list(active_connections[m]):
                try:
                    await conn.send_text(broadcast_payload)
                except Exception:
                    pass

    return {"success": True, "name": new_name}


@router.post("/groups/{group_id}/disband")
async def api_disband_group(group_id: str, payload: dict, token: str = Depends(get_auth_token)):
    group_id = group_id.strip().lower()
    requester = payload.get("requester", "").strip().lower()

    if not requester:
        raise HTTPException(status_code=400, detail="Requester is required")

    email = await get_email_by_username(requester)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    group_rows = await execute_pg_query("SELECT created_by FROM chat_groups WHERE id = $1", group_id)
    if not group_rows:
        raise HTTPException(status_code=404, detail="Group not found")
    if group_rows[0]["created_by"].lower() != requester:
        raise HTTPException(status_code=403, detail="Only the group creator can disband the group")

    member_rows = await execute_pg_query("SELECT username FROM chat_group_members WHERE group_id = $1", group_id)
    members = [r["username"] for r in member_rows if r.get("username")]

    await perform_group_disband_cleanup(group_id, members)
    return {"success": True, "message": "Group disbanded successfully"}
