from __future__ import annotations

import pytest
from pydantic import ValidationError

from pantaray_agents.local_runtime.integrations.contracts import (
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
