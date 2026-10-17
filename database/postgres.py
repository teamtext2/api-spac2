import contextvars
import time
from typing import Any, Optional
import asyncpg
from config import POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
from database.sqlite import execute_sqlite_query

pg_pool = None
_current_pg_conn: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar("_current_pg_conn", default=None)


async def get_pg_pool():
    return pg_pool


async def execute_pg_query(query: str, *args):
    """Execute a PostgreSQL query, binding to active transaction if present, falling back to SQLite for local development."""
    global pg_pool
    # 1. Bind to active transaction connection if within an atomic batch transaction
    active_conn = _current_pg_conn.get()
    if active_conn:
        if query.strip().upper().startswith("SELECT"):
            records = await active_conn.fetch(query, *args)
            return [dict(r) for r in records]
        else:
            await active_conn.execute(query, *args)
            return [{"success": True}]

    # 2. Acquire from pool for standalone query
    if pg_pool:
        try:
            async with pg_pool.acquire() as conn:
                if query.strip().upper().startswith("SELECT"):
                    records = await conn.fetch(query, *args)
                    return [dict(r) for r in records]
                else:
                    await conn.execute(query, *args)
                    return [{"success": True}]
        except Exception as e:
            print(f"PostgreSQL query error, attempting local SQLite fallback: {e}")

    # SQLite FALLBACK FOR LOCAL DEV
    return execute_sqlite_query(query, *args)


async def initialize_pg_pool():
    """Create the PostgreSQL connection pool. Called at startup."""
    global pg_pool
    try:
        pg_pool = await asyncpg.create_pool(
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            database=POSTGRES_DB,
            min_size=1,
            max_size=10
        )
        print("PostgreSQL connection pool created successfully!")
    except Exception as e:
        print(f"Failed to initialize PostgreSQL pool: {e}")
        pg_pool = None


async def initialize_pg_schema():
    """Create all required tables and indexes in PostgreSQL."""
    global pg_pool
    if not pg_pool:
        return
    async with pg_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id UUID PRIMARY KEY,
                sender_id VARCHAR(255) NOT NULL,
                recipient_id VARCHAR(255) NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                delivered BOOLEAN DEFAULT FALSE,
                read_at TIMESTAMP WITH TIME ZONE,
                reactions JSONB DEFAULT '{}',
                reply_to VARCHAR(255),
                forwarded_from VARCHAR(255)
            );
        """)
        await conn.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS read_at TIMESTAMP WITH TIME ZONE;")
        await conn.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS reactions JSONB DEFAULT '{}';")
        await conn.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS reply_to VARCHAR(255);")
        await conn.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS forwarded_from VARCHAR(255);")
        await conn.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS update_id BIGSERIAL;")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages(sender_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_recipient_id ON messages(recipient_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_update_id ON messages(update_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_recipient_delivered ON messages(recipient_id, delivered);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_pair ON messages(sender_id, recipient_id, created_at);")

        await conn.execute("""
            CREATE OR REPLACE FUNCTION bump_messages_update_id()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.update_id = nextval('messages_update_id_seq');
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)
        await conn.execute("DROP TRIGGER IF EXISTS trigger_bump_messages_update_id ON messages;")
        await conn.execute("""
            CREATE TRIGGER trigger_bump_messages_update_id
            BEFORE UPDATE ON messages
            FOR EACH ROW
            EXECUTE FUNCTION bump_messages_update_id();
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_push_subscriptions (
                id SERIAL PRIMARY KEY,
                device_id VARCHAR(255) UNIQUE,
                username VARCHAR(255),
                endpoint TEXT UNIQUE NOT NULL,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.execute("ALTER TABLE chat_push_subscriptions ADD COLUMN IF NOT EXISTS device_id VARCHAR(255) UNIQUE;")
        await conn.execute("ALTER TABLE chat_push_subscriptions ALTER COLUMN username DROP NOT NULL;")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_push_subscriptions_username ON chat_push_subscriptions(username);")


        # ── Central Ecosystem Users Table (Global Identity) ──────────────────
        await conn.execute("CREATE SEQUENCE IF NOT EXISTS users_user_id_seq START WITH 10001;")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                user_id BIGINT UNIQUE DEFAULT nextval('users_user_id_seq'),
                username VARCHAR(255) UNIQUE NOT NULL,
                name VARCHAR(255),
                bio TEXT,
                email VARCHAR(255) UNIQUE NOT NULL,
                status VARCHAR(50) DEFAULT 'online',
                avatar TEXT,
                password_hash VARCHAR(255),
                last_seen VARCHAR(255),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS user_id BIGINT UNIQUE;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS name VARCHAR(255);")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bio TEXT;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'online';")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar TEXT;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen VARCHAR(255);")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_verified BOOLEAN DEFAULT FALSE;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;")
        await conn.execute("UPDATE users SET user_id = 10000 + id WHERE user_id IS NULL;")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_lower_username ON users(LOWER(username));")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_lower_email ON users(LOWER(email));")

        # ── Safe Data Migration: chat_users -> users & Free Storage ───────────
        # Check if legacy chat_users table exists, copy data to users, then drop chat_users
        table_check = await conn.fetchval("SELECT to_regclass('public.chat_users');")
        if table_check:
            print("Migrating remaining legacy accounts from chat_users to central users table...")
            await conn.execute("""
                INSERT INTO users (user_id, username, name, bio, email, status, avatar, last_seen, created_at)
                SELECT COALESCE(user_id, 10000 + id), username, name, bio, email, status, avatar, last_seen, created_at
                FROM chat_users
                ON CONFLICT (email) DO UPDATE 
                SET username = EXCLUDED.username, 
                    avatar = EXCLUDED.avatar, 
                    user_id = COALESCE(users.user_id, EXCLUDED.user_id);
            """)
            await conn.execute("DROP TABLE IF EXISTS chat_users CASCADE;")
            print("Dropped legacy chat_users table. Storage freed successfully!")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_friends (
                user_id INTEGER NOT NULL,
                friend_id INTEGER NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, friend_id)
            );
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_friends_user_id ON chat_friends(user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_friends_friend_id ON chat_friends(friend_id);")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_groups (
                id VARCHAR(255) PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                avatar TEXT,
                bio TEXT DEFAULT '',
                created_by VARCHAR(255) NOT NULL,
                created_by_email VARCHAR(255),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.execute("ALTER TABLE chat_groups ADD COLUMN IF NOT EXISTS bio TEXT DEFAULT '';")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_group_members (
                group_id VARCHAR(255) NOT NULL,
                email VARCHAR(255) NOT NULL,
                username VARCHAR(255),
                role VARCHAR(50) DEFAULT 'member',
                joined_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, email),
                CONSTRAINT fk_group FOREIGN KEY (group_id) REFERENCES chat_groups(id) ON DELETE CASCADE
            );
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_group_members_group_id ON chat_group_members(group_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_group_members_email ON chat_group_members(email);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_group_members_username ON chat_group_members(username);")

        # ── App Cloud Sync: Notes (Revision-Based Delta Sync) ───────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_sync_notes (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                note_id VARCHAR(100) NOT NULL,
                title TEXT DEFAULT '',
                content TEXT DEFAULT '',
                color JSONB DEFAULT '{}',
                is_saved BOOLEAN DEFAULT FALSE,
                date TEXT DEFAULT '',
                rev BIGINT NOT NULL DEFAULT 1,
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at BIGINT DEFAULT (EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)*1000)::BIGINT,
                UNIQUE (user_id, note_id)
            );
        """)
        try:
            await conn.execute("ALTER TABLE user_sync_notes ADD COLUMN IF NOT EXISTS rev BIGINT NOT NULL DEFAULT 1;")
        except Exception:
            pass
        try:
            await conn.execute("ALTER TABLE user_sync_notes ALTER COLUMN updated_at DROP NOT NULL;")
        except Exception:
            pass
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_notes_user_rev ON user_sync_notes(user_id, rev);")
        except Exception:
            pass

        # ── App Cloud Sync: Tasks & Projects (Revision-Based Delta Sync) ────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_sync_task_projects (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                project_id VARCHAR(100) NOT NULL,
                name TEXT DEFAULT '',
                color JSONB DEFAULT '{}',
                created_at_str TEXT DEFAULT '',
                rev BIGINT NOT NULL DEFAULT 1,
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at BIGINT DEFAULT (EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)*1000)::BIGINT,
                UNIQUE (user_id, project_id)
            );
        """)
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_task_projects_user_rev ON user_sync_task_projects(user_id, rev);")
        except Exception:
            pass

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_sync_tasks (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                task_id VARCHAR(100) NOT NULL,
                project_id VARCHAR(100) NOT NULL DEFAULT '',
                title TEXT DEFAULT '',
                note TEXT DEFAULT '',
                priority VARCHAR(20) DEFAULT 'normal',
                due_date TEXT DEFAULT '',
                completed BOOLEAN DEFAULT FALSE,
                completed_at TEXT DEFAULT '',
                subtasks JSONB DEFAULT '[]',
                date TEXT DEFAULT '',
                rev BIGINT NOT NULL DEFAULT 1,
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at BIGINT DEFAULT (EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)*1000)::BIGINT,
                UNIQUE (user_id, task_id)
            );
        """)
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_tasks_user_rev ON user_sync_tasks(user_id, rev);")
        except Exception:
            pass

        # ── App Cloud Sync: Calendar Events (Revision-Based Delta Sync) ────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_sync_calendar_events (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                event_id VARCHAR(100) NOT NULL,
                date TEXT NOT NULL,
                title TEXT DEFAULT '',
                description TEXT DEFAULT '',
                start_time TEXT DEFAULT '',
                end_time TEXT DEFAULT '',
                color JSONB DEFAULT '{}',
                is_all_day BOOLEAN DEFAULT FALSE,
                location TEXT DEFAULT '',
                recurrence TEXT DEFAULT '',
                rev BIGINT NOT NULL DEFAULT 1,
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at BIGINT DEFAULT (EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)*1000)::BIGINT,
                UNIQUE (user_id, event_id)
            );
        """)
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_calendar_events_user_rev ON user_sync_calendar_events(user_id, rev);")
        except Exception:
            pass

        # ── App Cloud Sync: Countday Events (Revision-Based Delta Sync) ─────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_sync_countday_events (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                event_id VARCHAR(100) NOT NULL,
                title TEXT DEFAULT '',
                target_date TEXT DEFAULT '',
                order_index INTEGER DEFAULT 0,
                rev BIGINT NOT NULL DEFAULT 1,
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at BIGINT DEFAULT (EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)*1000)::BIGINT,
                UNIQUE (user_id, event_id)
            );
        """)
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_countday_events_user_rev ON user_sync_countday_events(user_id, rev);")
        except Exception:
            pass

        # ── App Cloud Sync: Mindmap Projects (Revision-Based Delta Sync) ─────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_sync_mindmap_projects (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                project_id VARCHAR(100) NOT NULL,
                name TEXT DEFAULT '',
                data JSONB DEFAULT '{}',
                created_at_str TEXT DEFAULT '',
                rev BIGINT NOT NULL DEFAULT 1,
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at BIGINT DEFAULT (EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)*1000)::BIGINT,
                UNIQUE (user_id, project_id)
            );
        """)
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_mindmap_projects_user_rev ON user_sync_mindmap_projects(user_id, rev);")
        except Exception:
            pass

        # ── App Cloud Sync: Table Projects (Revision-Based Delta Sync) ───────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_sync_table_projects (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                project_id VARCHAR(100) NOT NULL,
                name TEXT DEFAULT '',
                data JSONB DEFAULT '{}',
                last_edited BIGINT DEFAULT 0,
                rev BIGINT NOT NULL DEFAULT 1,
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at BIGINT DEFAULT (EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)*1000)::BIGINT,
                UNIQUE (user_id, project_id)
            );
        """)
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_table_projects_user_rev ON user_sync_table_projects(user_id, rev);")
        except Exception:
            pass

        # ── App Cloud Sync: Documents (Revision-Based Delta Sync) ───────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_sync_docs (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                doc_id VARCHAR(100) NOT NULL,
                title TEXT DEFAULT '',
                body TEXT DEFAULT '',
                preview_text TEXT DEFAULT '',
                word_count INT DEFAULT 0,
                pinned BOOLEAN DEFAULT FALSE,
                in_trash BOOLEAN DEFAULT FALSE,
                target INT DEFAULT 500,
                tabs JSONB DEFAULT '[]',
                active_tab_id VARCHAR(100) DEFAULT 'tab-default',
                history JSONB DEFAULT '[]',
                rev BIGINT NOT NULL DEFAULT 1,
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at BIGINT DEFAULT (EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)*1000)::BIGINT,
                UNIQUE (user_id, doc_id)
            );
        """)
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_docs_user_rev ON user_sync_docs(user_id, rev);")
        except Exception:
            pass

        # ── Spac2 V2 Architecture: Domain-Scoped Revision Counters ─────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_sync_counters (
                user_id BIGINT NOT NULL,
                app_code VARCHAR(30) NOT NULL,
                current_rev BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, app_code)
            );
        """)
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_counters_user ON user_sync_counters(user_id);")
        except Exception:
            pass

        # ── Spac2 V2 Architecture: Idempotency & Batch Commit Tracking ─────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_sync_batches (
                user_id BIGINT NOT NULL,
                app_code VARCHAR(30) NOT NULL,
                sync_batch_id UUID NOT NULL,
                payload_hash VARCHAR(64) NOT NULL,
                rev BIGINT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, app_code, sync_batch_id)
            );
        """)
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_batches_user_app ON user_sync_batches(user_id, app_code);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_sync_batches_created ON user_sync_batches(created_at);")
        except Exception:
            pass

        # ── Automated Seed & Reconcile: Initial Population from Existing Max Rev ─
        try:
            # Seed doc
            await conn.execute("""
                INSERT INTO user_sync_counters (user_id, app_code, current_rev, updated_at)
                SELECT user_id, 'doc', COALESCE(MAX(rev), 0), CURRENT_TIMESTAMP 
                FROM user_sync_docs GROUP BY user_id
                ON CONFLICT (user_id, app_code) 
                DO UPDATE SET current_rev = GREATEST(user_sync_counters.current_rev, EXCLUDED.current_rev), updated_at = CURRENT_TIMESTAMP;
            """)
            # Seed task (Unified task + task_projects domain)
            await conn.execute("""
                INSERT INTO user_sync_counters (user_id, app_code, current_rev, updated_at)
                SELECT u.user_id, 'task', GREATEST(
                    COALESCE((SELECT MAX(rev) FROM user_sync_tasks t WHERE t.user_id = u.user_id), 0),
                    COALESCE((SELECT MAX(rev) FROM user_sync_task_projects p WHERE p.user_id = u.user_id), 0)
                ), CURRENT_TIMESTAMP
                FROM users u
                ON CONFLICT (user_id, app_code) 
                DO UPDATE SET current_rev = GREATEST(user_sync_counters.current_rev, EXCLUDED.current_rev), updated_at = CURRENT_TIMESTAMP;
            """)
            # Seed note
            await conn.execute("""
                INSERT INTO user_sync_counters (user_id, app_code, current_rev, updated_at)
                SELECT user_id, 'note', COALESCE(MAX(rev), 0), CURRENT_TIMESTAMP 
                FROM user_sync_notes GROUP BY user_id
                ON CONFLICT (user_id, app_code) 
                DO UPDATE SET current_rev = GREATEST(user_sync_counters.current_rev, EXCLUDED.current_rev), updated_at = CURRENT_TIMESTAMP;
            """)
            # Seed calendar
            await conn.execute("""
                INSERT INTO user_sync_counters (user_id, app_code, current_rev, updated_at)
                SELECT user_id, 'calendar', COALESCE(MAX(rev), 0), CURRENT_TIMESTAMP 
                FROM user_sync_calendar_events GROUP BY user_id
                ON CONFLICT (user_id, app_code) 
                DO UPDATE SET current_rev = GREATEST(user_sync_counters.current_rev, EXCLUDED.current_rev), updated_at = CURRENT_TIMESTAMP;
            """)
            # Seed mindmap
            await conn.execute("""
                INSERT INTO user_sync_counters (user_id, app_code, current_rev, updated_at)
                SELECT user_id, 'mindmap', COALESCE(MAX(rev), 0), CURRENT_TIMESTAMP 
                FROM user_sync_mindmap_projects GROUP BY user_id
                ON CONFLICT (user_id, app_code) 
                DO UPDATE SET current_rev = GREATEST(user_sync_counters.current_rev, EXCLUDED.current_rev), updated_at = CURRENT_TIMESTAMP;
            """)
            # Seed table
            await conn.execute("""
                INSERT INTO user_sync_counters (user_id, app_code, current_rev, updated_at)
                SELECT user_id, 'table', COALESCE(MAX(rev), 0), CURRENT_TIMESTAMP 
                FROM user_sync_table_projects GROUP BY user_id
                ON CONFLICT (user_id, app_code) 
                DO UPDATE SET current_rev = GREATEST(user_sync_counters.current_rev, EXCLUDED.current_rev), updated_at = CURRENT_TIMESTAMP;
            """)
            # Seed countday
            await conn.execute("""
                INSERT INTO user_sync_counters (user_id, app_code, current_rev, updated_at)
                SELECT user_id, 'countday', COALESCE(MAX(rev), 0), CURRENT_TIMESTAMP 
                FROM user_sync_countday_events GROUP BY user_id
                ON CONFLICT (user_id, app_code) 
                DO UPDATE SET current_rev = GREATEST(user_sync_counters.current_rev, EXCLUDED.current_rev), updated_at = CURRENT_TIMESTAMP;
            """)
        except Exception as seed_err:
            print(f"[PostgresSchema] Notice during automatic seed reconcile: {seed_err}")

        # ── Spac2 Resource Architecture Contract v1.0 (Visibility, Global Unique, ACLs) ───
        sync_tables_cfg = [
            ("user_sync_docs", "doc_id"),
            ("user_sync_notes", "note_id"),
            ("user_sync_tasks", "task_id"),
            ("user_sync_task_projects", "project_id"),
            ("user_sync_calendar_events", "event_id"),
            ("user_sync_countday_events", "event_id"),
            ("user_sync_mindmap_projects", "project_id"),
            ("user_sync_table_projects", "project_id"),
        ]

        # ── Spac2 Legacy ID Aliasing & Remapping Table ────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_resource_id_aliases (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                app_code VARCHAR(30) NOT NULL,
                legacy_id VARCHAR(100) NOT NULL,
                canonical_id VARCHAR(100) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_user_legacy_alias UNIQUE (user_id, app_code, legacy_id)
            );
        """)

        for tbl, id_col in sync_tables_cfg:
            try:
                await conn.execute(f"ALTER TABLE {tbl} DROP COLUMN IF EXISTS visibility CASCADE;")
                await conn.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL;")
                
                # ── Safe Historical Collision Resolution with Backward Compatible Aliasing ───
                app_code_name = tbl.replace("user_sync_", "").replace("_events", "").replace("_projects", "").replace("s", "")
                if app_code_name == "doc":
                    app_code_name = "doc"
                elif app_code_name == "task":
                    app_code_name = "task"

                dup_rows = await conn.fetch(f"""
                    SELECT t2.id, t2.user_id, t2.{id_col} AS raw_id
                    FROM {tbl} t2
                    JOIN {tbl} t3 ON t2.{id_col} = t3.{id_col} AND t2.user_id != t3.user_id AND t2.id > t3.id
                """)
                for dup in dup_rows:
                    row_db_id = dup["id"]
                    u_id = dup["user_id"]
                    old_id = dup["raw_id"]
                    new_canonical = f"{old_id}_{u_id}_{int(time.time())}"
                    try:
                        await conn.execute("""
                            INSERT INTO user_resource_id_aliases (user_id, app_code, legacy_id, canonical_id, created_at)
                            VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)
                            ON CONFLICT (user_id, app_code, legacy_id) DO NOTHING;
                        """, u_id, app_code_name, old_id, new_canonical)
                        await conn.execute(f"UPDATE {tbl} SET {id_col} = $1 WHERE id = $2;", new_canonical, row_db_id)
                    except Exception as alias_err:
                        print(f"[PostgresSchema] Notice recording alias for {tbl} row {row_db_id}: {alias_err}")

                await conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{tbl}_global_{id_col} ON {tbl} ({id_col});")
            except Exception as tbl_alter_err:
                print(f"[PostgresSchema] Notice altering {tbl} for resource contract: {tbl_alter_err}")

        # Drop obsolete legacy ACL and activity tables
        await conn.execute("DROP TABLE IF EXISTS user_resource_acls CASCADE;")
        await conn.execute("DROP TABLE IF EXISTS user_resource_activity CASCADE;")

        print("PostgreSQL schema and V2 Sync Counters initialized successfully!")




