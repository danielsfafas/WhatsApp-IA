"""
Antes de que la IA responda: valida día/hora vs horario de clínica y (si hay Calendar) FreeBusy.
El texto se inyecta en el system prompt para que el modelo obedezca y redacte con tacto.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta
from typing import Any

import dateparser
import httpx
from zoneinfo import ZoneInfo

import clinic_schedule as cs
from google_calendar_client import calendar_configured, freebusy_busy_intervals_async

log = logging.getLogger(__name__)

_DURATION_MIN = max(5, int(os.getenv("CALENDAR_EVENT_DURATION_MINUTES", "30")))
_SUGGEST_STEP = max(5, int(os.getenv("CALENDAR_SUGGESTION_STEP_MINUTES", "30")))
_MAX_SUGGEST = max(3, int(os.getenv("CALENDAR_MAX_SUGGESTION_SLOTS", "8")))

_MONTHS_ES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)

_DAY_WORD_TO_IDX: dict[str, int] = {
    "lunes": 0,
    "martes": 1,
    "miércoles": 2,
    "miercoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sábado": 5,
    "sabado": 5,
    "domingo": 6,
}

_OPENAI_PARSE_SYSTEM = """Extraes UNA fecha y hora local de citas en México a partir de mensajes del paciente en español.
Devuelve SOLO JSON: {"start_local": "YYYY-MM-DDTHH:MM:00"} o {"start_local": null} si no hay fecha y hora claras.

Reglas:
- Usa la referencia "HOY" del mensaje del usuario (fecha y hora actuales del sistema) para interpretar "mañana", "pasado mañana" y el año si falta.
- Si el texto dice un día de la semana que NO coincide con la fecha numérica (ej. "viernes 10 de abril 2026" pero el 10/04/2026 no es viernes), **prioriza la fecha numérica** del calendario gregoriano y **ignora** el nombre del día erróneo.
- 10 am → 10:00, 3 pm → 15:00. Segundos :00.
- Si solo hay día sin hora razonable, null.
"""


def reference_today_for_prompt() -> str:
    now = datetime.now(_tz())
    nombres_d = (
        "lunes",
        "martes",
        "miércoles",
        "jueves",
        "viernes",
        "sábado",
        "domingo",
    )
    m_es = _MONTHS_ES[now.month - 1]
    return (
        f"HOY (servidor, {cs.timezone_name()}): {nombres_d[now.weekday()]} "
        f"{now.day} de {m_es} de {now.year}, {now.strftime('%H:%M')} — ISO {now.isoformat()}"
    )


def build_system_clock_appendix() -> str:
    if not calendar_configured():
        return ""
    now = datetime.now(_tz())
    nombres_d = (
        "lunes",
        "martes",
        "miércoles",
        "jueves",
        "viernes",
        "sábado",
        "domingo",
    )
    m_es = _MONTHS_ES[now.month - 1]
    return (
        "[Reloj del sistema — prioridad absoluta; no contradigas esto]\n"
        f"Hoy es *{nombres_d[now.weekday()]} {now.day} de {m_es} de {now.year}*, "
        f"hora *{now.strftime('%H:%M')}* ({cs.timezone_name()}). "
        f"ISO: `{now.isoformat()}`.\n"
        "Úsalo para “mañana”, año y día de la semana real. *No* inventes otra fecha de “hoy”.\n"
        "Si falta un bloque [Disponibilidad verificada] con hora *LIBRE* en esta misma respuesta, "
        "*no digas* que la cita quedó registrada con hora concreta.\n"
    )


def _iso_local_to_dt(s: str) -> datetime | None:
    raw = (s or "").strip().replace("Z", "")
    if "T" not in raw:
        return None
    base = raw[:19] if len(raw) >= 19 else raw
    try:
        dt = datetime.fromisoformat(base)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz())
    return dt.astimezone(_tz())


def _weekday_mismatch_note(blob: str, dt: datetime) -> str:
    low = (blob or "").lower()
    real_names = (
        "lunes",
        "martes",
        "miércoles",
        "jueves",
        "viernes",
        "sábado",
        "domingo",
    )
    real = real_names[dt.weekday()]
    for word, idx in _DAY_WORD_TO_IDX.items():
        if re.search(rf"\b{re.escape(word)}\b", low):
            if dt.weekday() != idx:
                return (
                    f"• El paciente dijo «{word}» pero la fecha interpretada ({dt.strftime('%d/%m/%Y')}) "
                    f"corresponde a *{real}*. Usa la **fecha numérica del calendario** y el día *{real}*; "
                    f"no repitas el nombre de día equivocado del paciente.\n"
                )
            break
    return ""


async def _parse_proposed_start_openai(blob: str) -> datetime | None:
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return None
    if os.getenv("CALENDAR_DATE_PARSE_FALLBACK", "1").lower() in ("0", "false", "no"):
        return None

    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("OPENAI_CALENDAR_DATE_MODEL", "").strip() or os.getenv(
        "OPENAI_MODEL", "gpt-4o-mini"
    )
    timeout = float(os.getenv("OPENAI_TIMEOUT_SEC", "45"))
    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', '').strip()}",
        "Content-Type": "application/json",
    }

    user_msg = (
        f"{reference_today_for_prompt()}\n\n"
        "--- Contexto (recepción + paciente) ---\n"
        f"{blob[:4500]}"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _OPENAI_PARSE_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 120,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"]
        data = json.loads(raw)
        sl = data.get("start_local")
        if not sl:
            return None
        dt = _iso_local_to_dt(str(sl))
        if dt:
            log.info("Calendar parse (OpenAI fallback) → %s", dt.isoformat())
        return dt
    except Exception:
        log.exception("Calendar: fallo parseo de fecha con OpenAI")
        return None


def _blob_suggests_appointment_time(blob: str) -> bool:
    return any(
        _line_might_contain_date_or_time(ln)
        for ln in (blob or "").split("\n")
        if ln.strip()
    )


async def resolve_proposed_slot_start(
    prior_turns: list[dict[str, Any]],
    user_text: str,
) -> datetime | None:
    blob = _collect_calendar_context_blob(prior_turns, user_text)
    dt = _parse_proposed_start(blob)
    if dt is not None:
        return dt
    if not calendar_configured():
        return None
    if not _blob_suggests_appointment_time(blob):
        return None
    return await _parse_proposed_start_openai(blob)


def _tz() -> ZoneInfo:
    return cs.tz()


def _open_close() -> tuple[int, int]:
    return cs.open_close_hours()


def _collect_user_blob(prior_turns: list[dict[str, Any]], user_text: str) -> str:
    parts: list[str] = []
    u = (user_text or "").strip()
    if u:
        parts.append(u)
    for m in reversed(prior_turns or []):
        if m.get("role") != "user":
            continue
        c = (m.get("content") or "").strip()
        if c and c not in parts:
            parts.append(c)
        if len(parts) >= 8:
            break
    return "\n".join(reversed(parts))


def _collect_calendar_context_blob(
    prior_turns: list[dict[str, Any]],
    user_text: str,
    max_assistant: int = 8,
) -> str:
    """
    Incluye últimos mensajes de *recepción* además del paciente, para interpretar
    «sí», «a las 12», «mismo día» y la fecha de la cita previa en reagendamientos.
    """
    ublob = _collect_user_blob(prior_turns, user_text)
    asst: list[str] = []
    for m in reversed(prior_turns or []):
        if m.get("role") != "assistant":
            continue
        c = (m.get("content") or "").strip()
        if c:
            asst.append(f"Recepción: {c}")
        if len(asst) >= max_assistant:
            break
    asst.reverse()
    if not asst:
        return ublob
    return "\n".join(asst) + "\n---\n" + ublob


_PROG_APPT_RE = re.compile(
    r"programad[ao]s?\s+para\s+el\s+"
    r"(?:(?:lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo)\s+)?"
    r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+(\d{4})\s+"
    r"a\s+las\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
    re.I,
)

# «cita agendada para el lunes 13 de abril de 2026 a las 14:00»
_PROG_APPT_AGENDADA = re.compile(
    r"(?:cita\s+)?agendad[ao]s?\s+para\s+el\s+"
    r"(?:(?:lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo)\s+)?"
    r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+(\d{4})\s+"
    r"a\s+las\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
    re.I,
)

# DD/MM/YYYY o DD-MM-YYYY (p. ej. 10/04/2026 … a las 11 am)
_PROG_APPT_NUMERIC_DM = re.compile(
    r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b.{0,200}?"
    r"\ba\s+las\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|horas?|h\.?)?",
    re.I,
)

# ISO YYYY-MM-DD … a las …
_PROG_APPT_NUMERIC_ISO = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})\b.{0,200}?"
    r"\ba\s+las\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|horas?|h\.?)?",
    re.I,
)


def thread_suggests_reschedule(text: str) -> bool:
    low = (text or "").lower()
    needles = (
        "reagendar",
        "reagenda",
        "cambiar la hora",
        "cambio de hora",
        "mover la cita",
        "mover cita",
        "cambiar mi cita",
    )
    return any(n in low for n in needles)


def _month_num_es(name: str) -> int | None:
    n = name.strip().lower()
    for i, m in enumerate(_MONTHS_ES, start=1):
        if m == n:
            return i
    if len(n) < 3:
        return None
    for i, m in enumerate(_MONTHS_ES, start=1):
        if m.startswith(n):
            return i
    return None


def _dt_at_local(d: date, hour: int, minute: int, ampm: str | None) -> datetime:
    h = hour
    ap = (ampm or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    return datetime(d.year, d.month, d.day, h, minute, 0, tzinfo=_tz())


def _exclusion_from_groups_date_and_time(
    d0: date,
    hh_s: str,
    mm_s: str | None,
    ampm: str | None,
) -> tuple[datetime, datetime] | None:
    try:
        hh = int(hh_s)
        mm = int(mm_s) if mm_s else 0
    except ValueError:
        return None
    ap = (ampm or "").lower()
    if "hora" in ap or ap in ("h", "h."):
        ampm_use = None
    else:
        ampm_use = ampm
    start = _dt_at_local(d0, hh, mm, ampm_use)
    end = start + timedelta(minutes=_DURATION_MIN)
    return (start, end)


def _fallback_reception_appointment_line(
    text: str, *, require_reschedule_intent: bool
) -> tuple[datetime, datetime] | None:
    """Último recurso: líneas de recepción con cita/programada/agenda/cancelación y dateparser."""
    if require_reschedule_intent and not thread_suggests_reschedule(text):
        return None
    now_local = datetime.now(_tz())
    reschedule_kw = (
        "programada",
        "cita actual",
        "tu cita",
        "cita está",
        "cita esta",
        "agendada para",
        "cita agendada",
        "está programada",
        "esta programada",
    )
    cancel_kw = ("cancelar", "cancelada", "cancelación", "cancelacion", "anular")
    for line in (text or "").splitlines():
        ln = line.strip()
        low = ln.lower()
        if len(ln) < 22:
            continue
        if "recepción:" not in low and "recepcion:" not in low:
            continue
        if require_reschedule_intent:
            if not any(k in low for k in reschedule_kw):
                continue
        else:
            if not (
                any(k in low for k in reschedule_kw)
                or any(k in low for k in cancel_kw)
            ):
                continue
        p = dateparser.parse(
            ln,
            languages=["es", "en"],
            settings={
                "TIMEZONE": cs.timezone_name(),
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": now_local.replace(tzinfo=None),
            },
        )
        if p is None:
            continue
        if p.tzinfo is None:
            p = p.replace(tzinfo=_tz())
        p = p.astimezone(_tz())
        end = p + timedelta(minutes=_DURATION_MIN)
        log.debug("Cita en texto (dateparser) %s → %s", p, end)
        return (p, end)
    return None


def first_appointment_window_in_text(raw: str) -> tuple[datetime, datetime] | None:
    """Primera ventana de cita detectada por patrones (recepción con fecha explícita)."""
    text = raw or ""
    for pattern in (
        _PROG_APPT_AGENDADA,
        _PROG_APPT_RE,
    ):
        for m in pattern.finditer(text):
            day_i, mon_name, year_s, hh_s, mm_s, ampm = (
                m.group(1),
                m.group(2),
                m.group(3),
                m.group(4),
                m.group(5),
                m.group(6),
            )
            mon = _month_num_es(mon_name)
            if mon is None:
                continue
            try:
                d0 = date(int(year_s), mon, int(day_i))
            except ValueError:
                continue
            pair = _exclusion_from_groups_date_and_time(d0, hh_s, mm_s, ampm)
            if pair:
                return pair

    for m in _PROG_APPT_NUMERIC_DM.finditer(text):
        day_i, mon_i, year_s, hh_s, mm_s, ampm = (
            m.group(1),
            m.group(2),
            m.group(3),
            m.group(4),
            m.group(5),
            m.group(6),
        )
        try:
            d0 = date(int(year_s), int(mon_i), int(day_i))
        except ValueError:
            continue
        pair = _exclusion_from_groups_date_and_time(d0, hh_s, mm_s, ampm)
        if pair:
            return pair

    for m in _PROG_APPT_NUMERIC_ISO.finditer(text):
        year_s, mon_i, day_i, hh_s, mm_s, ampm = (
            m.group(1),
            m.group(2),
            m.group(3),
            m.group(4),
            m.group(5),
            m.group(6),
        )
        try:
            d0 = date(int(year_s), int(mon_i), int(day_i))
        except ValueError:
            continue
        pair = _exclusion_from_groups_date_and_time(d0, hh_s, mm_s, ampm)
        if pair:
            return pair

    return None


def parse_stated_appointment_window(text: str) -> tuple[datetime, datetime] | None:
    """
    Ventana de la cita que la recepción enuncia (p. ej. cancelación confirmada).
    No exige palabras de reagendar.
    """
    w = first_appointment_window_in_text(text or "")
    if w:
        return w
    return _fallback_reception_appointment_line(
        text or "", require_reschedule_intent=False
    )


def parse_reschedule_exclusion_window(text: str) -> tuple[datetime, datetime] | None:
    """
    Si el hilo habla de reagendar y la recepción citó la cita actual con fecha literal,
    devuelve [inicio, fin) de ese evento para excluirlo de FreeBusy al proponer hora nueva.
    """
    if not thread_suggests_reschedule(text):
        return None
    w = first_appointment_window_in_text(text or "")
    if w:
        return w
    return _fallback_reception_appointment_line(text or "", require_reschedule_intent=True)


def reschedule_busy_exclusions_from_text(text: str) -> list[tuple[datetime, datetime]]:
    w = parse_reschedule_exclusion_window(text)
    return [w] if w else []


def _line_might_contain_date_or_time(line: str) -> bool:
    low = line.lower()
    if any(m in low for m in _MONTHS_ES):
        return True
    if re.search(r"\b\d{1,2}\s*(am|pm)\b", low):
        return True
    if re.search(r"\b\d{1,2}:\d{2}\b", low):
        return True
    if "a las" in low and re.search(r"\d", low):
        return True
    if re.search(r"\bel\s+\d{1,2}\b", low):
        return True
    if re.search(r"\b\d{1,2}\s+de\s+", low):
        return True
    dias = (
        "lunes",
        "martes",
        "miércoles",
        "miercoles",
        "jueves",
        "viernes",
        "sábado",
        "sabado",
        "domingo",
    )
    if any(d in low for d in dias):
        return True
    if "mañana" in low or "manana" in low or "pasado mañana" in low or "pasado manana" in low:
        return True
    return False


def _parse_line(line: str) -> datetime | None:
    now_local = datetime.now(_tz())
    p = dateparser.parse(
        line.strip(),
        languages=["es", "en"],
        settings={
            "TIMEZONE": cs.timezone_name(),
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now_local.replace(tzinfo=None),
        },
    )
    if p is None:
        return None
    if p.tzinfo is None:
        p = p.replace(tzinfo=_tz())
    return p.astimezone(_tz())


def _parse_proposed_start(blob: str) -> datetime | None:
    if len(blob.strip()) < 3:
        return None
    lines = [ln.strip() for ln in blob.split("\n") if len(ln.strip()) >= 3]
    # Mensajes recientes primero: suelen traer la hora definitiva
    for line in reversed(lines):
        if not _line_might_contain_date_or_time(line):
            continue
        p = _parse_line(line)
        if p is not None:
            log.debug("Calendar parse: línea=%r → %s", line[:80], p.isoformat())
            return p
    p = _parse_line(blob)
    if p is not None:
        log.debug("Calendar parse: blob completo → %s", p.isoformat())
    return p


def build_closed_day_hint_appendix(
    prior_turns: list[dict[str, Any]],
    user_text: str,
) -> str:
    """
    Si el paciente nombra sábado/domingo (u otro día fuera de calendario de actividad) en texto,
    fuerza instrucciones aunque no haya Google Calendar o falle el parseo.
    """
    blob = _collect_calendar_context_blob(prior_turns, user_text)
    low = blob.lower()
    bad: list[str] = []
    if re.search(r"\b(sábado|sabado)\b", low) and 5 not in cs.CLINIC_WEEKDAYS:
        bad.append("sábado")
    if re.search(r"\bdomingo\b", low) and 6 not in cs.CLINIC_WEEKDAYS:
        bad.append("domingo")
    # Otros días por nombre (lunes…viernes) si no están en CLINIC_WEEKDAYS
    day_map = [
        ("lunes", 0),
        ("martes", 1),
        ("miércoles", 2),
        ("miercoles", 2),
        ("jueves", 3),
        ("viernes", 4),
    ]
    for word, idx in day_map:
        if re.search(rf"\b{re.escape(word)}\b", low) and idx not in cs.CLINIC_WEEKDAYS:
            bad.append(word)
            break
    if not bad:
        return ""
    oh, ch = _open_close()
    return (
        "\n[Día sin actividad — detectado en lo que escribió el paciente]\n"
        f"Menciona: *{', '.join(bad)}*.\n"
        f"En esta clínica *no* hay actividad ese(s) día(s). Días de atención: *{cs.weekdays_human()}*, "
        f"{oh}:00–{ch}:00 ({cs.timezone_name()}).\n"
        "Debes responder con *empatía y respeto*: una disculpa breve, explicar en qué días y horario sí atienden, "
        "e invitar a proponer otra fecha dentro de ese calendario. *No* confirmes cita para el día que pidió.\n"
    )


def _merge_busy_intervals(
    ivs: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    if not ivs:
        return []
    tzz = _tz()
    norm = [(a.astimezone(tzz), b.astimezone(tzz)) for a, b in ivs]
    norm.sort(key=lambda x: x[0])
    out: list[tuple[datetime, datetime]] = [norm[0]]
    for c0, c1 in norm[1:]:
        p0, p1 = out[-1]
        if c0 <= p1:
            out[-1] = (p0, max(p1, c1))
        else:
            out.append((c0, c1))
    return out


def _subtract_one_busy(
    iv: tuple[datetime, datetime], exc: tuple[datetime, datetime]
) -> list[tuple[datetime, datetime]]:
    a0, a1 = iv
    e0, e1 = exc
    if a1 <= e0 or a0 >= e1:
        return [(a0, a1)]
    out: list[tuple[datetime, datetime]] = []
    if a0 < e0:
        out.append((a0, min(a1, e0)))
    if a1 > e1:
        out.append((max(a0, e1), a1))
    return out


def subtract_busy_intervals(
    busy: list[tuple[datetime, datetime]],
    exclude: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Quita ventanas (p. ej. cita propia a reagendar) del mapa de ocupado."""
    cur = list(busy)
    for exc in exclude or []:
        nxt: list[tuple[datetime, datetime]] = []
        for iv in cur:
            nxt.extend(_subtract_one_busy(iv, exc))
        cur = _merge_busy_intervals(nxt)
    return cur


async def fetch_merged_busy_for_date(d: date) -> list[tuple[datetime, datetime]]:
    day_floor = datetime.combine(d, time.min, tzinfo=_tz())
    day_ceil = day_floor + timedelta(days=1)
    raw = await freebusy_busy_intervals_async(
        day_floor.isoformat(),
        day_ceil.isoformat(),
    )
    return _merge_busy_intervals(raw)


async def verify_proposed_slot_bookable(
    slot_start: datetime,
    exclude_busy: list[tuple[datetime, datetime]] | None = None,
) -> tuple[bool, str]:
    slot_start = slot_start.astimezone(_tz())
    slot_end = slot_start + timedelta(minutes=_DURATION_MIN)
    ok, reason = _within_booking_window(slot_start, slot_end)
    if not ok:
        return False, reason
    busy = await fetch_merged_busy_for_date(slot_start.date())
    if exclude_busy:
        busy = subtract_busy_intervals(busy, exclude_busy)
    occ, det = _slot_overlaps_busy(slot_start, slot_end, busy)
    if occ:
        return False, det
    return True, ""


def _within_booking_window(start: datetime, end: datetime) -> tuple[bool, str]:
    """Día hábil, franja apertura–cierre y sin cruzar receso de comida."""
    if start.date() != end.date():
        return False, "la propuesta cruza medianoche; pide un solo día con hora de inicio y duración dentro del mismo día."
    wd = start.weekday()
    if wd not in cs.CLINIC_WEEKDAYS:
        nombre = (
            "lunes",
            "martes",
            "miércoles",
            "jueves",
            "viernes",
            "sábado",
            "domingo",
        )[wd]
        return (
            False,
            f"ese día es *{nombre}* y no hay consultas (solo {cs.weekdays_human()}).",
        )
    oh, ch = _open_close()
    open_m = oh * 60
    close_m = ch * 60
    sm = start.hour * 60 + start.minute
    em = end.hour * 60 + end.minute
    if sm < open_m or em > close_m:
        return (
            False,
            f"la hora queda fuera del horario {oh}:00–{ch}:00 ({cs.timezone_name()}); "
            f"intervalo interpretado {start.strftime('%H:%M')}–{end.strftime('%H:%M')}.",
        )
    lunch = cs.lunch_hours()
    if lunch:
        ls_h, le_h = lunch
        lm0 = ls_h * 60
        lm1 = le_h * 60
        if sm < lm1 and em > lm0:
            return (
                False,
                f"ese horario cruza el *receso de comida* ({ls_h}:00–{le_h}:00); no hay citas en ese tramo.",
            )
    return True, ""


def _slot_overlaps_busy(
    start: datetime,
    end: datetime,
    busy_merged: list[tuple[datetime, datetime]],
) -> tuple[bool, str]:
    tzz = _tz()
    s = start.astimezone(tzz)
    e = end.astimezone(tzz)
    for b0, b1 in busy_merged:
        if s < b1 and e > b0:
            return True, (
                f"coincide con evento ocupado "
                f"{b0.strftime('%d/%m %H:%M')}–{b1.strftime('%H:%M')}"
            )
    return False, ""


def _format_suggestion_list(times: list[datetime]) -> str:
    if not times:
        return "(no quedaron huecos calculados; pide otra fecha o que llamen a la clínica)"
    parts = [t.strftime("%H:%M") for t in times]
    return ", ".join(parts)


def _free_starts_for_day(
    d: datetime.date,
    busy_merged: list[tuple[datetime, datetime]],
) -> list[datetime]:
    if not cs.suggestable_day(d):
        return []
    candidates = cs.iter_slot_starts(d, _DURATION_MIN, _SUGGEST_STEP)
    tzz = _tz()
    out: list[datetime] = []
    for st in candidates:
        en = st + timedelta(minutes=_DURATION_MIN)
        bad = False
        for b0, b1 in busy_merged:
            if st < b1 and en > b0:
                bad = True
                break
        if not bad:
            out.append(st.astimezone(tzz))
        if len(out) >= _MAX_SUGGEST:
            break
    return out


async def build_availability_appendix_for_llm(
    prior_turns: list[dict[str, Any]],
    user_text: str,
) -> str:
    if not calendar_configured():
        return ""

    ctx_blob = _collect_calendar_context_blob(prior_turns, user_text)
    oh, ch = _open_close()
    slot_start = await resolve_proposed_slot_start(prior_turns, user_text)
    if slot_start is None:
        if _blob_suggests_appointment_time(ctx_blob):
            log.info(
                "Disponibilidad: hay indicios de fecha/hora pero no se interpretó; se pide aclaración al modelo."
            )
            return (
                "\n[Disponibilidad — no se interpretó fecha/hora]\n"
                "El paciente parece proponer día u hora pero el sistema *no* obtuvo una fecha/hora fiable.\n"
                "*No* confirmes cita con horario concreto. Pide fecha y hora claras (ej. *10/04/2026 10:00*).\n"
                f"Horario de la clínica: {cs.weekdays_human()} {oh}:00–{ch}:00 ({cs.timezone_name()}).\n"
            )
        log.info("Disponibilidad: sin indicios de fecha/hora en el hilo.")
        return ""

    slot_start = slot_start.astimezone(_tz())
    slot_end = slot_start + timedelta(minutes=_DURATION_MIN)

    ok_h, reason_h = _within_booking_window(slot_start, slot_end)

    busy_merged: list[tuple[datetime, datetime]] = []
    try:
        busy_merged = await fetch_merged_busy_for_date(slot_start.date())
    except Exception:
        log.exception("Calendar FreeBusy falló")
        return (
            "\n[Disponibilidad — error al leer Google Calendar]\n"
            "No se pudo consultar la agenda. No confirmes una hora concreta; pide repetir más tarde o que el personal confirme. "
            f"Horario de la clínica: {cs.weekdays_human()} {oh}:00–{ch}:00 ({cs.timezone_name()}).\n"
        )

    excl = reschedule_busy_exclusions_from_text(ctx_blob)
    busy_for_slots = (
        subtract_busy_intervals(busy_merged, excl) if excl else busy_merged
    )

    log.info(
        "FreeBusy día %s: %s fusionados, para huecos %s (excl. reagendar=%s) (zona %s)",
        slot_start.date(),
        len(busy_merged),
        len(busy_for_slots),
        bool(excl),
        cs.timezone_name(),
    )

    occupied, busy_detail = _slot_overlaps_busy(
        slot_start, slot_end, busy_for_slots
    )

    suggest_times: list[datetime] = []
    if cs.suggestable_day(slot_start.date()):
        suggest_times = _free_starts_for_day(slot_start.date(), busy_for_slots)
    suggest_txt = _format_suggestion_list(suggest_times)

    lunch = cs.lunch_hours()
    lunch_line = ""
    if lunch:
        lunch_line = f" Receso de comida (sin citas): *{lunch[0]}:00–{lunch[1]}:00*."

    human_slot = (
        f"{slot_start.strftime('%d/%m/%Y %H:%M')} → "
        f"{slot_end.strftime('%H:%M')} ({cs.timezone_name()})"
    )

    wm = _weekday_mismatch_note(ctx_blob, slot_start)
    lines = [
        "",
        "[Disponibilidad verificada — obedecer al responder]",
        f"Propuesta interpretada del paciente: *{human_slot}*.",
    ]
    if excl:
        e0, e1 = excl[0]
        lines.append(
            "• *Reagendamiento:* la franja de la *cita que se va a mover* (aprox. "
            f"{e0.strftime('%d/%m/%Y %H:%M')}–{e1.strftime('%H:%M')}) *no* cuenta como "
            "ocupación de otro paciente al evaluar el nuevo horario."
        )
    if wm:
        lines.append(wm.rstrip())
    lines.extend(
        [
            f"Calendario de actividad: *{cs.weekdays_human()}*, *{oh}:00–{ch}:00* ({cs.timezone_name()}).{lunch_line}",
            f"Duración estimada de cita para huecos: *{_DURATION_MIN} min*. Consulta Google Calendar ese día: *{len(busy_merged)}* tramo(s) ocupado(s) fusionado(s).",
        ]
    )

    if not ok_h:
        lines.append(f"• ¿Válido según días, horario y comida? *NO* — {reason_h}")
        lines.append(
            "• Redacta con *tacto*: disculpa breve, explica reglas de horario/comida si aplica, e invita a otra opción válida."
        )
        lines.append("• *No* digas que la cita quedó registrada para esa fecha/hora inválida.")
    else:
        lines.append("• ¿Dentro de días, horario y sin cruzar comida? *SÍ*.")

    if ok_h:
        if occupied:
            lines.append(f"• La hora propuesta en Google Calendar: *OCUPADA* ({busy_detail}).")
            lines.append(
                "• Dile con *educación* que ese horario ya está tomado y que elija *otra hora* el mismo día o en otro día hábil."
            )
        else:
            lines.append("• La hora propuesta en Google Calendar: *LIBRE* (no solapa eventos en ese tramo).")

    if suggest_times:
        lines.append(
            f"• *Horarios libres sugeridos* el {slot_start.strftime('%d/%m/%Y')} "
            f"(cada ~{_SUGGEST_STEP} min, citas ~{_DURATION_MIN} min): {suggest_txt}"
        )
        lines.append(
            "• Puedes ofrecer *uno o varios* de esos horarios al paciente; si la hora pedida está ocupada, orienta hacia la lista."
        )
    elif cs.suggestable_day(slot_start.date()):
        lines.append(
            "• No se encontraron huecos libres calculables ese día (agenda llena o reglas de horario); pide otro día hábil."
        )

    lines.append(
        "Si la hora está OCUPADA o es inválida: no cierres la cita en esa hora; pide nombre completo solo si falta, sin validar lo imposible."
    )

    log.info(
        "Disponibilidad: slot=%s ok_clínica=%s ocupado_agenda=%s sugerencias=%s",
        human_slot,
        ok_h,
        occupied if ok_h else "n/a",
        len(suggest_times),
    )

    return "\n".join(lines) + "\n"
