import asyncpg
from config import POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
from database.sqlite import execute_sqlite_query

pg_pool = None


async def get_pg_pool():
    return pg_pool


async def execute_pg_query(query: str, *args):
    """Execute a PostgreSQL query, falling back to SQLite for local development."""
    global pg_pool
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
                google_id VARCHAR(255),
                last_seen VARCHAR(255),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS user_id BIGINT UNIQUE;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id VARCHAR(255);")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen VARCHAR(255);")
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
                INSERT INTO users (user_id, username, name, bio, email, status, avatar, google_id, last_seen, created_at)
                SELECT COALESCE(user_id, 10000 + id), username, name, bio, email, status, avatar, google_id, last_seen, created_at
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
                created_by VARCHAR(255) NOT NULL,
                created_by_email VARCHAR(255),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

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

        print("PostgreSQL schema initialized successfully!")




