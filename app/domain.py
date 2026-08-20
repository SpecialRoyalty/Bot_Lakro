from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


def parse_hhmm(value: str) -> time:
    try:
        hour_text, minute_text = value.split(":", maxsplit=1)
        return time(hour=int(hour_text), minute=int(minute_text))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Heure invalide : {value!r}. Format attendu : HH:MM.") from exc


def format_hhmm(value: time) -> str:
    return f"{value.hour:02d} h {value.minute:02d}"


def _at(day: date, value: time, now: datetime) -> datetime:
    return datetime.combine(day, value, tzinfo=now.tzinfo)


@dataclass(frozen=True, slots=True)
class DailySchedule:
    opens_at: time
    closes_at: time

    def __post_init__(self) -> None:
        if self.opens_at == self.closes_at:
            raise ValueError("L'heure d'ouverture doit être différente de l'heure de fermeture.")

    @property
    def crosses_midnight(self) -> bool:
        return self.opens_at > self.closes_at

    def is_open(self, now: datetime) -> bool:
        current = now.timetz().replace(tzinfo=None)
        if self.crosses_midnight:
            return current >= self.opens_at or current < self.closes_at
        return self.opens_at <= current < self.closes_at

    def next_open(self, now: datetime) -> datetime:
        candidate = _at(now.date(), self.opens_at, now)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    def session_start(self, now: datetime) -> datetime | None:
        if not self.is_open(now):
            return None
        day = now.date()
        current = now.timetz().replace(tzinfo=None)
        if self.crosses_midnight and current < self.closes_at:
            day -= timedelta(days=1)
        return _at(day, self.opens_at, now)

    def session_end(self, now: datetime) -> datetime | None:
        start = self.session_start(now)
        if start is None:
            return None
        close_day = start.date() + (timedelta(days=1) if self.crosses_midnight else timedelta())
        return _at(close_day, self.closes_at, now)

    def duration(self, now: datetime) -> timedelta:
        base = _at(now.date(), self.opens_at, now)
        close_day = now.date() + (timedelta(days=1) if self.crosses_midnight else timedelta())
        return _at(close_day, self.closes_at, now) - base

    def session_key(self, now: datetime) -> str | None:
        start = self.session_start(now)
        return start.isoformat() if start else None


def is_effectively_open(auto_open: bool, schedule: DailySchedule, now: datetime) -> bool:
    return auto_open and schedule.is_open(now)


def format_duration(delta: timedelta) -> str:
    total_minutes = max(1, math.ceil(delta.total_seconds() / 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours} h {minutes:02d} min"
    if hours:
        return f"{hours} h"
    return f"{minutes} min"


def countdown_slot(now: datetime, next_open: datetime, *, force: bool = False) -> str | None:
    remaining_minutes = math.ceil((next_open - now).total_seconds() / 60)
    if remaining_minutes <= 0:
        return None

    if remaining_minutes <= 60:
        if not force and now.minute % 15 != 0:
            return None
        rounded_minute = (now.minute // 15) * 15
        slot = now.replace(minute=rounded_minute, second=0, microsecond=0)
        return f"quarter:{slot.isoformat()}"

    if not force and now.minute != 0:
        return None
    slot = now.replace(minute=0, second=0, microsecond=0)
    return f"hour:{slot.isoformat()}"


def closing_warning_threshold(now: datetime, end: datetime) -> int | None:
    remaining = math.ceil((end - now).total_seconds() / 60)
    if remaining <= 0:
        return None
    eligible = [threshold for threshold in (5, 15, 30) if remaining <= threshold]
    return min(eligible) if eligible else None


def current_rules_slot(now: datetime, schedule: DailySchedule) -> int | None:
    start = schedule.session_start(now)
    if start is None:
        return None
    duration_seconds = schedule.duration(now).total_seconds()
    elapsed_seconds = max(0.0, (now - start).total_seconds())
    offsets = (0.0, duration_seconds / 3, 2 * duration_seconds / 3)
    due = [index for index, offset in enumerate(offsets, start=1) if elapsed_seconds >= offset]
    return max(due) if due else None
