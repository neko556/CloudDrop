"""
CloudDrop — KMS Envelope Encryption Module

Implements envelope encryption using AWS KMS:
  1. KMS generates a Data Encryption Key (DEK).
  2. The plaintext DEK encrypts the file with AES-256-GCM.
  3. Only the *encrypted* DEK is stored alongside the ciphertext.
  4. On decryption, KMS decrypts the DEK, which then decrypts the file.

This means the master key never leaves KMS, and each file gets a unique DEK.
"""

import boto3
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import os
import base64
import logging

from config import settings

logger = logging.getLogger("clouddrop.encryption")

# ── KMS Client ──────────────────────────────────────────────────────────────

_kms_client = None


def _get_kms_client():
    global _kms_client
    if _kms_client is None:
        _kms_client = boto3.client("kms", region_name=settings.AWS_REGION)
    return _kms_client


# ── Envelope Encryption ─────────────────────────────────────────────────────

def encrypt_file(plaintext: bytes) -> tuple[bytes, str, str]:
    """
    Encrypt a file using KMS Envelope Encryption.

    Args:
        plaintext: Raw file bytes to encrypt.

    Returns:
        (ciphertext, encrypted_dek_b64, iv_b64)
        - ciphertext:         AES-256-GCM encrypted file bytes.
        - encrypted_dek_b64:  Base64-encoded *encrypted* DEK (safe to store).
        - iv_b64:             Base64-encoded 12-byte nonce / IV.
    """
    kms = _get_kms_client()

    # Step 1: Ask KMS for a fresh Data Encryption Key
    dek_response = kms.generate_data_key(
        KeyId=settings.KMS_KEY_ID,
        KeySpec="AES_256",
    )
    plaintext_dek = dek_response["Plaintext"]       # 32 bytes — use then discard
    encrypted_dek = dek_response["CiphertextBlob"]   # Encrypted copy — safe to store

    # Step 2: Generate a random 12-byte IV for AES-GCM
    iv = os.urandom(12)

    # Step 3: Encrypt the file content with the plaintext DEK
    aesgcm = AESGCM(plaintext_dek)
    ciphertext = aesgcm.encrypt(iv, plaintext, None)

    # Step 4: Zero out the plaintext DEK from memory (best-effort in Python)
    plaintext_dek = b"\x00" * len(plaintext_dek)  # noqa: F841

    logger.info("File encrypted with envelope encryption (AES-256-GCM)")

    return (
        ciphertext,
        base64.b64encode(encrypted_dek).decode("utf-8"),
        base64.b64encode(iv).decode("utf-8"),
    )


def decrypt_file(ciphertext: bytes, encrypted_dek_b64: str, iv_b64: str) -> bytes:
    """
    Decrypt a file using KMS Envelope Decryption.

    Args:
        ciphertext:          AES-256-GCM encrypted file bytes.
        encrypted_dek_b64:   Base64-encoded encrypted DEK from S3 metadata.
        iv_b64:              Base64-encoded IV from S3 metadata.

    Returns:
        The original plaintext file bytes.
    """
    kms = _get_kms_client()

    # Step 1: Ask KMS to decrypt the DEK
    encrypted_dek = base64.b64decode(encrypted_dek_b64)
    iv = base64.b64decode(iv_b64)

    dek_response = kms.decrypt(CiphertextBlob=encrypted_dek)
    plaintext_dek = dek_response["Plaintext"]

    # Step 2: Decrypt the file with the plaintext DEK
    aesgcm = AESGCM(plaintext_dek)
    plaintext = aesgcm.decrypt(iv, ciphertext, None)

    # Step 3: Zero out the DEK (best-effort)
    plaintext_dek = b"\x00" * len(plaintext_dek)  # noqa: F841

    logger.info("File decrypted successfully via KMS envelope decryption")

    return plaintext
