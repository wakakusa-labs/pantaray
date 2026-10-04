"""Pantaray Agents Test Suite Configuration"""

import atexit
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# NOTE:
# - `pantaray_agents` の import は下の env baseline を適用したあとで行う。
#   `config_shared` は import 時に NODE_ENV を検証して落ちるため、
#   module 冒頭で import すると baseline より先に評価されてしまう。
# - `.env.test` は廃止し、通常どおり
#   `agents/.env.local-runtime.dev` / `agents/.env.local-runtime` / 明示 export / CI env を使う。
# - ただしテストは外部 secret や実サービスへ依存してはならないため、
#   conftest が安全な baseline を先に与える。
# - 明示 export / CI env は優先するが、provider secret は local runtime へ注入しない。

# pytest プロセスごとの runtime ルート。
# 固定 /tmp パスを既定にすると、別 worktree / 別インタプリタで同時に走る suite が
# app runtime manifest・sqlite・lock を互いに上書きして偽の失敗を起こす。
# macOS の Unix domain socket 長制限（control socket はこのルート配下に作られる）に
# 収まるよう、TMPDIR ではなく短い /tmp 直下へ作成する。
_TEST_SESSION_ROOT = Path(tempfile.mkdtemp(prefix="pnt-", dir="/tmp"))
atexit.register(shutil.rmtree, _TEST_SESSION_ROOT, ignore_errors=True)

# `pantaray_agents.settings_loader` の import 時に開発用 dotenv が os.environ へ
# 読み込まれるため、setdefault ではそちらが勝ってしまう。隔離パスは常に上書きする。
_TEST_SESSION_PATHS: dict[str, str] = {
    "LOG_FILE_PATH": str(_TEST_SESSION_ROOT / "agent.log"),
    "LOCAL_DB_PATH": str(_TEST_SESSION_ROOT / "runtime.sqlite3"),
    "LOCAL_ARTIFACT_ROOT": str(_TEST_SESSION_ROOT / "artifacts"),
    "LOCAL_APP_RUNTIME_MANIFEST_PATH": str(
        _TEST_SESSION_ROOT / "app-runtime-manifest.json"
    ),
    "LOCAL_RUNTIME_LOCK_PATH": str(_TEST_SESSION_ROOT / "runtime.lock"),
}

_TEST_ENV_DEFAULTS: dict[str, str] = {
    "NODE_ENV": "test",
    "USE_MOCKS": "true",
    "LOG_LEVEL": "INFO",
    "HOST": "0.0.0.0",
    "PORT": "8005",
    "RELOAD": "false",
    "ALLOWED_ORIGINS": "http://localhost:3001",
    "ALLOWED_HOSTS": "localhost",
    "LOCAL_DB_JOURNAL_MODE": "wal",
    "LOCAL_DB_BUSY_TIMEOUT_MS": "5000",
    "LOCAL_DB_CHECKPOINT_PAGES": "1000",
    "LOCAL_RETENTION_ACTIVITY_LOG_DAYS": "7",
    "LOCAL_RETENTION_EVENT_DAYS": "7",
    "PANTARAY_LOCAL_BACKEND_BOUND_HOST": "127.0.0.1",
    "PANTARAY_LOCAL_BACKEND_BOUND_PORT": "49152",
    "LOCAL_LOOPBACK_BIND_HOST": "127.0.0.1",
    "LOCAL_LOOPBACK_BIND_PORT": "8005",
    "LOCAL_SCHEDULER_TICK_SECONDS": "5",
    "LOCAL_ACTIVITY_WINDOW_MINUTES": "60",
    # ローカルランタイムはこれらを読まない。
    # `test_local_auth_lifecycle.py::test_local_app_starts_without_supabase_or_cloud_configuration`
    # が「設定があっても無くても起動する」側を確かめるために消すので、baseline に置く。
    "SUPABASE_JWT_AUD": "test",
    "SUPABASE_JWT_ISS": "https://test.example.com/auth/v1",
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_PUBLISHABLE_KEY": "test-supabase-public-key",
    "LLM_PROXY_URL": "https://llm-proxy.test",
    "LLM_PROXY_AUDIENCE": "llm-proxy",
    "WEB_TOOLS_PROXY_URL": "https://search-proxy.test",
    "WEB_TOOLS_PROXY_AUDIENCE": "search-proxy",
}


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_APP_RUNTIME_MANIFEST_PATH = _TEST_SESSION_PATHS["LOCAL_APP_RUNTIME_MANIFEST_PATH"]
with open(_APP_RUNTIME_MANIFEST_PATH, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "python_path": os.path.realpath(sys.executable),
            "python_version": platform.python_version(),
            "python_sha256": _hash_file(sys.executable),
        },
        handle,
    )

os.environ.update(_TEST_SESSION_PATHS)
for env_key, env_value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(env_key, env_value)

# テストは実 provider secret / AWS 設定を参照しない。
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("TAVILY_API_KEY", None)
os.environ.pop("AWS_REGION", None)

# テストは常に test/mock として実行する（誤って本番設定で動かさない）
os.environ["NODE_ENV"] = "test"
os.environ["USE_MOCKS"] = "true"

from pantaray_agents.local_runtime.runtime.connection_store import (  # noqa: E402
    reset_connection_store,
)
from pantaray_agents.local_runtime.runtime.session_store import (  # noqa: E402
    reset_desktop_session_store,
)
from pantaray_agents.local_runtime.tooling.brokering import (  # noqa: E402
    command_runtime,
)
from pantaray_agents.settings_loader import (  # noqa: E402
    clear_active_settings_module,
)


@pytest.fixture
def tokyo_local_zone() -> Iterator[None]:
    """Run as a Mac set to Asia/Tokyo, so what a model sees as local is fixed."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Tokyo"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.fixture(autouse=True)
def clear_mock_data():
    """利用可能な環境では共有モックデータを前後でクリアする。"""
    if importlib.util.find_spec("pydantic") is None:
        yield
        return

    from pantaray_agents.mock.mock_repository import MockRepository

    MockRepository.clear_data()
    yield
    MockRepository.clear_data()


@pytest.fixture(autouse=True)
def reset_active_settings_module() -> None:
    """各テスト間で active settings registration を持ち越さない。"""
    clear_active_settings_module()
    yield
    clear_active_settings_module()


@pytest.fixture(autouse=True)
def reset_local_desktop_session() -> None:
    """Process-global desktop auth state must never leak across tests."""
    reset_connection_store()
    reset_desktop_session_store()
    yield
    reset_connection_store()
    reset_desktop_session_store()


@pytest.fixture(autouse=True)
def use_process_path_for_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Commands see this process's PATH, not whatever the developer's dotfiles set."""
    monkeypatch.setattr(command_runtime, "login_shell_path", lambda: os.environ["PATH"])
