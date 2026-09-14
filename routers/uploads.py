import uuid
import os
import urllib.parse
import unicodedata
import re
from fastapi import APIRouter, HTTPException, Depends, File, UploadFile, Query, Request
from fastapi.responses import StreamingResponse, FileResponse
import httpx
from database.postgres import execute_pg_query
from auth.deps import get_auth_token, verify_google_token
from websocket.manager import get_email_by_username
from storage.r2 import r2_client, R2_BUCKET_NAME, upload_file_to_r2, upload_fileobj_to_r2
from config import (
    R2_CDN_BASE,
    CLOUDFLARE_ACCOUNT_ID,
    AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY,
)
from fastapi.concurrency import run_in_threadpool
import boto3
from botocore.config import Config

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


@router.post("/upload")
async def api_upload_file(
    username: str = Query(...),
    file: UploadFile = File(...),
    token: str = Depends(get_auth_token),
    request: Request = None
):
    username = username.strip().lower()
    email = await get_email_by_username(username)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

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
        filename = f"{username}_{uuid.uuid4().hex}.{ext}"
        content_type = file.content_type or "application/octet-stream"

        if r2_client and R2_BUCKET_NAME:
            try:
                key = f"uploads/{filename}"
                public_url = await run_in_threadpool(
                    upload_fileobj_to_r2, key, file.file, content_type
                )
                return {"url": public_url, "filename": file.filename}
            except Exception as e:
                print(f"R2 upload failed, falling back to local: {e}")
                await file.seek(0)

        LOCAL_UPLOADS_DIR = "./data/uploads"
        os.makedirs(LOCAL_UPLOADS_DIR, exist_ok=True)
        local_path = os.path.join(LOCAL_UPLOADS_DIR, filename)
        
        # Write chunks of 1MB to disk to avoid high RAM usage
        with open(local_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)

        base_url = str(request.base_url).rstrip("/") if request else ""
        public_url = f"{base_url}/data/uploads/{filename}"
        return {"url": public_url, "filename": file.filename}
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Error handling upload: {e}")
        raise HTTPException(status_code=500, detail=f"File upload failed: {str(e)}")


@router.post("/upload/presigned")
async def api_get_presigned_url(
    filename: str = Query(...),
    content_type: str = Query(...),
    username: str = Query(...),
    token: str = Depends(get_auth_token)
):
    username = username.strip().lower()
    email = await get_email_by_username(username)
    if not email or not await verify_google_token(token, email):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not r2_client or not R2_BUCKET_NAME:
        raise HTTPException(status_code=501, detail="Direct R2 upload not configured on server")

    try:
        ext = os.path.splitext(filename)[1].lstrip(".") or "bin"
        unique_filename = f"{username}_{uuid.uuid4().hex}.{ext}"
        key = f"uploads/{unique_filename}"

        # Generate presigned URL
        endpoint_url = f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
        s3_client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            config=Config(signature_version="s3v4")
        )
        
        # We run presigned URL generation in a threadpool to avoid blocking
        presigned_url = await run_in_threadpool(
            s3_client.generate_presigned_url,
            ClientMethod="put_object",
            Params={
                "Bucket": R2_BUCKET_NAME,
                "Key": key,
                "ContentType": content_type
            },
            ExpiresIn=3600
        )
        
        public_url = f"{R2_CDN_BASE}/{key}"
        return {
            "presigned_url": presigned_url,
            "public_url": public_url,
            "filename": filename
        }
    except Exception as e:
        print(f"Error generating presigned URL: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate upload URL: {str(e)}")


@router.get("/download")
async def api_download_file(url: str, filename: str):
    content_disp = get_content_disposition(filename)

    try:
        if "/data/uploads/" in url:
            local_filename = os.path.basename(url.split("?")[0])
            local_path = os.path.join("./data/uploads", local_filename)
            if os.path.exists(local_path):
                return FileResponse(
                    local_path,
                    media_type="application/octet-stream",
                    headers={"Content-Disposition": content_disp}
                )
            raise HTTPException(status_code=404, detail="File not found locally")

        if url.startswith(f"{R2_CDN_BASE}/"):
            key = url.replace(f"{R2_CDN_BASE}/", "").split("?")[0]
            if r2_client and R2_BUCKET_NAME:
                try:
                    response = r2_client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
                    return StreamingResponse(
                        response["Body"],
                        media_type=response.get("ContentType", "application/octet-stream"),
                        headers={"Content-Disposition": content_disp}
                    )
                except Exception as r2_err:
                    print(f"R2 get_object failed: {r2_err}")

        if url.startswith("http://") or url.startswith("https://"):
            trusted_domains = ("cdn.spac2.com", "spac2.com", "spac2.com", "spac2.com", "api1.spac2.com", "cdn1.spac2.com", "localhost", "127.0.0.1")
            if not any(d in url for d in trusted_domains):
                raise HTTPException(status_code=400, detail="Untrusted download domain")

            async def stream_file():
                async with httpx.AsyncClient() as client:
                    async with client.stream("GET", url) as r:
                        if r.status_code != 200:
                            raise HTTPException(status_code=r.status_code, detail="Failed to fetch file from source")
                        async for chunk in r.aiter_bytes():
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
