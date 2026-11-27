"""Hook definitions: the closed types, parsed from a TOML document of hooks."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping, Sequence
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


MatchOp = Literal["equals", "contains"]


@dataclass(frozen=True, slots=True)
class FieldMatch:
    """The event's text field equals, or contains, one of ``values``.

    Case is ignored (Unicode casefold). A field the event lacks or holds no text
    in never matches.
    """

    field: str
    op: MatchOp
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NamedPredicate:
    """A check of current state the caller supplies by name."""

    name: str


# Field matches hold only all together; a predicate is checked by the caller.
Condition = tuple[FieldMatch, ...] | NamedPredicate


def fields_match(
    matches: Sequence[FieldMatch], payload: Mapping[str, EventValue]
) -> bool:
    """Every match holds on the payload: fields are all-of, values any-of."""
    return all(_field_matches(match, payload.get(match.field)) for match in matches)


def _field_matches(match: FieldMatch, value: EventValue) -> bool:
    if not isinstance(value, str):
        return False
    text = value.casefold()
    if match.op == "equals":
        return any(text == expected.casefold() for expected in match.values)
    return any(expected.casefold() in text for expected in match.values)


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


@dataclass(frozen=True, slots=True)
class RunMessage:
    """Send ``message`` to ``target``, or to a new target on each fire when None.

    What a target is, and how a new one starts, belongs to the caller.
    """

    target: str | None
    message: str


Run = RunJob | RunMessage


@dataclass(frozen=True, slots=True)
class HookDefinition:
    id: str
    trigger: Trigger
    # Every gate must be open for the hook to fire; a closed one makes it wait.
    requires: tuple[str, ...]
    run: Run
    # From this instant on the hook never fires.
    until: datetime | None = None


def hook_has_ended(hook: HookDefinition, now: datetime) -> bool:
    return hook.until is not None and now >= hook.until


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
        table,
        {"id", "trigger", "requires", "if", "deliver", "run", "until"},
        where="hook",
    )
    hook_id = _string(table.get("id"), where="hook id")
    run = _parse_run(
        _table(table.get("run"), where=f"hook {hook_id} run"), f"hook {hook_id}"
    )
    validate_run(run)
    return _hook(hook_id, table, names, run)


def _hook(
    hook_id: str, table: Mapping[str, object], names: _Vocabulary, run: Run
) -> HookDefinition:
    where = f"hook {hook_id}"
    requires = table.get("requires", [])
    if not isinstance(requires, list) or not all(
        isinstance(name, str) for name in requires
    ):
        raise HookDefinitionError(f"{where}: requires must be a list of gate names")
    unknown_gates = sorted(set(requires) - names.gates)
    if unknown_gates:
        raise HookDefinitionError(f"{where}: unknown gates {unknown_gates}")
    until = None
    if "until" in table:
        until = _instant(table["until"], where=f"{where} until")
    trigger_table = _table(table.get("trigger"), where=f"{where} trigger")
    trigger: Trigger
    if "on" in trigger_table:
        trigger = _parse_on(trigger_table, table, names, where)
    else:
        if "if" in table or "deliver" in table:
            raise HookDefinitionError(f"{where}: if and deliver need an on trigger")
        trigger = parse_time_trigger(trigger_table, where)
    return HookDefinition(
        id=hook_id, trigger=trigger, requires=tuple(requires), run=run, until=until
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
    # A table of field = value: each an equals match on one value.
    table = _table(value, where=where)
    if not table:
        raise HookDefinitionError(f"{where}: expected a predicate name or fields")
    return tuple(
        FieldMatch(
            field=field,
            op="equals",
            values=(_string(expected, where=f"{where} {field}"),),
        )
        for field, expected in table.items()
    )


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


def parse_time_trigger(table: Mapping[str, object], where: str) -> At | Every:
    """An at or every trigger table, as a ``[[hooks]]`` trigger is written."""
    if "at" in table:
        _reject_unknown_keys(table, {"at"}, where=f"{where} trigger")
        return At(at=_instant(table["at"], where=f"{where} at"))
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


def _instant(value: object, *, where: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise HookDefinitionError(f"{where}: expected a datetime with an offset")
    return value


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
