import os
from pathlib import Path

# Auto-load .env file if available
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
    else:
        load_dotenv()
except ImportError:
    pass

# --- POSTGRESQL CONFIGURATION ---
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "db")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres_pass")
POSTGRES_DB = os.getenv("POSTGRES_DB", "spac2")

# --- CLOUDFLARE R2 CONFIGURATION ---
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "spac2")
R2_CDN_BASE = os.environ.get("R2_CDN_BASE", "https://cdn1.spac2.com").rstrip("/")
R2_AVATAR_CDN_BASE = os.environ.get("R2_AVATAR_CDN_BASE", "https://cdn2.spac2.com").rstrip("/")

# --- VAPID (Push Notifications) ---
VAPID_PUBLIC_KEY = os.getenv(
    "VAPID_PUBLIC_KEY",
    "BDWTIGkOJOX0kctPXKTH3W0ifYyflWMUtmax6gQi4GMPq_hoXO_R_efGWmCm_AnmJIHtxtLdMlN4linlMunVMeA"
)
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "s0QgLuXwdttdBL6sZ7ZO2GkYH71wA8Iy507THaZ0JpI")
VAPID_CLAIMS = {"sub": os.getenv("VAPID_SUB", "mailto:admin@spac2.com")}

# --- LOCAL FILE STORAGE FALLBACK ---
LOCAL_DIR = "./data/profiles"
LOCAL_AVATARS_DIR = "./data/avatar"
LOCAL_UPLOADS_DIR = "./data/uploads"
LOCAL_CHAT_DIR = "./data/chat"

# --- SPAC2 UNIFIED AUTHENTICATION (JWT) ---
SPAC2_JWT_SECRET = os.getenv("SPAC2_JWT_SECRET", os.getenv("TEXT2_JWT_SECRET", "spac2_ecosystem_super_secret_jwt_key_2026_x89a!"))
SPAC2_TOKEN_EXPIRE_DAYS = int(os.getenv("SPAC2_TOKEN_EXPIRE_DAYS", os.getenv("TEXT2_TOKEN_EXPIRE_DAYS", "30")))

# Backward-compatible aliases
TEXT2_JWT_SECRET = SPAC2_JWT_SECRET
TEXT2_TOKEN_EXPIRE_DAYS = SPAC2_TOKEN_EXPIRE_DAYS



