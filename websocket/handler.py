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
    buffered_call_timestamps,
    get_email_by_username,
    broadcast_user_status,
    send_to_user_by_email,
    send_ws_to_user,
    send_ws_to_users,
    is_user_online,
    subscribe_resource,
    unsubscribe_resource,
    cleanup_socket_subscriptions,
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
        await websocket.send_text(json.dumps({
            "type": "error",
            "text": "Unauthorized: Invalid or missing token"
        }))
        await websocket.close(code=1008)
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

    # Flush buffered call signaling messages immediately (DB-backed cross-worker + local fallback)
    try:
        from database.postgres import pg_pool
        if pg_pool:
            cutoff = time.time() - 35.0
            await execute_pg_query("DELETE FROM call_signal_buffer WHERE created_at < $1", cutoff)
            db_signals = await execute_pg_query(
                "SELECT id, payload FROM call_signal_buffer WHERE recipient = $1 AND created_at >= $2 ORDER BY id ASC",
                username, cutoff
            )
            if db_signals:
                sig_ids = [r["id"] for r in db_signals if "id" in r]
                if sig_ids:
                    await execute_pg_query(
                        "DELETE FROM call_signal_buffer WHERE id = ANY($1::bigint[])",
                        sig_ids
                    )
                print(f"[CALL] Flushing {len(db_signals)} DB buffered call signals for @{username}")
                for row in db_signals:
                    sig_payload = row.get("payload")
                    if not sig_payload:
                        continue
                    try:
                        await websocket.send_text(sig_payload)
                    except Exception as e:
                        print(f"[CALL] Failed to send DB buffered call signal: {e}")
    except Exception as e:
        print(f"[CALL] Error flushing DB call signals for @{username}: {e}")

    if username in buffered_call_signals and buffered_call_signals[username]:
        print(f"[CALL] Flushing {len(buffered_call_signals[username])} local buffered call signals for @{username}")
        for signal_str in buffered_call_signals[username]:
            try:
                await websocket.send_text(signal_str)
            except Exception as e:
                print(f"[CALL] Failed to send buffered call signal: {e}")
        buffered_call_signals.pop(username, None)
        buffered_call_timestamps.pop(username, None)

    # Request cross-worker call signal flush in case another worker buffered signals
    try:
        from websocket.manager import _publish_cross_worker_event
        asyncio.create_task(_publish_cross_worker_event([username], json.dumps({
            "type": "_flush_worker_call_signals",
            "user": username
        })))
    except Exception:
        pass

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

            data = json.loads(data_str)
            msg_type = data.get("type")

            if msg_type == "ping":
                try:
                    await websocket.send_text(json.dumps({"type": "pong"}))
                except Exception:
                    pass
                continue

            elif msg_type == "visibility":
                visible = data.get("visible", True)
                if websocket in connection_metadata:
                    connection_metadata[websocket]["visible"] = visible
                    print(f"[VISIBILITY] User @{username} visibility updated to: {visible}")
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

    except WebSocketDisconnect:
        cleanup_socket_subscriptions(websocket)
        await _cleanup_connection(websocket, username)
        print(f"User disconnected: @{username}")
    except RuntimeError as e:
        cleanup_socket_subscriptions(websocket)
        await _cleanup_connection(websocket, username)
        if "not connected" not in str(e).lower():
            print(f"RuntimeError in WebSocket session for @{username}: {e}")
        else:
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
            # Grace window of 8s to prevent status flaps when navigating to /call/ or reloading
            async def delayed_offline(u: str):
                await asyncio.sleep(8)
                if u not in active_connections:
                    try:
                        now_iso = datetime.now(timezone.utc).isoformat()
                        await execute_pg_query(
                            "UPDATE users SET status = $1, last_seen = $2 WHERE username = $3",
                            "offline", now_iso, u
                        )
                        await broadcast_user_status(u, "offline", now_iso)
                    except Exception as e:
                        print(f"Failed to update status on disconnect for @{u}: {e}")
            asyncio.create_task(delayed_offline(username))
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

    msg_payload = json.dumps({
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
    })

    target_usernames = []
    for m_email in member_emails:
        m_username = (email_to_username.get(m_email) or member_usernames_by_email.get(m_email, "")).strip().lower()
        if m_username:
            target_usernames.append(m_username)

        # Always send push for non-sender members.
        # SW dedup (sw.js) handles suppressing banner when app is visible.
        if m_email != sender_email:
            asyncio.create_task(send_web_push(
                recipient_username=m_username or m_email,
                title=push_title,
                body=text,
                sender=recipient,
                avatar=data.get("senderAvatar") or DEFAULT_AVATAR_URL
            ))

    if target_usernames:
        await send_ws_to_users(target_usernames, msg_payload, exclude_ws=websocket)

    await execute_pg_query("UPDATE messages SET delivered = TRUE WHERE id = $1", msg_uuid)

    try:
        await websocket.send_text(json.dumps({
            "type": "status",
            "status": "delivered",
            "recipient": recipient,
            "id": str(msg_uuid),
            "text": "Group message processed.",
            "update_id": inserted_update_id
        }))
    except Exception as e:
        print(f"Failed to send status to sender: {e}")


async def _route_direct_message(websocket, username, sender, recipient, text, data, msg_uuid, timestamp, inserted_update_id):
    msg_payload = json.dumps({
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
    })

    # Deliver to recipient across all workers (and to other sender tabs/devices)
    local_sent = await send_ws_to_user(recipient, msg_payload, exclude_ws=websocket)
    recipient_online = (local_sent > 0) or (recipient == sender) or (await is_user_online(recipient))

    if recipient_online:
        await execute_pg_query(
            "UPDATE messages SET delivered = TRUE WHERE id = $1",
            msg_uuid
        )
        try:
            await websocket.send_text(json.dumps({
                "type": "status",
                "status": "delivered",
                "recipient": recipient,
                "id": str(msg_uuid),
                "text": f"Delivered to @{recipient}." if recipient != sender else "Saved message saved.",
                "update_id": inserted_update_id
            }))
        except Exception as e:
            print(f"Failed to send status to sender: {e}")
    else:
        try:
            await websocket.send_text(json.dumps({
                "type": "status",
                "status": "offline",
                "recipient": recipient,
                "id": str(msg_uuid),
                "text": f"@{recipient} is offline. Message queued.",
                "update_id": inserted_update_id
            }))
        except Exception as e:
            print(f"Failed to send status to sender: {e}")

    # Always send push — SW dedup handles suppressing banner when app is visible.
    if recipient != sender:
        asyncio.create_task(send_web_push(
            recipient_username=recipient,
            title=data.get("senderName") or sender.upper(),
            body=text,
            sender=sender,
            avatar=data.get("senderAvatar") or DEFAULT_AVATAR_URL
        ))

    # Broadcast to all OTHER connections of the sender (multi-tab/device sync across workers)
    if sender != recipient:
        await send_ws_to_user(sender, msg_payload, exclude_ws=websocket)


async def _handle_typing(username, email, data, msg_type):
    recipient = data.get("recipient", "").strip().lower()
    if recipient.startswith("group_"):
        try:
            members_rows = await execute_pg_query("SELECT email, username FROM chat_group_members WHERE group_id = $1", recipient)
            sender_email = username_to_email.get(username, email)
            targets = []
            for row in members_rows:
                m_email = row.get("email", "")
                if m_email == sender_email:
                    continue
                m_username = (email_to_username.get(m_email) or row.get("username", "")).strip().lower()
                if m_username:
                    targets.append(m_username)
            if targets:
                payload = json.dumps({
                    "type": msg_type,
                    "sender": username,
                    "recipient": recipient
                })
                await send_ws_to_users(targets, payload)
        except Exception:
            pass
    else:
        payload = json.dumps({
            "type": msg_type,
            "sender": username
        })
        await send_ws_to_user(recipient, payload)


async def _handle_read(websocket, username, data):
    recipient = data.get("recipient", "").strip().lower()
    message_ids = data.get("message_ids", [])

    payload = json.dumps({
        "type": "read",
        "sender": username,
        "message_ids": message_ids,
        "recipient": recipient
    })

    targets = [u for u in {recipient, username} if u]
    if targets:
        await send_ws_to_users(targets, payload, exclude_ws=websocket)


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

        payload = json.dumps({
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
                members = [r["username"] for r in members_rows if r.get("username")]
                if members:
                    await send_ws_to_users(members, payload, exclude_ws=websocket)
            except Exception:
                pass
        else:
            targets = [u for u in {recipient, sender} if u]
            if targets:
                await send_ws_to_users(targets, payload, exclude_ws=websocket)
    except Exception as e:
        print(f"Error handling reaction: {e}")


# Track active pending outgoing call wait tasks: (recipient, caller) -> asyncio.Task
_pending_call_wait_tasks = {}


def _cancel_pending_call_wait(recipient: str, caller: str):
    key = (recipient.strip().lower(), caller.strip().lower())
    task = _pending_call_wait_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def _handle_call_signal(websocket, username, data, data_str, msg_type):
    recipient = data.get("recipient", "").strip().lower()
    if not recipient:
        return

    sender = username.strip().lower()

    # If caller cancels or either side hangs up/rejects/answers, cancel any pending offline wait task
    if msg_type in ("call_cancel", "call_reject", "call_hangup", "call_answer"):
        _cancel_pending_call_wait(recipient, sender)
        _cancel_pending_call_wait(sender, recipient)
        try:
            from database.postgres import pg_pool
            if pg_pool:
                await execute_pg_query(
                    "DELETE FROM call_signal_buffer WHERE recipient = $1 AND sender = $2 AND msg_type IN ('call_offer', 'ice_candidate')",
                    recipient, sender
                )
                if msg_type == "call_answer":
                    await execute_pg_query(
                        "DELETE FROM call_signal_buffer WHERE recipient = $1 AND sender = $2 AND msg_type = 'call_offer'",
                        sender, recipient
                    )
        except Exception:
            pass
        if msg_type == "call_answer":
            buffered_call_signals.pop(sender, None)
            buffered_call_timestamps.pop(sender, None)

        # Clean up stale buffered offer/candidates from this sender to recipient
        if recipient in buffered_call_signals:
            filtered = []
            for sig_str in buffered_call_signals[recipient]:
                try:
                    sig_obj = json.loads(sig_str)
                    if (
                        sig_obj.get("sender", "").strip().lower() == sender
                        and sig_obj.get("type") in ("call_offer", "ice_candidate")
                    ):
                        continue
                except Exception:
                    pass
                filtered.append(sig_str)
            if filtered:
                buffered_call_signals[recipient] = filtered
            else:
                buffered_call_signals.pop(recipient, None)
                buffered_call_timestamps.pop(recipient, None)

    # Check local delivery first; if not on this worker, insert into DB buffer BEFORE pg_notify
    # so the receiving worker's DELETE never races ahead of our INSERT.
    from websocket.manager import _deliver_local_ws, _publish_cross_worker_event
    local_sent = await _deliver_local_ws(recipient, data_str)

    if local_sent == 0:
        from database.postgres import pg_pool
        now_ts = time.time()
        if pg_pool:
            try:
                if msg_type == "call_offer" and not data.get("renegotiate"):
                    await execute_pg_query(
                        "DELETE FROM call_signal_buffer WHERE recipient = $1 AND sender = $2 AND msg_type IN ('call_offer', 'ice_candidate')",
                        recipient, sender
                    )
                await execute_pg_query(
                    "INSERT INTO call_signal_buffer (recipient, sender, msg_type, payload, created_at) VALUES ($1, $2, $3, $4, $5)",
                    recipient, sender, msg_type, data_str, now_ts
                )
            except Exception as e:
                print(f"[CALL] Failed to buffer signal in DB: {e}")
        else:
            if recipient not in buffered_call_signals:
                buffered_call_signals[recipient] = []
            if msg_type == "call_offer" and not data.get("renegotiate"):
                buffered_call_signals[recipient] = [
                    s for s in buffered_call_signals[recipient]
                    if not (
                        '"sender": "{}"'.format(sender) in s.lower()
                        and ('"call_offer"' in s or '"ice_candidate"' in s)
                    )
                ]
            if len(buffered_call_signals[recipient]) < 60:
                buffered_call_signals[recipient].append(data_str)
            buffered_call_timestamps[recipient] = now_ts

        # Now publish across workers (if recipient is connected on another worker, it will deliver & delete from DB)
        await _publish_cross_worker_event([recipient], data_str)
        print(f"[CALL] Buffered & fanned out {msg_type} for @{recipient} (local_sent=0)")

        if msg_type == "call_offer" and not data.get("renegotiate"):
            is_video = data.get("isVideo", True)
            _cancel_pending_call_wait(recipient, sender)
            wait_key = (recipient, sender)

            async def _wait_for_recipient_reconnect():
                try:
                    push_sent = False
                    # Give cross-worker pg_notify ~1.2s to deliver if recipient is actually online on another worker
                    for sec in range(35):
                        await asyncio.sleep(1)

                        # Check if another worker already delivered & deleted the call_offer, or recipient flushed it
                        if pg_pool:
                            rows = await execute_pg_query(
                                "SELECT id FROM call_signal_buffer WHERE recipient = $1 AND sender = $2 AND msg_type = 'call_offer' LIMIT 1",
                                recipient, sender
                            )
                            if not rows:
                                return
                        else:
                            if recipient not in buffered_call_signals:
                                return

                        # After 1s, if still buffered (not delivered on any worker), send Push Notification once
                        if sec == 1 and not push_sent:
                            push_sent = True
                            asyncio.create_task(send_call_push(
                                recipient_username=recipient,
                                sender=sender,
                                avatar=data.get("senderAvatar", "") or data.get("callerAvatar", "") or DEFAULT_AVATAR_URL,
                                is_video=is_video
                            ))

                    # Still undelivered after 35s -> clean up and notify caller
                    if pg_pool:
                        await execute_pg_query(
                            "DELETE FROM call_signal_buffer WHERE recipient = $1 AND sender = $2 AND msg_type IN ('call_offer', 'ice_candidate')",
                            recipient, sender
                        )
                    else:
                        buffered_call_signals.pop(recipient, None)
                        buffered_call_timestamps.pop(recipient, None)

                    try:
                        await websocket.send_text(json.dumps({
                            "type": "call_busy",
                            "sender": recipient,
                            "recipient": sender,
                            "reason": "offline"
                        }))
                    except Exception as e:
                        print(f"[CALL] Failed to send call_busy to @{sender}: {e}")
                except asyncio.CancelledError:
                    pass
                finally:
                    _pending_call_wait_tasks.pop(wait_key, None)

            _pending_call_wait_tasks[wait_key] = asyncio.create_task(_wait_for_recipient_reconnect())
