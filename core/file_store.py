"""File storage backends for WebChat image uploads.

Two implementations behind a single Protocol so the upload + serve
handlers don't care whether bytes land on local disk or in R2:

- `LocalFileStore` (default): writes under `{root}/{token}/{file_id}.{ext}`.
  Zero external dependencies. `signed_url` is unsupported (returns None);
  the serve endpoint always streams bytes through itself for local mode.

- `R2FileStore` (opt-in): Cloudflare R2 via aiobotocore's S3-compatible
  client. Lazy-imports `aiobotocore.session` inside `__init__` so users
  who never configure R2 don't take the ImportError on plugin load. The
  module itself is always importable — the missing-dep branch only fires
  when something tries to *construct* an R2FileStore.

`open_local_path` is the bridge between the storage layer and AstrBot's
`provider.text_chat_stream(image_urls=[...])`. AstrBot wants local file
paths; for R2 we materialize the object into a temp-dir LRU cache, keyed
by storage_key. A per-key `asyncio.Lock` serializes concurrent fetches
of the same object so a burst of "send this image" requests doesn't
trigger N redundant GETs.

The local cache trims on every miss-fetch: list cache files by mtime,
evict oldest until total size ≤ cap. This is O(N) per miss but N is
bounded by the cache size cap and the typical chat-image footprint
(~100 KB after frontend resize), so the work is negligible in practice.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from astrbot.api import logger

from .image_util import ALLOWED_MIME_TO_EXT


class FileStore(Protocol):
    """Storage-driver-agnostic surface for image files.

    All methods are coroutines so a remote backend (R2) and the local
    backend share a single signature. The `storage_key` is opaque to
    callers — for LocalFileStore it's a relative path under the
    configured root; for R2FileStore it's the object key in the bucket.
    Callers persist the key in the DB and pass it back to read/delete.
    """

    async def save(
        self, *, storage_key: str, content: bytes, mime: str
    ) -> None: ...

    async def read(self, *, storage_key: str) -> bytes | None: ...

    async def open_local_path(
        self, *, storage_key: str
    ) -> str | None: ...

    async def signed_url(
        self, *, storage_key: str, ttl_seconds: int
    ) -> str | None: ...

    async def delete(self, *, storage_key: str) -> None: ...


# ----------------------------------------------------------------------
# Local-disk backend
# ----------------------------------------------------------------------


class LocalFileStore:
    """Writes files to a per-token subdirectory of `root`.

    Layout: `{root}/{token}/{file_id}.{ext}` where token is encoded as
    the first path segment of `storage_key`. The caller (upload handler)
    is responsible for the per-token subdir scheme — this class just
    enforces that the resolved final path stays inside `root` so a
    forged `storage_key` can't escape via `..` segments.
    """

    def __init__(self, root: str) -> None:
        self._root_path = Path(root).resolve()
        self._root_str = str(self._root_path)
        # Lazy directory create: we don't make the root at __init__
        # because the configured path may not exist yet (first plugin
        # load on a fresh AstrBot install). `save()` creates the
        # per-token subdir on demand.

    def _safe_resolve(self, storage_key: str) -> Path | None:
        """Return the absolute path for `storage_key` IF it resolves
        inside `_root_path`. Returns None on traversal attempts.

        Uses `Path.resolve()` + `is_relative_to()` rather than string
        prefix checks so symlink games, `..` segments, and absolute
        path injection are all caught uniformly.
        """
        if not storage_key:
            return None
        # Reject any storage_key that looks absolute — storage_key is
        # always relative to root by contract.
        candidate = (self._root_path / storage_key).resolve()
        try:
            if not candidate.is_relative_to(self._root_path):
                return None
        except ValueError:
            # is_relative_to raises ValueError on some Windows path
            # combos (cross-drive); treat as traversal.
            return None
        return candidate

    async def save(
        self, *, storage_key: str, content: bytes, mime: str
    ) -> None:
        """Persist `content` at the relative path `storage_key` under
        `self._root`. The caller (upload handler) is the authoritative
        owner of the storage_key layout — see PLAN_image_upload.md
        §"Storage path convention": `{token}/{file_id}.{ext}`. The
        `mime` argument is unused for local storage (filesystem has no
        notion of MIME) but kept on the Protocol for backend parity.
        """
        del mime  # unused on the local backend
        target = self._safe_resolve(storage_key)
        if target is None:
            raise ValueError(f"unsafe storage_key for save(): {storage_key!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Sync write inside a thread so the event loop isn't blocked on
        # disk I/O. For images <=20 MB this is fast enough that a thread
        # offload is the right tradeoff (vs. pulling in aiofiles).
        await asyncio.to_thread(_write_bytes_sync, str(target), content)

    async def read(self, *, storage_key: str) -> bytes | None:
        target = self._safe_resolve(storage_key)
        if target is None or not target.is_file():
            return None
        try:
            return await asyncio.to_thread(_read_bytes_sync, str(target))
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.exception(
                "[WebChatGateway] LocalFileStore.read failed key=%s err=%s",
                storage_key,
                exc,
            )
            return None

    async def open_local_path(
        self, *, storage_key: str
    ) -> str | None:
        target = self._safe_resolve(storage_key)
        if target is None:
            return None
        # `is_file` instead of `exists` so a stray directory at the key
        # doesn't get returned as a path the LLM bridge would then fail
        # to open.
        if not target.is_file():
            return None
        return str(target)

    async def signed_url(
        self, *, storage_key: str, ttl_seconds: int
    ) -> str | None:
        # Local disk has no signing concept — the serve handler always
        # proxies bytes for local mode.
        return None

    async def delete(self, *, storage_key: str) -> None:
        target = self._safe_resolve(storage_key)
        if target is None:
            return
        try:
            await asyncio.to_thread(_unlink_sync, str(target))
        except FileNotFoundError:
            # Idempotent: a re-run of cascade cleanup must not raise on
            # already-deleted files.
            pass
        except OSError as exc:
            logger.exception(
                "[WebChatGateway] LocalFileStore.delete failed key=%s err=%s",
                storage_key,
                exc,
            )


def _write_bytes_sync(path: str, content: bytes) -> None:
    # Atomic-ish write: write to a sibling .tmp and rename. Protects
    # against torn writes if the process is killed mid-flush; the file
    # either exists fully or not at all. `os.replace` is atomic on the
    # same filesystem (POSIX + Windows).
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "wb") as fh:
        fh.write(content)
    os.replace(tmp_path, path)


def _read_bytes_sync(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _unlink_sync(path: str) -> None:
    os.unlink(path)


# ----------------------------------------------------------------------
# R2 backend
# ----------------------------------------------------------------------


# Default cache TTL knobs. The cache size cap is configurable; these are
# internal constants for the on-miss bookkeeping.
_R2_CACHE_SUBDIR = "webchat_uploads_cache"


class R2FileStore:
    """Cloudflare R2-backed implementation.

    Uses aiobotocore with R2's S3-compatible endpoint. Each call opens
    a short-lived client via `session.create_client(...)` async context;
    we don't pin a long-lived client because aiobotocore's session is
    designed for per-operation use and internally pools connections.

    `open_local_path` materializes the object into AstrBot's temp dir
    (LRU-evicted by mtime). The cache exists so the LLM bridge can hand
    a real filesystem path to `provider.text_chat_stream` without
    blocking the request on a fresh R2 GET each time. A per-key
    `asyncio.Lock` serializes concurrent misses for the same object so
    parallel `/chat/stream` calls referencing the same image only do
    one network fetch.
    """

    def __init__(
        self,
        *,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        endpoint: str = "",
        cache_size_mb: int = 200,
    ) -> None:
        # Lazy import — mirrors `RedisBuffer.__init__` in stream_buffer.py.
        # The module file is always importable; only constructing this
        # class trips the dep check, so the plugin still loads on systems
        # without aiobotocore.
        try:
            import aiobotocore.session  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "R2FileStore requires the aiobotocore package. "
                "Install with: pip install aiobotocore>=2.13"
            ) from exc
        self._aiobotocore_session = aiobotocore.session
        self._account_id = account_id
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._bucket = bucket
        # R2 endpoints look like `https://<account>.r2.cloudflarestorage.com`.
        # If the caller passed an explicit endpoint we use it verbatim;
        # otherwise we synthesize from account_id (the common case).
        if endpoint:
            self._endpoint = endpoint.rstrip("/")
        elif account_id:
            self._endpoint = (
                f"https://{account_id}.r2.cloudflarestorage.com"
            )
        else:
            raise ValueError(
                "R2FileStore needs either an explicit endpoint or an account_id"
            )
        self._cache_size_bytes = max(0, int(cache_size_mb)) * 1024 * 1024
        # Lazy-create the cache directory on first open_local_path so
        # the module loads fine even before AstrBot's data path exists.
        self._cache_dir: Path | None = None
        # Per-key locks to serialize concurrent fetches of the same
        # object. Keys naturally bounded by the set of in-flight
        # downloads; we don't reap them — Python dict + tiny entries.
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._key_locks_lock = asyncio.Lock()
        # Store-level mutex serialising every `_trim_cache_dir_sync`
        # call. Without this, coroutine A trimming after just writing
        # file_A can evict file_B (still being returned by coroutine B
        # whose own trim hasn't run yet) — `protect_path` only shields
        # the file the current call just wrote. A single global lock
        # makes the "protect just-written file" contract extend across
        # concurrent fetches at the cost of serialising the trim work
        # itself, which is sub-100ms even on slow disks.
        self._trim_lock = asyncio.Lock()

    def _create_client(self) -> Any:
        """Return an aiobotocore S3 client async-context for one op."""
        session = self._aiobotocore_session.get_session()
        return session.create_client(
            "s3",
            region_name="auto",
            endpoint_url=self._endpoint,
            aws_access_key_id=self._access_key_id,
            aws_secret_access_key=self._secret_access_key,
        )

    async def _key_lock(self, storage_key: str) -> asyncio.Lock:
        async with self._key_locks_lock:
            lock = self._key_locks.get(storage_key)
            if lock is None:
                lock = asyncio.Lock()
                self._key_locks[storage_key] = lock
            return lock

    async def _ensure_cache_dir(self) -> Path:
        if self._cache_dir is not None:
            return self._cache_dir
        # Import inside the method so the module load path doesn't
        # depend on AstrBot's runtime being ready.
        from astrbot.core.utils.io import get_astrbot_temp_path
        base = Path(get_astrbot_temp_path()) / _R2_CACHE_SUBDIR
        base.mkdir(parents=True, exist_ok=True)
        self._cache_dir = base
        return base

    @staticmethod
    def _cache_basename(storage_key: str) -> str:
        # Flatten "token/file_id.ext" → "token__file_id.ext" so the
        # cache directory is a single flat layer. Avoids per-token
        # subdirs in temp (less directory churn on rotation).
        return storage_key.replace("/", "__").replace("\\", "__")

    async def save(
        self, *, storage_key: str, content: bytes, mime: str
    ) -> None:
        """PUT `content` to the R2 bucket with key=`storage_key`. The
        caller is authoritative on the storage_key layout
        (PLAN: `{token}/{file_id}.{ext}`); we just hand it through
        unchanged so the on-disk-vs-R2 layout stays 1:1.
        """
        try:
            async with self._create_client() as client:
                await client.put_object(
                    Bucket=self._bucket,
                    Key=storage_key,
                    Body=content,
                    ContentType=mime,
                )
        except Exception:
            logger.exception(
                "[WebChatGateway] R2FileStore.save failed key=%s", storage_key
            )
            raise

    async def read(self, *, storage_key: str) -> bytes | None:
        try:
            async with self._create_client() as client:
                resp = await client.get_object(
                    Bucket=self._bucket, Key=storage_key
                )
                async with resp["Body"] as body_stream:
                    return await body_stream.read()
        except Exception as exc:
            # NoSuchKey is the common-and-expected miss; everything else
            # is logged. aiobotocore exposes it via the client error
            # class but checking exc-name pattern keeps us decoupled from
            # the exact import path.
            name = type(exc).__name__
            if "NoSuchKey" in name or "404" in str(exc):
                return None
            logger.exception(
                "[WebChatGateway] R2FileStore.read failed key=%s", storage_key
            )
            return None

    async def open_local_path(
        self, *, storage_key: str
    ) -> str | None:
        cache_dir = await self._ensure_cache_dir()
        cache_path = cache_dir / self._cache_basename(storage_key)
        # Cheap fast path: already-cached. Bump mtime so the LRU eviction
        # heuristic treats this as recently used.
        if cache_path.is_file():
            try:
                now = time.time()
                os.utime(cache_path, (now, now))
            except OSError:
                pass
            return str(cache_path)
        # Miss path: serialize concurrent fetches for the same key so we
        # don't N-times-GET the same object under load.
        lock = await self._key_lock(storage_key)
        async with lock:
            # Recheck after acquiring lock — another waiter may have
            # filled the cache while we were parked.
            if cache_path.is_file():
                return str(cache_path)
            content = await self.read(storage_key=storage_key)
            if content is None:
                return None
            try:
                await asyncio.to_thread(
                    _write_bytes_sync, str(cache_path), content
                )
            except OSError as exc:
                logger.exception(
                    "[WebChatGateway] R2FileStore cache write failed "
                    "key=%s err=%s",
                    storage_key,
                    exc,
                )
                return None
            # Trim cache after a successful add. Eviction is best-effort:
            # a failure here just leaves the cache over-cap for a bit.
            # `protect_path` shields the just-written file from being
            # evicted in the same pass — without this, when
            # `cache_size_mb < single_file_size` the trim would
            # immediately unlink the file we're about to hand back to
            # the caller, who would then `open()` a non-existent path.
            # The store-level `_trim_lock` extends that shield across
            # concurrent fetches: without it, coroutine A's trim run
            # could evict coroutine B's just-written file (not in A's
            # protect set), and B would return a dead path.
            try:
                async with self._trim_lock:
                    await asyncio.to_thread(
                        _trim_cache_dir_sync,
                        str(cache_dir),
                        self._cache_size_bytes,
                        str(cache_path),
                    )
            except Exception:
                logger.exception(
                    "[WebChatGateway] R2FileStore cache trim failed"
                )
        return str(cache_path)

    async def signed_url(
        self, *, storage_key: str, ttl_seconds: int
    ) -> str | None:
        try:
            async with self._create_client() as client:
                url = await client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._bucket, "Key": storage_key},
                    ExpiresIn=max(1, int(ttl_seconds)),
                )
                return url
        except Exception:
            logger.exception(
                "[WebChatGateway] R2FileStore.signed_url failed key=%s",
                storage_key,
            )
            return None

    async def delete(self, *, storage_key: str) -> None:
        # Remove from R2 first, then from local cache. If R2 errors, we
        # still drop the cache entry — keeps cache consistent with
        # "this key is gone".
        try:
            async with self._create_client() as client:
                await client.delete_object(
                    Bucket=self._bucket, Key=storage_key
                )
        except Exception as exc:
            name = type(exc).__name__
            if "NoSuchKey" not in name:
                logger.exception(
                    "[WebChatGateway] R2FileStore.delete (R2) failed key=%s",
                    storage_key,
                )
        # Local cache eviction. Lazy ensure_cache_dir if it never
        # initialised — gives a clean path even if delete is called
        # before any open_local_path.
        try:
            cache_dir = await self._ensure_cache_dir()
            cache_path = cache_dir / self._cache_basename(storage_key)
            await asyncio.to_thread(_unlink_sync, str(cache_path))
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception(
                "[WebChatGateway] R2FileStore.delete (cache) failed key=%s",
                storage_key,
            )


def _trim_cache_dir_sync(
    cache_dir: str, max_bytes: int, protect_path: str | None = None
) -> None:
    """Walk `cache_dir`, evict oldest-mtime files until ≤ max_bytes.

    Sync because the work is short and runs inside `asyncio.to_thread`
    anyway. If max_bytes is 0 or negative we skip — a disabled cap.
    `protect_path`, if set, is never evicted — used to shield a file
    that was just written and is about to be returned to the caller.
    """
    if max_bytes <= 0:
        return
    try:
        entries: list[tuple[float, int, str]] = []
        with os.scandir(cache_dir) as it:
            for ent in it:
                if not ent.is_file(follow_symlinks=False):
                    continue
                try:
                    stat = ent.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append((stat.st_mtime, stat.st_size, ent.path))
    except FileNotFoundError:
        return
    total = sum(size for _, size, _ in entries)
    if total <= max_bytes:
        return
    # Oldest first.
    entries.sort(key=lambda row: row[0])
    protect_resolved = (
        os.path.realpath(protect_path) if protect_path else None
    )
    for _mtime, size, path in entries:
        if total <= max_bytes:
            break
        if (
            protect_resolved is not None
            and os.path.realpath(path) == protect_resolved
        ):
            continue
        try:
            os.unlink(path)
            total -= size
        except FileNotFoundError:
            continue
        except OSError:
            # Skip a file we can't remove; the next pass will pick it up.
            continue


# ----------------------------------------------------------------------
# Factory helper
# ----------------------------------------------------------------------


def make_file_store_from_config(uploads_cfg: Any) -> FileStore:
    """Construct a FileStore from the parsed `UploadsConfig` dataclass.

    Falls back to LocalFileStore (with a warning) if R2 is requested but
    aiobotocore isn't installed — the plugin still works, uploads still
    land somewhere, the operator gets a startup hint about the missing
    dep instead of a hard crash.

    The exact attribute names on `uploads_cfg` are part of the config
    surface owned by Agent C; this helper reads them defensively via
    `getattr` with sensible defaults so a partial config doesn't blow
    up before Agent C lands.
    """
    driver = str(getattr(uploads_cfg, "storage_driver", "local") or "local").lower()
    local_path = getattr(uploads_cfg, "local_path", "") or ""
    if driver == "r2":
        bucket = getattr(uploads_cfg, "r2_bucket", "") or ""
        if not bucket:
            logger.warning(
                "[WebChatGateway] storage_driver=r2 but r2_bucket is empty; "
                "falling back to LocalFileStore"
            )
            return LocalFileStore(root=local_path)
        endpoint = getattr(uploads_cfg, "r2_endpoint", "") or ""
        if endpoint:
            # Defensive URL validation. aiobotocore.create_client builds
            # malformed URLs if we hand it `s3.example.com` (no scheme),
            # then PUT/GET fail at first request with cryptic errors.
            # Fail fast at startup instead.
            try:
                parsed_endpoint = urlparse(endpoint)
            except Exception:
                parsed_endpoint = None
            if (
                parsed_endpoint is None
                or parsed_endpoint.scheme not in ("http", "https")
                or not parsed_endpoint.netloc
            ):
                logger.warning(
                    "[WebChatGateway] r2_endpoint=%r is not a valid"
                    " http(s):// URL; falling back to LocalFileStore",
                    endpoint,
                )
                return LocalFileStore(root=local_path)
        try:
            return R2FileStore(
                account_id=getattr(uploads_cfg, "r2_account_id", "") or "",
                access_key_id=getattr(uploads_cfg, "r2_access_key_id", "") or "",
                secret_access_key=getattr(
                    uploads_cfg, "r2_secret_access_key", ""
                ) or "",
                bucket=bucket,
                endpoint=endpoint,
                cache_size_mb=int(
                    getattr(uploads_cfg, "r2_cache_size_mb", 200) or 200
                ),
            )
        except RuntimeError as exc:
            # aiobotocore missing.
            logger.warning(
                "[WebChatGateway] R2FileStore unavailable (%s); "
                "falling back to LocalFileStore",
                exc,
            )
            return LocalFileStore(root=local_path)
        except Exception:
            logger.exception(
                "[WebChatGateway] R2FileStore construction failed; "
                "falling back to LocalFileStore"
            )
            return LocalFileStore(root=local_path)
    return LocalFileStore(root=local_path)


__all__ = [
    "FileStore",
    "LocalFileStore",
    "R2FileStore",
    "make_file_store_from_config",
]
