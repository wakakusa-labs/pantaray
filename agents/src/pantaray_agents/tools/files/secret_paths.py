"""Files that hold secrets, which no agent may read or write.

One closed rule set in two tiers. App secrets (env files, the macOS Keychains)
are always withheld. CLI credentials (SSH keys, cloud CLI logins, ...) are
withheld too, except from a command that runs with the user's login
environment, which needs them to sign in as the user does in a terminal.

The rules are written once, as POSIX extended regular expressions over an
absolute path: the Python check below and the kernel sandbox profiles and the
desktop app's page renderer that use ``secret_path_patterns`` all match the
same text, so they cannot drift apart. Every letter is written as a bracket
pair such as ``[eE]``, because APFS opens ``.ENV`` as ``.env`` and seatbelt's
``(regex #"...")`` has no case-insensitive flag.

APFS also opens ``/Uſers`` (long s) or a Kelvin sign as ``/Users`` and ``k``:
it compares names after Unicode NFD and case folding. Seatbelt matches the
name the kernel stored, but a consumer matching a path as it was asked for
must fold it the same way first (``unicodedata`` NFD, casefold, NFD again), as
the Python check here does.
"""

from __future__ import annotations

import os
import pwd
import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

from pantaray_agents.tools.contract import BrokerPolicyError

SECRET_PATH_WITHHELD = "SECRET_PATH_WITHHELD"

type SecretTier = Literal["app_secret", "cli_credential"]


@dataclass(frozen=True, slots=True)
class SecretPathPattern:
    """One rule: a path matching ``withheld`` and not ``excepted`` is withheld.

    Both are POSIX EREs over an absolute path, anchored at the home folder
    (``^/Users/me/[.]ssh/``) or at the file name (``/[.]netrc$``). They use no
    ``.`` wildcard, backreference or flag, so seatbelt, JavaScript and Python
    read them alike.
    """

    tier: SecretTier
    withheld: str
    excepted: str | None
    # A directory at a matching path is not withheld (a virtualenv named .env);
    # anything else is, including a file not created yet.
    directories_exempt: bool
    description: str


@dataclass(frozen=True, slots=True)
class SecretWithheld:
    # The name asked for or the real path it resolves to, whichever matched.
    path: Path
    reason: str


def secret_path_patterns(tier: SecretTier) -> tuple[SecretPathPattern, ...]:
    """The rules of one tier, for the current OS account's home folder."""

    return tuple(rule for rule, _, _ in _compiled(_home()) if rule.tier == tier)


def withheld_reason(path: Path, *, credentials_readable: bool) -> SecretWithheld | None:
    """Why an absolute path is withheld, or None when it may be used.

    Both the name asked for and its real path are matched, so a link named
    ``.env`` and a link leading to one are withheld alike.
    ``credentials_readable`` is true only for a command running with the user's
    login environment; file tools never pass it.
    """

    for candidate in dict.fromkeys((path, path.resolve(strict=False))):
        text = _folded(candidate.as_posix())
        for rule, withheld, excepted in _compiled(_home()):
            if credentials_readable and rule.tier == "cli_credential":
                continue
            if not withheld.search(text):
                continue
            if excepted is not None and excepted.search(text):
                continue
            if rule.directories_exempt and candidate.is_dir():
                continue
            return SecretWithheld(path=candidate, reason=rule.description)
    return None


def withheld_folder_reason(folder: Path) -> SecretWithheld | None:
    """Why a canonical folder lies inside one whose files are withheld (~/.ssh)."""

    # A trailing slash stands for "something in this folder": only the folder
    # rules match it, never a file-name rule.
    text = _folded(f"{folder.as_posix().rstrip('/')}/")
    for rule, withheld, _ in _compiled(_home()):
        if withheld.search(text):
            return SecretWithheld(path=folder, reason=rule.description)
    return None


def refuse_withheld_paths(*paths: Path) -> None:
    """Raise a file tool's refusal when any of these paths is withheld."""

    for path in paths:
        withheld = withheld_reason(path, credentials_readable=False)
        if withheld is not None:
            raise secret_path_error(withheld)


def secret_path_error(withheld: SecretWithheld) -> BrokerPolicyError:
    return BrokerPolicyError(
        f"{withheld.path} is withheld because {withheld.reason}. Its content is "
        "kept from agents on purpose; do not try another tool or command to get it.",
        code=SECRET_PATH_WITHHELD,
        fix_hint=(
            "Ask the user for the value you need, or read .env.example if the "
            "project has one."
        ),
    )


def _folded(text: str) -> str:
    # Canonical caseless form, which APFS uses to compare names.
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", text).casefold())


def _home() -> Path:
    # The OS account's home, not $HOME, which the process environment can move.
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


@cache
def _compiled(
    home: Path,
) -> tuple[tuple[SecretPathPattern, re.Pattern[str], re.Pattern[str] | None], ...]:
    return tuple(
        (
            rule,
            re.compile(rule.withheld),
            None if rule.excepted is None else re.compile(rule.excepted),
        )
        for rule in _rules(home)
    )


def _rules(home: Path) -> tuple[SecretPathPattern, ...]:
    home_prefix = f"^{_literal(home.as_posix().rstrip('/'))}/"

    def under(relative: str) -> str:
        return f"{home_prefix}{_literal(relative)}/"

    def exactly(relative: str) -> str:
        return f"{home_prefix}{_literal(relative)}$"

    def folder(relative: str, holds: str) -> SecretPathPattern:
        return SecretPathPattern(
            tier="cli_credential",
            withheld=under(relative),
            excepted=None,
            directories_exempt=False,
            description=f"it is in ~/{relative}, which holds {holds}",
        )

    def file(relative: str, holds: str) -> SecretPathPattern:
        return SecretPathPattern(
            tier="cli_credential",
            withheld=exactly(relative),
            excepted=None,
            directories_exempt=False,
            description=f"it is ~/{relative}, which holds {holds}",
        )

    def names(*options: str) -> str:
        return "(" + "|".join(options) + ")"

    env = _literal(".env")
    env_dot = _literal(".env.")
    env_templates = names(*(_literal(suffix) for suffix in _ENV_TEMPLATE_SUFFIXES))
    ssh_allowed = names(
        _literal("config"),
        _literal("known_hosts") + "[^/]*",
        "[^/]*" + _literal(".pub"),
    )
    ssh_key = names(*(_literal(f"id_{kind}") for kind in _SSH_KEY_KINDS))
    credential_names = names(*(_literal(name) for name in _CREDENTIAL_FILE_NAMES))
    return (
        SecretPathPattern(
            tier="app_secret",
            withheld=f"/{names(f'[^/]*{env}', f'{env_dot}[^/]*')}$",
            excepted=f"/{env_dot}{env_templates}$",
            directories_exempt=True,
            description="its name marks it as an env secrets file",
        ),
        SecretPathPattern(
            tier="app_secret",
            withheld=under("Library/Keychains"),
            excepted=None,
            directories_exempt=False,
            description="it is in the macOS Keychains folder",
        ),
        SecretPathPattern(
            tier="cli_credential",
            withheld=under(".ssh"),
            # ssh, and git over ssh, read these on every connection.
            excepted=f"{under('.ssh')}{ssh_allowed}$",
            directories_exempt=False,
            description="it is in ~/.ssh, which holds SSH keys",
        ),
        SecretPathPattern(
            tier="cli_credential",
            # A key file has no extension (id_ed25519_sk, id_rsa_github), so
            # id_rsa.pub and a source file such as id_rsa_parser.py stay readable.
            withheld=f"/{ssh_key}(_[^/.]*)?$",
            excepted=None,
            directories_exempt=False,
            description="its name marks it as an SSH private key",
        ),
        SecretPathPattern(
            tier="cli_credential",
            withheld=under(".aws"),
            # Profiles and regions only; the keys are in ~/.aws/credentials.
            excepted=exactly(".aws/config"),
            directories_exempt=False,
            description="it is in ~/.aws, which holds AWS credentials",
        ),
        folder(".kube", "Kubernetes credentials"),
        folder(".config/gcloud", "Google Cloud credentials"),
        folder(".azure", "Azure credentials"),
        folder(".gnupg", "GPG keys"),
        file(".docker/config.json", "container registry credentials"),
        file(".codex/auth.json", "Codex sign-in tokens"),
        # Not ~/.npmrc, which npm reads on every install, nor ~/.config/gh,
        # whose token gh keeps in the Keychain on macOS. Not *.pem or *.key
        # either: those names also cover Keynote decks and system cert.pem.
        SecretPathPattern(
            tier="cli_credential",
            withheld=f"/{credential_names}$",
            excepted=None,
            directories_exempt=False,
            description="its name marks it as a credentials file",
        ),
    )


_ENV_TEMPLATE_SUFFIXES = ("example", "sample", "template")
_SSH_KEY_KINDS = ("rsa", "dsa", "ecdsa", "ed25519")
_CREDENTIAL_FILE_NAMES = (".netrc", ".git-credentials", ".pgpass", ".envrc")
# Literal in a bracket expression in every dialect.
_BRACKETED = frozenset(".*+?(){}|$")
# A bracket expression cannot hold these portably, so they are escaped.
_ESCAPED = frozenset("^[]\\")


def _literal(text: str) -> str:
    """A case-insensitive ERE matching exactly ``text``."""

    return "".join(_literal_char(char) for char in text)


def _literal_char(char: str) -> str:
    if char.isascii() and char.isalpha():
        return f"[{char.lower()}{char.upper()}]"
    if char in _BRACKETED:
        return f"[{char}]"
    if char in _ESCAPED:
        return f"\\{char}"
    return char


__all__ = [
    "SECRET_PATH_WITHHELD",
    "SecretPathPattern",
    "SecretTier",
    "SecretWithheld",
    "refuse_withheld_paths",
    "secret_path_error",
    "secret_path_patterns",
    "withheld_folder_reason",
    "withheld_reason",
]
