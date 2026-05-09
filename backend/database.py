"""
CloudDrop — Database Module (PostgreSQL via SQLAlchemy)

Provides the persistent storage layer for the 3-tier architecture.
PostgreSQL is the durable source of truth; Redis is the Write-Back cache.

Tables:
    links            — Short-link metadata, encrypted DEKs, download counters
    upload_audit_log — Immutable audit trail of all upload/download events
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Text,
    Boolean,
    BigInteger,
    DateTime,
    func,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from config import settings

logger = logging.getLogger("clouddrop.database")

Base = declarative_base()


# ── ORM Models ──────────────────────────────────────────────────────────────


class Link(Base):
    """Persistent record of a short-link and its associated file metadata."""

    __tablename__ = "links"

    id = Column(Integer, primary_key=True, autoincrement=True)
    short_id = Column(String(32), unique=True, nullable=False, index=True)
    s3_key = Column(String(256), nullable=False)
    original_filename = Column(String(512), nullable=False)
    content_type = Column(String(128), nullable=False)
    encrypted_dek_b64 = Column(Text, nullable=True)  # Backup copy of DEK
    iv_b64 = Column(Text, nullable=True)  # Backup copy of IV
    max_downloads = Column(Integer, nullable=False, default=5)
    downloads_left = Column(Integer, nullable=False, default=5)
    uploader_email = Column(String(256), nullable=True)  # NULL for anonymous
    created_at = Column(DateTime, default=func.now())
    expires_at = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=True)


class UploadAuditLog(Base):
    """Immutable audit trail of upload/download/expiry events."""

    __tablename__ = "upload_audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    short_id = Column(String(32), nullable=False, index=True)
    uploader_email = Column(String(256), nullable=True)
    original_filename = Column(String(512), nullable=False)
    file_size_bytes = Column(BigInteger, nullable=True)
    action = Column(String(32), nullable=False)  # 'upload', 'download', 'expired', 'exhausted'
    created_at = Column(DateTime, default=func.now())


# ── Engine & Session Factory ────────────────────────────────────────────────

_engine = None
_read_engine = None
_SessionFactory = None
_ReadSessionFactory = None


def _get_engine():
    """Get or create the primary (read-write) database engine."""
    global _engine, _SessionFactory
    if _engine is None:
        if not settings.DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. PostgreSQL is required for the 3-tier architecture. "
                "Set DATABASE_URL=postgresql://user:pass@host:5432/clouddrop"
            )
        _engine = create_engine(
            settings.DATABASE_URL,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            echo=False,
        )
        _SessionFactory = sessionmaker(bind=_engine)
        logger.info(f"Primary database engine created: {settings.DATABASE_URL.split('@')[-1] if '@' in settings.DATABASE_URL else 'configured'}")
    return _engine


def _get_read_engine():
    """Get or create the read-replica database engine. Falls back to primary."""
    global _read_engine, _ReadSessionFactory
    if _read_engine is None:
        read_url = settings.DATABASE_READ_URL or settings.DATABASE_URL
        if not read_url:
            raise RuntimeError("DATABASE_URL is not set.")
        _read_engine = create_engine(
            read_url,
            pool_size=3,
            max_overflow=5,
            pool_pre_ping=True,
            echo=False,
        )
        _ReadSessionFactory = sessionmaker(bind=_read_engine)
        is_replica = bool(settings.DATABASE_READ_URL)
        logger.info(f"Read database engine created (replica={is_replica})")
    return _read_engine


def get_session() -> Session:
    """Get a new session for the primary (read-write) database."""
    _get_engine()
    return _SessionFactory()


def get_read_session() -> Session:
    """Get a new session for the read-replica database."""
    _get_read_engine()
    return _ReadSessionFactory()


# ── Schema Initialization ───────────────────────────────────────────────────


def create_tables():
    """Create all tables if they don't exist. Called on app startup."""
    engine = _get_engine()
    Base.metadata.create_all(engine)
    logger.info("Database tables verified/created.")


# ── CRUD Helpers ────────────────────────────────────────────────────────────


def db_create_link(
    short_id: str,
    s3_key: str,
    original_filename: str,
    content_type: str,
    max_downloads: int,
    encrypted_dek_b64: str = None,
    iv_b64: str = None,
    uploader_email: str = None,
) -> None:
    """Insert a new link record into PostgreSQL."""
    session = get_session()
    try:
        link = Link(
            short_id=short_id,
            s3_key=s3_key,
            original_filename=original_filename,
            content_type=content_type,
            encrypted_dek_b64=encrypted_dek_b64,
            iv_b64=iv_b64,
            max_downloads=max_downloads,
            downloads_left=max_downloads,
            uploader_email=uploader_email,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=settings.LINK_TTL_SECONDS),
        )
        session.add(link)
        session.commit()
        logger.info(f"DB: Inserted link {short_id}")
    except Exception as e:
        session.rollback()
        logger.error(f"DB: Failed to insert link {short_id}: {e}")
        raise
    finally:
        session.close()


def db_get_link(short_id: str) -> dict | None:
    """Fetch a link from PostgreSQL by short_id. Returns None if not found or inactive."""
    session = get_read_session()
    try:
        link = session.query(Link).filter(
            Link.short_id == short_id,
            Link.is_active == True,
        ).first()
        if link is None:
            return None

        # Check if expired
        now = datetime.now(timezone.utc)
        if link.expires_at.replace(tzinfo=timezone.utc) < now:
            return None

        return {
            "s3_key": link.s3_key,
            "original_filename": link.original_filename,
            "content_type": link.content_type,
            "downloads_left": link.downloads_left,
            "max_downloads": link.max_downloads,
            "encrypted_dek_b64": link.encrypted_dek_b64,
            "iv_b64": link.iv_b64,
        }
    finally:
        session.close()


def db_decrement_downloads(short_id: str) -> int:
    """Decrement downloads_left in PostgreSQL. Returns remaining count, -1 if not found."""
    session = get_session()
    try:
        link = session.query(Link).filter(
            Link.short_id == short_id,
            Link.is_active == True,
        ).first()
        if link is None:
            return -1

        link.downloads_left = max(0, link.downloads_left - 1)
        remaining = link.downloads_left

        if remaining <= 0:
            link.is_active = False
            logger.info(f"DB: Link {short_id} exhausted — marked inactive")

        session.commit()
        return remaining
    except Exception as e:
        session.rollback()
        logger.error(f"DB: Failed to decrement downloads for {short_id}: {e}")
        raise
    finally:
        session.close()


def db_get_user_uploads(email: str) -> list[dict]:
    """Fetch upload history for a user from the read replica."""
    session = get_read_session()
    try:
        links = session.query(Link).filter(
            Link.uploader_email == email,
        ).order_by(Link.created_at.desc()).limit(50).all()

        now = datetime.now(timezone.utc)
        result = []
        for link in links:
            expired = link.expires_at.replace(tzinfo=timezone.utc) < now
            result.append({
                "short_id": link.short_id,
                "filename": link.original_filename,
                "max_downloads": link.max_downloads,
                "downloads_left": link.downloads_left,
                "active": link.is_active and not expired,
            })
        return result
    finally:
        session.close()


def db_log_action(short_id: str, action: str, uploader_email: str = None,
                  original_filename: str = "", file_size_bytes: int = None) -> None:
    """Insert an audit log entry."""
    session = get_session()
    try:
        log = UploadAuditLog(
            short_id=short_id,
            uploader_email=uploader_email,
            original_filename=original_filename,
            file_size_bytes=file_size_bytes,
            action=action,
        )
        session.add(log)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.warning(f"DB: Failed to log action {action} for {short_id}: {e}")
    finally:
        session.close()
