import json
import time
import uuid
import asyncio
from typing import Dict, Optional, Any, Set, List
from fastapi import WebSocket

# Unique ID for this Uvicorn worker process (used for cross-worker Pub/Sub deduplication)
WORKER_ID = uuid.uuid4().hex[:8]

# Active WebSocket connections: username -> set of WebSocket connections
active_connections: Dict[str, set] = {}
connection_metadata: Dict[WebSocket, dict] = {}

# Resource Invalidation Pub/Sub: resource_key (e.g. "doc:977ViYdH", "note:ABC123") -> set of WebSockets
resource_subscribers: Dict[str, set] = {}
socket_subscriptions: Dict[WebSocket, set] = {}

# Server-Side Coalescing State: r_key -> latest payload dict, r_key -> asyncio Task
_coalesce_buffer: Dict[str, dict] = {}
_coalesce_tasks: Dict[str, asyncio.Task] = {}

# In-memory username <-> email mapping cache (updated when users connect, bounded by GC)
username_to_email: Dict[str, str] = {}  # username -> email
email_to_username: Dict[str, str] = {}  # email -> username (current)
MAX_IDENTITY_CACHE_SIZE = 5000

# Buffered call signaling messages (e.g. call_offer, ice_candidate) for offline or transitioning users
buffered_call_signals: Dict[str, list] = {}
buffered_call_timestamps: Dict[str, float] = {}


async def check_resource_permission(
    app: str,
    resource_id: str,
    user_id: Optional[int] = None,
    user_email: Optional[str] = None
) -> bool:
    """
    Validates if the requesting user is the owner of a resource.
    Ensures resources cannot be monitored or probed by unauthorized users.
    """
    if not resource_id:
        return False
    app_code = str(app).strip().lower()
    r_id = str(resource_id).strip()

    from database.postgres import execute_pg_query

    try:
        if app_code == "doc":
            rows = await execute_pg_query(
                "SELECT user_id, is_deleted FROM user_sync_docs WHERE doc_id = $1 AND is_deleted = FALSE",
                r_id
            )
            if not rows:
                return bool(user_id or user_email)
            doc = rows[0]
            return bool(user_id and doc.get("user_id") == user_id)

        elif app_code == "note":
            rows = await execute_pg_query(
                "SELECT user_id, is_deleted FROM user_sync_notes WHERE note_id = $1 AND is_deleted = FALSE",
                r_id
            )
            if not rows:
                return bool(user_id or user_email)
            note = rows[0]
            return bool(user_id and note.get("user_id") == user_id)

        elif app_code == "mindmap":
            rows = await execute_pg_query(
                "SELECT user_id, is_deleted FROM user_sync_mindmap_projects WHERE project_id = $1 AND is_deleted = FALSE",
                r_id
            )
            if not rows:
                return bool(user_id or user_email)
            proj = rows[0]
            return bool(user_id and proj.get("user_id") == user_id)

    except Exception as e:
        print(f"[WS Permission Check] Notice: {e}")
        return False

    return True


async def subscribe_resource(
    ws: WebSocket,
    resources: list,
    user_id: Optional[int] = None,
    user_email: Optional[str] = None
):
    """
    Subscribe a connected client to one or more resource invalidation topics
    AFTER verifying authorization permissions.
    """
    if not resources or not isinstance(resources, list):
        return
    if ws not in socket_subscriptions:
        socket_subscriptions[ws] = set()

    for r in resources:
        if not r or not isinstance(r, str):
            continue
        r_key = r.strip().lower()
        parts = r_key.split(":", 1)
        if len(parts) == 2:
            app_code, res_id = parts[0], parts[1]
        else:
            app_code, res_id = "doc", parts[0]

        # Authorize subscription
        is_allowed = await check_resource_permission(app_code, res_id, user_id, user_email)
        if not is_allowed:
            print(f"[WS Security] Subscription REJECTED for user_id={user_id} on {r_key} (unauthorized)")
            continue

        if r_key not in resource_subscribers:
            resource_subscribers[r_key] = set()
        resource_subscribers[r_key].add(ws)
        socket_subscriptions[ws].add(r_key)


def unsubscribe_resource(ws: WebSocket, resources: list):
    """Unsubscribe a connected client from specific resource topics."""
    if not resources or not isinstance(resources, list):
        return
    for r in resources:
        if not r or not isinstance(r, str):
            continue
        r_key = r.strip().lower()
        if r_key in resource_subscribers:
            resource_subscribers[r_key].discard(ws)
            if not resource_subscribers[r_key]:
                del resource_subscribers[r_key]
        if ws in socket_subscriptions:
            socket_subscriptions[ws].discard(r_key)


def cleanup_socket_subscriptions(ws: WebSocket):
    """Remove a disconnected socket from all resource subscriber lists."""
    if ws in socket_subscriptions:
        for r_key in list(socket_subscriptions[ws]):
            if r_key in resource_subscribers:
                resource_subscribers[r_key].discard(ws)
                if not resource_subscribers[r_key]:
                    del resource_subscribers[r_key]
        del socket_subscriptions[ws]


async def _dispatch_to_local_subscribers(ping_data: dict) -> int:
    """Delivers an invalidation signal to all local WebSockets viewing this resource."""
    app_code = str(ping_data.get("app", "")).strip().lower()
    r_id = str(ping_data.get("resource_id", "")).strip()
    r_key = f"{app_code}:{r_id}".lower()

    subscribers = resource_subscribers.get(r_key)
    if not subscribers:
        return 0

    payload = json.dumps(ping_data)
    dead_sockets = []
    sent_count = 0

    for ws in list(subscribers):
        try:
            await ws.send_text(payload)
            sent_count += 1
        except Exception:
            dead_sockets.append(ws)

    for ws in dead_sockets:
        cleanup_socket_subscriptions(ws)

    return sent_count


async def coalesce_and_dispatch_invalidation(data: dict):
    """
    Coalesces rapid invalidation signals on the server within a 150ms debounce window.
    Only the latest revision `rev` is dispatched, preventing WebSocket message storm.
    """
    app_code = str(data.get("app", "")).strip().lower()
    r_id = str(data.get("resource_id", "")).strip()
    if not app_code or not r_id:
        return

    r_key = f"{app_code}:{r_id}".lower()
    incoming_rev = int(data.get("rev", 0))

    if r_key in _coalesce_buffer:
        prev_rev = int(_coalesce_buffer[r_key].get("rev", 0))
        data["rev"] = max(prev_rev, incoming_rev)
    _coalesce_buffer[r_key] = data

    if r_key in _coalesce_tasks and not _coalesce_tasks[r_key].done():
        return

    async def _flush(key: str):
        try:
            await asyncio.sleep(0.15)  # 150ms server coalescing window
            ping = _coalesce_buffer.pop(key, None)
            _coalesce_tasks.pop(key, None)
            if ping:
                await _dispatch_to_local_subscribers(ping)
        except Exception as e:
            print(f"[Coalesce Flush Notice] {e}")

    _coalesce_tasks[r_key] = asyncio.create_task(_flush(r_key))


async def broadcast_resource_invalidation(
    app: str,
    resource_id: str,
    rev: int,
    actor_email: str = None,
    client_id: str = None
):
    """
    Broadcasts a tiny (<150 byte) invalidation ping ONLY after DB commit.
    Uses PostgreSQL NOTIFY for cross-instance fan-out across multiple API servers,
    with local server coalescing to avoid message storms.
    """
    app_code = str(app).strip().lower()
    r_id = str(resource_id).strip()
    if not app_code or not r_id:
        return 0

    payload_dict = {
        "type": "resource_changed",
        "app": app_code,
        "resource_id": r_id,
        "rev": int(rev),
        "actor": actor_email or "",
        "client_id": client_id or "",
        "ts": int(time.time() * 1000)
    }
    payload_str = json.dumps(payload_dict)

    # 1. Publish to PostgreSQL NOTIFY (delivers across all API instances)
    from database.postgres import execute_pg_query, pg_pool
    if pg_pool:
        try:
            await execute_pg_query("SELECT pg_notify('spac2_resource_changed', $1)", payload_str)
            return 1
        except Exception as e:
            print(f"[WS Invalidation] pg_notify notice: {e}")

    # 2. In-memory fallback (if running in SQLite local dev)
    await coalesce_and_dispatch_invalidation(payload_dict)
    return 1


async def _deliver_local_ws(username: str, payload: str, exclude_ws: Optional[WebSocket] = None) -> int:
    """Delivers a WebSocket text payload to all local active connections of `username` and prunes dead sockets."""
    if not username:
        return 0
    uname = username.strip().lower()
    conns = active_connections.get(uname)
    if not conns:
        return 0

    sent_count = 0
    dead_sockets = []
    for ws in list(conns):
        if exclude_ws is not None and ws == exclude_ws:
            continue
        try:
            await ws.send_text(payload)
            sent_count += 1
        except Exception:
            dead_sockets.append(ws)

    for ws in dead_sockets:
        conns.discard(ws)
        cleanup_socket_subscriptions(ws)
        connection_metadata.pop(ws, None)
    if not conns:
        active_connections.pop(uname, None)

    return sent_count


async def _handle_cross_worker_chat_event(data: dict):
    """Handles chat WebSocket events published from another worker process via PG NOTIFY."""
    try:
        origin_worker = data.get("w")
        if origin_worker == WORKER_ID:
            return
        targets = data.get("u") or []
        if isinstance(targets, str):
            targets = [targets]
        payload = data.get("p")
        if not payload and data.get("msg_id"):
            # Fallback for oversized messages (>7KB) that couldn't fit directly in pg_notify
            from database.postgres import execute_pg_query
            msg_id = data.get("msg_id")
            rows = await execute_pg_query("SELECT content FROM messages WHERE id = $1::uuid", msg_id)
            base_obj = data.get("meta") or {}
            if rows and rows[0].get("content") is not None:
                base_obj["text"] = rows[0]["content"]
                payload = json.dumps(base_obj)
        if not payload:
            return
        if "_flush_worker_call_signals" in payload:
            try:
                flush_info = json.loads(payload)
                flush_user = (flush_info.get("user") or "").strip().lower()
                if flush_user and flush_user in buffered_call_signals:
                    queued_sigs = buffered_call_signals.pop(flush_user, [])
                    buffered_call_timestamps.pop(flush_user, None)
                    for q_sig in queued_sigs:
                        await send_ws_to_user(flush_user, q_sig)
                return
            except Exception:
                pass
        for uname in targets:
            await _deliver_local_ws(uname, payload)
    except Exception as e:
        print(f"[PG Chat Event] Error handling cross-worker payload: {e}")


async def send_ws_to_user(
    username: str,
    payload: str,
    exclude_ws: Optional[WebSocket] = None,
    local_only: bool = False
) -> int:
    """
    Sends a WebSocket payload to `username` on the current worker AND fans out via
    PostgreSQL NOTIFY ('spac2_chat_events') so sockets connected to other workers also receive it.
    """
    if not username or not payload:
        return 0
    uname = username.strip().lower()
    local_sent = await _deliver_local_ws(uname, payload, exclude_ws=exclude_ws)

    if not local_only:
        await _publish_cross_worker_event([uname], payload)

    return local_sent


async def send_ws_to_users(
    usernames: List[str],
    payload: str,
    exclude_ws: Optional[WebSocket] = None,
    local_only: bool = False
) -> int:
    """Delivers a WebSocket payload to multiple usernames across all workers with a single PG NOTIFY."""
    if not usernames or not payload:
        return 0
    clean_users = list({u.strip().lower() for u in usernames if u and u.strip()})
    if not clean_users:
        return 0

    total_local = 0
    for uname in clean_users:
        total_local += await _deliver_local_ws(uname, payload, exclude_ws=exclude_ws)

    if not local_only:
        await _publish_cross_worker_event(clean_users, payload)

    return total_local


async def _publish_cross_worker_event(targets: List[str], payload: str):
    """Publishes a chat event envelope to PostgreSQL NOTIFY channel 'spac2_chat_events'."""
    from database.postgres import execute_pg_query, pg_pool
    if not pg_pool or not targets:
        return

    envelope = json.dumps({"w": WORKER_ID, "u": targets, "p": payload})
    if len(envelope.encode("utf-8")) > 7200:
        # Handle >7.2KB chat message by stripping `text` and letting receiving workers fetch by `msg_id`
        try:
            parsed = json.loads(payload)
            msg_id = parsed.get("id")
            if msg_id and "text" in parsed:
                meta = dict(parsed)
                meta.pop("text", None)
                envelope = json.dumps({"w": WORKER_ID, "u": targets, "msg_id": msg_id, "meta": meta})
        except Exception:
            return

    if len(envelope.encode("utf-8")) <= 7900:
        try:
            await execute_pg_query("SELECT pg_notify('spac2_chat_events', $1)", envelope)
        except Exception as e:
            print(f"[WS Cross-Worker] pg_notify error: {e}")


async def is_user_online(username: str) -> bool:
    """Checks whether `username` is connected on this worker or marked online in PostgreSQL."""
    if not username:
        return False
    uname = username.strip().lower()
    if uname in active_connections and len(active_connections[uname]) > 0:
        return True
    try:
        from database.postgres import execute_pg_query
        rows = await execute_pg_query("SELECT status FROM users WHERE username = $1", uname)
        if rows and rows[0].get("status") == "online":
            return True
    except Exception:
        pass
    return False


def prune_manager_caches():
    """Garbage-collects bounded identity caches, expired buffered call signals, and orphaned metadata."""
    now = time.time()

    # 1. Clean expired buffered call signals (> 45s old)
    for uname in list(buffered_call_signals.keys()):
        ts = buffered_call_timestamps.get(uname, 0)
        if now - ts > 45:
            buffered_call_signals.pop(uname, None)
            buffered_call_timestamps.pop(uname, None)

    # 2. Bound identity caches while preserving currently connected users
    if len(username_to_email) > MAX_IDENTITY_CACHE_SIZE:
        active_users = set(active_connections.keys())
        preserved_u2e = {u: username_to_email[u] for u in active_users if u in username_to_email}
        username_to_email.clear()
        username_to_email.update(preserved_u2e)
        email_to_username.clear()
        for u, em in preserved_u2e.items():
            email_to_username[em] = u

    # 3. Clean empty sets in active_connections & resource_subscribers
    for uname in list(active_connections.keys()):
        if not active_connections[uname]:
            active_connections.pop(uname, None)
    for r_key in list(resource_subscribers.keys()):
        if not resource_subscribers[r_key]:
            resource_subscribers.pop(r_key, None)


async def start_pg_invalidation_listener():
    """
    Background worker that listens on PostgreSQL channels:
    - 'spac2_resource_changed' (resource sync invalidation)
    - 'spac2_chat_events' (cross-worker real-time chat WebSocket delivery)
    """
    import asyncpg
    from config import POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB

    while True:
        try:
            conn = await asyncpg.connect(
                host=POSTGRES_HOST,
                port=POSTGRES_PORT,
                user=POSTGRES_USER,
                password=POSTGRES_PASSWORD,
                database=POSTGRES_DB
            )
            print(f"[PG Listen] Worker {WORKER_ID} connected to 'spac2_resource_changed' & 'spac2_chat_events'")

            def on_resource_notification(connection, pid, channel, payload):
                try:
                    data = json.loads(payload)
                    asyncio.create_task(coalesce_and_dispatch_invalidation(data))
                except Exception as err:
                    print(f"[PG Listen] Error parsing resource payload: {err}")

            def on_chat_notification(connection, pid, channel, payload):
                try:
                    data = json.loads(payload)
                    asyncio.create_task(_handle_cross_worker_chat_event(data))
                except Exception as err:
                    print(f"[PG Listen] Error parsing chat event payload: {err}")

            await conn.add_listener("spac2_resource_changed", on_resource_notification)
            await conn.add_listener("spac2_chat_events", on_chat_notification)

            while not conn.is_closed():
                await asyncio.sleep(20)
                await conn.execute("SELECT 1")
        except Exception:
            # Reconnect loop with 3s backoff
            await asyncio.sleep(3)


async def send_to_user_by_email(email: str, payload: str):
    """Send a message to a user identified by email across all workers."""
    email = email.strip().lower()
    username = await get_username_by_email(email)
    if username:
        await send_ws_to_user(username, payload)


async def get_username_by_email(email: str) -> str:
    """Look up the current username for an email address (memory cache first, then DB)."""
    from database.postgres import execute_pg_query
    email = email.strip().lower()
    if email in email_to_username:
        return email_to_username[email]
    res = await execute_pg_query("SELECT username FROM users WHERE email = $1", email)
    if res and res[0].get("username"):
        username = res[0]["username"].strip().lower()
        email_to_username[email] = username
        username_to_email[username] = email
        return username
    return ""


async def get_email_by_username(username: str) -> str:
    """Look up email for a given username (cached, email, & email-prefix fallback)."""
    if not username:
        return ""
    username = username.strip().lower()
    if username in username_to_email:
        return username_to_email[username]
    from database.postgres import execute_pg_query
    
    # 1. Direct username lookup
    res = await execute_pg_query("SELECT email, username FROM users WHERE LOWER(username) = $1", username)
    if res and res[0].get("email"):
        email = res[0]["email"].strip().lower()
        db_uname = res[0]["username"].strip().lower()
        username_to_email[username] = email
        username_to_email[db_uname] = email
        email_to_username[email] = db_uname
        return email
        
    # 2. Direct email lookup
    if "@" in username:
        res = await execute_pg_query("SELECT email, username FROM users WHERE LOWER(email) = $1", username)
        if res and res[0].get("email"):
            email = res[0]["email"].strip().lower()
            db_uname = res[0]["username"].strip().lower()
            username_to_email[username] = email
            username_to_email[db_uname] = email
            email_to_username[email] = db_uname
            return email

    # 3. Email prefix lookup (e.g. candidate username matching user's email prefix)
    res = await execute_pg_query("SELECT email, username FROM users WHERE LOWER(split_part(email, '@', 1)) = $1", username)
    if res and res[0].get("email"):
        email = res[0]["email"].strip().lower()
        db_uname = res[0]["username"].strip().lower()
        username_to_email[username] = email
        username_to_email[db_uname] = email
        email_to_username[email] = db_uname
        return email

    return ""


async def get_user_friends(username: str):
    """Return list of friend usernames for a given user."""
    from database.postgres import execute_pg_query
    username = username.strip().lower()
    try:
        user_res = await execute_pg_query("SELECT id FROM users WHERE username = $1", username)
        if not user_res:
            return []
        user_id = user_res[0]["id"]
        friend_rows = await execute_pg_query(
            "SELECT user_id, friend_id FROM chat_friends WHERE user_id = $1 OR friend_id = $1",
            user_id
        )
        friend_ids = []
        for r in friend_rows:
            fid = r["friend_id"] if r["user_id"] == user_id else r["user_id"]
            friend_ids.append(fid)
        if not friend_ids:
            return []
        friends = await execute_pg_query(
            "SELECT username FROM users WHERE id = ANY($1::integer[])",
            friend_ids
        )
        return [f["username"].strip().lower() for f in friends if f.get("username")]
    except Exception as e:
        print(f"Error getting user friends for status broadcast: {e}")
        return []


async def broadcast_user_status(username: str, status: str, last_seen: str):
    """Broadcast online/offline status to all friends of the user across all workers."""
    username = username.strip().lower()
    friends = await get_user_friends(username)
    if not friends:
        return
    payload = json.dumps({
        "type": "user_status",
        "username": username,
        "status": status,
        "last_seen": last_seen
    })
    await send_ws_to_users(friends, payload)
