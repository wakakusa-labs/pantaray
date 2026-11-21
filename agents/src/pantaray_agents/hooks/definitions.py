"""Hook definitions: the closed types and their parsing from a TOML document."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

CalendarUnit = Literal["hour", "day", "week", "month", "quarter"]
Zone = Literal["utc", "local"]

_CALENDAR_UNITS: Final[tuple[CalendarUnit, ...]] = (
    "hour",
    "day",
    "week",
    "month",
    "quarter",
)
# The shortest instance of each unit; an offset at or past it would move an
# occurrence into the next period.
_SHORTEST_CALENDAR_PERIOD: Final[Mapping[CalendarUnit, timedelta]] = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=28),
    "quarter": timedelta(days=90),
}
_DURATION_PATTERN: Final = re.compile(r"(\d+)([smhd])")
_DURATION_UNITS: Final[Mapping[str, timedelta]] = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
}
_DAY: Final = timedelta(days=1)


class HookDefinitionError(ValueError):
    """A hook definition is malformed or names something that does not exist."""


@dataclass(frozen=True, slots=True)
class At:
    """Fire once at an instant."""

    at: datetime


@dataclass(frozen=True, slots=True)
class Every:
    """Fire repeatedly.

    A calendar unit fires at each boundary of that unit plus ``offset``. A
    duration fires on a grid that starts at midnight of ``tz`` when ``align`` is
    set (so it must divide a day), and ``every`` after the previous fire when not.
    """

    every: timedelta | CalendarUnit
    align: bool
    offset: timedelta
    tz: Zone


# A value an event carries: events are flat records of scalars.
EventValue = str | int | bool | None
Collapse = Literal["all", "latest"]


@dataclass(frozen=True, slots=True)
class FieldEquals:
    """The event's field holds this value."""

    field: str
    value: str | int | bool


@dataclass(frozen=True, slots=True)
class NamedPredicate:
    """A check of current state the caller supplies by name."""

    name: str


Condition = FieldEquals | NamedPredicate


@dataclass(frozen=True, slots=True)
class MinInterval:
    """Fire no sooner than ``duration`` after the latest ``after`` event."""

    after: str
    duration: timedelta


@dataclass(frozen=True, slots=True)
class Delivery:
    """When an inbox is handed to a run, and how much of it.

    One run of a hook is active at a time: events arriving meanwhile wait in the
    inbox for the next run.
    """

    # One of these in the inbox fires the hook at once; any other event rides
    # along, waiting for one of these or, when set, until it is max_wait old.
    immediate: frozenset[str]
    max_wait: timedelta | None
    # all: every pending event is handed; latest: only the newest one.
    collapse: Collapse
    min_interval: MinInterval | None


@dataclass(frozen=True, slots=True)
class On:
    """Fire on events, collected in the hook's inbox until delivered.

    An event whose condition fails is consumed without being handed.
    """

    events: tuple[str, ...]
    conditions: Mapping[str, Condition]
    delivery: Delivery


Trigger = At | Every | On


@dataclass(frozen=True, slots=True)
class RunJob:
    """Enqueue one job of a type the caller knows, with string arguments."""

    job: str
    args: Mapping[str, str]


Run = RunJob


@dataclass(frozen=True, slots=True)
class HookDefinition:
    id: str
    trigger: Trigger
    # Every gate must be open for the hook to fire; a closed one makes it wait.
    requires: tuple[str, ...]
    run: Run


def inbox_events(hook: HookDefinition) -> frozenset[str]:
    """The events written to the hook's inbox: its own, and its interval anchor."""
    if not isinstance(hook.trigger, On):
        return frozenset()
    min_interval = hook.trigger.delivery.min_interval
    return frozenset(hook.trigger.events) | (
        frozenset() if min_interval is None else {min_interval.after}
    )


@dataclass(frozen=True, slots=True)
class _Vocabulary:
    """The names the caller defines: its gates, events and predicates."""

    gates: frozenset[str]
    events: frozenset[str]
    predicates: frozenset[str]


def load_hook_definitions(
    text: str,
    *,
    gate_names: frozenset[str],
    event_names: frozenset[str],
    predicate_names: frozenset[str],
    validate_run: Callable[[RunJob], None],
) -> tuple[HookDefinition, ...]:
    """Parse a TOML document of ``[[hooks]]`` tables.

    ``validate_run`` raises ``HookDefinitionError`` for a run the caller cannot
    start, so a bad definition fails here rather than on every tick.
    """
    names = _Vocabulary(
        gates=gate_names, events=event_names, predicates=predicate_names
    )
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise HookDefinitionError(f"hook definitions are not TOML: {error}") from error
    _reject_unknown_keys(document, {"hooks"}, where="document")
    tables = document.get("hooks")
    if not isinstance(tables, list):
        raise HookDefinitionError("hook definitions need a [[hooks]] array")
    hooks = tuple(
        _parse_hook(_table(raw, where="hooks"), names, validate_run) for raw in tables
    )
    ids = [hook.id for hook in hooks]
    if len(ids) != len(set(ids)):
        raise HookDefinitionError(f"hook ids are not unique: {ids}")
    return hooks


def _parse_hook(
    table: Mapping[str, object],
    names: _Vocabulary,
    validate_run: Callable[[RunJob], None],
) -> HookDefinition:
    _reject_unknown_keys(
        table, {"id", "trigger", "requires", "if", "deliver", "run"}, where="hook"
    )
    hook_id = _string(table.get("id"), where="hook id")
    where = f"hook {hook_id}"
    requires = table.get("requires", [])
    if not isinstance(requires, list) or not all(
        isinstance(name, str) for name in requires
    ):
        raise HookDefinitionError(f"{where}: requires must be a list of gate names")
    unknown_gates = sorted(set(requires) - names.gates)
    if unknown_gates:
        raise HookDefinitionError(f"{where}: unknown gates {unknown_gates}")
    run = _parse_run(_table(table.get("run"), where=f"{where} run"), where)
    validate_run(run)
    trigger_table = _table(table.get("trigger"), where=f"{where} trigger")
    trigger: Trigger
    if "on" in trigger_table:
        trigger = _parse_on(trigger_table, table, names, where)
    else:
        if "if" in table or "deliver" in table:
            raise HookDefinitionError(f"{where}: if and deliver need an on trigger")
        trigger = _parse_time_trigger(trigger_table, where)
    return HookDefinition(
        id=hook_id, trigger=trigger, requires=tuple(requires), run=run
    )


def _parse_on(
    trigger: Mapping[str, object],
    hook: Mapping[str, object],
    names: _Vocabulary,
    where: str,
) -> On:
    _reject_unknown_keys(trigger, {"on"}, where=f"{where} trigger")
    events = _event_names(trigger.get("on"), names, where=f"{where} on")
    if not events:
        raise HookDefinitionError(f"{where}: on needs at least one event")
    conditions: dict[str, Condition] = {}
    for event, raw in _table(hook.get("if", {}), where=f"{where} if").items():
        if event not in events:
            raise HookDefinitionError(f"{where}: if names {event}, not one of on")
        conditions[event] = _parse_condition(raw, names, where=f"{where} if {event}")
    return On(
        events=events,
        conditions=conditions,
        delivery=_parse_delivery(
            _table(hook.get("deliver"), where=f"{where} deliver"),
            events,
            names,
            where,
        ),
    )


def _parse_condition(value: object, names: _Vocabulary, *, where: str) -> Condition:
    if isinstance(value, str):
        if value not in names.predicates:
            raise HookDefinitionError(f"{where}: unknown predicate {value}")
        return NamedPredicate(name=value)
    table = _table(value, where=where)
    match list(table.items()):
        case [(str() as field, str() | int() | bool() as expected)]:
            return FieldEquals(field=field, value=expected)
    raise HookDefinitionError(f"{where}: expected a predicate name or one field")


def _parse_delivery(
    table: Mapping[str, object],
    events: tuple[str, ...],
    names: _Vocabulary,
    where: str,
) -> Delivery:
    _reject_unknown_keys(
        table,
        {"immediate", "max_wait", "collapse", "min_interval"},
        where=f"{where} deliver",
    )
    immediate = _event_names(
        table.get("immediate", []), names, where=f"{where} immediate"
    )
    if not set(immediate) <= set(events):
        raise HookDefinitionError(f"{where}: immediate events must be among on")
    max_wait = (
        None
        if "max_wait" not in table
        else _duration(table["max_wait"], where=f"{where} max_wait")
    )
    if not immediate and max_wait is None:
        raise HookDefinitionError(f"{where}: needs immediate events or a max_wait")
    collapse: Collapse
    match table.get("collapse"):
        case "all" | "latest" as value:
            collapse = value
        case _:
            raise HookDefinitionError(f"{where}: collapse must be all or latest")
    min_interval = None
    if "min_interval" in table:
        raw = _table(table["min_interval"], where=f"{where} min_interval")
        _reject_unknown_keys(raw, {"after", "duration"}, where=f"{where} min_interval")
        (after,) = _event_names([raw.get("after")], names, where=f"{where} after")
        if after in events:
            raise HookDefinitionError(f"{where}: min_interval after must not be in on")
        min_interval = MinInterval(
            after=after, duration=_duration(raw.get("duration"), where=where)
        )
    return Delivery(
        immediate=frozenset(immediate),
        max_wait=max_wait,
        collapse=collapse,
        min_interval=min_interval,
    )


def _event_names(value: object, names: _Vocabulary, *, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise HookDefinitionError(f"{where}: expected a list of event names")
    unknown = sorted(set(value) - names.events)
    if unknown or len(value) != len(set(value)):
        raise HookDefinitionError(f"{where}: unknown or repeated events {value}")
    return tuple(value)


def _parse_time_trigger(table: Mapping[str, object], where: str) -> At | Every:
    if "at" in table:
        _reject_unknown_keys(table, {"at"}, where=f"{where} trigger")
        at = table["at"]
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise HookDefinitionError(f"{where}: at must be a datetime with an offset")
        return At(at=at)
    if "every" in table:
        return _parse_every(table, where)
    raise HookDefinitionError(f"{where}: trigger needs at, every or on")


def _parse_every(table: Mapping[str, object], where: str) -> Every:
    _reject_unknown_keys(
        table, {"every", "align", "offset", "tz"}, where=f"{where} trigger"
    )
    tz: Zone
    match table.get("tz"):
        case "utc" | "local" as zone:
            tz = zone
        case _:
            raise HookDefinitionError(f"{where}: tz must be utc or local")
    offset = _duration(table.get("offset", "0s"), where=f"{where} offset")
    every = _string(table.get("every"), where=f"{where} every")
    for unit in _CALENDAR_UNITS:
        if every == unit:
            if "align" in table:
                raise HookDefinitionError(
                    f"{where}: a calendar unit always fires on its boundaries"
                )
            if offset >= _SHORTEST_CALENDAR_PERIOD[unit]:
                raise HookDefinitionError(
                    f"{where}: offset must be shorter than {unit}"
                )
            return Every(every=unit, align=True, offset=offset, tz=tz)
    interval = _duration(every, where=f"{where} every")
    align = table.get("align")
    if not isinstance(align, bool):
        raise HookDefinitionError(f"{where}: a duration needs align = true or false")
    if interval <= timedelta(0):
        raise HookDefinitionError(f"{where}: every must be positive")
    if align and _DAY % interval:
        raise HookDefinitionError(f"{where}: an aligned every must divide a day")
    if (align and offset >= interval) or (not align and offset):
        raise HookDefinitionError(
            f"{where}: offset must be shorter than an aligned every"
        )
    return Every(every=interval, align=align, offset=offset, tz=tz)


def _parse_run(table: Mapping[str, object], where: str) -> RunJob:
    _reject_unknown_keys(table, {"job", "args"}, where=f"{where} run")
    args = _table(table.get("args", {}), where=f"{where} args")
    if not all(isinstance(value, str) for value in args.values()):
        raise HookDefinitionError(f"{where}: args values must be strings")
    return RunJob(
        job=_string(table.get("job"), where=f"{where} job"),
        args={key: str(value) for key, value in args.items()},
    )


def _duration(value: object, *, where: str) -> timedelta:
    match = _DURATION_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise HookDefinitionError(f"{where}: expected a duration like 15m")
    return int(match[1]) * _DURATION_UNITS[match[2]]


def _string(value: object, *, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise HookDefinitionError(f"{where}: expected a non-empty string")
    return value


def _table(value: object, *, where: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise HookDefinitionError(f"{where}: expected a table")
    return value


def _reject_unknown_keys(
    table: Mapping[str, object], allowed: set[str], *, where: str
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise HookDefinitionError(f"{where}: unknown keys {unknown}")
