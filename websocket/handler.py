import json
import uuid
import time
import asyncio
from datetime import datetime, timezone

from fastapi import WebSocket, WebSocketDisconnect, Query

from database.postgres import execute_pg_query
from websocket.manager import (
    active_connections,
    connection_metadata,
    username_to_email,
    email_to_username,
    buffered_call_signals,
    get_email_by_username,
    broadcast_user_status,
    send_to_user_by_email,
    subscribe_resource,
    unsubscribe_resource,
    cleanup_socket_subscriptions,
    safe_send_ws,
    remove_dead_socket,
)
from auth.deps import verify_auth_token
from services.push import send_web_push
from services.call_push import send_call_push
from config import DEFAULT_AVATAR_URL


async def websocket_endpoint(websocket: WebSocket, username: str, token: str = Query(None)):
    username = username.strip().lower()
    print(f"[WS] Connection attempt for @{username} (token_present={bool(token)})")

    email = await get_email_by_username(username)
    if not email:
        email = f"{username}@spac2.com"
    print(f"[WS] Looked up email for @{username}: '{email}'")

    is_verified = await verify_auth_token(token, email)
    if not is_verified:
        print(f"[WS] Connection rejected: unauthorized. Email: '{email}', Verified: {is_verified}")
        await websocket.accept()
        await safe_send_ws(websocket, json.dumps({
            "type": "error",
            "text": "Unauthorized: Invalid or missing token"
        }), timeout=3.0)
        try:
            await websocket.close(code=1008)
        except Exception:
            pass
        return

    await websocket.accept()

    # Resolve user_id for permission verification
    current_user_id = None
    try:
        user_rows = await execute_pg_query(
            "SELECT id, user_id FROM users WHERE LOWER(email) = $1 OR LOWER(username) = $2",
            email, username
        )
        if user_rows and user_rows[0].get("user_id"):
            current_user_id = int(user_rows[0]["user_id"])
        elif user_rows and user_rows[0].get("id"):
            current_user_id = int(10000 + user_rows[0]["id"])
    except Exception:
        pass

    # Register connection
    is_new_connection = False
    if username not in active_connections:
        active_connections[username] = set()
        is_new_connection = True
    active_connections[username].add(websocket)
    connection_metadata[websocket] = {
        "username": username,
        "email": email,
        "user_id": current_user_id,
        "visible": True,
        "last_seen": time.time()
    }

    # Update username <-> email cache
    username_to_email[username] = email
    email_to_username[email] = username

    print(f"User connected: @{username} ({email}) (user_id={current_user_id}) (Total connections: {len(active_connections[username])})")

    # Flush buffered call signaling messages immediately
    if username in buffered_call_signals and buffered_call_signals[username]:
        print(f"[CALL] Flushing {len(buffered_call_signals[username])} buffered call signals for @{username}")
        for signal_str in buffered_call_signals[username]:
            await safe_send_ws(websocket, signal_str, timeout=3.0)
        del buffered_call_signals[username]

    async def sync_db_on_connect():
        # Keep group member's username column in sync
        try:
            await execute_pg_query(
                "UPDATE chat_group_members SET username = $1 WHERE email = $2",
                username, email
            )
        except Exception as e:
            print(f"Failed to sync username in group_members for @{username}: {e}")

        if is_new_connection:
            try:
                now_iso = datetime.now(timezone.utc).isoformat()
                await execute_pg_query(
                    "UPDATE users SET status = $1, last_seen = $2 WHERE username = $3",
                    "online", now_iso, username
                )
                await broadcast_user_status(username, "online", now_iso)
            except Exception as e:
                print(f"Failed to update status on connect for @{username}: {e}")

    # Run DB sync in background so it doesn't block call signaling
    asyncio.create_task(sync_db_on_connect())

    try:
        while True:
            data_str = await websocket.receive_text()
            if websocket in connection_metadata:
                connection_metadata[websocket]["last_seen"] = time.time()

            try:
                data = json.loads(data_str)
                msg_type = data.get("type")

                if msg_type == "ping":
                    await safe_send_ws(websocket, json.dumps({"type": "pong"}), timeout=2.0)
                    continue

                elif msg_type == "visibility":
                    visible = data.get("visible", True)
                    if websocket in connection_metadata:
                        connection_metadata[websocket]["visible"] = visible
                    continue

                elif msg_type in ("subscribe", "resource_subscribe"):
                    resources = data.get("resources") or data.get("topics") or []
                    if not resources and data.get("resource_id") and data.get("app"):
                        resources = [f"{data.get('app')}:{data.get('resource_id')}"]
                    if isinstance(resources, str):
                        resources = [resources]
                    meta = connection_metadata.get(websocket, {})
                    await subscribe_resource(
                        websocket,
                        resources,
                        user_id=meta.get("user_id") or current_user_id,
                        user_email=email
                    )
                    continue

                elif msg_type in ("unsubscribe", "resource_unsubscribe"):
                    resources = data.get("resources") or data.get("topics") or []
                    if not resources and data.get("resource_id") and data.get("app"):
                        resources = [f"{data.get('app')}:{data.get('resource_id')}"]
                    if isinstance(resources, str):
                        resources = [resources]
                    unsubscribe_resource(websocket, resources)
                    continue

                elif msg_type == "message":
                    await _handle_message(websocket, username, email, data, data_str)

                elif msg_type in ("typing", "stop_typing"):
                    await _handle_typing(username, email, data, msg_type)

                elif msg_type == "read":
                    await _handle_read(websocket, username, data)

                elif msg_type == "reaction":
                    await _handle_reaction(websocket, username, data)

                elif msg_type in ("call_offer", "call_answer", "ice_candidate", "call_reject", "call_cancel", "call_hangup", "call_busy"):
                    await _handle_call_signal(websocket, username, data, data_str, msg_type)

            except Exception as per_msg_err:
                print(f"[WS] Error processing message payload from @{username}: {per_msg_err}")

    except WebSocketDisconnect:
        cleanup_socket_subscriptions(websocket)
        await _cleanup_connection(websocket, username)
        print(f"User disconnected: @{username}")
    except Exception as e:
        print(f"Error in WebSocket session for @{username}: {e}")
        cleanup_socket_subscriptions(websocket)
        await _cleanup_connection(websocket, username)


async def _cleanup_connection(websocket: WebSocket, username: str):
    """Remove websocket from active connections and update user status."""
    cleanup_socket_subscriptions(websocket)
    if username in active_connections:
        active_connections[username].discard(websocket)
        if not active_connections[username]:
            del active_connections[username]
            try:
                now_iso = datetime.now(timezone.utc).isoformat()
                await execute_pg_query(
                    "UPDATE users SET status = $1, last_seen = $2 WHERE username = $3",
                    "offline", now_iso, username
                )
                await broadcast_user_status(username, "offline", now_iso)
            except Exception as e:
                print(f"Failed to update status on disconnect for @{username}: {e}")
    if websocket in connection_metadata:
        del connection_metadata[websocket]


async def _handle_message(websocket: WebSocket, username: str, email: str, data: dict, data_str: str):
    recipient = data.get("recipient", "").strip().lower()
    sender = data.get("sender", "").strip().lower()
    text = data.get("text", "")
    msg_id = data.get("id", f"{int(time.time() * 1000)}_{sender}")
    timestamp = data.get("timestamp", int(time.time() * 1000))
    reply_to = data.get("replyTo")
    forwarded_from = data.get("forwardedFrom")

    if sender != username:
        print(f"User @{username} tried to spoof sender @{sender}")
        return

    msg_uuid = None
    try:
        msg_uuid = uuid.UUID(msg_id)
    except ValueError:
        msg_uuid = uuid.uuid4()

    inserted_update_id = 0
    try:
        created_at_dt = datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc)
        res = await execute_pg_query(
            """
            INSERT INTO messages (id, sender_id, recipient_id, content, created_at, delivered, reply_to, forwarded_from)
            VALUES ($1, $2, $3, $4, $5, FALSE, $6, $7)
            RETURNING update_id
            """,
            msg_uuid, sender, recipient, text, created_at_dt, reply_to, forwarded_from
        )
        inserted_update_id = res[0].get("update_id", 0) if res else 0
    except Exception as e:
        print(f"Error saving message to Postgres: {e}")

    is_group = recipient.startswith("group_")
    if is_group:
        await _route_group_message(websocket, username, email, sender, recipient, text, data, msg_uuid, timestamp, inserted_update_id)
    else:
        await _route_direct_message(websocket, username, sender, recipient, text, data, msg_uuid, timestamp, inserted_update_id)


async def _route_group_message(websocket, username, email, sender, recipient, text, data, msg_uuid, timestamp, inserted_update_id):
    try:
        members_rows = await execute_pg_query("SELECT email, username FROM chat_group_members WHERE group_id = $1", recipient)
        member_emails = [r["email"] for r in members_rows if r.get("email")]
        member_usernames_by_email = {r["email"]: r.get("username", "") for r in members_rows if r.get("email")}
    except Exception as e:
        print(f"Error reading group members: {e}")
        member_emails = []
        member_usernames_by_email = {}

    sender_email = username_to_email.get(sender, email)
    if sender_email not in member_emails:
        print(f"Sender @{sender} ({sender_email}) is not a member of group {recipient}")
        return

    group_name = "Group"
    group_avatar = "https://spac2.com/favicon.ico"
    try:
        group_info = await execute_pg_query("SELECT name, avatar FROM chat_groups WHERE id = $1", recipient)
        if group_info:
            group_name = group_info[0].get("name") or "Group"
            group_avatar = group_info[0].get("avatar") or "https://spac2.com/favicon.ico"
    except Exception:
        pass

    sender_name = data.get("senderName") or sender.upper()
    push_title = f"{sender_name} in {group_name}"

    for m_email in member_emails:
        m_username = email_to_username.get(m_email) or member_usernames_by_email.get(m_email, "")
        member_delivered = False
        member_visible = False
        if m_username and m_username in active_connections:
            for r_ws in list(active_connections[m_username]):
                if m_username == sender and r_ws == websocket:
                    continue
                meta = connection_metadata.get(r_ws, {})
                if meta.get("visible", True):
                    member_visible = True
                ok = await safe_send_ws(r_ws, json.dumps({
                    "type": "message",
                    "id": str(msg_uuid),
                    "sender": sender,
                    "senderName": sender_name,
                    "recipient": recipient,
                    "text": text,
                    "senderAvatar": data.get("senderAvatar") or DEFAULT_AVATAR_URL,
                    "senderEmail": data.get("senderEmail", ""),
                    "groupName": group_name,
                    "groupAvatar": group_avatar,
                    "timestamp": timestamp,
                    "replyTo": data.get("replyTo"),
                    "forwarded": data.get("forwarded"),
                    "forwardedFrom": data.get("forwardedFrom"),
                    "update_id": inserted_update_id
                }), timeout=3.0)
                if ok:
                    member_delivered = True
                else:
                    remove_dead_socket(r_ws, m_username)

        # Always send push for non-sender members.
        if m_email != sender_email:
            asyncio.create_task(send_web_push(
                recipient_username=m_username or m_email,
                title=push_title,
                body=text,
                sender=recipient,
                avatar=data.get("senderAvatar") or DEFAULT_AVATAR_URL
            ))

    try:
        await execute_pg_query("UPDATE messages SET delivered = TRUE WHERE id = $1", msg_uuid)
    except Exception as e:
        print(f"[WS] Failed to mark group message delivered: {e}")

    await safe_send_ws(websocket, json.dumps({
        "type": "status",
        "status": "delivered",
        "recipient": recipient,
        "id": str(msg_uuid),
        "text": "Group message processed.",
        "update_id": inserted_update_id
    }), timeout=3.0)


async def _route_direct_message(websocket, username, sender, recipient, text, data, msg_uuid, timestamp, inserted_update_id):
    recipient_delivered = False
    recipient_visible = False

    if recipient in active_connections:
        for r_ws in list(active_connections[recipient]):
            if recipient == sender and r_ws == websocket:
                continue
            meta = connection_metadata.get(r_ws, {})
            if meta.get("visible", True):
                recipient_visible = True
            ok = await safe_send_ws(r_ws, json.dumps({
                "type": "message",
                "id": str(msg_uuid),
                "sender": sender,
                "recipient": recipient,
                "text": text,
                "senderAvatar": data.get("senderAvatar") or DEFAULT_AVATAR_URL,
                "senderEmail": data.get("senderEmail", ""),
                "timestamp": timestamp,
                "replyTo": data.get("replyTo"),
                "forwarded": data.get("forwarded"),
                "forwardedFrom": data.get("forwardedFrom"),
                "update_id": inserted_update_id
            }), timeout=3.0)
            if ok:
                recipient_delivered = True
            else:
                remove_dead_socket(r_ws, recipient)

    if recipient_delivered or (recipient == sender):
        try:
            await execute_pg_query(
                "UPDATE messages SET delivered = TRUE WHERE id = $1",
                msg_uuid
            )
        except Exception as e:
            print(f"[WS] Failed to update delivered status: {e}")

        await safe_send_ws(websocket, json.dumps({
            "type": "status",
            "status": "delivered",
            "recipient": recipient,
            "id": str(msg_uuid),
            "text": f"Delivered to @{recipient}." if recipient != sender else "Saved message saved.",
            "update_id": inserted_update_id
        }), timeout=3.0)
    else:
        await safe_send_ws(websocket, json.dumps({
            "type": "status",
            "status": "offline",
            "recipient": recipient,
            "id": str(msg_uuid),
            "text": f"@{recipient} is offline. Message queued.",
            "update_id": inserted_update_id
        }), timeout=3.0)

    # Always send push for offline or backgrounded user
    if recipient != sender:
        asyncio.create_task(send_web_push(
            recipient_username=recipient,
            title=data.get("senderName") or sender.upper(),
            body=text,
            sender=sender,
            avatar=data.get("senderAvatar") or DEFAULT_AVATAR_URL
        ))

    # Broadcast to all OTHER connections of the sender (multi-tab/device sync)
    if sender != recipient and sender in active_connections:
        for s_ws in list(active_connections[sender]):
            if s_ws != websocket:
                ok = await safe_send_ws(s_ws, json.dumps({
                    "type": "message",
                    "id": str(msg_uuid),
                    "sender": sender,
                    "recipient": recipient,
                    "text": text,
                    "senderAvatar": data.get("senderAvatar") or DEFAULT_AVATAR_URL,
                    "senderEmail": data.get("senderEmail", ""),
                    "timestamp": timestamp,
                    "replyTo": data.get("replyTo"),
                    "forwarded": data.get("forwarded"),
                    "forwardedFrom": data.get("forwardedFrom"),
                    "update_id": inserted_update_id
                }), timeout=3.0)
                if not ok:
                    remove_dead_socket(s_ws, sender)


async def _handle_typing(username, email, data, msg_type):
    recipient = data.get("recipient", "").strip().lower()
    if recipient.startswith("group_"):
        try:
            members_rows = await execute_pg_query("SELECT email, username FROM chat_group_members WHERE group_id = $1", recipient)
            sender_email = username_to_email.get(username, email)
            for row in members_rows:
                m_email = row.get("email", "")
                if m_email == sender_email:
                    continue
                m_username = email_to_username.get(m_email) or row.get("username", "")
                if m_username and m_username in active_connections:
                    for r_ws in list(active_connections[m_username]):
                        await safe_send_ws(r_ws, json.dumps({
                            "type": msg_type,
                            "sender": username,
                            "recipient": recipient
                        }), timeout=2.0)
        except Exception:
            pass
    else:
        if recipient in active_connections:
            for r_ws in list(active_connections[recipient]):
                await safe_send_ws(r_ws, json.dumps({
                    "type": msg_type,
                    "sender": username
                }), timeout=2.0)


async def _handle_read(websocket, username, data):
    recipient = data.get("recipient", "").strip().lower()
    message_ids = data.get("message_ids", [])
    read_payload = json.dumps({
        "type": "read",
        "sender": username,
        "message_ids": message_ids,
        "recipient": recipient
    })

    if recipient in active_connections:
        for r_ws in list(active_connections[recipient]):
            await safe_send_ws(r_ws, read_payload, timeout=2.0)

    # Broadcast to other sender connections
    if username in active_connections:
        for s_ws in list(active_connections[username]):
            if s_ws != websocket:
                await safe_send_ws(s_ws, read_payload, timeout=2.0)


async def _handle_reaction(websocket, username, data):
    msg_id = data.get("msgId")
    recipient = data.get("recipient", "").strip().lower()
    sender = data.get("sender", "").strip().lower()
    reaction = data.get("reaction")

    reactions_dict = data.get("reactions")
    if reactions_dict is not None and isinstance(reactions_dict, dict):
        reaction = reactions_dict.get(sender)

    if sender != username:
        print(f"User @{username} tried to spoof sender @{sender} in reaction")
        return

    msg_uuid = None
    try:
        msg_uuid = uuid.UUID(msg_id)
    except ValueError:
        return

    try:
        if reaction:
            await execute_pg_query(
                """
                UPDATE messages 
                SET reactions = jsonb_set(COALESCE(reactions, '{}'::jsonb), ARRAY[$1], to_jsonb($2::text))
                WHERE id = $3
                """,
                sender, reaction, msg_uuid
            )
        else:
            await execute_pg_query(
                """
                UPDATE messages 
                SET reactions = COALESCE(reactions, '{}'::jsonb) - $1
                WHERE id = $2
                """,
                sender, msg_uuid
            )

        res = await execute_pg_query("SELECT reactions FROM messages WHERE id = $1", msg_uuid)
        current_reactions = {}
        if res:
            reactions_val = res[0].get("reactions")
            if reactions_val:
                if isinstance(reactions_val, str):
                    current_reactions = json.loads(reactions_val)
                elif isinstance(reactions_val, dict):
                    current_reactions = reactions_val

        rx_payload = json.dumps({
            "type": "reaction",
            "msgId": str(msg_uuid),
            "reactions": current_reactions,
            "sender": sender,
            "recipient": recipient
        })

        is_group = recipient.startswith("group_")
        if is_group:
            try:
                members_rows = await execute_pg_query("SELECT username FROM chat_group_members WHERE group_id = $1", recipient)
                members = [r["username"] for r in members_rows]
                for member in members:
                    if member in active_connections:
                        for r_ws in list(active_connections[member]):
                            if member == sender and r_ws == websocket:
                                continue
                            await safe_send_ws(r_ws, rx_payload, timeout=2.0)
            except Exception:
                pass
        else:
            if recipient in active_connections:
                for r_ws in list(active_connections[recipient]):
                    await safe_send_ws(r_ws, rx_payload, timeout=2.0)

            if sender in active_connections:
                for s_ws in list(active_connections[sender]):
                    if s_ws != websocket:
                        await safe_send_ws(s_ws, rx_payload, timeout=2.0)
    except Exception as e:
        print(f"Error handling reaction: {e}")


async def _handle_call_signal(websocket, username, data, data_str, msg_type):
    recipient = data.get("recipient", "").strip().lower()
    if recipient in active_connections:
        for r_ws in list(active_connections[recipient]):
            await safe_send_ws(r_ws, data_str, timeout=3.0)
    else:
        if recipient not in buffered_call_signals:
            buffered_call_signals[recipient] = []
        buffered_call_signals[recipient].append(data_str)
        print(f"[CALL] Buffered {msg_type} for offline/transitioning user @{recipient}")

        if msg_type == "call_offer":
            is_video = data.get("isVideo", True)
            asyncio.create_task(send_call_push(
                recipient_username=recipient,
                sender=username,
                avatar=data.get("senderAvatar", "") or data.get("callerAvatar", "") or DEFAULT_AVATAR_URL,
                is_video=is_video
            ))

            # Run 30s offline reconnect wait in background so caller's WebSocket loop remains responsive
            async def _wait_and_handle_offline_call():
                forwarded = False
                for _ in range(30):
                    await asyncio.sleep(1)
                    if recipient in active_connections:
                        forwarded = True
                        print(f"[CALL] @{recipient} came online! Buffered signals flushed to recipient.")
                        break

                if not forwarded:
                    print(f"[CALL] @{recipient} still offline after 30s. Sending call_busy to @{username}.")
                    if recipient in buffered_call_signals:
                        try:
                            buffered_call_signals.pop(recipient, None)
                        except KeyError:
                            pass
                    await safe_send_ws(websocket, json.dumps({
                        "type": "call_busy",
                        "sender": recipient,
                        "recipient": username,
                        "reason": "offline"
                    }), timeout=3.0)

            asyncio.create_task(_wait_and_handle_offline_call())
