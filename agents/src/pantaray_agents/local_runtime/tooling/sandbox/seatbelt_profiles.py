from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from .command_sandbox_protocol import BrokerToSandboxCommandRequest
from .macos_runtime import toolchain_read_roots

PROFILE_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1]
    / "seatbelt_profile_templates"
    / "seatbelt_command.sbpl"
)
MACOS_SYSTEM_RUNTIME_READ_ROOTS: tuple[str, ...] = (
    "/System",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/private/var/select",
    "/private/etc/ssl/openssl.cnf",
    "/private/etc/ssl/cert.pem",
    "/usr/lib",
    "/usr/share",
    "/private/var/db/dyld",
)
PLACEHOLDER_START = "{{"
PLACEHOLDER_END = "}}"


def render_seatbelt_profile(
    request: BrokerToSandboxCommandRequest,
) -> str:
    template = PROFILE_TEMPLATE_PATH.read_text(encoding="utf-8")
    network_clause = _render_network_clause(request)
    replacements = {
        "{{NETWORK_CLAUSE}}": network_clause,
        "{{PATH_ANCESTOR_CLAUSES}}": _render_clause_block(
            f'    (path-ancestors "{_escape_seatbelt_string(path)}")'
            for path in (
                *MACOS_SYSTEM_RUNTIME_READ_ROOTS,
                *request.real_read_roots,
                *request.runtime_read_roots,
                request.app_runtime_root,
            )
            # sandbox-exec rejects the profile when "/" has no ancestors to list.
            if path != "/"
        ),
        "{{SYSTEM_RUNTIME_READ_ROOTS}}": _render_system_runtime_read_roots(),
        "{{RUNTIME_READ_ROOT_CLAUSES}}": _render_subpath_block(
            request.runtime_read_roots
        ),
        "{{FILE_READ_ROOT_CLAUSES}}": _render_file_read_root_clauses(request),
        "{{FILE_WRITE_ROOT_CLAUSES}}": _render_file_write_root_clauses(request),
        "{{PRIVATE_STORAGE_DENY_RULES}}": _render_private_storage_deny_rules(request),
        "{{LOGIN_ENVIRONMENT_CLAUSE}}": _render_login_environment_clause(request),
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    if PLACEHOLDER_START in rendered or PLACEHOLDER_END in rendered:
        raise RuntimeError("seatbelt profile contains an unresolved placeholder")
    return rendered


def _render_system_runtime_read_roots() -> str:
    return "\n".join(
        _render_subpath_clause(root) for root in MACOS_SYSTEM_RUNTIME_READ_ROOTS
    )


def _render_subpath_block(paths: Iterable[str]) -> str:
    return _render_clause_block(_render_subpath_clause(path) for path in paths)


def _render_file_read_root_clauses(request: BrokerToSandboxCommandRequest) -> str:
    return _render_subpath_block((*request.real_read_roots, request.app_runtime_root))


def _render_file_write_root_clauses(request: BrokerToSandboxCommandRequest) -> str:
    return _render_subpath_block(request.real_write_roots)


def _render_private_storage_deny_rules(request: BrokerToSandboxCommandRequest) -> str:
    # The command's own temp dir lives in private storage too.
    readable = [request.temp_dir]
    writable = [request.temp_dir]
    storage = request.action_storage
    if storage is not None:
        readable += [storage.workspace_root, storage.published_results_root]
        writable.append(storage.workspace_root)
    return "\n".join(
        (
            _render_private_storage_deny(
                "file-read*", request.private_storage_roots, readable
            ),
            _render_private_storage_deny(
                "file-write*", request.private_storage_roots, writable
            ),
        )
    )


def _render_private_storage_deny(
    operation: str, private_roots: Sequence[str], exceptions: Sequence[str]
) -> str:
    denied = f"(require-any\n{_render_subpath_block(private_roots)}\n)"
    # sandbox-exec rejects an empty (require-any), so no exceptions means none.
    if not exceptions:
        return f"(deny {operation} {denied})"
    # Exceptions remove this deny only; the ordinary read/write roots still apply.
    return (
        f"(deny {operation} (require-all {denied} "
        f"(require-not (require-any\n{_render_subpath_block(exceptions)}\n))))"
    )


def render_ripgrep_seatbelt_profile(
    *,
    read_roots: Sequence[str],
    private_storage_roots: Sequence[str],
    readable_private_roots: Sequence[str],
) -> str:
    """Let the discovery search read ``read_roots`` and run, and do nothing else.

    ripgrep walks and reopens directories by name itself, so a check Pantaray
    makes beforehand cannot bind what it reads: a directory swapped for a link
    mid-search would lead it anywhere. The kernel checks every path it really
    opens instead, with the private-storage denies of commands.
    """

    read_allow = _render_subpath_block(
        (*MACOS_SYSTEM_RUNTIME_READ_ROOTS, *toolchain_read_roots(), *read_roots)
    )
    return "\n".join(
        (
            "(version 1)",
            '(import "system.sb")',
            "(allow process-exec process-fork)",
            "(allow file-map-executable)",
            "(deny file-read*)",
            f"(allow file-read*\n{read_allow}\n)",
            _render_private_storage_deny(
                "file-read*", private_storage_roots, readable_private_roots
            ),
            "(deny file-write*)",
            "(deny network*)",
            "",
        )
    )


def _render_login_environment_clause(request: BrokerToSandboxCommandRequest) -> str:
    if not request.use_login_environment:
        return ""
    # CLIs read their stored sign-ins through the Keychain service.
    clauses = ['(allow mach-lookup (global-name "com.apple.SecurityServer"))']
    ssh_agent_socket = request.env.get("SSH_AUTH_SOCK")
    if ssh_agent_socket is not None:
        # Rendered after the network clause so an offline command can still sign.
        socket_path = _escape_seatbelt_string(str(Path(ssh_agent_socket).resolve()))
        clauses.append(f'(allow network-outbound (literal "{socket_path}"))')
    return "\n".join(clauses)


def _render_subpath_clause(path: str) -> str:
    return f'    (subpath "{_escape_seatbelt_string(path)}")'


def _render_clause_block(clauses: Iterable[str]) -> str:
    return "\n".join(dict.fromkeys(str(clause) for clause in clauses))


def _escape_seatbelt_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _render_network_clause(request: BrokerToSandboxCommandRequest) -> str:
    if request.network_policy == "deny":
        return "(deny network*)"
    protected = _escape_seatbelt_string(request.protected_backend_address)
    return (
        "(deny network*)\n"
        "(system-network)\n"
        '(allow network-outbound (literal "/private/var/run/mDNSResponder"))\n'
        '(allow network-outbound (remote tcp "*:*") (remote udp "*:*"))\n'
        '(allow network-bind (local ip "localhost:*"))\n'
        '(allow network-inbound (local tcp "localhost:*") (local udp "localhost:*"))\n'
        f'(deny network-outbound (remote tcp "{protected}"))'
    )
