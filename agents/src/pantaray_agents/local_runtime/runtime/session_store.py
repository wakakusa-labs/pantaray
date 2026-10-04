from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Literal

from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.users import ensure_user_row

from .control_payload import (
    CLEAR_REASON_EXPIRED,
    CLEAR_REASON_SIGNED_OUT,
    ClearReason,
    CloudSessionImport,
    normalize_session_version,
    parse_iso8601,
    parse_session_version,
    require_non_empty,
    require_positive_timeout,
    validate_cloud_session_fields,
)

AUTH_CONTEXT_PRESENT_STATE = "present"
AUTH_CONTEXT_EXPIRED_STATE = "expired"
AUTH_CONTEXT_ABSENT_STATE = "absent"

_SESSION_LOCK = Lock()
_ACTIVE_SESSION: ActiveDesktopSession | None = None
_EXPIRED_IDENTITY: ExpiredCloudIdentity | None = None
# Advances on every imported access token, including same-account rotations, so a
# clear aimed at an older token is recognized as stale.
_CREDENTIAL_GENERATION = 0
# True once Electron main has applied its startup configuration to this helper.
_CONFIGURED = False


@dataclass(frozen=True, slots=True)
class ActiveDesktopSession:
    user_id: str
    desktop_access_token: str
    expires_at: str
    session_version: str
    credential_generation: int = 0


@dataclass(frozen=True, slots=True)
class ExpiredCloudIdentity:
    """A cloud session whose token lapsed or could not be refreshed.

    The account keeps owning local data until an explicit sign-out; only cloud
    requests are blocked while the session is expired.
    """

    user_id: str
    session_version: str
    credential_generation: int


@dataclass(frozen=True, slots=True)
class CloudSessionIdentity:
    """Which account a cloud request is made under, and under which sign-in.

    Unlike ``ExpiredCloudIdentity`` this carries the state and leaves out
    ``credential_generation``, so an access-token refresh keeps one value.
    """

    state: Literal["present", "expired"]
    user_id: str
    session_version: str


@dataclass(frozen=True, slots=True)
class ClearDesktopSessionResult:
    state: str
    stale: bool


@dataclass(frozen=True, slots=True)
class CloudSessionClearPlan:
    """What a clear request would do, decided before anything is applied.

    A caller has to stop the work still using the current identity before that
    identity changes, and must stop nothing for a stale request — a clear aimed
    at an account, a sign-in, or a token that is no longer current (design 6.2).
    Both answers come from one snapshot here, so the stale rule keeps one owner.
    """

    reason: ClearReason
    # The cloud session this request matched, or None when it matched nothing
    # current, in which case applying the plan changes nothing.
    matched: ExpiredCloudIdentity | None
    # The cloud identity once this plan is applied; None once no cloud session
    # remains. A stale plan changes nothing, so it repeats what is there now.
    # That says nothing about an elapsed session waiting to be published as
    # expired: applying a plan, and any other read here, still settles it.
    # Whether to stop for that transition is decided separately, from
    # ``peek_pending_cloud_session_expiry``.
    identity_after: CloudSessionIdentity | None

    @property
    def stale(self) -> bool:
        return self.matched is None


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _ensure_active_user_row(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    timestamp: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        with connection:
            ensure_user_row(connection, user_id=user_id, timestamp=timestamp)


def _identity_of(session: ActiveDesktopSession) -> ExpiredCloudIdentity:
    return ExpiredCloudIdentity(
        user_id=session.user_id,
        session_version=session.session_version,
        credential_generation=session.credential_generation,
    )


def _expired_identity(*, user_id: str, session_version: str) -> CloudSessionIdentity:
    """The identity a cloud session keeps once it is published as ``expired``."""
    return CloudSessionIdentity(
        state="expired", user_id=user_id, session_version=session_version
    )


def _lapsed_session_locked() -> ActiveDesktopSession | None:
    """The present session whose token has run out, if there is one."""
    session = _ACTIVE_SESSION
    if session is None:
        return None
    expires_at = parse_iso8601(
        session.expires_at, field_name="desktop_access_token_expires_at"
    )
    return session if expires_at <= _now_utc() else None


def _settle_expiry_locked() -> CloudSessionIdentity | None:
    """Move an elapsed present session to expired, retaining its identity.

    Runs under the session lock. The runtime performs this itself so an expired
    token is never sent to the cloud while Electron main is still catching up.
    Returns the identity that became visible, or None when nothing had lapsed.
    """
    global _ACTIVE_SESSION, _EXPIRED_IDENTITY
    session = _lapsed_session_locked()
    if session is None:
        return None
    _EXPIRED_IDENTITY = _identity_of(session)
    _ACTIVE_SESSION = None
    return _expired_identity(
        user_id=session.user_id, session_version=session.session_version
    )


def _cloud_identity_locked() -> CloudSessionIdentity | None:
    if _ACTIVE_SESSION is not None:
        return CloudSessionIdentity(
            state="present",
            user_id=_ACTIVE_SESSION.user_id,
            session_version=_ACTIVE_SESSION.session_version,
        )
    if _EXPIRED_IDENTITY is not None:
        return CloudSessionIdentity(
            state="expired",
            user_id=_EXPIRED_IDENTITY.user_id,
            session_version=_EXPIRED_IDENTITY.session_version,
        )
    return None


def _clear_target_locked() -> ExpiredCloudIdentity | None:
    """The cloud session a clear request is matched against."""
    if _ACTIVE_SESSION is not None:
        return _identity_of(_ACTIVE_SESSION)
    return _EXPIRED_IDENTITY


def _state_locked() -> str:
    identity = _cloud_identity_locked()
    return AUTH_CONTEXT_ABSENT_STATE if identity is None else identity.state


def _current_version_locked() -> tuple[int, str] | None:
    identity = _cloud_identity_locked()
    if identity is None:
        return None
    return (parse_session_version(identity.session_version), identity.user_id)


def _require_version_may_advance_locked(*, user_id: str, session_version: int) -> None:
    """A sign-in version may not go backwards, nor be reused by another account."""
    current = _current_version_locked()
    if current is None:
        return
    current_version, current_user_id = current
    if session_version < current_version:
        raise MigrationError("session_version must not go backwards")
    if session_version == current_version and current_user_id != user_id:
        raise MigrationError("session_version conflicts with the active desktop user")
    # The version identifies a sign-in, not an access-token rotation.


def validate_cloud_session_import(
    *,
    user_id: str,
    desktop_access_token: str,
    expires_at: str,
    session_version: str,
) -> CloudSessionImport:
    """Check an incoming session the way applying it would, and normalize it.

    A caller that stops in-flight work before it applies asks here first, so a
    payload the store would refuse -- a clock skew, a callback for an older
    sign-in -- never costs the user a canceled Action (design 6.2).
    ``apply_cloud_session_import`` runs the same checks again, which is where
    they bind. This one leaves an elapsed session unsettled and still answers
    the same, because settling keeps the account and the version it compares.
    """
    session = validate_cloud_session_fields(
        user_id=user_id,
        desktop_access_token=desktop_access_token,
        expires_at=expires_at,
        session_version=session_version,
        now=_now_utc(),
    )
    with _SESSION_LOCK:
        _require_version_may_advance_locked(
            user_id=session.user_id, session_version=int(session.session_version)
        )
    return session


def apply_cloud_session_import(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    session: CloudSessionImport,
) -> str:
    """Make a validated session the current one, advancing the generation."""
    global _ACTIVE_SESSION, _EXPIRED_IDENTITY, _CREDENTIAL_GENERATION
    require_positive_timeout(busy_timeout_ms)
    with _SESSION_LOCK:
        _settle_expiry_locked()
        _require_version_may_advance_locked(
            user_id=session.user_id, session_version=int(session.session_version)
        )
        _ensure_active_user_row(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=session.user_id,
            timestamp=format_utc_iso(_now_utc()),
        )
        _CREDENTIAL_GENERATION += 1
        _ACTIVE_SESSION = ActiveDesktopSession(
            user_id=session.user_id,
            desktop_access_token=session.desktop_access_token,
            expires_at=session.expires_at,
            session_version=session.session_version,
            credential_generation=_CREDENTIAL_GENERATION,
        )
        _EXPIRED_IDENTITY = None
    return AUTH_CONTEXT_PRESENT_STATE


def import_desktop_session(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    desktop_access_token: str,
    expires_at: str,
    session_version: str,
) -> str:
    """Validate and apply at once, for a caller with no barrier to run."""
    return apply_cloud_session_import(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        session=validate_cloud_session_import(
            user_id=user_id,
            desktop_access_token=desktop_access_token,
            expires_at=expires_at,
            session_version=session_version,
        ),
    )


def classify_cloud_session_clear(
    *,
    user_id: str,
    reason: ClearReason,
    expected_session_version: str | None = None,
    expected_credential_generation: int | None = None,
) -> CloudSessionClearPlan:
    """Decide what a clear request does, without changing anything.

    A clear whose account, session version, or credential generation does not
    match the current session is stale (an old callback) and changes nothing.
    Reading here deliberately leaves an elapsed session alone: settling it is
    itself a transition the caller has to stop for (design 6.2), and it would
    not change the answer because it keeps all three matched values.
    """
    normalized_user_id = require_non_empty(user_id, field_name="user_id")
    if reason not in (CLEAR_REASON_SIGNED_OUT, CLEAR_REASON_EXPIRED):
        raise MigrationError("reason must be signed_out or expired")
    expected_version = (
        parse_session_version(expected_session_version)
        if expected_session_version is not None
        else None
    )
    with _SESSION_LOCK:
        target = _clear_target_locked()
        if (
            target is None
            or target.user_id != normalized_user_id
            or (
                expected_version is not None
                and parse_session_version(target.session_version) != expected_version
            )
            or (
                expected_credential_generation is not None
                and target.credential_generation != expected_credential_generation
            )
        ):
            return CloudSessionClearPlan(
                reason=reason,
                matched=None,
                identity_after=_cloud_identity_locked(),
            )
        if reason == CLEAR_REASON_EXPIRED:
            return CloudSessionClearPlan(
                reason=reason,
                matched=target,
                identity_after=_expired_identity(
                    user_id=target.user_id, session_version=target.session_version
                ),
            )
        return CloudSessionClearPlan(reason=reason, matched=target, identity_after=None)


def apply_cloud_session_clear(plan: CloudSessionClearPlan) -> ClearDesktopSessionResult:
    """Apply a plan, or nothing when it no longer matches the current session.

    Between classifying and applying, the store can still settle an elapsed
    session on its own. That keeps the account, sign-in, and generation, so a
    plan taken before the settling still applies: an explicit sign-out reaches
    ``absent`` from ``expired`` just as it does from ``present`` (design 6.2).
    Settling happens here first in either case, a stale plan included, so it is
    not the stale answer that decides whether an expiry may be published.
    """
    global _ACTIVE_SESSION, _EXPIRED_IDENTITY
    with _SESSION_LOCK:
        _settle_expiry_locked()
        matched = plan.matched
        if matched is None or _clear_target_locked() != matched:
            return ClearDesktopSessionResult(state=_state_locked(), stale=True)
        if plan.reason == CLEAR_REASON_EXPIRED:
            _ACTIVE_SESSION = None
            _EXPIRED_IDENTITY = matched
            return ClearDesktopSessionResult(
                state=AUTH_CONTEXT_EXPIRED_STATE, stale=False
            )
        _ACTIVE_SESSION = None
        _EXPIRED_IDENTITY = None
        return ClearDesktopSessionResult(state=AUTH_CONTEXT_ABSENT_STATE, stale=False)


def clear_desktop_session(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    reason: ClearReason = CLEAR_REASON_SIGNED_OUT,
    expected_session_version: str | None = None,
    expected_credential_generation: int | None = None,
) -> ClearDesktopSessionResult:
    """Classify and apply a clear at once, for a caller with no barrier to run."""
    del db_path
    require_positive_timeout(busy_timeout_ms)
    return apply_cloud_session_clear(
        classify_cloud_session_clear(
            user_id=user_id,
            reason=reason,
            expected_session_version=expected_session_version,
            expected_credential_generation=expected_credential_generation,
        )
    )


def load_active_desktop_session(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
) -> ActiveDesktopSession:
    del db_path
    require_positive_timeout(busy_timeout_ms)
    normalized_user_id = require_non_empty(user_id, field_name="user_id")
    with _SESSION_LOCK:
        _settle_expiry_locked()
        active_session = _ACTIVE_SESSION
        expired_identity = _EXPIRED_IDENTITY
    if active_session is not None and active_session.user_id == normalized_user_id:
        return active_session
    if expired_identity is not None and expired_identity.user_id == normalized_user_id:
        raise MigrationError("desktop session has expired")
    raise MigrationError("desktop session is not active")


def restore_expired_cloud_identity(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    session_version: str,
) -> str:
    """Restore an expired account identity from Electron's stored session.

    Used by ``configure`` after a helper restart: the account keeps ownership
    while cloud calls fail until re-login or explicit sign-out.
    """
    global _ACTIVE_SESSION, _EXPIRED_IDENTITY, _CREDENTIAL_GENERATION
    require_positive_timeout(busy_timeout_ms)
    normalized_user_id = require_non_empty(user_id, field_name="account_user_id")
    version = normalize_session_version(session_version)
    with _SESSION_LOCK:
        _ensure_active_user_row(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=normalized_user_id,
            timestamp=format_utc_iso(_now_utc()),
        )
        _CREDENTIAL_GENERATION += 1
        _ACTIVE_SESSION = None
        _EXPIRED_IDENTITY = ExpiredCloudIdentity(
            user_id=normalized_user_id,
            session_version=version,
            credential_generation=_CREDENTIAL_GENERATION,
        )
    return AUTH_CONTEXT_EXPIRED_STATE


def forget_cloud_session() -> str:
    """Drop any cloud identity (``configure`` with an absent session)."""
    global _ACTIVE_SESSION, _EXPIRED_IDENTITY
    with _SESSION_LOCK:
        _ACTIVE_SESSION = None
        _EXPIRED_IDENTITY = None
    return AUTH_CONTEXT_ABSENT_STATE


def mark_configured() -> None:
    global _CONFIGURED
    with _SESSION_LOCK:
        _CONFIGURED = True


def read_configured() -> bool:
    with _SESSION_LOCK:
        return _CONFIGURED


def reset_desktop_session_store() -> None:
    global _ACTIVE_SESSION, _EXPIRED_IDENTITY, _CREDENTIAL_GENERATION, _CONFIGURED
    with _SESSION_LOCK:
        _ACTIVE_SESSION = None
        _EXPIRED_IDENTITY = None
        _CREDENTIAL_GENERATION = 0
        _CONFIGURED = False


def read_auth_context_state() -> str:
    with _SESSION_LOCK:
        _settle_expiry_locked()
        return _state_locked()


def read_credential_generation() -> int:
    with _SESSION_LOCK:
        return _CREDENTIAL_GENERATION


def peek_cloud_session_identity_without_settling() -> CloudSessionIdentity | None:
    """Read both cloud states under one lock, leaving an elapsed session alone.

    Every other read here settles an elapsed ``present`` session into
    ``expired`` as a side effect. A caller that compares the identity before and
    after an operation must not do that settling itself, or the transition it
    has to stop for would already have happened by the time it looks (design
    6.2). A lapsed session therefore still reads ``present`` until something
    else settles it.
    """
    with _SESSION_LOCK:
        return _cloud_identity_locked()


def settle_cloud_session_expiry() -> CloudSessionIdentity | None:
    """Publish a lapsed cloud session as ``expired``, keeping its identity.

    Returns the identity that became visible, or None when nothing had lapsed.
    Design 6.2 puts this transition behind the same stop barrier as an
    ``expired`` clear; every other read here still settles it as a side effect,
    so this names the operation without changing when it happens.
    """
    with _SESSION_LOCK:
        return _settle_expiry_locked()


def seconds_until_cloud_session_expiry() -> float | None:
    """How long the present session has left, or None when there is none.

    The stop barrier arms its timer from this, so it deliberately does not
    settle an elapsed session on the way: one whose time is already up answers
    ``0.0`` and the timer runs the transition through the barrier at once.
    """
    with _SESSION_LOCK:
        session = _ACTIVE_SESSION
        if session is None:
            return None
        expires_at = parse_iso8601(
            session.expires_at, field_name="desktop_access_token_expires_at"
        )
        return max(0.0, (expires_at - _now_utc()).total_seconds())


def peek_pending_cloud_session_expiry() -> CloudSessionIdentity | None:
    """What settling would publish, without publishing it.

    A caller that has to stop in-flight work before the transition becomes
    visible needs to know the transition is due, and which identity it will
    publish, while the session still reads ``present`` everywhere else.
    """
    with _SESSION_LOCK:
        session = _lapsed_session_locked()
        if session is None:
            return None
        return _expired_identity(
            user_id=session.user_id, session_version=session.session_version
        )


def peek_active_desktop_session() -> ActiveDesktopSession | None:
    with _SESSION_LOCK:
        _settle_expiry_locked()
        return _ACTIVE_SESSION


def peek_expired_cloud_identity() -> ExpiredCloudIdentity | None:
    with _SESSION_LOCK:
        _settle_expiry_locked()
        return _EXPIRED_IDENTITY


__all__ = [
    "AUTH_CONTEXT_ABSENT_STATE",
    "AUTH_CONTEXT_EXPIRED_STATE",
    "AUTH_CONTEXT_PRESENT_STATE",
    "ActiveDesktopSession",
    "ClearDesktopSessionResult",
    "CloudSessionClearPlan",
    "CloudSessionIdentity",
    "ExpiredCloudIdentity",
    "apply_cloud_session_clear",
    "apply_cloud_session_import",
    "classify_cloud_session_clear",
    "clear_desktop_session",
    "forget_cloud_session",
    "import_desktop_session",
    "load_active_desktop_session",
    "mark_configured",
    "peek_active_desktop_session",
    "peek_cloud_session_identity_without_settling",
    "peek_expired_cloud_identity",
    "peek_pending_cloud_session_expiry",
    "read_auth_context_state",
    "read_configured",
    "read_credential_generation",
    "reset_desktop_session_store",
    "restore_expired_cloud_identity",
    "seconds_until_cloud_session_expiry",
    "settle_cloud_session_expiry",
    "validate_cloud_session_import",
]
