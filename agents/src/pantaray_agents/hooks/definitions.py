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


Trigger = At | Every


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


def load_hook_definitions(
    text: str,
    *,
    gate_names: frozenset[str],
    validate_run: Callable[[RunJob], None],
) -> tuple[HookDefinition, ...]:
    """Parse a TOML document of ``[[hooks]]`` tables.

    ``validate_run`` raises ``HookDefinitionError`` for a run the caller cannot
    start, so a bad definition fails here rather than on every tick.
    """
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise HookDefinitionError(f"hook definitions are not TOML: {error}") from error
    _reject_unknown_keys(document, {"hooks"}, where="document")
    tables = document.get("hooks")
    if not isinstance(tables, list):
        raise HookDefinitionError("hook definitions need a [[hooks]] array")
    hooks = tuple(
        _parse_hook(_table(raw, where="hooks"), gate_names, validate_run)
        for raw in tables
    )
    ids = [hook.id for hook in hooks]
    if len(ids) != len(set(ids)):
        raise HookDefinitionError(f"hook ids are not unique: {ids}")
    return hooks


def _parse_hook(
    table: Mapping[str, object],
    gate_names: frozenset[str],
    validate_run: Callable[[RunJob], None],
) -> HookDefinition:
    _reject_unknown_keys(table, {"id", "trigger", "requires", "run"}, where="hook")
    hook_id = _string(table.get("id"), where="hook id")
    where = f"hook {hook_id}"
    requires = table.get("requires", [])
    if not isinstance(requires, list) or not all(
        isinstance(name, str) for name in requires
    ):
        raise HookDefinitionError(f"{where}: requires must be a list of gate names")
    unknown_gates = sorted(set(requires) - gate_names)
    if unknown_gates:
        raise HookDefinitionError(f"{where}: unknown gates {unknown_gates}")
    run = _parse_run(_table(table.get("run"), where=f"{where} run"), where)
    validate_run(run)
    return HookDefinition(
        id=hook_id,
        trigger=_parse_trigger(
            _table(table.get("trigger"), where=f"{where} trigger"), where
        ),
        requires=tuple(requires),
        run=run,
    )


def _parse_trigger(table: Mapping[str, object], where: str) -> Trigger:
    if "at" in table:
        _reject_unknown_keys(table, {"at"}, where=f"{where} trigger")
        at = table["at"]
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise HookDefinitionError(f"{where}: at must be a datetime with an offset")
        return At(at=at)
    if "every" in table:
        return _parse_every(table, where)
    raise HookDefinitionError(f"{where}: trigger needs at or every")


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
