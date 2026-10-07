"""The layers below the agents must not load them.

`pantaray_agents.tools` and `pantaray_agents.conversation` are imported by the
agents, never the other way round. The check runs in a fresh interpreter
because this test process has already imported agent modules. Any import path
counts, including the package `__init__` modules of lower layers a package
reaches transitively.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

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
    if name == "pantaray_agents.agents" or name.startswith("pantaray_agents.agents.")
)
print("\\n".join(loaded))
"""


@pytest.mark.parametrize(
    "package", ["pantaray_agents.tools", "pantaray_agents.conversation"]
)
def test_importing_the_package_does_not_load_agent_modules(package: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(package=package)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == []
