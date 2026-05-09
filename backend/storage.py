"""
CloudDrop — S3 Storage Module

Handles upload and download of encrypted file blobs to/from Amazon S3.
The encrypted DEK and IV are stored as S3 object metadata, keeping
everything self-contained in a single object.
"""

import boto3
import logging

from config import settings

logger = logging.getLogger("clouddrop.storage")

# ── S3 Client ───────────────────────────────────────────────────────────────

_s3_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=settings.AWS_REGION)
    return _s3_client


# ── Upload ───────────────────────────────────────────────────────────────────

def upload_to_s3(
    object_key: str,
    encrypted_data: bytes,
    encrypted_dek_b64: str,
    iv_b64: str,
    original_filename: str,
    content_type: str,
) -> str:
    """
    Upload an encrypted file blob to S3.

    The encrypted DEK and IV are stored as S3 user-metadata so they travel
    with the object and can be retrieved during decryption.

    Args:
        object_key:          S3 key (e.g., "uploads/abc123.enc").
        encrypted_data:      The AES-256-GCM ciphertext bytes.
        encrypted_dek_b64:   Base64-encoded encrypted DEK.
        iv_b64:              Base64-encoded IV.
        original_filename:   Original uploaded filename (for Content-Disposition).
        content_type:        MIME type of the original file.

    Returns:
        The S3 URI of the uploaded object.
    """
    s3 = _get_s3_client()

    s3.put_object(
        Bucket=settings.S3_BUCKET,
        Key=object_key,
        Body=encrypted_data,
        ContentType="application/octet-stream",
        Metadata={
            "encrypted-dek": encrypted_dek_b64,
            "iv": iv_b64,
            "original-filename": original_filename,
            "original-content-type": content_type,
        },
    )

    s3_uri = f"s3://{settings.S3_BUCKET}/{object_key}"
    logger.info(f"Uploaded encrypted object to {s3_uri}")
    return s3_uri


# ── Download ─────────────────────────────────────────────────────────────────

def download_from_s3(object_key: str) -> dict:
    """
    Download an encrypted file blob from S3.

    Returns:
        dict with keys:
            - encrypted_data (bytes)
            - encrypted_dek_b64 (str)
            - iv_b64 (str)
            - original_filename (str)
            - original_content_type (str)
    """
    s3 = _get_s3_client()

    response = s3.get_object(
        Bucket=settings.S3_BUCKET,
        Key=object_key,
    )

    metadata = response["Metadata"]

    result = {
        "encrypted_data": response["Body"].read(),
        "encrypted_dek_b64": metadata["encrypted-dek"],
        "iv_b64": metadata["iv"],
        "original_filename": metadata.get("original-filename", "download"),
        "original_content_type": metadata.get("original-content-type", "application/octet-stream"),
    }

    logger.info(f"Downloaded encrypted object: {object_key}")
    return result


# ── Delete ───────────────────────────────────────────────────────────────────

def delete_from_s3(object_key: str) -> None:
    """Delete an object from S3 (called when download counter hits 0)."""
    s3 = _get_s3_client()
    s3.delete_object(Bucket=settings.S3_BUCKET, Key=object_key)
    logger.info(f"Deleted S3 object: {object_key}")
