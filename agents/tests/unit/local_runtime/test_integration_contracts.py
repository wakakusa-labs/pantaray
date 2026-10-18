from __future__ import annotations

import pytest
from pydantic import ValidationError

from pantaray_agents.local_runtime.integrations.contracts import (
    ApiDenial,
    GatewayLimits,
    IntegrationAccount,
    IntegrationError,
)

WORK = IntegrationAccount(account="ca_work", toolkit="gmail", label="work@example.com")
HOME = IntegrationAccount(account="ca_home", toolkit="gmail", label="home@example.com")


def test_account_required_names_the_accounts_to_choose_from() -> None:
    error = IntegrationError(kind="account_required", candidates=[WORK, HOME])

    assert [candidate.label for candidate in error.candidates] == [
        "work@example.com",
        "home@example.com",
    ]
    with pytest.raises(ValidationError):
        IntegrationError(kind="account_required")
    with pytest.raises(ValidationError):
        IntegrationError(kind="account_required", candidates=[WORK])


def test_other_errors_carry_no_candidates() -> None:
    assert (
        IntegrationError(kind="rate_limited", retry_after_seconds=30).candidates == []
    )
    with pytest.raises(ValidationError):
        IntegrationError(kind="permission_denied", candidates=[WORK, HOME])
    with pytest.raises(ValidationError):
        IntegrationError(kind="permission_denied", candidates=[WORK])


def test_limits_carry_one_connection_method_reason_per_denied_capability() -> None:
    execute = ApiDenial(source="connection_method", capability="execute")
    connect = ApiDenial(source="connection_method", capability="connect")

    GatewayLimits(can_connect=False, can_execute=False, reasons=[execute, connect])
    for can_connect, can_execute, reasons in (
        (True, False, []),
        (True, True, [execute]),
        (False, True, [execute]),
        (True, False, [execute, execute]),
        (
            True,
            False,
            [ApiDenial(source="account_grant", capability="execute")],
        ),
    ):
        with pytest.raises(ValidationError):
            GatewayLimits(
                can_connect=can_connect, can_execute=can_execute, reasons=reasons
            )
