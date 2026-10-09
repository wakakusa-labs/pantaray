from __future__ import annotations

import asyncio
import errno
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    ValidatedCommandRequest,
)
from pantaray_agents.local_runtime.tooling.sandbox import (
    command_sandbox_request,
    command_sandbox_worker,
    seatbelt_profiles,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_protocol import (
    SandboxCommandCompletion,
    SandboxOutputChunk,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_request import (
    build_sandbox_request,
)
from pantaray_agents.local_runtime.tooling.sandbox.macos_runtime import user_temp_dir
from pantaray_agents.local_runtime.tooling.sandbox.seatbelt_profiles import (
    MACOS_SYSTEM_RUNTIME_READ_ROOTS,
    render_ripgrep_seatbelt_profile,
    render_seatbelt_profile,
)


def _validated_command_request(*, network_policy: str) -> ValidatedCommandRequest:
    return ValidatedCommandRequest(
        tool_invocation_id="invocation-1",
        manifest_id="manifest-1",
        execution_session_id="execution-session-1",
        action_id="action-1",
        approval_session_id="approval-session-1",
        approval_source="settings",
        private_storage_roots=["/app-data", "/artifacts"],
        action_workspace_root="/workspace",
        published_results_root="/published-results",
        app_runtime_python="/app-runtime/python",
        cwd="/workspace",
        command_summary_json={
            "summary_kind": "bash",
            "command": "pytest -q",
            "cwd": "/workspace",
            "timeout_ms": 30_000,
        },
        argv=["/bin/bash", "--noprofile", "--norc", "-c", "pytest -q"],
        resolved_executable_path="/bin/bash",
        execution_kind="workspace_command",
        executable_source_kind="trusted_system_executable",
        env={"PATH": "/workspace/.venv/bin"},
        timeout_ms=30_000,
        stdout_max_bytes=1_048_576,
        stderr_max_bytes=1_048_576,
        temp_storage_limit_bytes=67_108_864,
        child_count_limit=8,
        open_file_lease_limit=16,
        network_policy=network_policy,  # type: ignore[arg-type]
        use_login_environment=False,
        run_outside_sandbox=False,
        real_read_roots=["/workspace"],
        real_write_roots=["/workspace"],
        tool_request_id="request-1",
        requested_at="2026-03-23T00:00:00Z",
        preflight_only=False,
    )


def test_render_workspace_profile_is_limited_to_declared_roots() -> None:
    request = build_sandbox_request(
        request=_validated_command_request(network_policy="deny"),
        temp_dir=Path("/system-temp/worker-temp"),
    )
    profile = render_seatbelt_profile(request)

    for root in MACOS_SYSTEM_RUNTIME_READ_ROOTS:
        assert f'(subpath "{root}")' in profile
    assert '(subpath "/Library")' not in profile
    assert "(allow process-exec process-fork)" in profile
    assert "(allow process*)" not in profile
    assert '(subpath "/workspace")' in profile
    assert '(subpath "/dev/fd")' in profile
    assert '(subpath "/app-temp/action-1")' not in profile
    assert '(subpath "/system-temp/worker-temp")' in profile


def test_render_workspace_profile_includes_toolchain_read_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        command_sandbox_request,
        "toolchain_read_roots",
        lambda: ("/xcode/usr/share/git-core",),
    )
    request = build_sandbox_request(
        request=_validated_command_request(network_policy="deny"),
        temp_dir=Path("/system-temp/worker-temp"),
    )
    profile = render_seatbelt_profile(request)
    assert '(subpath "/xcode/usr/share/git-core")' in profile


def test_render_profile_escapes_every_path_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = '/workspace/"quoted"\\root'
    temp_dir = '/system-temp/"worker"\\temp'
    validated_request = _validated_command_request(network_policy="deny").model_copy(
        update={
            "app_runtime_python": '/app-runtime/"quoted"\\runtime/bin/python',
            "private_storage_roots": ['/app-data/"quoted"\\private'],
            "action_workspace_root": workspace_root,
            "published_results_root": '/results/"quoted"\\public',
            "cwd": workspace_root,
            "real_read_roots": [workspace_root],
            "real_write_roots": [workspace_root],
        }
    )
    monkeypatch.setattr(
        command_sandbox_request,
        "toolchain_read_roots",
        lambda: ('/xcode/"quoted"\\git-core',),
    )
    request = build_sandbox_request(
        request=validated_request,
        temp_dir=Path(temp_dir),
    )

    profile = render_seatbelt_profile(request)

    for raw_path in (
        workspace_root,
        temp_dir,
        '/app-runtime/"quoted"\\runtime',
        '/xcode/"quoted"\\git-core',
        '/app-data/"quoted"\\private',
        '/results/"quoted"\\public',
    ):
        assert _seatbelt_escaped(raw_path) in profile
    assert "{{" not in profile
    assert "}}" not in profile


def test_render_profile_rejects_unresolved_placeholders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template_path = tmp_path / "profile.sbpl"
    template_path.write_text(
        "(version 1)\n{{RUNTIME_READ_ROOT_CLAUSES}}\n{{UNKNOWN_PLACEHOLDER}}\n",
        encoding="utf-8",
    )
    request = build_sandbox_request(
        request=_validated_command_request(network_policy="deny"),
        temp_dir=Path("/system-temp/worker-temp"),
    )

    monkeypatch.setattr(seatbelt_profiles, "PROFILE_TEMPLATE_PATH", template_path)
    with pytest.raises(RuntimeError, match="unresolved placeholder"):
        render_seatbelt_profile(request)


def _seatbelt_escaped(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def test_forwarded_output_keeps_utf8_across_pipe_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = "a" * 4095 + "起動に失敗しました\n"
    stream = asyncio.StreamReader()
    stream.feed_data(output.encode("utf-8"))
    stream.feed_eof()
    messages: list[object] = []
    monkeypatch.setattr(command_sandbox_worker, "_emit_message", messages.append)
    capture = command_sandbox_worker.StreamCapture()

    await command_sandbox_worker._forward_stream(
        stream=stream,
        stream_name="stderr",
        request_id="request-1",
        max_bytes=10_000,
        capture=capture,
        budget_exceeded=asyncio.Event(),
    )

    assert (
        "".join(
            message.data
            for message in messages
            if isinstance(message, SandboxOutputChunk)
        )
        == output
    )
    assert capture.bytes_read == len(output.encode("utf-8"))


async def test_command_spawn_failure_includes_os_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = build_sandbox_request(
        request=_validated_command_request(network_policy="deny"), temp_dir=tmp_path
    )
    messages: list[object] = []
    monkeypatch.setattr(command_sandbox_worker, "_emit_message", messages.append)

    async def fail_spawn(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", request.cwd)

    monkeypatch.setattr(
        command_sandbox_worker.asyncio, "create_subprocess_exec", fail_spawn
    )
    await command_sandbox_worker._run_helper(request)

    assert isinstance(messages[0], SandboxOutputChunk)
    assert "FileNotFoundError: [Errno 2]" in messages[0].data
    assert request.cwd in messages[0].data
    assert isinstance(messages[1], SandboxCommandCompletion)
    assert messages[1].outcome == "spawn_failed"
    assert messages[1].stderr_bytes == len(messages[0].data.encode("utf-8"))


def test_private_storage_deny_overrides_broad_read_and_write_grants() -> None:
    validated = _validated_command_request(network_policy="deny").model_copy(
        update={"real_read_roots": ["/"], "real_write_roots": ["/"]}
    )
    request = build_sandbox_request(
        request=validated, temp_dir=Path("/system-temp/worker-temp")
    )
    profile = render_seatbelt_profile(request)
    assert (
        "(deny file-read* (require-all (require-any\n"
        '    (subpath "/app-data")\n    (subpath "/artifacts")\n) '
        '(require-not (require-any\n    (subpath "/system-temp/worker-temp")\n'
        '    (subpath "/workspace")\n    (subpath "/published-results")\n))))'
    ) in profile
    assert (
        "(deny file-write* (require-all (require-any\n"
        '    (subpath "/app-data")\n    (subpath "/artifacts")\n) '
        '(require-not (require-any\n    (subpath "/system-temp/worker-temp")\n'
        '    (subpath "/workspace")\n))))'
    ) in profile


def test_system_temp_roots_open_to_commands_beneath_private_storage_deny() -> None:
    temp_root = user_temp_dir()
    tmp_root = str(Path("/tmp").resolve())
    private_root = f"{temp_root}/app-data"
    validated = _validated_command_request(network_policy="deny").model_copy(
        update={"private_storage_roots": [private_root]}
    )
    profile = render_seatbelt_profile(
        build_sandbox_request(
            request=validated, temp_dir=Path("/system-temp/worker-temp")
        )
    )

    write_allow = profile[profile.index("(allow file-write*") :]
    write_allow = write_allow[: write_allow.index("\n)\n")]
    read_allow = profile[profile.index("(allow file-read*\n") :]
    read_allow = read_allow[: read_allow.index("\n)\n")]
    for root in (temp_root, tmp_root):
        assert f'(subpath "{root}")' in write_allow
        assert f'(subpath "{root}")' in read_allow
    # Seatbelt applies the last matching rule, so the denies must follow the allows.
    private_deny = f'(require-any\n    (subpath "{private_root}")\n)'
    assert profile.index(f"(deny file-write* (require-all {private_deny}") > (
        profile.index(write_allow)
    )
    assert profile.index(f"(deny file-read* (require-all {private_deny}") > (
        profile.index(read_allow)
    )


def test_login_environment_profile_adds_keychain_and_agent() -> None:
    normal = build_sandbox_request(
        request=_validated_command_request(network_policy="deny"),
        temp_dir=Path("/system-temp/worker-temp"),
    )
    login = build_sandbox_request(
        request=_validated_command_request(network_policy="deny").model_copy(
            update={
                "use_login_environment": True,
                "env": {"SSH_AUTH_SOCK": "/agent-dir/agent.sock"},
                "real_read_roots": ["/", "/workspace"],
            }
        ),
        temp_dir=Path("/system-temp/worker-temp"),
    )
    normal_profile = render_seatbelt_profile(normal)
    login_profile = render_seatbelt_profile(login)

    keychain = '(allow mach-lookup (global-name "com.apple.SecurityServer"))'
    agent = '(allow network-outbound (literal "/agent-dir/agent.sock"))'
    assert keychain not in normal_profile
    assert keychain in login_profile
    # An offline command still reaches ssh-agent: the grant follows the deny.
    assert login_profile.index("(deny network*)") < login_profile.index(agent)
    assert '    (subpath "/")' in login_profile
    # sandbox-exec rejects the whole profile for an ancestor query on "/".
    assert '(path-ancestors "/")' not in login_profile
    assert '(path-ancestors "/workspace")' in login_profile
    assert login_profile.index('    (subpath "/")') < login_profile.index(
        '(deny file-read* (require-all (require-any\n    (subpath "/app-data")'
    )


def test_ripgrep_profile_without_own_roots_denies_all_private_storage() -> None:
    profile = render_ripgrep_seatbelt_profile(
        read_roots=("/",),
        private_storage_roots=("/app-data",),
        readable_private_roots=(),
    )

    # sandbox-exec rejects an empty (require-any), which would fail every search.
    assert '(deny file-read* (require-any\n    (subpath "/app-data")\n))' in profile
    assert "require-not" not in profile
