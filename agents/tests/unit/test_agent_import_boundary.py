"""Lower layers must not load the layers that use them.

`pantaray_agents.tools` and `pantaray_agents.conversation` are imported by the
agents and by the broker, never the other way round. The runtime's stop barrier
stops the chat's turns without loading the chat, and Suggestion's research
tools share memory_sql with the Action without loading the Action. The check
runs in a fresh interpreter because this test process has already imported
those modules. Any
import path counts, including the package `__init__` modules of lower layers a
package reaches transitively.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

_BELOW_AGENTS_AND_BROKER = (
    "pantaray_agents.agents",
    "pantaray_agents.local_runtime.tooling.brokering",
)

_PROBE = """
import importlib
import pkgutil
import sys

package = importlib.import_module({package!r})

for module in pkgutil.walk_packages(package.__path__, prefix={package!r} + "."):
    importlib.import_module(module.name)

loaded = sorted(
    name
    for name in sys.modules
    if any(
        name == forbidden or name.startswith(forbidden + ".")
        for forbidden in {forbidden!r}
    )
)
print("\\n".join(loaded))
"""


@pytest.mark.parametrize(
    ("package", "forbidden"),
    [
        ("pantaray_agents.tools", _BELOW_AGENTS_AND_BROKER),
        ("pantaray_agents.conversation", _BELOW_AGENTS_AND_BROKER),
        (
            "pantaray_agents.local_runtime.runtime",
            ("pantaray_agents.local_runtime.chat", "pantaray_agents.agents.chat_agent"),
        ),
        (
            "pantaray_agents.local_runtime.tooling.suggestion_research",
            (
                "pantaray_agents.agents.action_agent",
                "pantaray_agents.agents.chat_agent",
            ),
        ),
    ],
)
def test_importing_the_package_does_not_load_the_layers_above_it(
    package: str, forbidden: tuple[str, ...]
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE.format(package=package, forbidden=forbidden),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == []
