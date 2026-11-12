import uuid
import os
import urllib.parse
import unicodedata
import re
from typing import Optional, Dict, Any, List, Union
from fastapi import APIRouter, HTTPException, Depends, File, UploadFile, Query, Request
from fastapi.responses import StreamingResponse, FileResponse, RedirectResponse
import httpx
from database.postgres import execute_pg_query
from auth.deps import get_auth_token, verify_auth_token, decode_spac2_token
from websocket.manager import get_email_by_username
from storage.r2 import (
    r2_client,
    R2_BUCKET_NAME,
    upload_file_to_r2,
    upload_fileobj_to_r2,
    compress_image_if_needed,
    clean_extract_r2_key,
    generate_r2_presigned_download_url,
)
from config import (
    R2_CDN_BASE,
    R2_AVATAR_CDN_BASE,
    LOCAL_AVATARS_DIR,
    LOCAL_UPLOADS_DIR,
    LOCAL_CHAT_DIR,
)
from fastapi.concurrency import run_in_threadpool

router = APIRouter(prefix="/api", tags=["uploads"])


def get_content_disposition(filename: str, disposition: str = "attachment") -> str:
    safe_filename = os.path.basename(filename).strip() or "download"
    # Normalize unicode to ASCII for legacy fallback parameter (safe for latin-1 headers)
    ascii_filename = unicodedata.normalize('NFKD', safe_filename).encode('ascii', 'ignore').decode('ascii')
    ascii_filename = re.sub(r'[\r\n"\\;]', '', ascii_filename).strip()
    if not ascii_filename:
        ext = os.path.splitext(safe_filename)[1]
        ascii_filename = f"download{ext}" if ext else "download"
    
    # RFC 5987 / RFC 6266 encoding for full UTF-8 support (Vietnamese, special characters, etc.)
    utf8_encoded = urllib.parse.quote(safe_filename)
    return f'{disposition}; filename="{ascii_filename}"; filename*=UTF-8\'\'{utf8_encoded}'


@router.post("/upload/avatar")
async def api_upload_avatar(
    username: Optional[str] = Query(None),
    file: UploadFile = File(...),
    token: str = Depends(get_auth_token),
    request: Request = None
):
    """Upload, compress, and store user avatar to Cloudflare R2 under avatar/ with cdn2.spac2.com CDN domain."""
    if not token or str(token).strip() in ("", "undefined", "null", "None"):
        raise HTTPException(status_code=401, detail="Unauthorized: Authentication token required")

    from auth.deps import decode_spac2_token
    token_payload = decode_spac2_token(token)
    
    # Extract identity from token
    token_username = token_payload.get("username") if token_payload else ""
    token_email = token_payload.get("email") if token_payload else ""

    # Effective username for the avatar filename
    target_username = (username or token_username or "user").strip().lower()
    target_username = re.sub(r"[^a-z0-9_]", "", target_username) or "user"

    try:
        content = await file.read()
        if len(content) > 15 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Avatar image too large (max 15MB)")

        # Server-side compression and square crop to max 512x512 WebP/JPEG
        optimized_bytes, content_type, ext = compress_image_if_needed(content, max_dim=512, quality=85)
        filename = f"{target_username}_{uuid.uuid4().hex[:12]}.{ext}"

        if r2_client and R2_BUCKET_NAME:
            try:
                key = f"avatar/{filename}"
                r2_client.put_object(
                    Bucket=R2_BUCKET_NAME,
                    Key=key,
                    Body=optimized_bytes,
                    ContentType=content_type
                )
                public_url = f"{R2_AVATAR_CDN_BASE}/{key}"
                print(f"[AVATAR] Uploaded to R2: {public_url}")
                return {"url": public_url, "filename": filename, "key": key}
            except Exception as e:
                print(f"R2 avatar upload failed, falling back to local: {e}")

        # Local fallback
        os.makedirs(LOCAL_AVATARS_DIR, exist_ok=True)
        local_path = os.path.join(LOCAL_AVATARS_DIR, filename)
        with open(local_path, "wb") as f:
            f.write(optimized_bytes)

        base_url = str(request.base_url).rstrip("/") if request else ""
        public_url = f"{base_url}/data/avatar/{filename}"
        return {"url": public_url, "filename": filename, "key": filename}
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Error handling avatar upload: {e}")
        raise HTTPException(status_code=500, detail=f"Avatar upload failed: {str(e)}")


@router.post("/upload")
async def api_upload_file(
    username: Optional[str] = Query(None),
    file: UploadFile = File(...),
    token: str = Depends(get_auth_token),
    folder: Optional[str] = Query(None),
    request: Request = None
):
    if not token or str(token).strip() in ("", "undefined", "null", "None"):
        raise HTTPException(status_code=401, detail="Unauthorized: Authentication token required")

    from auth.deps import decode_spac2_token
    token_payload = decode_spac2_token(token)
    token_username = token_payload.get("username") if token_payload else ""

    target_username = (username or token_username or "user").strip().lower()
    target_username = re.sub(r"[^a-z0-9_]", "", target_username) or "user"

    try:
        # Determine file size without reading entire file into memory
        file_size = getattr(file, "size", None)
        if file_size is None:
            await file.seek(0, os.SEEK_END)
            file_size = await file.tell()
            await file.seek(0)
        else:
            await file.seek(0)

        # Allow up to 4 GB (4096 MB)
        MAX_FILE_SIZE = 4096 * 1024 * 1024
        if file_size > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="File too large (max 4GB)")

        ext = os.path.splitext(file.filename)[1].lstrip(".") or "bin"
        filename = f"{target_username}_{uuid.uuid4().hex}.{ext}"
        content_type = file.content_type or "application/octet-stream"

        # Determine target folder on Cloudflare R2 (chat/ for images and chat media, uploads/ for general files)
        target_folder = "chat" if (folder == "chat" or content_type.startswith("image/")) else "uploads"

        if r2_client and R2_BUCKET_NAME:
            try:
                key = f"{target_folder}/{filename}"
                public_url = await run_in_threadpool(
                    upload_fileobj_to_r2, key, file.file, content_type
                )
                print(f"[MEDIA] Uploaded to R2: {public_url}")
                return {"url": public_url, "filename": file.filename, "key": key}
            except Exception as e:
                print(f"R2 upload failed, falling back to local: {e}")
                await file.seek(0)

        from config import LOCAL_UPLOADS_DIR, LOCAL_CHAT_DIR
        target_local_dir = LOCAL_CHAT_DIR if target_folder == "chat" else LOCAL_UPLOADS_DIR
        os.makedirs(target_local_dir, exist_ok=True)
        local_path = os.path.join(target_local_dir, filename)
        
        # Write chunks of 1MB to disk to avoid high RAM usage
        with open(local_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)

        base_url = str(request.base_url).rstrip("/") if request else ""
        public_url = f"{base_url}/data/{target_folder}/{filename}"
        return {"url": public_url, "filename": file.filename, "key": f"{target_folder}/{filename}"}
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Error handling upload: {e}")
        raise HTTPException(status_code=500, detail=f"File upload failed: {str(e)}")


@router.post("/upload/chat")
async def api_upload_chat(
    file: UploadFile = File(...),
    username: Optional[str] = Query(None),
    token: str = Depends(get_auth_token),
    request: Request = None
):
    """Direct upload endpoint for chat attachments and images to Cloudflare R2 chat/ folder."""
    return await api_upload_file(
        username=username,
        file=file,
        token=token,
        folder="chat",
        request=request
    )


@router.post("/upload/presigned")
async def api_get_presigned_url(
    filename: str = Query(...),
    content_type: str = Query(...),
    username: str = Query(...),
    folder: Optional[str] = Query(None),
    token: str = Depends(get_auth_token)
):
    username = (username or "").strip().lower()
    if not token or str(token).strip() in ("", "undefined", "null", "None"):
        raise HTTPException(status_code=401, detail="Unauthorized")

    token_payload = decode_spac2_token(token)
    is_authorized = False
    if token_payload and (token_payload.get("username") or token_payload.get("email")):
        is_authorized = True
    else:
        email = await get_email_by_username(username)
        if email and await verify_auth_token(token, email):
            is_authorized = True

    if not is_authorized:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not r2_client or not R2_BUCKET_NAME:
        raise HTTPException(status_code=501, detail="Direct R2 upload not configured on server")

    try:
        safe_user = re.sub(r"[^a-z0-9_]", "", username) or "user"
        ext = os.path.splitext(filename)[1].lstrip(".") or "bin"
        unique_filename = f"{safe_user}_{uuid.uuid4().hex}.{ext}"
        target_folder = "chat" if (folder == "chat" or (content_type and content_type.startswith("image/"))) else "uploads"
        key = f"{target_folder}/{unique_filename}"

        # Reuse singleton r2_client in threadpool (zero client-instantiation overhead)
        presigned_url = await run_in_threadpool(
            r2_client.generate_presigned_url,
            ClientMethod="put_object",
            Params={
                "Bucket": R2_BUCKET_NAME,
                "Key": key,
                "ContentType": content_type or "application/octet-stream"
            },
            ExpiresIn=3600
        )

        public_url = f"{R2_CDN_BASE}/{key}"
        return {
            "presigned_url": presigned_url,
            "public_url": public_url,
            "filename": filename,
            "key": key
        }
    except Exception as e:
        print(f"Error generating presigned URL: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate upload URL: {str(e)}")


@router.get("/download")
async def api_download_file(url: str, filename: str, redirect: int = Query(0)):
    content_disp = get_content_disposition(filename)

    try:
        if any(p in url for p in ("/data/uploads/", "/data/chat/", "/data/avatar/", "/data/avatars/")):
            local_filename = os.path.basename(url.split("?")[0])
            for d in [LOCAL_UPLOADS_DIR, LOCAL_CHAT_DIR, LOCAL_AVATARS_DIR, "./data/uploads", "./data/chat", "./data/avatar", "./data/avatars"]:
                local_path = os.path.join(d, local_filename)
                if os.path.isfile(local_path):
                    return FileResponse(
                        local_path,
                        media_type="application/octet-stream",
                        headers={"Content-Disposition": content_disp}
                    )
            raise HTTPException(status_code=404, detail="File not found locally")

        # Extract R2 key if URL points to our R2 bucket / CDN domains
        r2_key = ""
        if url.startswith(f"{R2_CDN_BASE}/") or url.startswith(f"{R2_AVATAR_CDN_BASE}/"):
            cdn_base = f"{R2_AVATAR_CDN_BASE}/" if url.startswith(f"{R2_AVATAR_CDN_BASE}/") else f"{R2_CDN_BASE}/"
            r2_key = url.replace(cdn_base, "").split("?")[0]
        elif any(d in url for d in ("cdn.spac2.com", "cdn1.spac2.com", "cdn2.spac2.com")):
            r2_key = clean_extract_r2_key(url)

        if r2_key and r2_client and R2_BUCKET_NAME:
            # Zero-RAM mode: redirect browser directly to presigned R2 GET URL with Content-Disposition
            if redirect == 1:
                try:
                    presigned_dl = await run_in_threadpool(
                        generate_r2_presigned_download_url, r2_key, content_disp, 3600
                    )
                    if presigned_dl:
                        return RedirectResponse(url=presigned_dl, status_code=302)
                except Exception as redir_err:
                    print(f"[R2 DOWNLOAD] Presigned redirect fallback notice: {redir_err}")

            # Chunked streaming mode with guaranteed socket cleanup & Content-Length
            try:
                response = await run_in_threadpool(
                    lambda: r2_client.get_object(Bucket=R2_BUCKET_NAME, Key=r2_key)
                )
                body = response["Body"]
                headers = {"Content-Disposition": content_disp}
                if response.get("ContentLength") is not None:
                    headers["Content-Length"] = str(response["ContentLength"])

                def iter_r2_chunks():
                    try:
                        for chunk in body.iter_chunks(chunk_size=65536):
                            if chunk:
                                yield chunk
                    finally:
                        try:
                            body.close()
                        except Exception:
                            pass

                return StreamingResponse(
                    iter_r2_chunks(),
                    media_type=response.get("ContentType", "application/octet-stream"),
                    headers=headers
                )
            except Exception as r2_err:
                print(f"R2 get_object failed for {r2_key}: {r2_err}")

        if url.startswith("http://") or url.startswith("https://"):
            trusted_domains = ("cdn.spac2.com", "spac2.com", "api1.spac2.com", "cdn1.spac2.com", "cdn2.spac2.com", "localhost", "127.0.0.1")
            if not any(d in url for d in trusted_domains):
                raise HTTPException(status_code=400, detail="Untrusted download domain")

            async def stream_file():
                timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    async with client.stream("GET", url) as r:
                        if r.status_code != 200:
                            raise HTTPException(status_code=r.status_code, detail="Failed to fetch file from source")
                        async for chunk in r.aiter_bytes(chunk_size=65536):
                            yield chunk

            return StreamingResponse(
                stream_file(),
                media_type="application/octet-stream",
                headers={"Content-Disposition": content_disp}
            )

        raise HTTPException(status_code=400, detail="Invalid URL format")

    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Error handling download proxy: {e}")
        raise HTTPException(status_code=500, detail=str(e))
