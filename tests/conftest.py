"""Stub the AstrBot framework BEFORE the plugin modules are imported.

Pytest collects conftest.py before test modules, so any test that does
`from astrbot_plugin_webchat_gateway... import ...` will see the stubs
in `sys.modules` and never reach the real (uninstalled) astrbot package.

This lets CI run the suite without installing AstrBot — the framework
isn't pip-installable as a library, only as a deployable app, and we
don't want to drag its full runtime into the test environment.

Each test gets its own temp data dir via the `tmp_data_dir` fixture,
which also reconfigures the stub `StarTools.get_data_dir` to return it.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

# --- astrbot.api stub --------------------------------------------------------

# Logger name kept generic so call sites that do `from astrbot.api import
# logger` get a working logger object instead of None.
_logger = logging.getLogger("astrbot.stub")
_logger.addHandler(logging.NullHandler())

astrbot_pkg = types.ModuleType("astrbot")
astrbot_api = types.ModuleType("astrbot.api")
astrbot_api.logger = _logger  # type: ignore[attr-defined]
astrbot_api_star = types.ModuleType("astrbot.api.star")


class _StarToolsStub:
    """Mimic astrbot.api.star.StarTools.get_data_dir(plugin_name).

    Real `StarTools.get_data_dir` returns an absolute `Path` under
    AstrBot's working dir; this stub returns one under a per-test
    `tmp_path` so tests don't write outside their sandbox.
    """

    _data_root: Path = Path("/tmp/wcg_tests_default_root")

    @classmethod
    def get_data_dir(cls, plugin_name: str) -> Path:
        d = cls._data_root / plugin_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    @classmethod
    def _set_root(cls, root: Path) -> None:
        cls._data_root = root


astrbot_api_star.StarTools = _StarToolsStub  # type: ignore[attr-defined]
# Context + Star are imported by main.py — not exercised here but the
# import resolution must succeed if a test indirectly drags them in.
astrbot_api_star.Context = type("Context", (), {})  # type: ignore[attr-defined]
astrbot_api_star.Star = type("Star", (), {})  # type: ignore[attr-defined]

astrbot_api.AstrBotConfig = dict  # type: ignore[attr-defined]
astrbot_pkg.api = astrbot_api  # type: ignore[attr-defined]
astrbot_api.star = astrbot_api_star  # type: ignore[attr-defined]

sys.modules.setdefault("astrbot", astrbot_pkg)
sys.modules.setdefault("astrbot.api", astrbot_api)
sys.modules.setdefault("astrbot.api.star", astrbot_api_star)


# --- plugin import path ------------------------------------------------------
#
# Compute the plugin parent dir from this file's location so the suite
# is portable: developer machine, CI runner, anywhere — the layout is
# always `<parent>/astrbot_plugin_webchat_gateway/tests/conftest.py`.
# Python's namespace-package rules turn the plugin dir into an
# importable package without needing __init__.py at the root.

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent
_PLUGIN_PARENT = _PLUGIN_DIR.parent
if str(_PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_PARENT))


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Per-test isolated StarTools data root."""
    root = tmp_path / "starroot"
    root.mkdir()
    _StarToolsStub._set_root(root)
    return root


@pytest.fixture
def StarTools() -> type:
    return _StarToolsStub
