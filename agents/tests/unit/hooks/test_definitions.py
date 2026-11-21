from __future__ import annotations

from datetime import timedelta

import pytest

from pantaray_agents.hooks.definitions import (
    Every,
    HookDefinitionError,
    RunJob,
    load_hook_definitions,
)

_GATES = frozenset({"open"})


def _accept(_run: RunJob) -> None:
    return None


def _load(text: str) -> object:
    return load_hook_definitions(text, gate_names=_GATES, validate_run=_accept)


def test_a_definition_parses_into_its_closed_shape() -> None:
    (hook,) = load_hook_definitions(
        """
        [[hooks]]
        id = "builtin.daily"
        trigger = { every = "day", offset = "10m", tz = "utc" }
        requires = ["open"]
        run = { job = "summarize", args = { kind = "24h" } }
        """,
        gate_names=_GATES,
        validate_run=_accept,
    )

    assert hook.id == "builtin.daily"
    assert hook.trigger == Every(
        every="day", align=True, offset=timedelta(minutes=10), tz="utc"
    )
    assert hook.requires == ("open",)
    assert hook.run == RunJob(job="summarize", args={"kind": "24h"})


_RUN = 'run = { job = "j" }'


@pytest.mark.parametrize(
    "body",
    [
        # A typo would otherwise leave a hook that silently never fires.
        f'id = "a"\ntrigger = {{ every = "15m", align = true, tz = "utc" }}\n{_RUN}\nrequire = ["open"]',
        f'id = "a"\ntrigger = {{ every = "15m", align = true, tz = "utc" }}\n{_RUN}\nrequires = ["closed"]',
        f'id = "a"\ntrigger = {{ every = "15m", align = true }}\n{_RUN}',
        f'id = "a"\ntrigger = {{ every = "7m", align = true, tz = "utc" }}\n{_RUN}',
        f'id = "a"\ntrigger = {{ every = "15m", tz = "utc" }}\n{_RUN}',
        f'id = "a"\ntrigger = {{ every = "hour", align = false, tz = "utc" }}\n{_RUN}',
        f'id = "a"\ntrigger = {{ every = "hour", offset = "60m", tz = "utc" }}\n{_RUN}',
        f'id = "a"\ntrigger = {{ every = "2h", align = false, offset = "5m", tz = "utc" }}\n{_RUN}',
        f'id = "a"\ntrigger = {{ at = 2026-11-21T09:00:00 }}\n{_RUN}',
        'id = "a"\ntrigger = { every = "15m", align = true, tz = "utc" }\nrun = { job = "j", args = { n = 1 } }',
    ],
)
def test_a_malformed_definition_is_rejected(body: str) -> None:
    with pytest.raises(HookDefinitionError):
        _load(f"[[hooks]]\n{body}\n")


def test_duplicate_ids_are_rejected() -> None:
    # Two hooks sharing an id would share one state row and starve each other.
    hook = f'[[hooks]]\nid = "a"\ntrigger = {{ every = "day", tz = "utc" }}\n{_RUN}\n'
    with pytest.raises(HookDefinitionError, match="unique"):
        _load(hook + hook)


def test_the_callers_run_check_rejects_at_load() -> None:
    def reject(run: RunJob) -> None:
        raise HookDefinitionError(f"unknown job {run.job}")

    with pytest.raises(HookDefinitionError, match="unknown job j"):
        load_hook_definitions(
            f'[[hooks]]\nid = "a"\ntrigger = {{ every = "day", tz = "utc" }}\n{_RUN}\n',
            gate_names=_GATES,
            validate_run=reject,
        )
