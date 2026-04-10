"""
Configuración compartida: días, horario de atención y receso de comida (variables de entorno).
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TZ_NAME = os.getenv("CALENDAR_TIMEZONE", "America/Mexico_City")
_OPEN_H = int(os.getenv("CLINIC_OPEN_HOUR", "9"))
_CLOSE_H = int(os.getenv("CLINIC_CLOSE_HOUR", "18"))
_WD_RAW = os.getenv("CLINIC_WEEKDAYS", "0,1,2,3,4")
CLINIC_WEEKDAYS: frozenset[int] = frozenset(
    int(x.strip())
    for x in _WD_RAW.split(",")
    if x.strip().isdigit()
) or frozenset({0, 1, 2, 3, 4})

# Receso de comida (sin citas). Ej.: 14 y 15 = de 14:00 a 15:00. Vacío = sin receso explícito.
_LUNCH_S = os.getenv("CLINIC_LUNCH_START_HOUR", "").strip()
_LUNCH_E = os.getenv("CLINIC_LUNCH_END_HOUR", "").strip()

_WD_NAMES = (
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
)


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(_TZ_NAME)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def weekdays_human() -> str:
    if CLINIC_WEEKDAYS == frozenset({0, 1, 2, 3, 4}):
        return "lunes a viernes"
    return ", ".join(_WD_NAMES[d] for d in sorted(CLINIC_WEEKDAYS))


def open_close_hours() -> tuple[int, int]:
    return _OPEN_H, _CLOSE_H


def timezone_name() -> str:
    return _TZ_NAME


def lunch_hours() -> tuple[int, int] | None:
    """(hora_inicio, hora_fin) en reloj 24h, ej. (14, 15). None si no configurado."""
    if not _LUNCH_S or not _LUNCH_E:
        return None
    try:
        a = int(_LUNCH_S)
        b = int(_LUNCH_E)
    except ValueError:
        return None
    if b <= a or a < 0 or b > 24:
        return None
    return a, b


def schedule_paragraph_for_system_prompt() -> str:
    oh, ch = open_close_hours()
    lunch = lunch_hours()
    lunch_txt = ""
    if lunch:
        ls, le = lunch
        lunch_txt = (
            f" Hay *receso de comida* (no se agendan citas) de *{ls}:00 a {le}:00*."
        )
    return (
        f"Horario y días de actividad de la clínica: *{weekdays_human()}*, "
        f"de {oh}:00 a {ch}:00 (zona {_TZ_NAME}).{lunch_txt} "
        "No confirmes citas fuera de esos días, fuera de la franja de atención ni durante el receso de comida. "
        "Si el paciente propone día sin actividad, responde con *tacto*: disculpa breve, aclara días y horario, invita a otra fecha."
    )


def suggestable_day(d: date) -> bool:
    return d.weekday() in CLINIC_WEEKDAYS


def iter_slot_starts(
    d: date,
    duration_minutes: int,
    step_minutes: int,
) -> list[datetime]:
    """
    Inicios posibles de cita en `d` (zona clínica), respetando apertura, cierre y comida.
    """
    tzz = tz()
    oh, ch = open_close_hours()
    lunch = lunch_hours()
    day_close = datetime.combine(d, time(ch, 0), tzinfo=tzz)
    latest_start = day_close - timedelta(minutes=duration_minutes)
    day_open = datetime.combine(d, time(oh, 0), tzinfo=tzz)
    if latest_start < day_open:
        return []

    out: list[datetime] = []
    step = max(5, step_minutes)

    def walk_segment(first: datetime, last_start: datetime) -> None:
        t = first
        while t <= last_start:
            out.append(t)
            t += timedelta(minutes=step)

    if lunch:
        ls_h, le_h = lunch
        lunch_start = datetime.combine(d, time(ls_h, 0), tzinfo=tzz)
        lunch_end = datetime.combine(d, time(le_h, 0), tzinfo=tzz)
        morning_last = min(lunch_start - timedelta(minutes=duration_minutes), latest_start)
        if day_open <= morning_last:
            walk_segment(day_open, morning_last)
        afternoon_first = lunch_end
        if afternoon_first <= latest_start:
            walk_segment(afternoon_first, latest_start)
    else:
        walk_segment(day_open, latest_start)

    return out
