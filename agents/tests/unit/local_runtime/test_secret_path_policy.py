from __future__ import annotations

import shutil
import subprocess
import unicodedata
from dataclasses import replace
from pathlib import Path

import pytest
from tests.unit.local_runtime.ripgrep_backend_test_support import (
    install_fake_ripgrep_backend,
)

from pantaray_agents.local_runtime.tooling.brokering import action_path_policy
from pantaray_agents.local_runtime.tooling.outside_workspace_grant import (
    folder_can_be_granted,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files import read_paths, secret_paths
from pantaray_agents.tools.files.manifest_paths import (
    ResolvedManifestPath,
    resolve_local_path,
)
from pantaray_agents.tools.files.secret_paths import (
    SECRET_PATH_WITHHELD,
    SecretTier,
    secret_path_patterns,
    withheld_reason,
)

from .broker_test_support import _grant_workspace_full_access
from .path_access_policy_support import bootstrap_path_policy_runtime_db
from .test_private_app_storage_policy import _patch_args, _run
from .test_read_only_file_tools import _call, _tools

# Path (relative ones are under the home folder) -> the tier that withholds it.
_TABLE: tuple[tuple[str, SecretTier | None], ...] = (
    ("/p/.env", "app_secret"),
    ("/p/.ENV.local", "app_secret"),
    ("/p/config/prod.env", "app_secret"),
    ("/p/.env.example", None),
    ("/p/.Env.Sample", None),
    ("/p/.env.template", None),
    ("/p/.environment", None),
    ("/p/.env/bin/python", None),
    ("Library/Keychains/login.keychain-db", "cli_credential"),
    (".ssh/id_ed25519_x", "cli_credential"),
    (".SSH/ID_RSA", "cli_credential"),
    (".ssh/config", None),
    (".ssh/known_hosts", None),
    (".ssh/known_hosts.old", None),
    (".ssh/id_ed25519.pub", None),
    ("/p/keys/id_rsa_github", "cli_credential"),
    ("/p/keys/id_rsa_github.pub", None),
    ("/p/src/id_rsa_parser.py", None),
    (".aws/credentials", "cli_credential"),
    (".aws/config", None),
    (".kube/config", "cli_credential"),
    (".config/gcloud/application_default_credentials.json", "cli_credential"),
    (".azure/msal_token_cache.json", "cli_credential"),
    (".docker/config.json", "cli_credential"),
    (".docker/daemon.json", None),
    (".gnupg/private-keys-v1.d/a.key", "cli_credential"),
    (".codex/auth.json", "cli_credential"),
    (".codex/config.toml", None),
    ("/p/.netrc", "cli_credential"),
    ("/p/.git-credentials", "cli_credential"),
    ("/p/.pgpass", "cli_credential"),
    ("/p/.envrc", "cli_credential"),
    (".npmrc", None),
    (".config/gh/hosts.yml", None),
    ("/p/deck.key", None),
    ("/p/cert.pem", None),
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = (tmp_path / "home").resolve()
    folder.mkdir(exist_ok=True)
    monkeypatch.setattr(secret_paths, "_home", lambda: folder)
    return folder


def _absolute(home: Path, path: str) -> str:
    return path if path.startswith("/") else str(home / path)


def test_the_python_check_follows_the_rule_table(home: Path) -> None:
    for path, tier in _TABLE:
        absolute = Path(_absolute(home, path))
        assert (withheld_reason(absolute, credentials_readable=False) is not None) == (
            tier is not None
        ), path
        assert (withheld_reason(absolute, credentials_readable=True) is not None) == (
            tier == "app_secret"
        ), path


def test_unicode_aliases_that_apfs_opens_as_the_same_file_are_withheld(
    home: Path,
) -> None:
    long_s = "\u017f"  # ſ, which APFS folds to s
    kelvin = "\u212a"  # Kelvin sign, which APFS folds to k
    # The home folder itself spelled another way, as /Uſers would be.
    home_alias = str(home).replace("s", long_s, 1)
    assert home_alias != str(home)

    for path in (
        f"{home_alias}/.aws/credentials",
        f"{home}/.{long_s}{long_s}h/id_rsa",
        f"{home}/.{kelvin}ube/config",
        f"/p/.git-credential{long_s}",
        "/p/.\u0045NV",
    ):
        assert withheld_reason(Path(path), credentials_readable=False) is not None, path
    assert (
        withheld_reason(Path(f"{home_alias}/notes.txt"), credentials_readable=False)
        is None
    )


@pytest.mark.parametrize("name", ["jos\u00e9", "Je\u0301r\u00f4me", "\ubbfc\uc218"])
def test_a_home_with_non_ascii_letters_keeps_its_credentials_withheld(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    home = tmp_path / name
    monkeypatch.setattr(secret_paths, "_home", lambda: home)

    for spelled in (
        unicodedata.normalize("NFC", str(home)),
        unicodedata.normalize("NFD", str(home)),
    ):
        for relative in (".aws/credentials", "Library/Keychains/login.keychain-db"):
            assert (
                withheld_reason(Path(spelled, relative), credentials_readable=False)
                is not None
            ), (spelled, relative)


@pytest.mark.skipif(shutil.which("grep") is None, reason="needs a POSIX grep -E")
def test_the_extended_regexes_withhold_what_the_python_check_does(home: Path) -> None:
    paths = [_absolute(home, path) for path, _tier in _TABLE]

    def matching(pattern: str) -> set[str]:
        result = subprocess.run(
            ["grep", "-E", "-e", pattern],
            input="\n".join(paths) + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode in (0, 1), result.stderr
        return set(result.stdout.splitlines())

    def withheld_by_regex(*tiers: SecretTier) -> set[str]:
        withheld: set[str] = set()
        for tier in tiers:
            for rule in secret_path_patterns(tier):
                excepted = set() if rule.excepted is None else matching(rule.excepted)
                withheld |= matching(rule.withheld) - excepted
        return withheld

    def withheld_by_python(*, credentials_readable: bool) -> set[str]:
        return {
            path
            for path in paths
            if withheld_reason(Path(path), credentials_readable=credentials_readable)
            is not None
        }

    assert withheld_by_regex("app_secret") == withheld_by_python(
        credentials_readable=True
    )
    assert withheld_by_regex("app_secret", "cli_credential") == withheld_by_python(
        credentials_readable=False
    )


@pytest.mark.asyncio
async def test_read_withholds_env_files_by_name_and_by_link(tmp_path: Path) -> None:
    registry, folder, _storage, _spill = _tools(tmp_path)
    (folder / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (folder / ".ENV.local").write_text("API_KEY=secret\n", encoding="utf-8")
    (folder / ".env.example").write_text("API_KEY=\n", encoding="utf-8")
    (folder / "notes.txt").write_text("plain\n", encoding="utf-8")
    (folder / "linked-config").symlink_to(folder / ".env")
    (folder / "app").mkdir()
    (folder / "app" / ".env").symlink_to(folder / "notes.txt")
    venv = folder / "tool" / ".env"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "activate").write_text("export VIRTUAL_ENV\n", encoding="utf-8")

    for path in (".env", ".ENV.local", "linked-config", "app/.env"):
        result = await _call(registry, "read", {"path": str(folder / path)})
        assert result.status == "error", path
        assert result.output["error_code"] == SECRET_PATH_WITHHELD, path
        assert "API_KEY" not in str(result.output), path
        assert "env secrets file" in str(result.output), path
    for path in (".env.example", "tool/.env/bin/activate"):
        result = await _call(registry, "read", {"path": str(folder / path)})
        assert result.status == "success", path

    listed = await _call(registry, "list", {"path": str(folder), "max_depth": 1})
    assert ".env" in str(listed.output["entries"])


@pytest.mark.asyncio
async def test_read_withholds_ssh_keys_but_not_what_ssh_reads(
    tmp_path: Path, home: Path
) -> None:
    registry, folder, _storage, _spill = _tools(tmp_path)
    assert folder == home
    ssh = home / ".ssh"
    ssh.mkdir()
    for name in ("id_ed25519_x", "config", "known_hosts", "id_ed25519_x.pub"):
        (ssh / name).write_text(f"{name}\n", encoding="utf-8")

    key = await _call(registry, "read", {"path": str(ssh / "id_ed25519_x")})
    assert key.output["error_code"] == SECRET_PATH_WITHHELD
    for name in ("config", "known_hosts", "id_ed25519_x.pub"):
        result = await _call(registry, "read", {"path": str(ssh / name)})
        assert result.status == "success", name


@pytest.mark.asyncio
async def test_grep_skips_a_withheld_file_without_saying_it_matched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_ripgrep_backend(monkeypatch)
    registry, folder, _storage, _spill = _tools(tmp_path)
    (folder / ".env").write_text("TOKEN=needle\n", encoding="utf-8")
    (folder / "notes.txt").write_text("needle\n", encoding="utf-8")

    result = await _call(
        registry, "grep", {"base_path": str(folder), "pattern": "needle"}
    )

    assert result.status == "success"
    assert [entry["path"] for entry in result.output["files"]] == ["notes.txt"]
    assert "TOKEN" not in str(result.output)
    # A note that a secret matched would let repeated patterns read it out.
    assert ".env" not in str(result.output)


@pytest.mark.asyncio
async def test_action_tools_neither_read_nor_patch_an_env_file(tmp_path: Path) -> None:
    db_path, context = bootstrap_path_policy_runtime_db(
        tmp_path, allowed_tool_ids=("read", "apply_patch")
    )
    _grant_workspace_full_access(db_path=db_path, capability="scoped_write")
    workspace = context.workspace_path  # type: ignore[attr-defined]
    env = workspace / ".env"
    env.write_text("kept\n", encoding="utf-8")

    async def refused(tool_id: str, args: dict[str, JSONValue]) -> None:
        with pytest.raises(BrokerPolicyError) as caught:
            await _run(db_path=db_path, context=context, tool_id=tool_id, args=args)
        assert caught.value.code == SECRET_PATH_WITHHELD, (tool_id, args)

    await refused("read", {"path": ".env"})
    await refused("apply_patch", _patch_args("update", ".env"))
    await refused("apply_patch", _patch_args("delete", str(env)))
    await refused("apply_patch", _patch_args("add", ".env.production"))

    assert env.read_text(encoding="utf-8") == "kept\n"
    assert not (workspace / ".env.production").exists()


@pytest.mark.asyncio
async def test_a_link_swapped_to_a_secret_between_the_two_resolutions_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, context = bootstrap_path_policy_runtime_db(
        tmp_path, allowed_tool_ids=("read", "apply_patch")
    )
    _grant_workspace_full_access(db_path=db_path, capability="scoped_write")
    workspace = context.workspace_path  # type: ignore[attr-defined]
    (workspace / "notes.txt").write_text("kept\n", encoding="utf-8")
    env = workspace / ".env"
    env.write_text("kept\n", encoding="utf-8")

    # notes.txt resolves to .env for the root lookup the I/O uses, as when a
    # link is swapped after the name was checked.
    def swapped(**kwargs: object) -> ResolvedManifestPath:
        resolved = resolve_local_path(**kwargs)  # type: ignore[arg-type]
        return replace(resolved, path=env, root_relative_path=".env")

    monkeypatch.setattr(read_paths, "resolve_local_path", swapped)
    monkeypatch.setattr(action_path_policy, "resolve_local_path", swapped)

    for tool_id, args in (
        ("read", {"path": "notes.txt"}),
        ("apply_patch", _patch_args("update", "notes.txt")),
    ):
        with pytest.raises(BrokerPolicyError) as caught:
            await _run(db_path=db_path, context=context, tool_id=tool_id, args=args)
        assert caught.value.code == SECRET_PATH_WITHHELD, tool_id
    assert env.read_text(encoding="utf-8") == "kept\n"


def test_a_folder_inside_a_credentials_folder_is_never_allowed_for_writing(
    tmp_path: Path, home: Path
) -> None:
    (home / ".aws" / "sso").mkdir(parents=True)
    (home / "project").mkdir()
    db_path = tmp_path / "app-data" / "runtime.db"

    assert not folder_can_be_granted(folder=home / ".aws", db_path=db_path)
    assert not folder_can_be_granted(folder=home / ".aws" / "sso", db_path=db_path)
    assert folder_can_be_granted(folder=home / "project", db_path=db_path)
