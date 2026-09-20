"""Exercise the real MySQL migration ladder with a recording DB driver.

No MySQL server is required. These checks cover migration ordering, restart
after partial DDL, and capacity for Office MIME types; they do not execute
SQL on a MySQL engine.
"""

from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_webchat_gateway.storage.ddl import CURRENT_SCHEMA_VERSION


class RecordingCursor:
    def __init__(self, version):
        self.version = version
        self.statements = []
        self.fail_message_alter = False

    async def execute(self, sql, params=None):
        self.statements.append(sql)
        if self.fail_message_alter and sql.startswith("ALTER TABLE webchat_drop_messages"):
            raise RuntimeError("interrupted migration")
        if sql.startswith(("INSERT INTO _schema_meta", "UPDATE _schema_meta")):
            self.version = params[0]

    async def fetchone(self):
        return (self.version,) if self.version else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def mysql_storage(monkeypatch):
    # Keep the optional production driver optional in unit-test installs.
    driver = ModuleType("aiomysql")
    driver.create_pool = AsyncMock()
    driver.OperationalError = type("OperationalError", (Exception,), {})
    constants = ModuleType("pymysql.constants")
    constants.CLIENT = SimpleNamespace(FOUND_ROWS=2)
    monkeypatch.setitem(sys.modules, "aiomysql", driver)
    monkeypatch.setitem(sys.modules, "pymysql", ModuleType("pymysql"))
    monkeypatch.setitem(sys.modules, "pymysql.constants", constants)
    # Load under a test-only name so other tests never inherit this driver.
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_webchat_gateway.storage._test_mysql_backend",
        Path(__file__).parents[1] / "storage" / "mysql_backend.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def make(version):
        cursor = RecordingCursor(version)
        storage = module.MysqlStorage("mysql://test@localhost/test")

        @asynccontextmanager
        async def write_tx():
            yield SimpleNamespace(cursor=lambda: cursor)

        storage._write_tx = write_tx
        return storage, cursor

    return make


OFFICE_MIMES = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "5", "6", CURRENT_SCHEMA_VERSION, "99"])
async def test_mysql_office_capacity_on_fresh_install_and_upgrade(mysql_storage, version):
    storage, cursor = mysql_storage(version)
    await storage.initialize()
    assert cursor.version == ("99" if version == "99" else CURRENT_SCHEMA_VERSION)
    alters = [sql for sql in cursor.statements if "MODIFY COLUMN mime" in sql]
    if version in ("5", "6"):
        assert len(alters) == 2
        definitions = alters
        # Only stamp the schema after BOTH upload and send tables can store PPTX.
        assert cursor.statements[-1].startswith("UPDATE _schema_meta")
    else:
        assert alters == []
        definitions = cursor.statements
    for table in ("webchat_files", "webchat_drop_messages"):
        definition = next(sql for sql in definitions if re.search(rf"\b{table}\b", sql))
        capacity = int(re.search(r"\bmime\s+VARCHAR\((\d+)\)", definition, re.I)[1])
        assert all(len(mime) <= capacity for mime in OFFICE_MIMES)
        assert "NOT NULL" in definition
        if table == "webchat_drop_messages":
            assert re.search(r"\bmime\s+VARCHAR\(\d+\)\s+NOT NULL DEFAULT ''", definition)


@pytest.mark.asyncio
async def test_partial_mysql_upgrade_retries_before_stamping_version(mysql_storage):
    storage, cursor = mysql_storage("6")
    cursor.fail_message_alter = True
    with pytest.raises(RuntimeError, match="interrupted migration"):
        await storage.initialize()
    assert cursor.version == "6"
    assert not any(sql.startswith("UPDATE _schema_meta") for sql in cursor.statements)

    cursor.fail_message_alter = False
    cursor.statements.clear()
    await storage.initialize()
    assert cursor.version == CURRENT_SCHEMA_VERSION
    assert len([sql for sql in cursor.statements if "MODIFY COLUMN mime" in sql]) == 2
