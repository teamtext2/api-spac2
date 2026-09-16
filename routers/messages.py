import json
import uuid
import time
import os
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends, Query
from database.postgres import execute_pg_query
from auth.deps import get_auth_token, verify_google_token
from models.schemas import MarkDeliveredRequest, MarkReadRequest, DeleteMessagesRequest, DeleteConversationRequest
from websocket.manager import get_email_by_username, get_username_by_email
from storage.r2 import r2_client, R2_BUCKET_NAME

router = APIRouter(prefix="/api", tags=["messages"])


def _is_valid_uuid(val: str) -> bool:
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


async def delete_message_attachments(contents: list):
    """Parse attachment URLs from message content and delete them from R2/local."""
    import re
    url_pattern = re.compile(r'(?:📎\s*)?\[Attachment:\s*[^\]]+\]\s*\((https?://[^\s\)]+|/[^\s\)]+)\)', re.IGNORECASE)
    from config import LOCAL_UPLOADS_DIR, LOCAL_CHAT_DIR
    for content in contents:
        if not content:
            continue
        for match in url_pattern.finditer(content):
            file_url = match.group(1)
            for folder in ["chat/", "uploads/", "avatar/"]:
                if folder in file_url:
                    key = folder + file_url.split(folder)[-1]
                    if r2_client and R2_BUCKET_NAME:
                        try:
                            r2_client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
                            print(f"Deleted R2 attachment: {key}")
                        except Exception as e:
                            print(f"Failed to delete R2 attachment {key}: {e}")
                    
                    target_dir = LOCAL_CHAT_DIR if folder == "chat/" else LOCAL_UPLOADS_DIR
                    local_filename = file_url.split("/")[-1]
                    local_path = os.path.join(target_dir, local_filename)
                    if os.path.exists(local_path):
                        try:
                            os.remove(local_path)
                            print(f"Deleted local fallback attachment: {local_path}")
                        except Exception as e:
                            print(f"Failed to delete local attachment {local_path}: {e}")


@router.get("/messages/undelivered/{username}")
async def api_get_undelivered_messages(username: str, token: str = Depends(get_auth_token)):
    username = username.strip().lower()

    email = await get_email_by_username(username)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        messages = await execute_pg_query(
            "SELECT id, sender_id AS sender, recipient_id AS recipient, content AS text, created_at, read_at, reactions, reply_to, forwarded_from FROM messages WHERE recipient_id = $1 AND delivered = FALSE",
            username
        )
        if messages:
            message_ids = [msg["id"] for msg in messages]
            await execute_pg_query(
                "UPDATE messages SET delivered = TRUE WHERE recipient_id = $1 AND id = ANY($2::uuid[])",
                username, message_ids
            )
            print(f"Marked {len(messages)} offline messages as delivered (TRUE) in Postgres for @{username}")

            for msg in messages:
                if isinstance(msg.get("id"), uuid.UUID):
                    msg["id"] = str(msg["id"])
                dt = msg.get("created_at")
                msg["timestamp"] = int(dt.timestamp() * 1000) if dt else int(time.time() * 1000)
                if "created_at" in msg:
                    del msg["created_at"]
                read_dt = msg.get("read_at")
                msg["read_at"] = int(read_dt.timestamp() * 1000) if read_dt else None
                rx = msg.get("reactions")
                if rx is None:
                    msg["reactions"] = {}
                elif isinstance(rx, str):
                    try:
                        msg["reactions"] = json.loads(rx)
                    except Exception:
                        msg["reactions"] = {}
                msg["replyTo"] = msg.get("reply_to")
                if "reply_to" in msg:
                    del msg["reply_to"]
                msg["forwardedFrom"] = msg.get("forwarded_from")
                if "forwarded_from" in msg:
                    del msg["forwarded_from"]
                msg["forwarded"] = True if msg.get("forwardedFrom") else False
        return messages
    except Exception as e:
        print(f"Error getting undelivered messages from Postgres: {e}")
        return []


@router.get("/conversations/{username}")
async def api_get_conversations(username: str, token: str = Depends(get_auth_token)):
    username = username.strip().lower()
    email = await get_email_by_username(username)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        user_identifiers = list(set(filter(None, [username.lower(), email.lower() if email else ""])))

        # 1. Get user groups
        group_rows = await execute_pg_query(
            "SELECT group_id FROM chat_group_members WHERE email = $1 OR username = $2",
            email, username
        )
        user_groups = [r["group_id"].strip().lower() for r in group_rows] if group_rows else []

        # 2. Query direct messages
        direct_msgs = await execute_pg_query(
            """
            SELECT DISTINCT ON (peer)
                   id,
                   CASE 
                       WHEN LOWER(sender_id) = ANY($1::varchar[]) THEN LOWER(recipient_id)
                       ELSE LOWER(sender_id)
                   END AS peer,
                   sender_id AS sender,
                   recipient_id AS recipient,
                   content AS text,
                   created_at,
                   read_at,
                   delivered,
                   update_id
            FROM messages
            WHERE (LOWER(sender_id) = ANY($1::varchar[]) OR LOWER(recipient_id) = ANY($1::varchar[]))
              AND NOT LOWER(recipient_id) LIKE 'group_%'
            ORDER BY peer, created_at DESC
            """,
            user_identifiers
        )

        # 3. Query latest group messages
        group_msgs = []
        if user_groups:
            group_msgs = await execute_pg_query(
                """
                SELECT DISTINCT ON (LOWER(recipient_id))
                       id,
                       LOWER(recipient_id) AS peer,
                       sender_id AS sender,
                       recipient_id AS recipient,
                       content AS text,
                       created_at,
                       read_at,
                       delivered,
                       update_id
                FROM messages
                WHERE LOWER(recipient_id) = ANY($1::varchar[])
                ORDER BY LOWER(recipient_id), created_at DESC
                """,
                user_groups
            )

        # 4. Friends list
        user_res = await execute_pg_query("SELECT id FROM users WHERE LOWER(username) = $1", username)
        friend_users = []
        if user_res:
            uid = user_res[0]["id"]
            f_rows = await execute_pg_query(
                "SELECT user_id, friend_id FROM chat_friends WHERE user_id = $1 OR friend_id = $1",
                uid
            )
            f_ids = [r["friend_id"] if r["user_id"] == uid else r["user_id"] for r in f_rows]
            if f_ids:
                f_details = await execute_pg_query(
                    "SELECT username, email FROM users WHERE id = ANY($1::integer[])",
                    f_ids
                )
                friend_users = [u["username"].strip().lower() for u in f_details if u.get("username")]

        # 5. Compute unread counts
        unread_rows = await execute_pg_query(
            """
            SELECT LOWER(sender_id) AS peer, COUNT(*) AS count
            FROM messages
            WHERE LOWER(recipient_id) = ANY($1::varchar[]) AND read_at IS NULL
            GROUP BY LOWER(sender_id)
            """,
            user_identifiers
        )
        unread_map = {r["peer"]: r["count"] for r in unread_rows} if unread_rows else {}

        # 6. Gather all unique peers
        all_peers = set()
        peer_latest_msg = {}
        for m in (direct_msgs or []) + (group_msgs or []):
            peer = m["peer"]
            all_peers.add(peer)
            peer_latest_msg[peer] = m

        for g in user_groups:
            all_peers.add(g)
        for f in friend_users:
            all_peers.add(f)

        # 7. Metadata
        group_peers = [p for p in all_peers if p.startswith("group_")]
        user_peers = [p for p in all_peers if not p.startswith("group_")]

        group_meta = {}
        if group_peers:
            g_rows = await execute_pg_query(
                "SELECT id, name, avatar, created_by, bio FROM chat_groups WHERE id = ANY($1::varchar[])",
                group_peers
            )
            for r in g_rows:
                group_meta[r["id"].strip().lower()] = r

        user_meta = {}
        if user_peers:
            u_rows = await execute_pg_query(
                "SELECT username, name, bio, email, status, avatar, last_seen FROM users WHERE LOWER(username) = ANY($1::varchar[]) OR LOWER(email) = ANY($1::varchar[])",
                user_peers
            )
            for r in u_rows:
                uname = r["username"].strip().lower() if r.get("username") else ""
                uemail = r["email"].strip().lower() if r.get("email") else ""
                if uname:
                    user_meta[uname] = r
                if uemail:
                    user_meta[uemail] = r

        # 8. Build final list
        from websocket.manager import active_connections
        conversations = []
        for peer in all_peers:
            is_group = peer.startswith("group_")
            is_saved = not is_group and (peer == username or peer == email)

            lmsg = peer_latest_msg.get(peer)
            ts = 0
            formatted_lmsg = None
            if lmsg:
                dt = lmsg["created_at"]
                ts = int(dt.timestamp() * 1000) if dt else int(time.time() * 1000)
                formatted_lmsg = {
                    "id": str(lmsg["id"]) if isinstance(lmsg["id"], uuid.UUID) else lmsg["id"],
                    "sender": lmsg["sender"],
                    "recipient": lmsg["recipient"],
                    "text": lmsg["text"],
                    "timestamp": ts,
                    "read_at": int(lmsg["read_at"].timestamp() * 1000) if lmsg.get("read_at") else None,
                    "delivered": lmsg.get("delivered", False),
                    "update_id": lmsg.get("update_id", 0)
                }

            if is_group:
                ginfo = group_meta.get(peer, {})
                conversations.append({
                    "id": peer,
                    "username": peer,
                    "email": f"{peer}@group.text2chat",
                    "name": ginfo.get("name") or f"Group {peer[6:12]}",
                    "avatar": ginfo.get("avatar") or "https://spac2.com/favicon.ico",
                    "status": "online",
                    "last_seen": None,
                    "bio": ginfo.get("bio") or (f"Group created by @{ginfo.get('created_by')}" if ginfo.get("created_by") else "Group chat"),
                    "isGroup": True,
                    "isSavedMessages": False,
                    "lastMessage": formatted_lmsg,
                    "timestamp": ts,
                    "unreadCount": 0
                })
            elif is_saved:
                conversations.append({
                    "id": username,
                    "username": username,
                    "email": email or f"{username}@gmail.com",
                    "name": "Saved Messages",
                    "avatar": "https://spac2.com/chat/apple-touch-icon.png",
                    "status": "online",
                    "last_seen": None,
                    "bio": "Your personal cloud storage & notes",
                    "isGroup": False,
                    "isSavedMessages": True,
                    "lastMessage": formatted_lmsg,
                    "timestamp": ts,
                    "unreadCount": 0
                })
            else:
                uinfo = user_meta.get(peer, {})
                peer_uname = uinfo.get("username") or peer
                peer_email = uinfo.get("email") or f"{peer}@gmail.com"
                is_online = peer_uname in active_connections
                conversations.append({
                    "id": peer_uname,
                    "username": peer_uname,
                    "email": peer_email,
                    "name": uinfo.get("name") or peer_uname.upper(),
                    "avatar": uinfo.get("avatar") or "https://spac2.com/favicon.ico",
                    "status": "online" if is_online else (uinfo.get("status") or "offline"),
                    "last_seen": uinfo.get("last_seen"),
                    "bio": uinfo.get("bio") or "Hey there! I am using Spac2 Chat.",
                    "isGroup": False,
                    "isSavedMessages": False,
                    "lastMessage": formatted_lmsg,
                    "timestamp": ts,
                    "unreadCount": unread_map.get(peer_uname, 0)
                })

        conversations.sort(key=lambda c: (1 if c.get("isSavedMessages") else 0, c.get("timestamp", 0)), reverse=True)
        return conversations
    except Exception as e:
        print(f"Error fetching conversations for @{username}: {e}")
        return []


@router.get("/messages/sync/{username}")
async def api_sync_messages(username: str, since: float = 0.0, token: str = Depends(get_auth_token)):
    username = username.strip().lower()

    email = await get_email_by_username(username)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        user_identifiers = list(set(filter(None, [username.lower(), email.lower() if email else ""])))
        group_rows = await execute_pg_query(
            "SELECT group_id FROM chat_group_members WHERE email = $1 OR username = $2",
            email, username
        )
        user_groups_clean = [r["group_id"].strip().lower() for r in group_rows] if group_rows else []
        is_sequence = (since <= 1000000000.0)

        if is_sequence:
            messages = await execute_pg_query(
                """
                SELECT id, sender_id AS sender, recipient_id AS recipient, content AS text, 
                       created_at, read_at, reactions, delivered, update_id, reply_to, forwarded_from
                FROM messages
                WHERE (LOWER(sender_id) = ANY($1::varchar[]) OR LOWER(recipient_id) = ANY($1::varchar[]) OR LOWER(recipient_id) = ANY($3::varchar[])) 
                  AND (COALESCE(update_id, 0) > $2 OR (update_id IS NULL AND $2 = 0))
                ORDER BY COALESCE(update_id, 0) ASC, created_at ASC
                """,
                user_identifiers, int(since), user_groups_clean
            )
        else:
            if since > 1e11:
                since = since / 1000.0
            since_dt = datetime.fromtimestamp(since, tz=timezone.utc)
            messages = await execute_pg_query(
                """
                SELECT id, sender_id AS sender, recipient_id AS recipient, content AS text, 
                       created_at, read_at, reactions, delivered, update_id, reply_to, forwarded_from
                FROM messages
                WHERE (LOWER(sender_id) = ANY($1::varchar[]) OR LOWER(recipient_id) = ANY($1::varchar[]) OR LOWER(recipient_id) = ANY($3::varchar[])) 
                  AND created_at > $2
                ORDER BY COALESCE(update_id, 0) ASC, created_at ASC
                """,
                user_identifiers, since_dt, user_groups_clean
            )

        unique_senders = list(set(msg["sender"] for msg in messages)) if messages else []
        sender_avatars = {}
        if unique_senders:
            user_rows = await execute_pg_query(
                "SELECT username, avatar FROM users WHERE username = ANY($1::varchar[])",
                unique_senders
            )
            for r in user_rows:
                sender_avatars[r["username"].strip().lower()] = r["avatar"]

        group_recipients = list(set(
            msg["recipient"].strip().lower() for msg in messages
            if msg["recipient"] and msg["recipient"].startswith("group_")
        ))
        group_details = {}
        if group_recipients:
            group_rows = await execute_pg_query(
                "SELECT id, name, avatar FROM chat_groups WHERE id = ANY($1::varchar[])",
                group_recipients
            )
            for r in group_rows:
                group_details[r["id"].strip().lower()] = {"name": r["name"], "avatar": r["avatar"]}

        undelivered_ids = [msg["id"] for msg in messages if msg["recipient"] == username and not msg["delivered"]]
        if undelivered_ids:
            await execute_pg_query(
                "UPDATE messages SET delivered = TRUE WHERE id = ANY($1::uuid[])",
                undelivered_ids
            )
            print(f"[SYNC] Marked {len(undelivered_ids)} messages as delivered for @{username}")

        formatted_messages = []
        for msg in messages:
            mid = str(msg["id"]) if isinstance(msg["id"], uuid.UUID) else msg["id"]
            dt = msg["created_at"]
            if isinstance(dt, str):
                try:
                    dt = datetime.fromisoformat(dt)
                except Exception:
                    dt = datetime.now(timezone.utc)
            ts = int(dt.timestamp() * 1000) if dt else int(time.time() * 1000)

            read_dt = msg["read_at"]
            if isinstance(read_dt, str):
                try:
                    read_dt = datetime.fromisoformat(read_dt)
                except Exception:
                    read_dt = None
            read_ts = int(read_dt.timestamp() * 1000) if read_dt else None

            rx = msg["reactions"]
            reactions_dict = {}
            if rx:
                if isinstance(rx, str):
                    try:
                        reactions_dict = json.loads(rx)
                    except Exception:
                        pass
                elif isinstance(rx, dict):
                    reactions_dict = rx

            sender_id = msg["sender"].strip().lower()
            recipient_id = msg["recipient"].strip().lower() if msg["recipient"] else ""
            group_info = group_details.get(recipient_id)

            msg_payload = {
                "id": mid,
                "sender": msg["sender"],
                "recipient": msg["recipient"],
                "text": msg["text"],
                "timestamp": ts,
                "read_at": read_ts,
                "reactions": reactions_dict,
                "delivered": True if msg["recipient"] == username else msg["delivered"],
                "senderAvatar": sender_avatars.get(sender_id, "https://spac2.com/favicon.ico"),
                "update_id": msg.get("update_id", 0),
                "replyTo": msg.get("reply_to"),
                "forwardedFrom": msg.get("forwarded_from"),
                "forwarded": True if msg.get("forwarded_from") else False
            }
            if group_info:
                msg_payload["groupName"] = group_info["name"]
                msg_payload["groupAvatar"] = group_info["avatar"]
            formatted_messages.append(msg_payload)

        return formatted_messages
    except Exception as e:
        print(f"Error syncing messages for @{username}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/messages/history/{username}")
async def api_get_messages_history(
    username: str,
    partner: str = Query(...),
    before: float = Query(None),
    limit: int = Query(20),
    token: str = Depends(get_auth_token)
):
    username = username.strip().lower()
    partner = partner.strip().lower()

    email = await get_email_by_username(username)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        before_dt = None
        if before is not None:
            if before > 1e11:
                before = before / 1000.0
            before_dt = datetime.fromtimestamp(before, tz=timezone.utc)

        is_group = partner.startswith("group_")

        if is_group:
            member_check = await execute_pg_query(
                "SELECT role FROM chat_group_members WHERE LOWER(group_id) = $1 AND (LOWER(email) = $2 OR LOWER(username) = $3)",
                partner, email.lower(), username
            )
            if not member_check:
                raise HTTPException(status_code=403, detail="You are not a member of this group")

            query = """
                SELECT id, sender_id AS sender, recipient_id AS recipient, content AS text, 
                       created_at, read_at, reactions, delivered, reply_to, forwarded_from
                FROM messages
                WHERE LOWER(recipient_id) = $1
            """
            params = [partner]
            if before_dt:
                query += " AND created_at < $2"
                params.append(before_dt)
            query += f" ORDER BY created_at DESC LIMIT ${len(params) + 1}"
            params.append(limit)
        else:
            p_email = await get_email_by_username(partner) if "@" not in partner else partner
            p_uname = await get_username_by_email(partner) if "@" in partner else partner
            if not p_uname and "@" not in partner:
                p_uname = partner

            u_identifiers = list(set(filter(None, [username.lower(), email.lower() if email else ""])))
            p_identifiers = list(set(filter(None, [
                partner.lower(),
                p_email.lower() if p_email else "",
                p_uname.lower() if p_uname else ""
            ])))

            query = """
                SELECT id, sender_id AS sender, recipient_id AS recipient, content AS text, 
                       created_at, read_at, reactions, delivered, reply_to, forwarded_from
                FROM messages
                WHERE ((LOWER(sender_id) = ANY($1::varchar[]) AND LOWER(recipient_id) = ANY($2::varchar[])) 
                    OR (LOWER(sender_id) = ANY($2::varchar[]) AND LOWER(recipient_id) = ANY($1::varchar[])))
            """
            params = [u_identifiers, p_identifiers]
            if before_dt:
                query += " AND created_at < $3"
                params.append(before_dt)
            query += f" ORDER BY created_at DESC LIMIT ${len(params) + 1}"
            params.append(limit)

        messages = await execute_pg_query(query, *params)
        messages = sorted(messages, key=lambda m: m["created_at"])

        unique_senders = list(set(msg["sender"] for msg in messages)) if messages else []
        sender_avatars = {}
        if unique_senders:
            user_rows = await execute_pg_query(
                "SELECT username, avatar FROM users WHERE username = ANY($1::varchar[])",
                unique_senders
            )
            for r in user_rows:
                sender_avatars[r["username"].strip().lower()] = r["avatar"]

        formatted_messages = []
        for msg in messages:
            mid = str(msg["id"]) if isinstance(msg["id"], uuid.UUID) else msg["id"]
            dt = msg["created_at"]
            ts = int(dt.timestamp() * 1000) if dt else int(time.time() * 1000)
            read_dt = msg["read_at"]
            read_ts = int(read_dt.timestamp() * 1000) if read_dt else None
            rx = msg["reactions"]
            reactions_dict = {}
            if rx:
                if isinstance(rx, str):
                    try:
                        reactions_dict = json.loads(rx)
                    except Exception:
                        pass
                elif isinstance(rx, dict):
                    reactions_dict = rx

            formatted_messages.append({
                "id": mid,
                "sender": msg["sender"],
                "recipient": msg["recipient"],
                "text": msg["text"],
                "timestamp": ts,
                "read_at": read_ts,
                "reactions": reactions_dict,
                "delivered": True if msg["recipient"] == username else msg["delivered"],
                "senderAvatar": sender_avatars.get(msg["sender"].strip().lower(), "https://spac2.com/favicon.ico"),
                "replyTo": msg.get("reply_to"),
                "forwardedFrom": msg.get("forwarded_from"),
                "forwarded": True if msg.get("forwarded_from") else False
            })

        return formatted_messages
    except Exception as e:
        print(f"Error fetching message history for @{username} with {partner}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/messages/mark_delivered")
async def api_mark_delivered(req: MarkDeliveredRequest, token: str = Depends(get_auth_token)):
    recipient = req.recipient.strip().lower()

    email = await get_email_by_username(recipient)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not req.message_ids:
        return {"status": "success"}
    try:
        uuids = [uuid.UUID(mid) for mid in req.message_ids if _is_valid_uuid(mid)]
        if uuids:
            await execute_pg_query(
                "UPDATE messages SET delivered = TRUE WHERE recipient_id = $1 AND id = ANY($2::uuid[])",
                recipient, uuids
            )
        return {"status": "success"}
    except Exception as e:
        print(f"Error marking messages as delivered in Postgres: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/messages/mark_read")
async def api_mark_read(req: MarkReadRequest, token: str = Depends(get_auth_token)):
    username = req.username.strip().lower()

    email = await get_email_by_username(username)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not req.message_ids:
        return {"status": "success"}
    try:
        uuids = [uuid.UUID(mid) for mid in req.message_ids if _is_valid_uuid(mid)]
        if uuids:
            await execute_pg_query(
                "UPDATE messages SET read_at = CURRENT_TIMESTAMP WHERE recipient_id = $1 AND id = ANY($2::uuid[])",
                username, uuids
            )
        return {"status": "success"}
    except Exception as e:
        print(f"Error marking messages as read in Postgres: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/messages/delete")
async def api_delete_messages(req: DeleteMessagesRequest, token: str = Depends(get_auth_token)):
    username = req.username.strip().lower()

    email = await get_email_by_username(username)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not req.message_ids:
        return {"status": "success"}
    try:
        uuids = [uuid.UUID(mid) for mid in req.message_ids if _is_valid_uuid(mid)]
        if uuids:
            rows = await execute_pg_query(
                "SELECT id, sender_id, recipient_id, content FROM messages WHERE (LOWER(sender_id) = $1 OR LOWER(recipient_id) = $1) AND id = ANY($2::uuid[])",
                username, uuids
            )
            if rows:
                await delete_message_attachments([row.get("content") or "" for row in rows])

                # Gather affected peers
                affected_peers = set()
                del_ids = [str(r["id"]) for r in rows]
                for r in rows:
                    s = r.get("sender_id", "").strip().lower()
                    rec = r.get("recipient_id", "").strip().lower()
                    if s: affected_peers.add(s)
                    if rec: affected_peers.add(rec)

                await execute_pg_query(
                    "DELETE FROM messages WHERE (LOWER(sender_id) = $1 OR LOWER(recipient_id) = $1) AND id = ANY($2::uuid[])",
                    username, uuids
                )

                # Broadcast deletion to all connections of affected participants
                from websocket.manager import active_connections
                del_payload = json.dumps({
                    "type": "messages_deleted",
                    "sender": username,
                    "message_ids": del_ids
                })
                for peer in affected_peers:
                    if peer in active_connections:
                        for ws in list(active_connections[peer]):
                            try:
                                await ws.send_text(del_payload)
                            except Exception:
                                pass
        return {"status": "success"}
    except Exception as e:
        print(f"Error deleting messages in Postgres: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/conversation/delete")
async def api_delete_conversation(req: DeleteConversationRequest, token: str = Depends(get_auth_token)):
    username = req.username.strip().lower()
    friend_username = req.friend_username.strip().lower()

    email = await get_email_by_username(username)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        rows = await execute_pg_query(
            "SELECT content FROM messages WHERE (LOWER(sender_id) = $1 AND LOWER(recipient_id) = $2) OR (LOWER(sender_id) = $2 AND LOWER(recipient_id) = $1)",
            username, friend_username
        )
        if rows:
            await delete_message_attachments([row.get("content") or "" for row in rows])

        await execute_pg_query(
            "DELETE FROM messages WHERE (LOWER(sender_id) = $1 AND LOWER(recipient_id) = $2) OR (LOWER(sender_id) = $2 AND LOWER(recipient_id) = $1)",
            username, friend_username
        )

        user_res = await execute_pg_query("SELECT id FROM users WHERE LOWER(username) = $1", username)
        friend_res = await execute_pg_query("SELECT id FROM users WHERE LOWER(username) = $1", friend_username)
        if user_res and friend_res:
            uid = user_res[0]["id"]
            fid = friend_res[0]["id"]
            id1, id2 = min(uid, fid), max(uid, fid)
            await execute_pg_query(
                "DELETE FROM chat_friends WHERE user_id = $1 AND friend_id = $2",
                id1, id2
            )

        # Broadcast conversation deletion to active connections
        from websocket.manager import active_connections
        del_conv_payload = json.dumps({
            "type": "conversation_deleted",
            "sender": username,
            "friend_username": friend_username
        })
        for peer in [username, friend_username]:
            if peer in active_connections:
                for ws in list(active_connections[peer]):
                    try:
                        await ws.send_text(del_conv_payload)
                    except Exception:
                        pass

        return {"status": "success"}
    except Exception as e:
        print(f"Error deleting conversation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/link-preview")
async def api_link_preview(url: str):
    url = url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid URL protocol")
    
    import re
    import httpx
    import html as html_lib
    from urllib.parse import urlparse
    
    try:
        async with httpx.AsyncClient(timeout=3.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                # Return basic fallback instead of failing
                parsed_url = urlparse(url)
                return {
                    "title": parsed_url.netloc,
                    "description": "Click to open link",
                    "image": "",
                    "url": url,
                    "domain": parsed_url.netloc
                }
                
            html_text = resp.text
            
            title_match = re.search(r'<title>(.*?)</title>', html_text, re.IGNORECASE | re.DOTALL)
            title = title_match.group(1).strip() if title_match else ""
            
            def find_meta(name_or_prop):
                pattern = rf'<meta\s+[^>]*?(?:property|name)=["\']{re.escape(name_or_prop)}["\'][^>]*?content=["\'](.*?)["\']'
                m = re.search(pattern, html_text, re.IGNORECASE | re.DOTALL)
                if not m:
                    pattern = rf'<meta\s+[^>]*?content=["\'](.*?)["\'][^>]*?(?:property|name)=["\']{re.escape(name_or_prop)}["\']'
                    m = re.search(pattern, html_text, re.IGNORECASE | re.DOTALL)
                return m.group(1).strip() if m else None

            og_title = find_meta("og:title")
            og_desc = find_meta("og:description") or find_meta("description")
            og_image = find_meta("og:image")
            
            parsed_url = urlparse(url)
            domain = parsed_url.netloc
            
            final_title = og_title or title or domain
            final_desc = og_desc or ""
            final_image = og_image or ""
            
            if final_image and not (final_image.startswith("http://") or final_image.startswith("https://")):
                if final_image.startswith("/"):
                    final_image = f"{parsed_url.scheme}://{domain}{final_image}"
                else:
                    final_image = f"{parsed_url.scheme}://{domain}/{final_image}"
            
            return {
                "title": html_lib.unescape(final_title),
                "description": html_lib.unescape(final_desc),
                "image": final_image,
                "url": url,
                "domain": domain
            }
    except Exception as e:
        print(f"Error fetching link preview: {e}")
        try:
            domain = urlparse(url).netloc
        except Exception:
            domain = "link"
        return {
            "title": domain,
            "description": "Click to open link",
            "image": "",
            "url": url,
            "domain": domain
        }
