"""
CloudDrop — FastAPI Application

Routes:
    GET  /               → Upload page (frontend)
    POST /upload         → Upload a file (open to everyone)
    GET  /d/{short_id}   → Download a file via short-link
    GET  /api/info/{id}  → Get link metadata (JSON)
    GET  /health         → ALB health-check
    POST /auth/signup    → Register with email + password
    POST /auth/confirm   → Verify email with code
    POST /auth/login     → Login with email + password
    POST /auth/logout    → Clear session
    GET  /api/my-uploads → View upload history (logged-in users)
"""

import re
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from config import settings
from encryption import encrypt_file, decrypt_file
from storage import upload_to_s3, download_from_s3, delete_from_s3
from shortener import (
    generate_short_id,
    create_link,
    get_link,
    decrement_downloads,
    is_slug_available,
    track_user_upload,
    get_user_uploads,
)
from auth import (
    signup_user,
    confirm_signup,
    login_user,
    create_session,
    delete_session,
    get_user_from_request,
)
from database import create_tables
from db_sync import start_sync_thread, stop_sync_thread

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-25s | %(levelname)-5s | %(message)s",
)
logger = logging.getLogger("clouddrop.main")

# ── App Lifespan (startup / shutdown) ────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and start Write-Back sync thread on startup."""
    # ── Startup ──
    if settings.DATABASE_URL:
        try:
            create_tables()
            logger.info("PostgreSQL tables verified/created.")
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
            logger.warning("App will run with Redis-only mode (no PostgreSQL persistence).")

        try:
            start_sync_thread()
            logger.info("Write-Back sync thread started.")
        except Exception as e:
            logger.error(f"Failed to start sync thread: {e}")
    else:
        logger.warning("DATABASE_URL not set — running without PostgreSQL. Data is Redis-only.")

    yield  # App is running

    # ── Shutdown ──
    stop_sync_thread()
    logger.info("Write-Back sync thread stopped.")


# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="CloudDrop",
    description="Ephemeral encrypted file-sharing service",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ── Constants ────────────────────────────────────────────────────────────────
MAX_FILE_BYTES = settings.MAX_FILE_SIZE_MB * 1024 * 1024
SLUG_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")


# ── Pydantic Models ──────────────────────────────────────────────────────────

class AuthRequest(BaseModel):
    email: str
    password: str

class ConfirmRequest(BaseModel):
    email: str
    code: str


# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the upload page."""
    user_email = get_user_from_request(request)
    cognito_enabled = bool(settings.COGNITO_CLIENT_ID and settings.COGNITO_USER_POOL_ID)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "max_file_size_mb": settings.MAX_FILE_SIZE_MB,
            "max_downloads": settings.MAX_DOWNLOADS,
            "user_email": user_email,
            "cognito_enabled": cognito_enabled,
        },
    )


@app.get("/health")
async def health():
    """Health-check endpoint for ALB target group."""
    return {"status": "healthy", "service": "clouddrop"}


@app.get("/debug/redis")
async def debug_redis():
    """Diagnostic endpoint to test Redis connectivity."""
    import socket
    results = {
        "redis_host": settings.REDIS_HOST,
        "redis_port": settings.REDIS_PORT,
        "redis_use_tls": settings.REDIS_USE_TLS,
        "redis_password_set": bool(settings.REDIS_PASSWORD),
    }

    # Step 1: DNS resolution
    try:
        ip = socket.gethostbyname(settings.REDIS_HOST)
        results["dns_resolved"] = ip
    except Exception as e:
        results["dns_error"] = str(e)
        return results

    # Step 2: Raw TCP connection test
    try:
        sock = socket.create_connection((settings.REDIS_HOST, settings.REDIS_PORT), timeout=5)
        sock.close()
        results["tcp_connect"] = "OK"
    except Exception as e:
        results["tcp_connect_error"] = str(e)
        return results

    # Step 3: Redis PING (plaintext)
    try:
        import redis
        r = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=0,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        r.ping()
        results["redis_ping_plaintext"] = "OK"
        return results
    except Exception as e:
        results["redis_ping_plaintext_error"] = str(e)

    # Step 4: Redis PING (TLS)
    try:
        import redis
        r = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=0,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            ssl=True,
            ssl_cert_reqs="none",
        )
        r.ping()
        results["redis_ping_tls"] = "OK"
    except Exception as e:
        results["redis_ping_tls_error"] = str(e)

    return results


# ── Authentication ───────────────────────────────────────────────────────────

@app.post("/auth/signup")
async def auth_signup(body: AuthRequest):
    """Register a new user with email + password."""
    if not settings.COGNITO_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Authentication not configured.")
    result = signup_user(body.email, body.password)
    if result["success"]:
        return result
    raise HTTPException(status_code=400, detail=result["error"])


@app.post("/auth/confirm")
async def auth_confirm(body: ConfirmRequest):
    """Confirm signup with the email verification code."""
    result = confirm_signup(body.email, body.code)
    if result["success"]:
        return result
    raise HTTPException(status_code=400, detail=result["error"])


@app.post("/auth/login")
async def auth_login(body: AuthRequest):
    """Login with email + password. Returns session cookie."""
    if not settings.COGNITO_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Authentication not configured.")

    result = login_user(body.email, body.password)
    if not result["success"]:
        status = 400
        if result.get("needs_confirmation"):
            status = 403
        raise HTTPException(status_code=status, detail=result["error"])

    # Create session and set cookie
    session_token = create_session(result["email"])
    response = JSONResponse(content={"success": True, "email": result["email"]})
    response.set_cookie(
        key="clouddrop_session",
        value=session_token,
        max_age=86400,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/auth/logout")
async def auth_logout(request: Request):
    """Clear session cookie."""
    token = request.cookies.get("clouddrop_session")
    delete_session(token)
    response = JSONResponse(content={"success": True})
    response.delete_cookie("clouddrop_session")
    return response


# ── Upload ───────────────────────────────────────────────────────────────────

@app.post("/upload")
def upload_file(
    request: Request,
    files: list[UploadFile] = File(...),
    custom_slug: str = Form(default=""),
    max_downloads: int = Form(default=5),
):
    """
    Upload a file (or files) → encrypt → store in S3 → create short-link in Redis.
    Write-Back: Redis is updated immediately, background thread flushes to PostgreSQL.
    Open to everyone. Logged-in users have uploads tracked in their history.
    """

    # ── Extract user identity (if logged in) ─────────────────────────────
    user_email = get_user_from_request(request)
    logger.info(f"Upload request from: {user_email or 'anonymous'}")

    # ── Process files (Zip if multiple) ──────────────────────────────────
    if not files or len(files) == 0:
        raise HTTPException(status_code=400, detail="Empty file list.")

    if len(files) == 1:
        content = files[0].file.read()
        original_filename = files[0].filename or "download"
        content_type = files[0].content_type or "application/octet-stream"
    else:
        import io
        import zipfile
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for f in files:
                zip_file.writestr(f.filename or "file", f.file.read())
        content = zip_buffer.getvalue()
        original_filename = "bundle.zip"
        content_type = "application/zip"

    # ── Validate file size ───────────────────────────────────────────────
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File(s) too large. Maximum size is {settings.MAX_FILE_SIZE_MB} MB.",
        )

    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Empty file content.")

    # ── Determine short ID ───────────────────────────────────────────────
    if custom_slug:
        slug = custom_slug.strip()
        if not SLUG_PATTERN.match(slug):
            raise HTTPException(
                status_code=400,
                detail="Custom slug must be 3-32 characters: letters, digits, hyphens, underscores.",
            )
        if not is_slug_available(slug):
            raise HTTPException(status_code=409, detail="That custom slug is already taken.")
        short_id = slug
    else:
        short_id = generate_short_id()

    # ── Clamp max downloads ──────────────────────────────────────────────
    max_downloads = max(1, min(max_downloads, 100))

    # ── Encrypt the file (KMS Envelope Encryption) ───────────────────────
    try:
        ciphertext, encrypted_dek_b64, iv_b64 = encrypt_file(content)
    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        raise HTTPException(status_code=500, detail="Encryption failed. Check KMS configuration.")

    # ── Upload encrypted blob to S3 ──────────────────────────────────────
    s3_key = f"uploads/{short_id}.enc"

    try:
        upload_to_s3(
            object_key=s3_key,
            encrypted_data=ciphertext,
            encrypted_dek_b64=encrypted_dek_b64,
            iv_b64=iv_b64,
            original_filename=original_filename,
            content_type=content_type,
        )
    except Exception as e:
        logger.error(f"S3 upload failed: {e}")
        raise HTTPException(status_code=500, detail="Storage upload failed. Check S3 configuration.")

    # ── Create short-link in Redis (Write-Back → PostgreSQL) ────────────
    try:
        create_link(
            short_id=short_id,
            s3_key=s3_key,
            original_filename=original_filename,
            content_type=content_type,
            max_downloads=max_downloads,
            encrypted_dek_b64=encrypted_dek_b64,
            iv_b64=iv_b64,
            uploader_email=user_email,
            file_size_bytes=len(content),
        )
    except Exception as e:
        logger.error(f"Redis link creation failed: {e}")
        raise HTTPException(status_code=500, detail="Link creation failed. Check Redis configuration.")

    # ── Track upload for logged-in users ─────────────────────────────────
    if user_email:
        try:
            track_user_upload(user_email, short_id, original_filename, max_downloads)
        except Exception as e:
            logger.warning(f"Upload tracking failed for {user_email}: {e}")

    # ── Build response ───────────────────────────────────────────────────
    base_url = settings.BASE_URL.strip()
    download_url = f"{base_url}/d/{short_id}"

    return JSONResponse(content={
        "success": True,
        "short_id": short_id,
        "download_url": download_url,
        "filename": original_filename,
        "max_downloads": max_downloads,
        "expires_in": "24 hours",
    })


# ── Download ─────────────────────────────────────────────────────────────────

@app.get("/d/{short_id}", response_class=HTMLResponse)
async def download_page(request: Request, short_id: str):
    """
    Render the download UI for a short-link.
    """
    link = get_link(short_id)
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found or has expired.")
    if link["downloads_left"] <= 0:
        raise HTTPException(status_code=410, detail="This link has been used up. No downloads remaining.")

    return templates.TemplateResponse(
        request=request,
        name="download.html",
        context={
            "short_id": short_id,
            "filename": link["original_filename"],
            "content_type": link["content_type"],
            "filesize": "Unknown",  # We don't store file size right now, but could
            "downloads_left": link["downloads_left"],
            "max_downloads": link["max_downloads"],
        },
    )

@app.get("/api/download/{short_id}")
def api_download_file(short_id: str):
    """
    Download the actual bytes of the file.
    Fetches from S3, decrypts with KMS, serves the plaintext file,
    and decrements the download counter.
    """

    # ── Look up the link in Redis ────────────────────────────────────────
    link = get_link(short_id)
    if link is None:
        raise HTTPException(
            status_code=404,
            detail="Link not found or has expired.",
        )

    if link["downloads_left"] <= 0:
        raise HTTPException(
            status_code=410,
            detail="This link has been used up. No downloads remaining.",
        )

    # ── Fetch encrypted file from S3 ─────────────────────────────────────
    try:
        s3_data = download_from_s3(link["s3_key"])
    except Exception as e:
        logger.error(f"S3 download failed for {short_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve file from storage.")

    # ── Decrypt the file ─────────────────────────────────────────────────
    try:
        plaintext = decrypt_file(
            ciphertext=s3_data["encrypted_data"],
            encrypted_dek_b64=s3_data["encrypted_dek_b64"],
            iv_b64=s3_data["iv_b64"],
        )
    except Exception as e:
        logger.error(f"Decryption failed for {short_id}: {e}")
        raise HTTPException(status_code=500, detail="File decryption failed.")

    # ── Decrement download counter ───────────────────────────────────────
    remaining = decrement_downloads(short_id)

    # If downloads exhausted, also delete the S3 object
    if remaining <= 0:
        try:
            delete_from_s3(link["s3_key"])
            logger.info(f"S3 object deleted after last download: {link['s3_key']}")
        except Exception as e:
            logger.warning(f"S3 cleanup failed for {link['s3_key']}: {e}")

    # ── Serve the decrypted file ─────────────────────────────────────────
    safe_filename = s3_data["original_filename"].replace('"', '\\"')
    content_type = s3_data["original_content_type"]
    
    previewable_types = [
        "image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml",
        "video/mp4", "video/webm", "audio/mpeg", "audio/ogg", "audio/wav",
        "application/pdf", "text/plain"
    ]
    disposition = "inline" if content_type in previewable_types else "attachment"

    return Response(
        content=plaintext,
        media_type=content_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_filename}"',
            "X-Downloads-Remaining": str(max(0, remaining)),
        },
    )


# ── My Uploads (API) ────────────────────────────────────────────────────────

@app.get("/api/my-uploads")
async def api_my_uploads(request: Request):
    """Get upload history for the logged-in user."""
    user_email = get_user_from_request(request)
    if not user_email:
        raise HTTPException(status_code=401, detail="Not logged in.")

    uploads = get_user_uploads(user_email)
    return {
        "email": user_email,
        "uploads": uploads,
        "base_url": settings.BASE_URL,
    }


# ── Link Info (API) ──────────────────────────────────────────────────────────

@app.get("/api/info/{short_id}")
async def link_info(short_id: str):
    """Get metadata about a short-link (without downloading the file)."""
    link = get_link(short_id)
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found or has expired.")

    return {
        "short_id": short_id,
        "filename": link["original_filename"],
        "content_type": link["content_type"],
        "downloads_left": link["downloads_left"],
        "max_downloads": link["max_downloads"],
        "ttl_seconds": link.get("ttl_seconds", 0),
        "download_url": f"{settings.BASE_URL.strip()}/d/{short_id}",
    }


# ═══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
