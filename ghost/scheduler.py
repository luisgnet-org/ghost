"""
Schedule parsing and job execution logic for ghost.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TypedDict, Optional


class Job(TypedDict, total=False):
    name: str
    schedule: str
    action: str
    message: str
    enabled: bool


class ScheduleMatch:
    """Result of checking if a job should run."""
    def __init__(self, should_run: bool, next_run: Optional[datetime] = None):
        self.should_run = should_run
        self.next_run = next_run


def parse_time(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' or 'H:MM' into (hour, minute)."""
    parts = time_str.split(':')
    return int(parts[0]), int(parts[1])


def parse_interval(interval_str: str) -> timedelta:
    """Parse interval like '5m', '12h', '30s' into timedelta."""
    match = re.match(r'^(\d+)(s|m|h|d)$', interval_str)
    if not match:
        raise ValueError(f"Invalid interval: {interval_str}")

    value = int(match.group(1))
    unit = match.group(2)

    if unit == 's':
        return timedelta(seconds=value)
    elif unit == 'm':
        return timedelta(minutes=value)
    elif unit == 'h':
        return timedelta(hours=value)
    elif unit == 'd':
        return timedelta(days=value)


WEEKDAYS = {'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
            'friday': 4, 'saturday': 5, 'sunday': 6}


def should_run(job: Job, now: datetime, last_run: Optional[datetime]) -> ScheduleMatch:
    """
    Determine if a job should run based on its schedule.

    Schedule formats:
    - "every 5m" / "every 12h" / "every 30s" - interval from last run
    - "daily 3:00" - every day at specific time
    - "weekdays 6:00" - Mon-Fri at specific time
    - "monday 10:00" - specific day at specific time
    - "on_*" - event-driven (handled by daemon, not scheduler)
    """
    schedule = job.get('schedule', '')

    if not job.get('enabled', True):
        return ScheduleMatch(False)

    if isinstance(schedule, list):
        for part in schedule:
            if part.startswith('on_'):
                continue
            sub_job = dict(job, schedule=part)
            match = should_run(sub_job, now, last_run)
            if match.should_run:
                return match
        return ScheduleMatch(False)

    if schedule.startswith('every '):
        interval_str = schedule[6:].strip()
        interval = parse_interval(interval_str)

        if last_run is None:
            return ScheduleMatch(True)

        if now - last_run >= interval:
            return ScheduleMatch(True)

        return ScheduleMatch(False, last_run + interval)

    if schedule.startswith('daily '):
        time_str = schedule[6:].strip()
        hour, minute = parse_time(time_str)
        target_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if last_run is None or last_run.date() < now.date():
            if now >= target_today:
                return ScheduleMatch(True)

        if now < target_today:
            return ScheduleMatch(False, target_today)
        else:
            return ScheduleMatch(False, target_today + timedelta(days=1))

    if schedule.startswith('weekdays '):
        time_str = schedule[9:].strip()
        hour, minute = parse_time(time_str)

        if now.weekday() > 4:
            return ScheduleMatch(False)

        target_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if last_run is None or last_run.date() < now.date():
            if now >= target_today:
                return ScheduleMatch(True)

        return ScheduleMatch(False, target_today if now < target_today else None)

    for day_name, day_num in WEEKDAYS.items():
        if schedule.lower().startswith(day_name + ' '):
            time_str = schedule[len(day_name) + 1:].strip()
            hour, minute = parse_time(time_str)

            if now.weekday() != day_num:
                return ScheduleMatch(False)

            target_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

            if last_run is None or last_run.date() < now.date():
                if now >= target_today:
                    return ScheduleMatch(True)

            return ScheduleMatch(False, target_today if now < target_today else None)

    if schedule.startswith("on_"):
        return ScheduleMatch(False)

    raise ValueError(f"Unknown schedule format: {schedule}")


def format_next_run(next_run: Optional[datetime], now: datetime) -> str:
    """Format next run time for display."""
    if next_run is None:
        return "unknown"

    delta = next_run - now

    if delta.total_seconds() < 60:
        return f"{int(delta.total_seconds())}s"
    elif delta.total_seconds() < 3600:
        return f"{int(delta.total_seconds() / 60)}m"
    elif delta.total_seconds() < 86400:
        return f"{delta.total_seconds() / 3600:.1f}h"
    else:
        return f"{delta.days}d"
