import os

# --- POSTGRESQL CONFIGURATION ---
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "db")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres_pass")
POSTGRES_DB = os.getenv("POSTGRES_DB", "text2chat")

# --- CLOUDFLARE R2 CONFIGURATION ---
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")
R2_CDN_BASE = os.environ.get("R2_CDN_BASE", "https://cdn.text2os.com").rstrip("/")

# --- VAPID (Push Notifications) ---
VAPID_PUBLIC_KEY = os.getenv(
    "VAPID_PUBLIC_KEY",
    "BDWTIGkOJOX0kctPXKTH3W0ifYyflWMUtmax6gQi4GMPq_hoXO_R_efGWmCm_AnmJIHtxtLdMlN4linlMunVMeA"
)
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "s0QgLuXwdttdBL6sZ7ZO2GkYH71wA8Iy507THaZ0JpI")
VAPID_CLAIMS = {"sub": "mailto:admin@text2.co"}

# --- LOCAL FILE STORAGE FALLBACK ---
LOCAL_DIR = "./data/profiles"
LOCAL_AVATARS_DIR = "./data/avatars"
LOCAL_UPLOADS_DIR = "./data/uploads"

# --- TEXT2 UNIFIED AUTHENTICATION (JWT) ---
TEXT2_JWT_SECRET = os.getenv("TEXT2_JWT_SECRET", "text2_ecosystem_super_secret_jwt_key_2026_x89a!")
TEXT2_TOKEN_EXPIRE_DAYS = int(os.getenv("TEXT2_TOKEN_EXPIRE_DAYS", "30"))

