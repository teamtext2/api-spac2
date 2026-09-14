from typing import Dict
from fastapi import WebSocket

# Active WebSocket connections: username -> set of WebSocket connections
active_connections: Dict[str, set] = {}
connection_metadata: Dict[WebSocket, dict] = {}

# In-memory username <-> email mapping cache (updated when users connect)
username_to_email: Dict[str, str] = {}  # username -> email
email_to_username: Dict[str, str] = {}  # email -> username (current)

# Buffered call signaling messages (e.g. call_offer, ice_candidate) for offline or transitioning users
buffered_call_signals: Dict[str, list] = {}


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
