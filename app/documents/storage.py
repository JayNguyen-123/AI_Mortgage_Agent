"""
Document storage abstraction.

Local filesystem implementation for dev/test. Production should use
`S3Storage` / `GCSStorage` (stubbed below -- fill in with boto3 /
google-cloud-storage) so raw borrower documents never sit on the API
host's local disk, and so you get encryption-at-rest, access logging,
and lifecycle/retention policies for free from the cloud provider.

storage_uri scheme:
  local://<relative-path>
  s3://<bucket>/<key>
  gcs://<bucket>/<key>
"""
from __future__ import annotations

import abc
import logging
import uuid
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


class StorageBackend(abc.ABC):
    @abc.abstractmethod
    def save(self, file_bytes: bytes, loan_id: str, doc_type: str, filename: str) -> str:
        """Persist bytes, return a storage_uri."""

    @abc.abstractmethod
    def load(self, storage_uri: str) -> bytes:
        """Fetch bytes given a storage_uri previously returned by save()."""


class LocalFilesystemStorage(StorageBackend):
    def __init__(self, base_dir: str = "/tmp/mortgage-agent-docs"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, file_bytes: bytes, loan_id: str, doc_type: str, filename: str) -> str:
        safe_name = f"{uuid.uuid4().hex}_{Path(filename).name}"
        rel_path = Path(loan_id) / doc_type / safe_name
        full_path = self.base_dir / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(file_bytes)
        logger.info("Saved document to %s (dev-only local storage)", full_path)
        return f"local://{rel_path.as_posix()}"

    def load(self, storage_uri: str) -> bytes:
        if not storage_uri.startswith("local://"):
            raise ValueError(f"Not a local:// URI: {storage_uri}")
        rel_path = storage_uri[len("local://"):]
        return (self.base_dir / rel_path).read_bytes()


class S3Storage(StorageBackend):
    """Fill in with boto3 before using. Kept as a clear extension point
    rather than a fake implementation that would silently no-op."""

    def __init__(self, bucket: str):
        self.bucket = bucket

    def save(self, file_bytes: bytes, loan_id: str, doc_type: str, filename: str) -> str:
        raise NotImplementedError(
            "Wire this to boto3 s3.put_object(...) with server-side "
            "encryption (SSE-KMS) enabled, and a bucket policy that "
            "blocks public access."
        )

    def load(self, storage_uri: str) -> bytes:
        raise NotImplementedError("Wire this to boto3 s3.get_object(...).")


class GCSStorage(StorageBackend):
    """Fill in with google-cloud-storage before using."""

    def __init__(self, bucket: str):
        self.bucket = bucket

    def save(self, file_bytes: bytes, loan_id: str, doc_type: str, filename: str) -> str:
        raise NotImplementedError("Wire this to google.cloud.storage Client().bucket(...).blob(...).upload_from_string(...).")

    def load(self, storage_uri: str) -> bytes:
        raise NotImplementedError("Wire this to google.cloud.storage blob.download_as_bytes().")


_backend: StorageBackend | None = None


def get_storage_backend() -> StorageBackend:
    """
    Dev default: local filesystem. Set DOC_STORAGE_BUCKET + swap this
    factory to S3Storage/GCSStorage for any real deployment -- local
    filesystem storage must never hold real borrower PII in production.
    """
    global _backend
    if _backend is None:
        settings = get_settings()
        logger.warning(
            "Using LocalFilesystemStorage (bucket=%s ignored) -- "
            "replace with S3Storage/GCSStorage before handling real "
            "borrower documents.",
            settings.doc_storage_bucket,
        )
        _backend = LocalFilesystemStorage()
    return _backend
