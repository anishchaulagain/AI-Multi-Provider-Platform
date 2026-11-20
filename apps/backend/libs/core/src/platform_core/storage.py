"""Storage for raw uploads until they are processed.

Workers never receive file bytes over the queue: the API writes the object,
the job carries only its key.

- RedisBlobStore (default): the file is held temporarily (with a TTL) and deleted
  once ingestion finishes, so the original is never kept or downloadable.
- MinioBlobStore: durable S3-compatible storage, for deployments that need it.
"""

import asyncio
import io
from typing import Protocol
from urllib.parse import urlparse

from minio import Minio
from minio.deleteobjects import DeleteObject
from minio.error import S3Error
from redis.asyncio import Redis


class BlobNotFoundError(Exception):
    pass


class BlobStore(Protocol):
    async def ensure_bucket(self) -> None: ...

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def exists(self, key: str) -> bool: ...

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

    async def exists(self, key: str) -> bool:
        def run() -> bool:
            try:
                self._client.stat_object(self.bucket, key)
            except S3Error:
                return False
            return True

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


class RedisBlobStore:
    """Transient upload storage in Redis. Every object expires after `ttl_seconds`
    as a safety net; the worker deletes it as soon as ingestion is done."""

    def __init__(self, url: str, *, ttl_seconds: int = 24 * 3600, prefix: str = "upload:") -> None:
        # Separate client: file bytes must not be decoded as text.
        self._redis = Redis.from_url(url, decode_responses=False)
        self._ttl = ttl_seconds
        self._prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def ensure_bucket(self) -> None:
        return None

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await self._redis.set(self._key(key), data, ex=self._ttl)

    async def get(self, key: str) -> bytes:
        data = await self._redis.get(self._key(key))
        if data is None:
            raise BlobNotFoundError(key)
        return data if isinstance(data, bytes) else data.encode()

    async def exists(self, key: str) -> bool:
        return bool(await self._redis.exists(self._key(key)))

    async def delete_prefix(self, prefix: str) -> None:
        keys = [k async for k in self._redis.scan_iter(match=f"{self._key(prefix)}*")]
        if keys:
            await self._redis.delete(*keys)

    async def ping(self) -> None:
        await self._redis.ping()

    async def aclose(self) -> None:
        await self._redis.aclose()


class InMemoryBlobStore:
    """Test double."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def ensure_bucket(self) -> None:
        return None

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    async def exists(self, key: str) -> bool:
        return key in self.objects

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


def build_blob_store(
    kind: str,
    *,
    redis_url: str,
    retention_hours: int,
    s3_endpoint: str = "",
    s3_access_key: str = "",
    s3_secret_key: str = "",
    s3_bucket: str = "documents",
) -> BlobStore:
    if kind == "redis":
        return RedisBlobStore(redis_url, ttl_seconds=retention_hours * 3600)
    if kind == "minio":
        return MinioBlobStore(s3_endpoint, s3_access_key, s3_secret_key, s3_bucket)
    raise ValueError(f"Unknown BLOB_STORE '{kind}' (use 'redis' or 'minio')")
