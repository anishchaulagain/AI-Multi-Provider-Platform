"""Object storage for raw uploads (MinIO / any S3-compatible store).

Workers never receive file bytes over the queue: the API writes the object,
the job carries only its key.
"""

import asyncio
import io
from typing import Protocol
from urllib.parse import urlparse

from minio import Minio
from minio.deleteobjects import DeleteObject
from minio.error import S3Error


class BlobNotFoundError(Exception):
    pass


class BlobStore(Protocol):
    async def ensure_bucket(self) -> None: ...

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete_prefix(self, prefix: str) -> None: ...

    async def ping(self) -> None: ...


class MinioBlobStore:
    """The minio client is synchronous; calls run in a worker thread."""

    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str) -> None:
        url = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
        self._client = Minio(
            url.netloc,
            access_key=access_key,
            secret_key=secret_key,
            secure=url.scheme == "https",
        )
        self.bucket = bucket

    async def ensure_bucket(self) -> None:
        def run() -> None:
            if not self._client.bucket_exists(self.bucket):
                self._client.make_bucket(self.bucket)

        await asyncio.to_thread(run)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object,
            self.bucket,
            key,
            io.BytesIO(data),
            len(data),
            content_type=content_type,
        )

    async def get(self, key: str) -> bytes:
        def run() -> bytes:
            try:
                response = self._client.get_object(self.bucket, key)
            except S3Error as exc:
                if exc.code in ("NoSuchKey", "NoSuchBucket"):
                    raise BlobNotFoundError(key) from exc
                raise
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(run)

    async def delete_prefix(self, prefix: str) -> None:
        def run() -> None:
            objects = self._client.list_objects(self.bucket, prefix=prefix, recursive=True)
            to_delete = [DeleteObject(o.object_name) for o in objects if o.object_name]
            for error in self._client.remove_objects(self.bucket, to_delete):
                raise RuntimeError(f"Failed to delete {error.name}: {error.message}")

        await asyncio.to_thread(run)

    async def ping(self) -> None:
        await asyncio.to_thread(self._client.bucket_exists, self.bucket)


class InMemoryBlobStore:
    """Test double."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def ensure_bucket(self) -> None:
        return None

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    async def get(self, key: str) -> bytes:
        try:
            return self.objects[key][0]
        except KeyError as exc:
            raise BlobNotFoundError(key) from exc

    async def delete_prefix(self, prefix: str) -> None:
        for key in [k for k in self.objects if k.startswith(prefix)]:
            del self.objects[key]

    async def ping(self) -> None:
        return None
