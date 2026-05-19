"""P0-3 regression test: `_is_no_such_key` recognises both shapes of
botocore's NoSuchKey error.

Failure mode the fix addresses: some botocore versions raise a typed
subclass whose class name contains "NoSuchKey" (the only case the
previous code handled); other versions raise a plain `ClientError`
with `response["Error"]["Code"] == "NoSuchKey"`. Class-name substring
matching alone misses the second shape — `R2FileStore.read` and
`.delete` then `logger.exception` for what is actually the
expected-missing path, polluting logs and obscuring real R2 incidents.

These tests:
  * Exercise both shapes (class-name and Error.Code) at the helper
    level
  * Exercise the read + delete paths end-to-end with a stub client
    that raises each shape, asserting `logger.exception` is NOT called
    in either case
  * Exercise a non-NoSuchKey failure to confirm we still log on real
    errors (no over-broad swallowing)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import pytest

from astrbot_plugin_webchat_gateway.core.file_store import (
    R2FileStore,
    _is_no_such_key,
)


# --- helper-level coverage --------------------------------------------------


class _ClassNameNoSuchKey(Exception):
    """Mimics the typed-subclass shape (e.g. `botocore.errorfactory.NoSuchKey`)."""


class _ClientErrorWithCode(Exception):
    """Mimics botocore's `ClientError`: plain Exception with a `response`
    attribute carrying the structured error payload."""

    def __init__(self, code: str, message: str = "no such key") -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class TestIsNoSuchKey:
    def test_class_name_shape(self):
        assert _is_no_such_key(_ClassNameNoSuchKey("gone")) is True

    def test_response_error_code_shape(self):
        """The shape the previous substring match missed."""
        assert _is_no_such_key(_ClientErrorWithCode("NoSuchKey")) is True

    def test_unrelated_class_name_with_no_response_is_not(self):
        assert _is_no_such_key(RuntimeError("bucket misconfigured")) is False

    def test_client_error_with_different_code_is_not(self):
        assert _is_no_such_key(_ClientErrorWithCode("AccessDenied")) is False

    def test_response_attribute_not_a_dict_is_not(self):
        exc = RuntimeError("weird")
        exc.response = "garbage"  # type: ignore[attr-defined]
        assert _is_no_such_key(exc) is False

    def test_response_error_not_a_dict_is_not(self):
        exc = RuntimeError("weird")
        exc.response = {"Error": "garbage"}  # type: ignore[attr-defined]
        assert _is_no_such_key(exc) is False


# --- end-to-end coverage of read + delete -----------------------------------


class _StubClient:
    """Programmable replacement for the aiobotocore S3 client.

    Each method either raises a pre-configured exception or returns a
    successful response. The test rebinds `_create_client` on a
    `_TestableR2FileStore` instance to return an async context yielding
    one of these.
    """

    def __init__(
        self,
        *,
        get_exc: BaseException | None = None,
        delete_exc: BaseException | None = None,
    ) -> None:
        self.get_exc = get_exc
        self.delete_exc = delete_exc
        self.get_calls = 0
        self.delete_calls = 0

    async def get_object(self, **kwargs: Any) -> Any:
        self.get_calls += 1
        if self.get_exc is not None:
            raise self.get_exc
        raise AssertionError("get_object called without configured exception")

    async def delete_object(self, **kwargs: Any) -> Any:
        self.delete_calls += 1
        if self.delete_exc is not None:
            raise self.delete_exc
        return {}


class _TestableR2FileStore(R2FileStore):
    """Bypass `__init__` (it requires aiobotocore + real credentials)
    and inject a stub client. The base class's `_create_client` is the
    only contact point with aiobotocore in the methods under test."""

    def __init__(self, *, client: _StubClient, tmp_path: Any) -> None:  # noqa: D401
        # Mirror just the fields read by `read` / `delete`. We deliberately
        # do NOT call super().__init__ — that would import aiobotocore and
        # require real R2 credentials.
        from pathlib import Path as _Path

        self._client = client
        self._bucket = "test-bucket"
        self._endpoint = ""
        self._access_key_id = ""
        self._secret_access_key = ""
        # `delete` also touches the local cache via `_ensure_cache_dir`,
        # which short-circuits when `self._cache_dir` is non-None. Use a
        # Path (not str) so the `cache_dir / basename` pathjoin works.
        import asyncio as _asyncio

        self._cache_dir = _Path(tmp_path) / "cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._key_locks = {}  # type: ignore[var-annotated]
        self._key_locks_meta_lock = _asyncio.Lock()
        self._trim_lock = _asyncio.Lock()

    def _create_client(self) -> Any:
        @asynccontextmanager
        async def _ctx():
            yield self._client

        return _ctx()


@pytest.mark.asyncio
class TestReadAndDeleteDoNotLogOnMissingKey:
    """The user-visible regression: `logger.exception` was called for
    the expected-missing path, dumping a full traceback into ops logs.
    These tests pin down the new behavior."""

    @pytest.mark.parametrize(
        "exc",
        [
            _ClassNameNoSuchKey("missing"),
            _ClientErrorWithCode("NoSuchKey"),
        ],
        ids=["class_name_shape", "response_error_code_shape"],
    )
    async def test_read_returns_none_without_exception_log(
        self,
        exc: BaseException,
        tmp_path: Any,
        caplog: pytest.LogCaptureFixture,
    ):
        client = _StubClient(get_exc=exc)
        store = _TestableR2FileStore(client=client, tmp_path=tmp_path)
        with caplog.at_level(logging.DEBUG, logger="astrbot.stub"):
            result = await store.read(storage_key="missing/object.bin")

        assert result is None
        assert client.get_calls == 1
        exception_records = [
            r for r in caplog.records if r.exc_info is not None
        ]
        assert not exception_records, (
            f"R2FileStore.read called logger.exception for the "
            f"expected-missing path: {[r.getMessage() for r in exception_records]!r}"
        )

    @pytest.mark.parametrize(
        "exc",
        [
            _ClassNameNoSuchKey("missing"),
            _ClientErrorWithCode("NoSuchKey"),
        ],
        ids=["class_name_shape", "response_error_code_shape"],
    )
    async def test_delete_swallows_silently_without_exception_log(
        self,
        exc: BaseException,
        tmp_path: Any,
        caplog: pytest.LogCaptureFixture,
    ):
        client = _StubClient(delete_exc=exc)
        store = _TestableR2FileStore(client=client, tmp_path=tmp_path)
        with caplog.at_level(logging.DEBUG, logger="astrbot.stub"):
            # delete is replay-safe — `None` return is the success
            # contract even when the key wasn't there.
            await store.delete(storage_key="missing/object.bin")

        assert client.delete_calls == 1
        exception_records = [
            r for r in caplog.records if r.exc_info is not None
        ]
        assert not exception_records, (
            f"R2FileStore.delete called logger.exception for the "
            f"expected-missing path: {[r.getMessage() for r in exception_records]!r}"
        )


@pytest.mark.asyncio
class TestRealErrorsStillSurface:
    """Inverse check: the helper must NOT broaden the swallow envelope.
    Genuine R2 errors (auth, network, malformed bucket) should still
    surface as `logger.exception` so ops can see them."""

    async def test_read_logs_on_unrelated_error(
        self, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ):
        client = _StubClient(get_exc=_ClientErrorWithCode("AccessDenied"))
        store = _TestableR2FileStore(client=client, tmp_path=tmp_path)
        with caplog.at_level(logging.DEBUG, logger="astrbot.stub"):
            result = await store.read(storage_key="some/key")

        assert result is None
        exception_records = [
            r for r in caplog.records if r.exc_info is not None
        ]
        assert len(exception_records) == 1, (
            f"Expected exactly one logger.exception for AccessDenied; "
            f"got {len(exception_records)}: "
            f"{[r.getMessage() for r in exception_records]!r}"
        )

    async def test_delete_logs_on_unrelated_error(
        self, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ):
        client = _StubClient(delete_exc=RuntimeError("network down"))
        store = _TestableR2FileStore(client=client, tmp_path=tmp_path)
        with caplog.at_level(logging.DEBUG, logger="astrbot.stub"):
            await store.delete(storage_key="some/key")

        exception_records = [
            r for r in caplog.records if r.exc_info is not None
        ]
        assert len(exception_records) == 1, (
            f"Expected exactly one logger.exception for RuntimeError; "
            f"got {len(exception_records)}: "
            f"{[r.getMessage() for r in exception_records]!r}"
        )
