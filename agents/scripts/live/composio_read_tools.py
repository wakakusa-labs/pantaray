"""Print every toolkit's read-classified Composio tools for a human to review.

A read tool runs without a confirmation, so each new toolkit row gets this look
before it ships. Read tools whose slug or output field names mention a secret
are marked `secret?` (descriptions say "token" too often to help): block each one that returns a secret's value, and
keep one that returns only names, metadata, public keys or masked values. Also
checks that each change probe is a read tool whose arguments its input schema
accepts. Reads COMPOSIO_API_KEY from the environment and never prints it; no
connected account is needed.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import UTC, datetime

from jsonschema import Draft7Validator  # type: ignore[import-untyped]

from pantaray_agents.local_runtime.integrations.change_probe import (
    substitute_window,
)
from pantaray_agents.local_runtime.integrations.composio.client import (
    METADATA_TIMEOUT,
    ComposioClient,
)
from pantaray_agents.local_runtime.integrations.composio.mapping import (
    RawTool,
    tool_disposition,
)
from pantaray_agents.local_runtime.integrations.composio.toolkits import TOOLKITS

_SECRET_WORDS = re.compile(
    r"secret|api_?key|apikey|service_?role|token|password|(^|_)pass($|_)|"
    r"credential|signing|encryption|private_?key|keypair|pem$|jwt|webhook|"
    r"connection_?string|form_?digest",
    re.IGNORECASE,
)
# Pagination cursors are tokens too, but not secrets.
_PAGING = re.compile(r"page_?token|next_?token|sync_?token|delta|cursor", re.I)


def _field_names(schema: object) -> list[str]:
    if isinstance(schema, dict):
        names = list((schema.get("properties") or {}).keys())
        return names + [n for value in schema.values() for n in _field_names(value)]
    if isinstance(schema, list):
        return [n for value in schema for n in _field_names(value)]
    return []


def _secret_hints(item: dict[str, object]) -> list[str]:
    words = [str(item.get("slug", "")), *_field_names(item.get("output_parameters"))]
    return sorted(
        {
            match.group(0).lower()
            for word in words
            if not _PAGING.search(word)
            for match in [_SECRET_WORDS.search(word)]
            if match
        }
    )


async def main() -> int:
    client = ComposioClient(os.environ["COMPOSIO_API_KEY"])
    failures = 0
    for entry in TOOLKITS:
        items = await client.list_all(
            "/tools",
            {"toolkit_slug": entry.toolkit, "include_deprecated": "false"},
            timeout=METADATA_TIMEOUT,
        )
        reads: list[tuple[RawTool, list[str]]] = []
        for item in items:
            raw = RawTool.model_validate(item)
            if tool_disposition(raw, entry.class_overrides) == "read":
                assert isinstance(item, dict)
                reads.append((raw, _secret_hints(item)))
        print(f"## {entry.toolkit}: {len(reads)} read of {len(items)} listed")
        for raw, hints in sorted(reads, key=lambda pair: pair[0].slug):
            print(f"  {raw.slug}" + (f"  secret? {', '.join(hints)}" if hints else ""))
        for slug, override in sorted(entry.class_overrides.items()):
            print(f"  override {slug} -> {override}")
        if entry.probe is None:
            continue
        probe = next((r for r, _ in reads if r.slug == entry.probe.tool_slug), None)
        arguments = substitute_window(entry.probe.arguments, datetime.now(UTC))
        errors: list[str] = []
        if probe is None:
            errors.append("not listed as a read tool")
        else:
            validator = Draft7Validator(probe.input_parameters)
            errors += [error.message for error in validator.iter_errors(arguments)]
            properties = probe.input_parameters.get("properties")
            known = properties if isinstance(properties, dict) else {}
            errors += [f"unknown argument {n}" for n in arguments if n not in known]
        print(f"  probe {entry.probe.tool_slug}: {'ok' if not errors else errors}")
        failures += bool(errors)
    print(f"failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
