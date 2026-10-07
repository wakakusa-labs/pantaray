"""`pantaray_agents.tools` sits below the agents and must not load them.

The check runs in a fresh interpreter because this test process has already
imported agent modules. Any import path counts, including the package
`__init__` modules of lower layers that `tools/` reaches transitively.
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = """
import importlib
import pkgutil
import sys

import pantaray_agents.tools as tools

for module in pkgutil.walk_packages(tools.__path__, prefix="pantaray_agents.tools."):
    importlib.import_module(module.name)

loaded = sorted(
    name
    for name in sys.modules
    if name == "pantaray_agents.agents" or name.startswith("pantaray_agents.agents.")
)
print("\\n".join(loaded))
"""


def test_importing_tools_does_not_load_agent_modules() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == []
