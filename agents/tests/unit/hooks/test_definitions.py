from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest

from pantaray_agents.hooks.definitions import (
    Delivery,
    Every,
    FieldEquals,
    HookDefinition,
    HookDefinitionError,
    MinInterval,
    NamedPredicate,
    On,
    RunJob,
    load_hook_definitions,
)


def _accept(_run: RunJob) -> None:
    return None


def _load(
    text: str, validate_run: Callable[[RunJob], None] = _accept
) -> tuple[HookDefinition, ...]:
    return load_hook_definitions(
        text,
        gate_names=frozenset({"open"}),
        event_names=frozenset({"a.done", "a.failed", "chat.said", "b.started"}),
        predicate_names=frozenset({"still_true"}),
        validate_run=validate_run,
    )


def test_a_definition_parses_into_its_closed_shape() -> None:
    (hook,) = _load(
        """
        [[hooks]]
        id = "builtin.daily"
        trigger = { every = "day", offset = "10m", tz = "utc" }
        requires = ["open"]
        run = { job = "summarize", args = { kind = "24h" } }
        """
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
        _load(
            f'[[hooks]]\nid = "a"\ntrigger = {{ every = "day", tz = "utc" }}\n{_RUN}\n',
            reject,
        )


def test_an_on_hook_parses_its_conditions_and_delivery() -> None:
    (hook,) = _load(
        """
        [[hooks]]
        id = "builtin.memory"
        trigger = { on = ["a.done", "chat.said", "a.failed"] }
        if = { "a.done" = { kind = "24h" }, "chat.said" = "still_true" }
        deliver = { immediate = ["a.done"], max_wait = "15m", collapse = "all", min_interval = { after = "b.started", duration = "15m" } }
        run = { job = "j" }
        """
    )

    assert hook.trigger == On(
        events=("a.done", "chat.said", "a.failed"),
        conditions={
            "a.done": FieldEquals(field="kind", value="24h"),
            "chat.said": NamedPredicate(name="still_true"),
        },
        delivery=Delivery(
            immediate=frozenset({"a.done"}),
            max_wait=timedelta(minutes=15),
            collapse="all",
            min_interval=MinInterval(after="b.started", duration=timedelta(minutes=15)),
        ),
    )


_ON = 'id = "a"\ntrigger = { on = ["a.done"] }\nrun = { job = "j" }'


@pytest.mark.parametrize(
    "extra",
    [
        # Each would leave an on hook that silently never fires or never waits.
        'deliver = { collapse = "all" }',
        'deliver = { immediate = ["a.done"], collapse = "some" }',
        'deliver = { immediate = ["chat.said"], collapse = "all" }',
        'deliver = { immediate = ["a.done"], collapse = "all", min_interval = { after = "a.done", duration = "1m" } }',
        'deliver = { immediate = ["a.done"], collapse = "all" }\nif = { "chat.said" = "still_true" }',
        'deliver = { immediate = ["a.done"], collapse = "all" }\nif = { "a.done" = "unknown" }',
        'deliver = { immediate = ["a.done"], collapse = "all" }\nif = { "a.done" = { x = 1, y = 2 } }',
        # A misspelled event name is never written.
        'deliver = { immediate = ["a.dnoe"], collapse = "all" }',
    ],
)
def test_a_malformed_on_hook_is_rejected(extra: str) -> None:
    with pytest.raises(HookDefinitionError):
        _load(f"[[hooks]]\n{_ON}\n{extra}\n")
