import os
import io
import base64
import re
import time
import httpx
import boto3
from botocore.config import Config

try:
    from PIL import Image
except ImportError:
    Image = None

from config import (
    CLOUDFLARE_ACCOUNT_ID,
    AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY,
    R2_BUCKET_NAME,
    R2_CDN_BASE,
    R2_AVATAR_CDN_BASE,
    LOCAL_AVATARS_DIR,
    LOCAL_CHAT_DIR,
    LOCAL_UPLOADS_DIR,
)

# --- Initialize R2 client at module load ---
r2_client = None

if CLOUDFLARE_ACCOUNT_ID and AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
    endpoint_url = f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
    try:
        r2_client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            config=Config(signature_version="s3v4")
        )
        print("Cloudflare R2 client initialized successfully!")
    except Exception as e:
        print(f"Failed to initialize Cloudflare R2 client: {e}")
else:
    print("Cloudflare R2 credentials not fully configured in environment. Fallback to local files.")


def compress_image_if_needed(avatar_bytes: bytes, max_dim: int = 512, quality: int = 85) -> tuple:
    """
    Server-side image optimizer using Pillow:
    Resizes avatar to a standard square (max 512x512), strips EXIF,
    and encodes to WebP format for minimal bandwidth and fastest CDN loading.
    Returns (optimized_bytes, content_type, extension).
    """
    if not Image:
        print("[AVATAR SERVER] Pillow is not available, using raw bytes.")
        return avatar_bytes, "image/jpeg", "jpg"

    try:
        img = Image.open(io.BytesIO(avatar_bytes))
        
        # Convert transparent/palette modes to RGBA/RGB
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGBA", img.size, (255, 255, 255, 0))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if len(img.split()) == 4 else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Resize if larger than max_dim
        w, h = img.size
        if w > max_dim or h > max_dim:
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.ANTIALIAS)

        out_io = io.BytesIO()
        try:
            img.save(out_io, format="WEBP", quality=quality, method=6)
            optimized_bytes = out_io.getvalue()
            content_type = "image/webp"
            ext = "webp"
        except Exception:
            # Fallback to JPEG if WebP encoder not available in Pillow build
            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            out_io = io.BytesIO()
            img.save(out_io, format="JPEG", quality=quality, optimize=True)
            optimized_bytes = out_io.getvalue()
            content_type = "image/jpeg"
            ext = "jpg"

        orig_kb = len(avatar_bytes) / 1024
        opt_kb = len(optimized_bytes) / 1024
        print(f"[AVATAR SERVER] Compressed on server: {orig_kb:.1f} KB -> {opt_kb:.1f} KB ({ext.upper()})")
        return optimized_bytes, content_type, ext
    except Exception as e:
        print(f"[AVATAR SERVER] Image compression notice: {e}. Using original bytes.")
        return avatar_bytes, "image/jpeg", "jpg"


def process_and_upload_avatar(username: str, avatar_src: str, base_url: str = "") -> str:
    """
    Process an avatar source (base64 data URL or remote URL), optimize/compress on server,
    upload to Cloudflare R2 (or fallback to local storage), and return the public CDN URL.
    Folder on R2: avatar/
    CDN Base: https://cdn2.spac2.com (via R2_AVATAR_CDN_BASE)
    """
    if not avatar_src:
        return ""

    # Already on our avatar CDN or local static — no re-upload needed
    if (
        f"{R2_AVATAR_CDN_BASE}/avatar/" in avatar_src
        or f"{R2_CDN_BASE}/avatar/" in avatar_src
        or f"{R2_CDN_BASE}/avatars/" in avatar_src
        or "/data/avatar/" in avatar_src
        or "/data/avatars/" in avatar_src
    ):
        return avatar_src

    avatar_bytes = None
    initial_content_type = "image/jpeg"

    if avatar_src.startswith("data:image/"):
        try:
            pattern = re.compile(r"^data:(image/[^;]+);base64,(.*)$")
            match = pattern.match(avatar_src)
            if match:
                initial_content_type = match.group(1)
                avatar_bytes = base64.b64decode(match.group(2))
        except Exception as e:
            print(f"[AVATAR SERVER] Error decoding base64 avatar: {e}")

    elif avatar_src.startswith("http"):
        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                response = client.get(avatar_src)
                if response.status_code == 200:
                    avatar_bytes = response.content
                    initial_content_type = response.headers.get("content-type", "image/jpeg")
        except Exception as e:
            print(f"[AVATAR SERVER] Error downloading remote avatar: {e}")

    if not avatar_bytes:
        return avatar_src

    # Server-side compression and optimization
    avatar_bytes, content_type, ext = compress_image_if_needed(avatar_bytes, max_dim=512, quality=85)

    filename = f"{username}_{int(time.time())}.{ext}"

    # Try Cloudflare R2 under avatar/ directory
    if r2_client and R2_BUCKET_NAME:
        try:
            key = f"avatar/{filename}"
            r2_client.put_object(
                Bucket=R2_BUCKET_NAME,
                Key=key,
                Body=avatar_bytes,
                ContentType=content_type
            )
            public_url = f"{R2_AVATAR_CDN_BASE}/{key}"
            print(f"[AVATAR SERVER] Successfully uploaded to Cloudflare R2: {public_url}")
            return public_url
        except Exception as e:
            import traceback
            print(f"[AVATAR SERVER] R2 upload error: {e}")
            traceback.print_exc()
            print("[AVATAR SERVER] Falling back to local storage.")

    # Local fallback
    try:
        os.makedirs(LOCAL_AVATARS_DIR, exist_ok=True)
        local_path = os.path.join(LOCAL_AVATARS_DIR, filename)
        with open(local_path, "wb") as f:
            f.write(avatar_bytes)

        if base_url:
            local_url = f"{base_url.rstrip('/')}/data/avatar/{filename}"
        else:
            local_url = f"/data/avatar/{filename}"

        print(f"[AVATAR SERVER] Avatar saved to local storage fallback: {local_url}")
        return local_url
    except Exception as e:
        print(f"[AVATAR SERVER] Failed to save avatar locally: {e}")

    return avatar_src


def delete_r2_object(key: str):
    """Delete an object from R2 by its key."""
    if r2_client and R2_BUCKET_NAME:
        try:
            r2_client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
            print(f"Deleted R2 object: {key}")
        except Exception as e:
            print(f"Failed to delete R2 object {key}: {e}")


def delete_user_r2_and_local_files(username: str, avatar_url: str = "") -> dict:
    """
    Completely purge all files belonging to a user from Cloudflare R2 and local disk storage:
    - avatar/{username}_*
    - chat/{username}_*
    - uploads/{username}_*
    - avatar_url (if hosted on R2)
    - Local fallback files
    """
    deleted_r2 = 0
    deleted_local = 0
    clean_username = (username or "").strip().lower()

    if not clean_username:
        return {"deleted_r2": 0, "deleted_local": 0}

    # 1. Cloudflare R2 Cleanup
    if r2_client and R2_BUCKET_NAME:
        prefixes = [f"avatar/{clean_username}_", f"chat/{clean_username}_", f"uploads/{clean_username}_"]
        for prefix in prefixes:
            try:
                paginator = r2_client.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix):
                    contents = page.get("Contents", [])
                    if contents:
                        delete_keys = [{"Key": obj["Key"]} for obj in contents if "Key" in obj]
                        if delete_keys:
                            r2_client.delete_objects(
                                Bucket=R2_BUCKET_NAME,
                                Delete={"Objects": delete_keys}
                            )
                            deleted_r2 += len(delete_keys)
                            print(f"[R2 CLEANUP] Purged {len(delete_keys)} objects with prefix '{prefix}' for user @{clean_username}")
            except Exception as e:
                print(f"[R2 CLEANUP] Error listing/deleting prefix '{prefix}': {e}")

        # Check explicit avatar URL if not caught by prefix
        if avatar_url:
            for base in [f"{R2_AVATAR_CDN_BASE}/", f"{R2_CDN_BASE}/"]:
                if avatar_url.startswith(base):
                    key = avatar_url.replace(base, "").split("?")[0]
                    try:
                        r2_client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
                        deleted_r2 += 1
                        print(f"[R2 CLEANUP] Explicit avatar deleted: {key}")
                    except Exception as e:
                        print(f"[R2 CLEANUP] Failed to delete avatar key {key}: {e}")

    # 2. Local Fallback Directories Cleanup
    for dir_path in [LOCAL_AVATARS_DIR, LOCAL_CHAT_DIR, LOCAL_UPLOADS_DIR]:
        if os.path.exists(dir_path):
            try:
                for fname in os.listdir(dir_path):
                    if fname.lower().startswith(f"{clean_username}_"):
                        fpath = os.path.join(dir_path, fname)
                        if os.path.isfile(fpath):
                            try:
                                os.remove(fpath)
                                deleted_local += 1
                                print(f"[LOCAL CLEANUP] Removed {fpath}")
                            except Exception as fe:
                                print(f"[LOCAL CLEANUP] Failed to remove {fpath}: {fe}")
            except Exception as e:
                print(f"[LOCAL CLEANUP] Error scanning {dir_path}: {e}")

    return {"deleted_r2": deleted_r2, "deleted_local": deleted_local}


def delete_user_folder_files(username: str, folder: str) -> dict:
    """
    Delete all files belonging to a user in a specific folder category (avatar, chat, uploads)
    from Cloudflare R2 and local disk storage.
    """
    deleted_r2 = 0
    deleted_local = 0
    clean_username = (username or "").strip().lower()
    clean_folder = (folder or "").strip().lower()

    if not clean_username or clean_folder not in ("avatar", "chat", "uploads"):
        return {"deleted_r2": 0, "deleted_local": 0}

    # 1. Cloudflare R2 Cleanup
    if r2_client and R2_BUCKET_NAME:
        prefix = f"{clean_folder}/{clean_username}_"
        try:
            paginator = r2_client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix):
                contents = page.get("Contents", [])
                if contents:
                    delete_keys = [{"Key": obj["Key"]} for obj in contents if "Key" in obj]
                    if delete_keys:
                        r2_client.delete_objects(
                            Bucket=R2_BUCKET_NAME,
                            Delete={"Objects": delete_keys}
                        )
                        deleted_r2 += len(delete_keys)
                        print(f"[R2 CLEANUP] Purged {len(delete_keys)} objects in '{prefix}' for user @{clean_username}")
        except Exception as e:
            print(f"[R2 CLEANUP] Error deleting prefix '{prefix}': {e}")

    # 2. Local Fallback Directory Cleanup
    dir_map = {
        "avatar": LOCAL_AVATARS_DIR,
        "chat": LOCAL_CHAT_DIR,
        "uploads": LOCAL_UPLOADS_DIR
    }
    dir_path = dir_map.get(clean_folder)
    if dir_path and os.path.exists(dir_path):
        try:
            for fname in os.listdir(dir_path):
                if fname.lower().startswith(f"{clean_username}_"):
                    fpath = os.path.join(dir_path, fname)
                    if os.path.isfile(fpath):
                        try:
                            os.remove(fpath)
                            deleted_local += 1
                            print(f"[LOCAL CLEANUP] Removed {fpath}")
                        except Exception as fe:
                            print(f"[LOCAL CLEANUP] Failed to remove {fpath}: {fe}")
        except Exception as e:
            print(f"[LOCAL CLEANUP] Error scanning {dir_path}: {e}")

    return {"deleted_r2": deleted_r2, "deleted_local": deleted_local}


def upload_file_to_r2(key: str, file_bytes: bytes, content_type: str) -> str:
    """Upload a generic file to R2 and return its CDN URL."""
    r2_client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=file_bytes,
        ContentType=content_type
    )
    return f"{R2_CDN_BASE}/{key}"


def upload_fileobj_to_r2(key: str, fileobj, content_type: str) -> str:
    """Upload a file-like object to R2 and return its CDN URL."""
    r2_client.upload_fileobj(
        fileobj,
        Bucket=R2_BUCKET_NAME,
        Key=key,
        ExtraArgs={"ContentType": content_type}
    )
    return f"{R2_CDN_BASE}/{key}"


def format_bytes(size_bytes: int) -> str:
    """Format bytes into a human-readable string (B, KB, MB, GB, TB)."""
    if not size_bytes or size_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    unit_index = 0
    size = float(size_bytes)
    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.2f} {units[unit_index]}"


def get_user_files_storage(username: str, avatar_url: str = "") -> dict:
    """
    Calculate total storage in bytes and file count for a user across Cloudflare R2 and local disk fallback:
    - avatar/{username}_*
    - chat/{username}_*
    - uploads/{username}_*
    - explicit avatar if on R2
    """
    clean_username = (username or "").strip().lower()
    total_r2_bytes = 0
    total_r2_files = 0
    breakdown_r2 = {"avatar": 0, "chat": 0, "uploads": 0}

    total_local_bytes = 0
    total_local_files = 0
    breakdown_local = {"avatar": 0, "chat": 0, "uploads": 0}

    if not clean_username:
        return {
            "total_bytes": 0,
            "total_formatted": "0 B",
            "total_files": 0,
            "r2_bytes": 0,
            "r2_formatted": "0 B",
            "r2_files": 0,
            "local_bytes": 0,
            "local_formatted": "0 B",
            "local_files": 0,
            "breakdown": {
                "avatar_bytes": 0,
                "avatar_formatted": "0 B",
                "chat_bytes": 0,
                "chat_formatted": "0 B",
                "uploads_bytes": 0,
                "uploads_formatted": "0 B"
            }
        }

    # 1. Cloudflare R2 Scan
    if r2_client and R2_BUCKET_NAME:
        folders = ["avatar", "chat", "uploads"]
        for folder in folders:
            prefix = f"{folder}/{clean_username}_"
            try:
                paginator = r2_client.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix):
                    contents = page.get("Contents", [])
                    for obj in contents:
                        sz = obj.get("Size", 0)
                        total_r2_bytes += sz
                        total_r2_files += 1
                        breakdown_r2[folder] += sz
            except Exception as e:
                print(f"[STORAGE SCAN] Error listing R2 prefix {prefix}: {e}")

        # Check explicit avatar URL if not in prefix
        if avatar_url:
            for base in [f"{R2_AVATAR_CDN_BASE}/", f"{R2_CDN_BASE}/"]:
                if avatar_url.startswith(base):
                    key = avatar_url.replace(base, "").split("?")[0]
                    if not key.startswith(f"avatar/{clean_username}_"):
                        try:
                            head = r2_client.head_object(Bucket=R2_BUCKET_NAME, Key=key)
                            sz = head.get("ContentLength", 0)
                            total_r2_bytes += sz
                            total_r2_files += 1
                            breakdown_r2["avatar"] += sz
                        except Exception:
                            pass

    # 2. Local Fallback Directories Scan
    dirs = [
        ("avatar", LOCAL_AVATARS_DIR),
        ("chat", LOCAL_CHAT_DIR),
        ("uploads", LOCAL_UPLOADS_DIR)
    ]
    for folder, dir_path in dirs:
        if os.path.exists(dir_path):
            try:
                for fname in os.listdir(dir_path):
                    if fname.lower().startswith(f"{clean_username}_"):
                        fpath = os.path.join(dir_path, fname)
                        if os.path.isfile(fpath):
                            try:
                                sz = os.path.getsize(fpath)
                                total_local_bytes += sz
                                total_local_files += 1
                                breakdown_local[folder] += sz
                            except Exception:
                                pass
            except Exception as e:
                print(f"[STORAGE SCAN] Error scanning local dir {dir_path}: {e}")

    total_bytes = total_r2_bytes + total_local_bytes
    total_files = total_r2_files + total_local_files

    return {
        "total_bytes": total_bytes,
        "total_formatted": format_bytes(total_bytes),
        "total_files": total_files,
        "r2_bytes": total_r2_bytes,
        "r2_formatted": format_bytes(total_r2_bytes),
        "r2_files": total_r2_files,
        "local_bytes": total_local_bytes,
        "local_formatted": format_bytes(total_local_bytes),
        "local_files": total_local_files,
        "breakdown": {
            "avatar_bytes": breakdown_r2["avatar"] + breakdown_local["avatar"],
            "avatar_formatted": format_bytes(breakdown_r2["avatar"] + breakdown_local["avatar"]),
            "chat_bytes": breakdown_r2["chat"] + breakdown_local["chat"],
            "chat_formatted": format_bytes(breakdown_r2["chat"] + breakdown_local["chat"]),
            "uploads_bytes": breakdown_r2["uploads"] + breakdown_local["uploads"],
            "uploads_formatted": format_bytes(breakdown_r2["uploads"] + breakdown_local["uploads"]),
        }
    }


def get_all_r2_storage_summary() -> dict:
    """Scan all objects in R2 bucket and return overall size and file count."""
    total_bytes = 0
    total_files = 0
    folder_breakdown = {"avatar": 0, "chat": 0, "uploads": 0, "other": 0}

    if r2_client and R2_BUCKET_NAME:
        try:
            paginator = r2_client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=R2_BUCKET_NAME):
                contents = page.get("Contents", [])
                for obj in contents:
                    sz = obj.get("Size", 0)
                    key = obj.get("Key", "")
                    total_bytes += sz
                    total_files += 1
                    if key.startswith("avatar/"):
                        folder_breakdown["avatar"] += sz
                    elif key.startswith("chat/"):
                        folder_breakdown["chat"] += sz
                    elif key.startswith("uploads/"):
                        folder_breakdown["uploads"] += sz
                    else:
                        folder_breakdown["other"] += sz
        except Exception as e:
            print(f"[R2 SUMMARY SCAN] Error: {e}")

    # Also scan local directories
    local_bytes = 0
    local_files = 0
    for dir_path in [LOCAL_AVATARS_DIR, LOCAL_CHAT_DIR, LOCAL_UPLOADS_DIR]:
        if os.path.exists(dir_path):
            try:
                for fname in os.listdir(dir_path):
                    fpath = os.path.join(dir_path, fname)
                    if os.path.isfile(fpath):
                        try:
                            sz = os.path.getsize(fpath)
                            local_bytes += sz
                            local_files += 1
                        except Exception:
                            pass
            except Exception:
                pass

    all_bytes = total_bytes + local_bytes
    all_files = total_files + local_files

    return {
        "total_bytes": all_bytes,
        "total_formatted": format_bytes(all_bytes),
        "total_files": all_files,
        "r2_bytes": total_bytes,
        "r2_formatted": format_bytes(total_bytes),
        "r2_files": total_files,
        "local_bytes": local_bytes,
        "local_formatted": format_bytes(local_bytes),
        "local_files": local_files,
        "folders": {k: format_bytes(v) for k, v in folder_breakdown.items()}
    }


