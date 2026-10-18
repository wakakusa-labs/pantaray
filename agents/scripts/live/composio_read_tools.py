"""Print every toolkit's read-classified Composio tools for a human to review.

A read tool runs without a confirmation, so each new toolkit row gets this look
before it ships. Also checks that each change probe is a read tool whose
arguments its input schema accepts. Reads COMPOSIO_API_KEY from the environment
and never prints it; no connected account is needed.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime

from jsonschema import Draft7Validator  # type: ignore[import-untyped]

from pantaray_agents.local_runtime.integrations.change_probe import (
    substitute_window,
)
from pantaray_agents.local_runtime.integrations.composio.client import ComposioClient
from pantaray_agents.local_runtime.integrations.composio.gateway import (
    ComposioGateway,
)
from pantaray_agents.local_runtime.integrations.composio.toolkits import TOOLKITS


async def main() -> int:
    gateway = ComposioGateway(ComposioClient(os.environ["COMPOSIO_API_KEY"]))
    failures = 0
    for entry in TOOLKITS:
        tools = await gateway.list_tools(entry.toolkit)
        reads = sorted(tool.slug for tool in tools if tool.tool_class == "read")
        print(f"## {entry.toolkit}: {len(reads)} read of {len(tools)} listed")
        for slug in reads:
            print(f"  {slug}")
        for slug, override in sorted(entry.class_overrides.items()):
            print(f"  override {slug} -> {override}")
        if entry.probe is None:
            continue
        probe = next((t for t in tools if t.slug == entry.probe.tool_slug), None)
        arguments = substitute_window(entry.probe.arguments, datetime.now(UTC))
        errors: list[str] = []
        if probe is None:
            errors.append("not listed")
        else:
            validator = Draft7Validator(probe.input_schema)
            errors += [error.message for error in validator.iter_errors(arguments)]
            properties = probe.input_schema.get("properties")
            known = properties if isinstance(properties, dict) else {}
            errors += [f"unknown argument {n}" for n in arguments if n not in known]
            if probe.tool_class != "read":
                errors.append("not a read tool")
        print(f"  probe {entry.probe.tool_slug}: {'ok' if not errors else errors}")
        failures += bool(errors)
    print(f"failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
