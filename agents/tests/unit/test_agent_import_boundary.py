"""The layers below the agents must not load them, nor the tool broker.

`pantaray_agents.tools` and `pantaray_agents.conversation` are imported by the
agents and by the broker, never the other way round. The check runs in a fresh
interpreter because this test process has already imported those modules. Any
import path counts, including the package `__init__` modules of lower layers a
package reaches transitively.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

_FORBIDDEN_PACKAGES = (
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
    "package", ["pantaray_agents.tools", "pantaray_agents.conversation"]
)
def test_importing_the_package_does_not_load_agent_or_broker_modules(
    package: str,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE.format(package=package, forbidden=_FORBIDDEN_PACKAGES),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == []
