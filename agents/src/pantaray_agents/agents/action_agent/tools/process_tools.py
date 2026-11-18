"""Tools that reach the processes bash started with run_in_background."""

from __future__ import annotations

from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    READ_PROCESS_OUTPUT_TOOL_ID,
    STOP_PROCESS_TOOL_ID,
)
from pantaray_agents.local_runtime.tooling.brokering.process_protocol import (
    ReadProcessOutputToolArgs,
    StopProcessToolArgs,
)
from pantaray_agents.local_runtime.tooling.sandbox import runtime_policy
from pantaray_agents.tools.contract import ToolConcurrency

from .base import (
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    tool_execution_policy,
)
from .bash_tool import PROCESS_OUTPUT_SCHEMA
from .broker_tool_input_schema import (
    BrokerToolFieldPresentation,
    broker_tool_input_spec_from_model,
)

_PROCESS_ID_FIELD = BrokerToolFieldPresentation(
    name="process_id",
    description="The process_id a bash call with run_in_background returned.",
)

READ_PROCESS_OUTPUT_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=READ_PROCESS_OUTPUT_TOOL_ID,
        name="Read Process Output",
        description=(
            "Read the output a background process wrote since it was last "
            "returned, and whether it is still running."
        ),
        guide=ToolGuideSpec(
            what=(
                "Returns the process's new stdout and stderr and its status: "
                "running, or how it ended (exited, stopped, limit_exceeded, "
                "spawn_failed, failed) with its exit code or signal."
            ),
            when=(
                "Use it to see whether a dev server is up or what a watcher "
                "reported, or to wait for a background command to end."
            ),
            pitfalls=(
                "It returns as soon as the process ends, or after wait_seconds. "
                "Output not read in time is kept only at its start and end; "
                "stdout_dropped_bytes and stderr_dropped_bytes count the rest."
            ),
        ),
        concurrency=ToolConcurrency("sequential"),
        execution_policy=tool_execution_policy(
            intent_class="process_exec_local",
            required_capabilities=("process_exec_local",),
            default_timeout_ms=(runtime_policy.PROCESS_READ_WAIT_MAX_SECONDS + 15)
            * 1000,
        ),
        input_spec=broker_tool_input_spec_from_model(
            model=ReadProcessOutputToolArgs,
            fields=(
                _PROCESS_ID_FIELD,
                BrokerToolFieldPresentation(
                    name="wait_seconds",
                    description=(
                        "How long to wait for the process to end before "
                        "returning what it wrote so far, from 0 to "
                        f"{runtime_policy.PROCESS_READ_WAIT_MAX_SECONDS}. Default "
                        f"{runtime_policy.PROCESS_READ_WAIT_DEFAULT_SECONDS}."
                    ),
                ),
            ),
            description="Read a background process's new output and status.",
        ),
        output_schema=PROCESS_OUTPUT_SCHEMA,
    )
)

STOP_PROCESS_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=STOP_PROCESS_TOOL_ID,
        name="Stop Process",
        description="Stop a background process and return how it ended.",
        guide=ToolGuideSpec(
            what=(
                "Asks the process to end, kills it if it has not after a few "
                "seconds, and returns its last output and how it ended."
            ),
            when="Use it to end a background process this run started.",
            pitfalls=("Stopping a process that already ended returns how it ended."),
        ),
        concurrency=ToolConcurrency("sequential"),
        execution_policy=tool_execution_policy(
            intent_class="process_exec_local",
            required_capabilities=("process_exec_local",),
            default_timeout_ms=30_000,
        ),
        input_spec=broker_tool_input_spec_from_model(
            model=StopProcessToolArgs,
            fields=(_PROCESS_ID_FIELD,),
            description="Stop a background process.",
        ),
        output_schema=PROCESS_OUTPUT_SCHEMA,
    )
)
