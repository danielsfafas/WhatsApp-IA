"""
Tras una respuesta de recepción: intenta detectar cita concreta (nombre + fecha + hora)
y crear evento en Google Calendar. Fallos aquí no bloquean el mensaje a WhatsApp.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, time, timedelta
from typing import Any

import httpx
import redis.asyncio as redis
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import parser as dateutil_parser

from calendar_availability import (
    parse_stated_appointment_window,
    reference_today_for_prompt,
    reschedule_busy_exclusions_from_text,
    verify_proposed_slot_bookable,
)
from google_calendar_client import (
    calendar_configured,
    delete_calendar_event_async,
    insert_timed_event_async,
    list_timed_events_between_async,
)
from reception import PREFIJO_RECEPCIONISTA

log = logging.getLogger(__name__)

_TZ_NAME = os.getenv("CALENDAR_TIMEZONE", "America/Mexico_City")
_DURATION_MIN = max(5, int(os.getenv("CALENDAR_EVENT_DURATION_MINUTES", "30")))
_LAST_EVENT_KEY_PREFIX = os.getenv("CALENDAR_LAST_EVENT_KEY_PREFIX", "wa:calevt:last:")
_DEDUP_TTL = int(os.getenv("CALENDAR_DEDUP_TTL_SEC", "604800"))
_LAST_EVENT_TTL = int(os.getenv("CALENDAR_LAST_EVENT_TTL_SEC", str(_DEDUP_TTL)))


def _truthy_create(v: Any) -> bool:
    if v is True:
        return True
    if isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"):
        return True
    return False


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(_TZ_NAME)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _strip_prefijo(s: str) -> str:
    p = PREFIJO_RECEPCIONISTA
    t = (s or "").strip()
    return t[len(p) :].strip() if t.startswith(p) else t


def _assistant_blocks_calendar_write(assistant_reply: str) -> bool:
    """
    Evita insertar en Google cuando la recepción solo *propone* o *pide confirmación*,
    pero el modelo dijo create=true al ver intención previa del paciente.
    """
    t = _strip_prefijo(assistant_reply).strip()
    if not t:
        return True
    low = t.lower()
    if (
        "registrada" in low
        or "quedó agendada" in low
        or "quedo agendada" in low
        or "anotada para" in low
        or "quedó anotada" in low
        or "quedo anotada" in low
        or "confirmada para el personal" in low
        or "cita está registrada" in low
        or "cita quedó registrada" in low
        or ("tu cita" in low and ("registrada" in low or "agendada" in low))
    ):
        return False
    if "¿" in t or "?" in t:
        if any(
            tr in low
            for tr in (
                "te gustaría",
                "te gustaria",
                "quieres que",
                "deseas que",
                "confirmas",
                "confirmarme",
                "te parece",
                "procedo",
                "lo hago",
                "lo agendo",
                "puedo reagendarla",
                "puedo reagendar",
                "quieres que lo",
                "deseas que lo",
                "indícame",
                "indicame",
                "prefieres",
            )
        ):
            return True
    if (
        "registrada" not in low
        and "no puedo" not in low
        and ("puedo reagendarla" in low or "puedo reagendarlo" in low)
    ):
        return True
    return False


def format_thread_for_extract(
    prior_turns: list[dict[str, Any]],
    last_user: str,
    assistant_reply: str,
) -> str:
    lines: list[str] = []
    for m in prior_turns:
        role = m.get("role")
        c = m.get("content", "")
        if role == "user":
            lines.append(f"Paciente: {c}")
        elif role == "assistant":
            lines.append(f"Recepción: {c}")
    lines.append(f"Paciente: {(last_user or '').strip()}")
    lines.append(f"Recepción: {_strip_prefijo(assistant_reply)}")
    return "\n".join(lines)


_EXTRACTION_SYSTEM = """Eres un extractor para Google Calendar (clínica en México, zona horaria de referencia en el mensaje usuario).

Te pasan la conversación Paciente / Recepción. Tu trabajo es decidir si debe crearse UN evento en calendario.

Responde SOLO un JSON válido (sin markdown):
{
  "create": true o false,
  "summary": "título corto, ej. Cita — Nombre — servicio",
  "description": "detalle opcional",
  "start_local": "YYYY-MM-DDTHH:MM:00",
  "end_local": "YYYY-MM-DDTHH:MM:00"
}

Cuándo create DEBE ser true (muy importante):
- El último mensaje de Recepción indica que la solicitud/cita quedó *registrada*, *anotada*, *confirmada para el personal*, *te confirmarán*, o equivalente, Y
- En el hilo hay *nombre completo del paciente*: al menos dos tokens de nombre propio distintos (nombre + apellido), ej. "María González". NO basta un solo nombre ("Daniel", "Laura") salvo que en el mismo mensaje vengan apellido y nombre explícitos.
- Hay día concreto (ej. 10 de abril) y hora concreta (ej. 10 am, 10:00).
- La recepción *no* está solo ofreciendo alternativas, pidiendo aclaración de fecha, ni diciendo que el horario está ocupado o inválido.
- La recepción *no* está solo *preguntando* si el paciente desea el cambio («¿te gustaría…?», «¿confirmas?», «¿procedo?»): en ese caso create es **false** aunque el paciente ya haya dicho que quiere otro horario en mensajes anteriores.

Cuándo create es false:
- Solo un nombre de pila o apodo sin apellido.
- La recepción solo pide más datos (p. ej. nombre completo) sin cerrar.
- No hay hora clara o no hay día concreto.
- Solo intención de agendar sin fecha/hora.

Formato de fechas:
- Usa la fecha de referencia “hoy” del usuario del mensaje para el AÑO: si dice “10 de abril” y abril aún no pasó respecto a hoy, usa el año de “hoy”; si ya pasó abril este año, usa año siguiente.
- start_local y end_local en hora local (sin Z), segundos :00.
- Si falta end_local pero create=true, puedes poner end_local una hora después de start_local (el sistema también puede completar duración).

summary debe incluir nombre del paciente y tipo de servicio si aparecen en el hilo.
"""


async def _openai_extract_json(user_content: str) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY requerida para extracción de cita")

    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("OPENAI_CALENDAR_MODEL", "").strip() or os.getenv(
        "OPENAI_MODEL", "gpt-4o-mini"
    )
    timeout = float(os.getenv("OPENAI_TIMEOUT_SEC", "45"))
    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    ref = reference_today_for_prompt()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _EXTRACTION_SYSTEM},
            {
                "role": "user",
                "content": f"{ref} (zona calendario: {_TZ_NAME})\n\n---\n{user_content}",
            },
        ],
        "max_tokens": 400,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, headers=headers, json=payload)
    r.raise_for_status()
    data = r.json()
    raw = data["choices"][0]["message"]["content"]
    return json.loads(raw)


def _parse_local_dt(s: str) -> datetime:
    raw = (s or "").strip().replace("Z", "")
    if "T" not in raw:
        raise ValueError(f"Se esperaba fecha-hora ISO: {s!r}")
    base = raw[:19] if len(raw) >= 19 else raw
    dt = datetime.fromisoformat(base)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz())
    return dt


def _dedup_key(jid: str, summary: str, start_local: str) -> str:
    h = hashlib.sha256(f"{summary}|{start_local}".encode()).hexdigest()[:20]
    return f"wa:calevt:{jid}:{h}"


def _last_event_redis_key(jid: str) -> str:
    return _LAST_EVENT_KEY_PREFIX + jid


async def _load_last_calendar_event(
    redis_client: redis.Redis, chat_jid: str
) -> dict[str, Any] | None:
    raw = await redis_client.get(_last_event_redis_key(chat_jid))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Calendar: payload inválido en último evento Redis")
        return None
    return data if isinstance(data, dict) else None


async def _save_last_calendar_event(
    redis_client: redis.Redis,
    chat_jid: str,
    *,
    event_id: str,
    start_local: str,
    end_local: str,
    summary: str,
) -> None:
    payload = {
        "id": event_id,
        "start_local": start_local,
        "end_local": end_local,
        "summary": summary[:400],
    }
    await redis_client.set(
        _last_event_redis_key(chat_jid),
        json.dumps(payload, ensure_ascii=False),
        ex=_LAST_EVENT_TTL,
    )


def _tracked_event_id(tracked: dict[str, Any] | None) -> str | None:
    if not tracked:
        return None
    eid = tracked.get("id") or tracked.get("event_id")
    if eid is None:
        return None
    s = str(eid).strip()
    return s or None


def _event_item_bounds(item: dict[str, Any]) -> tuple[datetime, datetime] | None:
    st = item.get("start") or {}
    en = item.get("end") or {}
    ds = st.get("dateTime")
    de = en.get("dateTime")
    if not ds or not de:
        return None
    s_raw = str(ds).replace("Z", "+00:00")
    e_raw = str(de).replace("Z", "+00:00")
    return dateutil_parser.isoparse(s_raw), dateutil_parser.isoparse(e_raw)


async def _cancel_prior_reschedule_events(
    chat_jid: str,
    old_window: tuple[datetime, datetime],
    skip_event_id: str | None = None,
) -> bool:
    """Quita eventos de este chat que solapen la ventana. Devuelve True si borró al menos uno."""
    ex0, ex1 = old_window
    tzz = _tz()
    ex0 = ex0.astimezone(tzz)
    ex1 = ex1.astimezone(tzz)
    day_floor = datetime.combine(ex0.date(), time.min, tzinfo=tzz)
    day_ceil = day_floor + timedelta(days=1)
    try:
        items = await list_timed_events_between_async(
            day_floor.isoformat(),
            day_ceil.isoformat(),
        )
    except Exception:
        log.exception("Calendar: no se listaron eventos para limpieza de reagendar")
        return False

    deleted_any = False
    tag = f"Chat JID: {chat_jid}"
    for it in items:
        desc = str(it.get("description", "") or "")
        if tag not in desc and (chat_jid or "") not in desc:
            continue
        eid = it.get("id")
        if not eid:
            continue
        if skip_event_id and str(eid) == str(skip_event_id):
            continue
        bounds = _event_item_bounds(it)
        if not bounds:
            continue
        s, e = bounds
        s = s.astimezone(tzz)
        e = e.astimezone(tzz)
        if s < ex1 and e > ex0:
            try:
                await delete_calendar_event_async(str(eid))
                deleted_any = True
                log.info(
                    "Calendar: evento previo eliminado tras reagendar (id=%s)",
                    str(eid)[:24],
                )
            except Exception:
                log.exception(
                    "Calendar: falló eliminar evento previo %s", str(eid)[:24]
                )
    return deleted_any


def _thread_suggests_cancellation(thread: str) -> bool:
    low = (thread or "").lower()
    return any(
        x in low
        for x in (
            "cancelar",
            "cancelación",
            "cancelacion",
            "anular la cita",
            "anular cita",
            "dar de baja",
            "baja de cita",
            "desistir de la cita",
        )
    )


def _assistant_confirmed_cancellation(assistant_reply: str) -> bool:
    t = _strip_prefijo(assistant_reply).lower()
    return any(
        p in t
        for p in (
            "ha sido cancelada",
            "ha sido cancelado",
            "cita ha sido cancelada",
            "cita fue cancelada",
            "quedó cancelada",
            "quedo cancelada",
            "cancelé tu cita",
            "cancele tu cita",
            "he cancelado",
            "he cancelado la cita",
            "eliminé tu cita",
            "elimine tu cita",
            "borré tu cita",
            "borre tu cita",
            "anulé tu cita",
            "anule tu cita",
            "cita cancelada",
            "ya cancelé",
            "ya cancele",
            "la cita ha sido cancelada",
        )
    )


def _tracked_matches_stated_window(
    tracked: dict[str, Any] | None, slot: tuple[datetime, datetime]
) -> bool:
    if not tracked:
        return False
    sl = tracked.get("start_local")
    if not sl:
        return False
    try:
        dt = _parse_local_dt(str(sl))
    except ValueError:
        return False
    tzz = _tz()
    dt = dt.astimezone(tzz)
    s0 = slot[0].astimezone(tzz)
    return abs((dt - s0).total_seconds()) <= 150


async def try_cancel_event_from_thread(
    redis_client: redis.Redis,
    chat_jid: str,
    prior_turns: list[dict[str, Any]],
    last_user: str,
    assistant_reply: str,
) -> None:
    """
    Si la recepción confirma una cancelación y el hilo pidió cancelar,
    borra el evento en Google Calendar (por id en Redis o por ventana horaria + JID).
    """
    if not calendar_configured():
        return
    thread = format_thread_for_extract(prior_turns, last_user, assistant_reply)
    if len(thread) < 24:
        return
    if not _thread_suggests_cancellation(thread):
        return
    if not _assistant_confirmed_cancellation(assistant_reply):
        log.debug(
            "Calendar: cancelación en chat pero recepción no confirmó borrado explícito"
        )
        return

    slot = parse_stated_appointment_window(thread)
    tracked = await _load_last_calendar_event(redis_client, chat_jid)
    eid = _tracked_event_id(tracked)

    deleted = False
    if eid and (slot is None or _tracked_matches_stated_window(tracked, slot)):
        try:
            await delete_calendar_event_async(eid)
            deleted = True
            log.info(
                "Calendar: cita cancelada (id Redis, fecha coherente o sin fecha parseada) id=%s…",
                eid[:28],
            )
        except Exception:
            log.exception("Calendar: falló borrar por id al cancelar id=%s…", eid[:28])

    if not deleted and slot is not None:
        if await _cancel_prior_reschedule_events(chat_jid, slot, skip_event_id=None):
            deleted = True
            log.info("Calendar: cita cancelada (lista Calendar por ventana + Chat JID)")

    if not deleted and eid and slot is None:
        try:
            await delete_calendar_event_async(eid)
            deleted = True
            log.warning(
                "Calendar: cita cancelada solo por id Redis (no se parseó fecha en el hilo) id=%s…",
                eid[:28],
            )
        except Exception:
            log.exception("Calendar: borrar por id sin fecha en texto falló")

    if deleted:
        try:
            await redis_client.delete(_last_event_redis_key(chat_jid))
        except Exception:
            log.exception("Calendar: no se pudo limpiar último evento en Redis")


async def try_create_event_from_thread(
    redis_client: redis.Redis,
    chat_jid: str,
    prior_turns: list[dict[str, Any]],
    last_user: str,
    assistant_reply: str,
) -> None:
    if not calendar_configured():
        log.debug("Calendar: desactivado o falta JSON/ID (revisa GOOGLE_CALENDAR_* en .env)")
        return
    if not os.getenv("OPENAI_API_KEY", "").strip():
        log.warning("Calendar: OPENAI_API_KEY vacía; no se puede extraer la cita")
        return

    thread = format_thread_for_extract(prior_turns, last_user, assistant_reply)
    if len(thread) < 20:
        log.debug("Calendar: hilo muy corto (%s chars), omitido", len(thread))
        return

    if _assistant_blocks_calendar_write(assistant_reply):
        log.info(
            "Calendar: no se llama al extractor — recepción no cerró la cita (p. ej. pidió confirmación)."
        )
        return

    log.info("Calendar: extrayendo cita (hilo %s chars, jid=%s…)", len(thread), chat_jid[:28])

    try:
        parsed = await _openai_extract_json(thread)
    except Exception:
        log.exception("Calendar: fallo extracción JSON (OpenAI)")
        return

    log.info(
        "Calendar: extractor → create=%r start=%r end=%r summary=%r",
        parsed.get("create"),
        parsed.get("start_local"),
        parsed.get("end_local"),
        (str(parsed.get("summary") or "")[:80] + "…")
        if len(str(parsed.get("summary") or "")) > 80
        else parsed.get("summary"),
    )

    if not _truthy_create(parsed.get("create")):
        log.info("Calendar: no se crea evento (create≠true). Revisa el JSON arriba o el hilo.")
        return

    summary = str(parsed.get("summary") or "Cita WhatsApp").strip()
    description = str(parsed.get("description") or "").strip()
    if chat_jid:
        description = (description + f"\n\nChat JID: {chat_jid}").strip()

    start_s = str(parsed.get("start_local") or "").strip()
    end_s = str(parsed.get("end_local") or "").strip()

    if not start_s:
        log.warning("Calendar: create=true pero sin start_local")
        return

    try:
        start_dt = _parse_local_dt(start_s)
        if end_s:
            end_dt = _parse_local_dt(end_s)
        else:
            end_dt = start_dt + timedelta(minutes=_DURATION_MIN)
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(minutes=_DURATION_MIN)
    except ValueError as e:
        log.warning("Calendar: fechas inválidas: %s", e)
        return

    # Formato local sin offset para la API (timeZone en body)
    fmt = "%Y-%m-%dT%H:%M:%S"
    start_local = start_dt.astimezone(_tz()).strftime(fmt)
    end_local = end_dt.astimezone(_tz()).strftime(fmt)

    excl = reschedule_busy_exclusions_from_text(thread)
    ok_book, vreason = await verify_proposed_slot_bookable(
        start_dt,
        exclude_busy=excl or None,
    )
    if not ok_book:
        log.warning(
            "Calendar: no se inserta evento; hueco no reservable (misma regla que disponibilidad): %s",
            vreason,
        )
        return

    dkey = _dedup_key(chat_jid, summary, start_local)
    if await redis_client.get(dkey):
        log.info("Calendar: evento ya creado (dedup) %s", dkey[:48])
        return

    prev_tracked = await _load_last_calendar_event(redis_client, chat_jid)

    try:
        ev = await insert_timed_event_async(
            summary=summary,
            description=description,
            start_local=start_local,
            end_local=end_local,
            time_zone=_TZ_NAME,
        )
    except Exception:
        log.exception("Calendar: error insertando evento")
        return

    new_id = str(ev.get("id") or "").strip() or "1"
    await redis_client.set(dkey, new_id, ex=_DEDUP_TTL)
    link = ev.get("htmlLink", "")
    log.info("Calendar: evento creado %s | %s", summary[:60], link or "(sin link)")

    deleted_tracked = False
    if excl:
        prev_eid = _tracked_event_id(prev_tracked)
        if prev_eid and prev_eid != new_id:
            try:
                await delete_calendar_event_async(prev_eid)
                deleted_tracked = True
                log.info(
                    "Calendar: cita anterior eliminada (id Redis) id=%s…",
                    prev_eid[:28],
                )
            except Exception:
                log.exception(
                    "Calendar: falló borrar evento por id Redis %s…", prev_eid[:28]
                )
        if not deleted_tracked:
            await _cancel_prior_reschedule_events(
                chat_jid, excl[0], skip_event_id=new_id
            )

    await _save_last_calendar_event(
        redis_client,
        chat_jid,
        event_id=new_id,
        start_local=start_local,
        end_local=end_local,
        summary=summary,
    )
