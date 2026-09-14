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

