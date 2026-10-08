import json
import time
import asyncio
from typing import Dict, Optional, Any, Set
from fastapi import WebSocket

# Active WebSocket connections: username -> set of WebSocket connections
active_connections: Dict[str, set] = {}
connection_metadata: Dict[WebSocket, dict] = {}

# Resource Invalidation Pub/Sub: resource_key (e.g. "doc:977ViYdH", "note:ABC123") -> set of WebSockets
resource_subscribers: Dict[str, set] = {}
socket_subscriptions: Dict[WebSocket, set] = {}

# Server-Side Coalescing State: r_key -> latest payload dict, r_key -> asyncio Task
_coalesce_buffer: Dict[str, dict] = {}
_coalesce_tasks: Dict[str, asyncio.Task] = {}

# In-memory username <-> email mapping cache (updated when users connect)
username_to_email: Dict[str, str] = {}  # username -> email
email_to_username: Dict[str, str] = {}  # email -> username (current)

# Buffered call signaling messages (e.g. call_offer, ice_candidate) for offline or transitioning users
buffered_call_signals: Dict[str, list] = {}


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


async def start_pg_invalidation_listener():
    """
    Background worker that listens on PostgreSQL channel 'spac2_resource_changed'.
    Ensures that invalidation pings published on any API instance are fanned out
    to WebSockets on this instance.
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
            print("[PG Listen] Connected to PostgreSQL NOTIFY channel 'spac2_resource_changed'")

            def on_notification(connection, pid, channel, payload):
                try:
                    data = json.loads(payload)
                    asyncio.create_task(coalesce_and_dispatch_invalidation(data))
                except Exception as err:
                    print(f"[PG Listen] Error parsing notification payload: {err}")

            await conn.add_listener("spac2_resource_changed", on_notification)

            while not conn.is_closed():
                await asyncio.sleep(15)
                await conn.execute("SELECT 1")
        except Exception as e:
            # Reconnect loop with 3s backoff
            await asyncio.sleep(3)


async def send_to_user_by_email(email: str, payload: str):
    """Send a message to a user identified by email, looking up their current username."""
    email = email.strip().lower()
    username = email_to_username.get(email)
    if username and username in active_connections:
        for ws in list(active_connections[username]):
            try:
                await ws.send_text(payload)
            except Exception:
                pass


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
    """Broadcast online/offline status to all friends of the user."""
    import json
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
    for friend in friends:
        if friend in active_connections:
            for ws in list(active_connections[friend]):
                try:
                    await ws.send_text(payload)
                except Exception:
                    pass
