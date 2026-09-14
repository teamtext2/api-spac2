import sqlite3
import re
import json
from datetime import datetime, timezone


def execute_sqlite_query(query: str, *args):
    """SQLite fallback database handler. Mirrors the PostgreSQL API as closely as possible."""
    with sqlite3.connect("local_mock_chat.db") as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Ensure tables and schema exist
        _ensure_sqlite_schema(cursor, conn)

        sql = query.strip()

        if sql.startswith("INSERT INTO chat_friends"):
            cursor.execute(
                "INSERT OR IGNORE INTO chat_friends (user_id, friend_id, created_at) VALUES (?, ?, ?)",
                [args[0], args[1], datetime.now(timezone.utc).isoformat()]
            )
            conn.commit()
            return [{"success": True}]

        elif "FROM chat_friends" in sql:
            cursor.execute(
                "SELECT user_id, friend_id FROM chat_friends WHERE user_id = ? OR friend_id = ?",
                [args[0], args[0]]
            )
            return [dict(r) for r in cursor.fetchall()]

        elif sql.startswith("DELETE FROM chat_friends"):
            cursor.execute(
                "DELETE FROM chat_friends WHERE user_id = ? AND friend_id = ?",
                [args[0], args[1]]
            )
            conn.commit()
            return [{"success": True}]

        elif "id = ANY($1::varchar[])" in sql:
            ids = args[0]
            if ids:
                placeholders = ",".join(["?"] * len(ids))
                cursor.execute(
                    f"SELECT id, name, avatar FROM chat_groups WHERE id IN ({placeholders})",
                    [str(i) for i in ids]
                )
                return [dict(r) for r in cursor.fetchall()]
            return []

        elif sql.startswith("INSERT INTO messages"):
            created_at_str = args[4].isoformat() if hasattr(args[4], "isoformat") else str(args[4])
            reply_to = args[5] if len(args) > 5 else None
            forwarded_from = args[6] if len(args) > 6 else None
            cursor.execute(
                "INSERT INTO messages (id, sender_id, recipient_id, content, created_at, delivered, reply_to, forwarded_from) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                [str(args[0]), args[1], args[2], args[3], created_at_str, reply_to, forwarded_from]
            )
            last_id = cursor.lastrowid
            cursor.execute("UPDATE messages SET update_id = ? WHERE id = ?", [last_id, str(args[0])])
            conn.commit()
            return [{"success": True, "update_id": last_id}]

        elif "ANY($1::uuid[])" in sql:
            ids = args[0]
            if ids:
                placeholders = ",".join(["?"] * len(ids))
                cursor.execute(
                    f"UPDATE messages SET delivered = 1 WHERE id IN ({placeholders})",
                    [str(i) for i in ids]
                )
                conn.commit()
            return [{"success": True}]

        elif sql.startswith("UPDATE messages SET delivered = TRUE WHERE id = $1"):
            cursor.execute("UPDATE messages SET delivered = 1 WHERE id = ?", [str(args[0])])
            conn.commit()
            return [{"success": True}]

        elif "jsonb_set" in sql:
            sender, reaction, msg_uuid = args[0], args[1], args[2]
            cursor.execute("SELECT reactions FROM messages WHERE id = ?", [str(msg_uuid)])
            row = cursor.fetchone()
            reactions_dict = {}
            if row and row["reactions"]:
                try:
                    reactions_dict = json.loads(row["reactions"])
                except Exception:
                    pass
            reactions_dict[sender] = reaction
            cursor.execute(
                "UPDATE messages SET reactions = ? WHERE id = ?",
                [json.dumps(reactions_dict), str(msg_uuid)]
            )
            conn.commit()
            return [{"success": True}]

        elif "COALESCE(reactions, '{}'::jsonb) - $1" in sql:
            sender, msg_uuid = args[0], args[1]
            cursor.execute("SELECT reactions FROM messages WHERE id = ?", [str(msg_uuid)])
            row = cursor.fetchone()
            reactions_dict = {}
            if row and row["reactions"]:
                try:
                    reactions_dict = json.loads(row["reactions"])
                except Exception:
                    pass
            if sender in reactions_dict:
                del reactions_dict[sender]
            cursor.execute(
                "UPDATE messages SET reactions = ? WHERE id = ?",
                [json.dumps(reactions_dict), str(msg_uuid)]
            )
            conn.commit()
            return [{"success": True}]

        elif sql.startswith("SELECT reactions FROM messages"):
            cursor.execute("SELECT reactions FROM messages WHERE id = ?", [str(args[0])])
            row = cursor.fetchone()
            return [dict(row)] if row else []

        elif "recipient_id = ANY($3::varchar[])" in sql:
            return _handle_sync_query(cursor, sql, args)

        elif "recipient_id = $1 AND delivered = FALSE" in sql:
            return _handle_undelivered_query(cursor, args)

        elif "created_at DESC LIMIT" in sql or "ORDER BY created_at DESC LIMIT" in sql:
            return _handle_history_query(cursor, sql, args)

        else:
            return _handle_generic_query(cursor, conn, sql, args)


def _handle_sync_query(cursor, sql, args):
    username = args[0]
    since_val = args[1]
    user_groups = args[2]

    is_seq_query = "update_id >" in sql
    compare_col = "update_id" if is_seq_query else "created_at"

    if is_seq_query:
        compare_val = int(since_val) if since_val is not None else 0
    else:
        compare_val = since_val.isoformat() if hasattr(since_val, "isoformat") else str(since_val)

    if user_groups:
        placeholders = ",".join(["?"] * len(user_groups))
        sqlite_sql = f"""
            SELECT id, sender_id AS sender, recipient_id AS recipient, content AS text, 
                   created_at, read_at, reactions, delivered, update_id, reply_to, forwarded_from
            FROM messages
            WHERE (sender_id = ? OR recipient_id = ? OR recipient_id IN ({placeholders})) AND {compare_col} > ?
            ORDER BY {compare_col} ASC
        """
        cursor.execute(sqlite_sql, [username, username] + user_groups + [compare_val])
    else:
        sqlite_sql = f"""
            SELECT id, sender_id AS sender, recipient_id AS recipient, content AS text, 
                   created_at, read_at, reactions, delivered, update_id, reply_to, forwarded_from
            FROM messages
            WHERE (sender_id = ? OR recipient_id = ?) AND {compare_col} > ?
            ORDER BY {compare_col} ASC
        """
        cursor.execute(sqlite_sql, [username, username, compare_val])

    rows = cursor.fetchall()
    res = []
    for r in rows:
        d = dict(r)
        dt_str = d["created_at"]
        try:
            d["created_at"] = datetime.fromisoformat(dt_str)
        except Exception:
            d["created_at"] = datetime.now(timezone.utc)
        res.append(d)
    return res


def _handle_undelivered_query(cursor, args):
    cursor.execute("""
        SELECT id, sender_id AS sender, recipient_id AS recipient, content AS text, 
               created_at, read_at, reactions, delivered, reply_to, forwarded_from
        FROM messages
        WHERE recipient_id = ? AND delivered = 0
        ORDER BY created_at ASC
    """, [args[0]])
    rows = cursor.fetchall()
    res = []
    for r in rows:
        d = dict(r)
        dt_str = d["created_at"]
        try:
            d["created_at"] = datetime.fromisoformat(dt_str)
        except Exception:
            d["created_at"] = datetime.now(timezone.utc)
        res.append(d)
    return res


def _handle_history_query(cursor, sql, args):
    is_group = "recipient_id = $1" in sql
    has_before = "created_at <" in sql

    params = []
    if is_group:
        partner = args[0]
        if has_before:
            before_dt = args[1]
            limit = args[2]
            before_str = before_dt.isoformat() if hasattr(before_dt, "isoformat") else str(before_dt)
            sqlite_sql = """
                SELECT id, sender_id AS sender, recipient_id AS recipient, content AS text, 
                       created_at, read_at, reactions, delivered, reply_to, forwarded_from
                FROM messages
                WHERE recipient_id = ? AND created_at < ?
                ORDER BY created_at DESC LIMIT ?
            """
            params = [partner, before_str, limit]
        else:
            limit = args[1]
            sqlite_sql = """
                SELECT id, sender_id AS sender, recipient_id AS recipient, content AS text, 
                       created_at, read_at, reactions, delivered, reply_to, forwarded_from
                FROM messages
                WHERE recipient_id = ?
                ORDER BY created_at DESC LIMIT ?
            """
            params = [partner, limit]
    else:
        user1, user2 = args[0], args[1]
        if has_before:
            before_dt = args[2]
            limit = args[3]
            before_str = before_dt.isoformat() if hasattr(before_dt, "isoformat") else str(before_dt)
            sqlite_sql = """
                SELECT id, sender_id AS sender, recipient_id AS recipient, content AS text, 
                       created_at, read_at, reactions, delivered, reply_to, forwarded_from
                FROM messages
                WHERE ((sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?)) AND created_at < ?
                ORDER BY created_at DESC LIMIT ?
            """
            params = [user1, user2, user2, user1, before_str, limit]
        else:
            limit = args[2]
            sqlite_sql = """
                SELECT id, sender_id AS sender, recipient_id AS recipient, content AS text, 
                       created_at, read_at, reactions, delivered, reply_to, forwarded_from
                FROM messages
                WHERE ((sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?))
                ORDER BY created_at DESC LIMIT ?
            """
            params = [user1, user2, user2, user1, limit]

    cursor.execute(sqlite_sql, params)
    rows = cursor.fetchall()
    res = []
    for r in rows:
        d = dict(r)
        dt_str = d["created_at"]
        try:
            d["created_at"] = datetime.fromisoformat(dt_str)
        except Exception:
            d["created_at"] = datetime.now(timezone.utc)
        res.append(d)
    return res


def _handle_generic_query(cursor, conn, sql, args):
    """General fallback SQL translation from PostgreSQL syntax to SQLite."""
    sqlite_sql = sql
    sqlite_args = list(args)

    # Translate ILIKE to LIKE for SQLite compatibility
    sqlite_sql = re.sub(r'\bilike\b', 'LIKE', sqlite_sql, flags=re.IGNORECASE)

    # Translate = ANY($X::type[]) -> IN (?, ?, ...)
    any_matches = list(re.finditer(
        r'([a-zA-Z0-9_]+)\s*=\s*ANY\(\$(\d+)::[a-zA-Z0-9_\[\]]+\)',
        sqlite_sql, re.IGNORECASE
    ))
    if any_matches:
        for match in sorted(any_matches, key=lambda m: m.start(), reverse=True):
            col_name = match.group(1)
            param_idx = int(match.group(2)) - 1
            arr_val = args[param_idx] if param_idx < len(args) else []
            if not isinstance(arr_val, (list, tuple, set)):
                arr_val = [arr_val]
            arr_val = list(arr_val)
            if arr_val:
                placeholders = ",".join(["?"] * len(arr_val))
                in_clause = f"{col_name} IN ({placeholders})"
            else:
                in_clause = "1=0"
            start, end = match.span()
            sqlite_sql = sqlite_sql[:start] + in_clause + sqlite_sql[end:]
            sqlite_args = sqlite_args[:param_idx] + arr_val + sqlite_args[param_idx + 1:]

    # Strip PostgreSQL to_jsonb(...) function
    sqlite_sql = re.sub(r'\bto_jsonb\((.*?)\)', r'\1', sqlite_sql, flags=re.IGNORECASE)

    # Strip PostgreSQL type casts like ::jsonb, ::text, ::varchar, ::int, etc.
    sqlite_sql = re.sub(r'::[a-zA-Z0-9_]+(\[\])?', '', sqlite_sql)

    # Map $1, $2, etc. correctly in order of appearance
    param_indices = [int(m.group(1)) - 1 for m in re.finditer(r'\$(\d+)', sqlite_sql)]
    if param_indices:
        sqlite_args = [args[idx] for idx in param_indices if idx < len(args)]
        sqlite_sql = re.sub(r'\$\d+', '?', sqlite_sql)

    try:
        if sqlite_sql.strip().upper().startswith("SELECT"):
            cursor.execute(sqlite_sql, sqlite_args)
            return [dict(r) for r in cursor.fetchall()]
        else:
            cursor.execute(sqlite_sql, sqlite_args)
            conn.commit()
            return [{"success": True}]
    except Exception as sqlite_err:
        print(f"[PG FALLBACK WARNING] Unhandled query mapping or SQLite error for: {sql}. Error: {sqlite_err}")
        return []


def _ensure_sqlite_schema(cursor, conn):
    """Create all required SQLite tables and columns if missing."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            sender_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivered INTEGER DEFAULT 0,
            read_at TEXT,
            reactions TEXT DEFAULT '{}',
            reply_to TEXT,
            forwarded_from TEXT
        );
    """)

    cursor.execute("PRAGMA table_info(messages)")
    columns = [row[1] for row in cursor.fetchall()]
    if "update_id" not in columns:
        try:
            cursor.execute("ALTER TABLE messages ADD COLUMN update_id INTEGER;")
            cursor.execute("UPDATE messages SET update_id = rowid;")
            conn.commit()
        except Exception as se:
            print(f"Failed to add update_id column in SQLite: {se}")

    if "reply_to" not in columns:
        try:
            cursor.execute("ALTER TABLE messages ADD COLUMN reply_to TEXT;")
            conn.commit()
        except Exception as se:
            print(f"Failed to add reply_to column in SQLite: {se}")

    if "forwarded_from" not in columns:
        try:
            cursor.execute("ALTER TABLE messages ADD COLUMN forwarded_from TEXT;")
            conn.commit()
        except Exception as se:
            print(f"Failed to add forwarded_from column in SQLite: {se}")

    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS trigger_bump_messages_update_id
        AFTER UPDATE OF read_at, reactions, delivered ON messages
        BEGIN
            UPDATE messages SET update_id = (SELECT COALESCE(MAX(update_id), 0) + 1 FROM messages)
            WHERE id = NEW.id;
        END;
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            username TEXT UNIQUE NOT NULL,
            name TEXT,
            bio TEXT,
            email TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'online',
            avatar TEXT,
            password_hash TEXT,
            google_id TEXT,
            last_seen TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cursor.execute("PRAGMA table_info(users)")
    central_user_cols = [row[1] for row in cursor.fetchall()]
    for col, col_type in [("password_hash", "TEXT"), ("google_id", "TEXT"), ("last_seen", "TEXT"), ("user_id", "INTEGER")]:
        if col not in central_user_cols:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type};")
                conn.commit()
            except Exception as se:
                print(f"Failed to add {col} column to users in SQLite: {se}")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            name TEXT,
            bio TEXT,
            email TEXT UNIQUE NOT NULL,
            status TEXT,
            avatar TEXT,
            google_id TEXT,
            last_seen TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cursor.execute("PRAGMA table_info(chat_users)")
    user_columns = [row[1] for row in cursor.fetchall()]
    for col, col_type in [("google_id", "TEXT"), ("last_seen", "TEXT")]:
        if col not in user_columns:
            try:
                cursor.execute(f"ALTER TABLE chat_users ADD COLUMN {col} {col_type};")
                conn.commit()
            except Exception as se:
                print(f"Failed to add {col} column to chat_users in SQLite: {se}")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_friends (
            user_id INTEGER NOT NULL,
            friend_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, friend_id)
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_groups (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            avatar TEXT,
            created_by TEXT NOT NULL,
            created_by_email TEXT,
            created_at TEXT NOT NULL
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_group_members (
            group_id TEXT NOT NULL,
            email TEXT NOT NULL,
            username TEXT,
            role TEXT DEFAULT 'member',
            joined_at TEXT NOT NULL,
            PRIMARY KEY (group_id, email),
            FOREIGN KEY (group_id) REFERENCES chat_groups(id) ON DELETE CASCADE
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT UNIQUE,
            username TEXT,
            endpoint TEXT UNIQUE NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_push_subscriptions_username ON chat_push_subscriptions(username);")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_sync_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            note_id TEXT NOT NULL,
            title TEXT DEFAULT '',
            content TEXT DEFAULT '',
            color TEXT DEFAULT '{}',
            is_saved INTEGER DEFAULT 0,
            date TEXT DEFAULT '',
            rev INTEGER NOT NULL DEFAULT 1,
            is_deleted INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, note_id)
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_notes_user_rev ON user_sync_notes(user_id, rev);")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_sync_task_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            project_id TEXT NOT NULL,
            name TEXT DEFAULT '',
            color TEXT DEFAULT '{}',
            created_at_str TEXT DEFAULT '',
            rev INTEGER NOT NULL DEFAULT 1,
            is_deleted INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, project_id)
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_task_projects_user_rev ON user_sync_task_projects(user_id, rev);")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_sync_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            title TEXT DEFAULT '',
            note TEXT DEFAULT '',
            priority TEXT DEFAULT 'normal',
            due_date TEXT DEFAULT '',
            completed INTEGER DEFAULT 0,
            completed_at TEXT DEFAULT '',
            subtasks TEXT DEFAULT '[]',
            date TEXT DEFAULT '',
            rev INTEGER NOT NULL DEFAULT 1,
            is_deleted INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, task_id)
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_tasks_user_rev ON user_sync_tasks(user_id, rev);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_tasks_user_project ON user_sync_tasks(user_id, project_id);")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_sync_calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            date TEXT NOT NULL,
            title TEXT DEFAULT '',
            description TEXT DEFAULT '',
            start_time TEXT DEFAULT '',
            end_time TEXT DEFAULT '',
            color TEXT DEFAULT '{}',
            is_all_day INTEGER DEFAULT 0,
            location TEXT DEFAULT '',
            recurrence TEXT DEFAULT '',
            rev INTEGER NOT NULL DEFAULT 1,
            is_deleted INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, event_id)
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_calendar_events_user_rev ON user_sync_calendar_events(user_id, rev);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_calendar_events_user_date ON user_sync_calendar_events(user_id, date);")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_sync_countday_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            title TEXT DEFAULT '',
            target_date TEXT DEFAULT '',
            order_index INTEGER DEFAULT 0,
            rev INTEGER NOT NULL DEFAULT 1,
            is_deleted INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, event_id)
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_countday_events_user_rev ON user_sync_countday_events(user_id, rev);")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_sync_mindmap_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            project_id TEXT NOT NULL,
            name TEXT DEFAULT '',
            data TEXT DEFAULT '{}',
            created_at_str TEXT DEFAULT '',
            rev INTEGER NOT NULL DEFAULT 1,
            is_deleted INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, project_id)
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_mindmap_projects_user_rev ON user_sync_mindmap_projects(user_id, rev);")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_sync_table_projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            project_id TEXT NOT NULL,
            name TEXT DEFAULT '',
            data TEXT DEFAULT '{}',
            last_edited INTEGER DEFAULT 0,
            rev INTEGER NOT NULL DEFAULT 1,
            is_deleted INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, project_id)
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_table_projects_user_rev ON user_sync_table_projects(user_id, rev);")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_sync_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            doc_id TEXT NOT NULL,
            title TEXT DEFAULT '',
            body TEXT DEFAULT '',
            preview_text TEXT DEFAULT '',
            word_count INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0,
            in_trash INTEGER DEFAULT 0,
            target INTEGER DEFAULT 500,
            tabs TEXT DEFAULT '[]',
            active_tab_id TEXT DEFAULT 'tab-default',
            history TEXT DEFAULT '[]',
            rev INTEGER NOT NULL DEFAULT 1,
            is_deleted INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at INTEGER DEFAULT 0,
            UNIQUE (user_id, doc_id)
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_docs_user_rev ON user_sync_docs(user_id, rev);")








