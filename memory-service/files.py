"""MinIO file operations."""

from __future__ import annotations

import io
import logging
import mimetypes
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from minio import Minio

from config import settings

logger = logging.getLogger(__name__)

_client: Minio | None = None


def get_minio() -> Minio:
    global _client
    if _client is None:
        _client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        # Ensure bucket exists
        if not _client.bucket_exists(settings.minio_bucket):
            _client.make_bucket(settings.minio_bucket)
    return _client


def _object_key(mime_type: str, original_name: str) -> str:
    """Generate a date-partitioned object key."""
    now = datetime.now(timezone.utc)

    if mime_type.startswith("image/"):
        prefix = "photos"
    elif mime_type.startswith("audio/"):
        prefix = "audio"
    elif mime_type in ("application/pdf", "application/msword") or mime_type.startswith("text/"):
        prefix = "documents"
    else:
        prefix = "misc"

    ext = Path(original_name).suffix or mimetypes.guess_extension(mime_type) or ""
    return f"{prefix}/{now:%Y}/{now:%m}/{uuid.uuid4()}{ext}"


def upload_file(
    file_data: bytes,
    original_name: str,
    mime_type: str,
) -> tuple[str, int]:
    """Upload file to MinIO. Returns (minio_key, size_bytes)."""
    client = get_minio()
    key = _object_key(mime_type, original_name)

    client.put_object(
        settings.minio_bucket,
        key,
        io.BytesIO(file_data),
        length=len(file_data),
        content_type=mime_type,
    )

    return key, len(file_data)


def get_presigned_url(minio_key: str, expires_hours: int = 1) -> str:
    """Generate a presigned URL for file access."""
    client = get_minio()
    return client.presigned_get_object(
        settings.minio_bucket,
        minio_key,
        expires=timedelta(hours=expires_hours),
    )


def delete_file(minio_key: str):
    """Delete a file from MinIO."""
    client = get_minio()
    client.remove_object(settings.minio_bucket, minio_key)


def ping() -> bool:
    try:
        client = get_minio()
        client.bucket_exists(settings.minio_bucket)
        return True
    except Exception:
        return False
