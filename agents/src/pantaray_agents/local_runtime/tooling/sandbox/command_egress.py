"""Give an Action command with network on an egress lease of its own.

The seatbelt profile then lets the command reach loopback alone, so every other
connection goes through the proxy, and the result records where it went.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from dataclasses import asdict, replace
from typing import cast

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import BrokerPolicyError

from ..brokering.broker_common import BrokerContext
from ..brokering.broker_outcome import UnprojectedBrokerToolOutcome
from ..brokering.broker_protocol import ValidatedCommandRequest
from ..resources.resource_tracking import (
    register_path_resource,
    register_process_group_resource,
)
from .command_sandbox_client import (
    CreateSubprocessExec,
    RegisterResource,
    attach_canceled_command_output,
    canceled_command_output,
    run_command_in_sandbox,
    write_command_invocation_audit,
)
from .egress_proxy import (
    EgressLease,
    EgressProxyStartError,
    NetworkDestination,
    command_egress_lease,
)

COMMAND_NETWORK_UNAVAILABLE = "COMMAND_NETWORK_UNAVAILABLE"


def _network_output(destinations: Sequence[NetworkDestination]) -> list[JSONValue]:
    return [cast(JSONValue, asdict(destination)) for destination in destinations]


async def run_command_via_sandbox(
    *,
    context: BrokerContext,
    request: ValidatedCommandRequest,
    create_subprocess_exec: CreateSubprocessExec = asyncio.create_subprocess_exec,
    register_process_group_resource_fn: RegisterResource = register_process_group_resource,
    register_path_resource_fn: RegisterResource = register_path_resource,
) -> UnprojectedBrokerToolOutcome:
    """Run one Action command, holding a lease for it while it runs when its
    network is on.

    The destinations are read after the lease has ended, when they are final.
    An unsandboxed command gets no lease: the user approved it to run with their
    own permissions, network included, and it is not recorded.
    """

    def run(egress: EgressLease | None) -> Awaitable[UnprojectedBrokerToolOutcome]:
        return run_command_in_sandbox(
            context=context,
            request=request,
            egress=egress,
            create_subprocess_exec=create_subprocess_exec,
            register_process_group_resource_fn=register_process_group_resource_fn,
            register_path_resource_fn=register_path_resource_fn,
        )

    if request.network_policy == "deny" or request.run_outside_sandbox:
        return await run(None)
    lease: EgressLease | None = None
    try:
        async with command_egress_lease() as lease:
            outcome = await run(lease)
    except EgressProxyStartError as exc:
        # Fail closed: the command never gets a direct route instead.
        write_command_invocation_audit(
            context=context,
            request=request,
            terminal_outcome="broker_failed",
            exit_code=None,
            signal_value=None,
            stdout_bytes=0,
            stderr_bytes=0,
            budget_exceeded_kind=None,
            sandbox_violation_kind=None,
            sandbox_violation_summary=str(exc),
            updated_at=now_utc_iso(),
        )
        raise BrokerPolicyError(
            "The command did not run: its network connection could not be set up.",
            code=COMMAND_NETWORK_UNAVAILABLE,
            fix_hint="Retry the call once; if it fails again, tell the user that "
            "commands cannot reach the network right now.",
        ) from exc
    except asyncio.CancelledError as canceled:
        partial = canceled_command_output(canceled)
        if partial is not None and lease is not None:
            attach_canceled_command_output(
                canceled,
                stdout=partial.stdout,
                stderr=partial.stderr,
                network=tuple(_network_output(lease.destinations())),
            )
        raise
    assert isinstance(outcome.output, dict)  # A command's result is a JSON object.
    return replace(
        outcome,
        output={**outcome.output, "network": _network_output(lease.destinations())},
    )


__all__ = ["COMMAND_NETWORK_UNAVAILABLE", "run_command_via_sandbox"]
