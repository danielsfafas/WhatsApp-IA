"""
Google Calendar API v3 con cuenta de servicio.

Configuración en Google Cloud:
1) Crear proyecto → APIs y servicios → Biblioteca → activar "Google Calendar API".
2) Cuentas de servicio → crear → clave JSON.
3) En Google Calendar (web): Configuración del calendario → Compartir con
   el correo `algo@proyecto.iam.gserviceaccount.com` con permiso "Realizar cambios en eventos".

Variables:
  GOOGLE_CALENDAR_SERVICE_ACCOUNT_FILE  ruta absoluta o relativa al JSON de la cuenta de servicio
  GOOGLE_CALENDAR_ID                    ID del calendario (suele ser el email del calendario compartido)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from dateutil import parser as date_parser
from google.oauth2 import service_account
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

# Raíz del repo (whatsapp-qr-bridge): …/python-worker → parent
_BRIDGE_ROOT = Path(__file__).resolve().parent.parent
_WORKER_DIR = Path(__file__).resolve().parent


def _resolve_sa_path(raw: str) -> str:
    p = raw.strip()
    if os.path.isfile(p):
        return p
    for base in (_BRIDGE_ROOT, _WORKER_DIR):
        cand = base / p
        if cand.is_file():
            return str(cand)
    return p

_SCOPES = ("https://www.googleapis.com/auth/calendar",)


def _credentials():
    path = os.getenv("GOOGLE_CALENDAR_SERVICE_ACCOUNT_FILE", "").strip()
    if not path:
        raise ValueError("Falta GOOGLE_CALENDAR_SERVICE_ACCOUNT_FILE")
    resolved = _resolve_sa_path(path)
    if not os.path.isfile(resolved):
        raise FileNotFoundError(
            f"No existe el JSON de cuenta de servicio: {path} (resuelto: {resolved})"
        )
    return service_account.Credentials.from_service_account_file(
        resolved, scopes=_SCOPES
    )


def _calendar_id() -> str:
    cid = os.getenv("GOOGLE_CALENDAR_ID", "").strip()
    if not cid:
        raise ValueError("Falta GOOGLE_CALENDAR_ID (email o ID del calendario destino)")
    return cid


def insert_timed_event(
    summary: str,
    description: str,
    start_local: str,
    end_local: str,
    time_zone: str,
) -> dict[str, Any]:
    """
    start_local / end_local: 'YYYY-MM-DDTHH:MM:SS' sin offset (zona en time_zone).
    Devuelve el recurso evento de la API (incluye htmlLink).
    """
    creds = _credentials()
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    body: dict[str, Any] = {
        "summary": (summary or "Cita")[:1024],
        "description": (description or "")[:8000],
        "start": {"dateTime": start_local, "timeZone": time_zone},
        "end": {"dateTime": end_local, "timeZone": time_zone},
    }
    cal = _calendar_id()
    try:
        return (
            service.events()
            .insert(calendarId=cal, body=body, sendUpdates="none")
            .execute()
        )
    except Exception as e:
        err = str(e)
        if "403" in err or "Forbidden" in err:
            log.error(
                "Calendar API 403: la cuenta de servicio no puede escribir en %r. "
                "Comprueba compartir el calendario con el client_email del JSON y permiso “cambiar eventos”.",
                cal,
            )
        elif "404" in err or "Not Found" in err:
            log.error(
                "Calendar API 404: calendario %r no encontrado. Revisa GOOGLE_CALENDAR_ID (email del calendario).",
                cal,
            )
        raise


async def insert_timed_event_async(
    summary: str,
    description: str,
    start_local: str,
    end_local: str,
    time_zone: str,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        insert_timed_event, summary, description, start_local, end_local, time_zone
    )


def freebusy_busy_intervals(
    time_min_rfc3339: str, time_max_rfc3339: str
) -> list[tuple[datetime, datetime]]:
    """Intervalos ocupados en el calendario configurado (inicio, fin), datetimes con tz."""
    creds = _credentials()
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    cal = _calendar_id()
    fb = (
        service.freebusy()
        .query(
            body={
                "timeMin": time_min_rfc3339,
                "timeMax": time_max_rfc3339,
                "items": [{"id": cal}],
            }
        )
        .execute()
    )
    busy = fb.get("calendars", {}).get(cal, {}).get("busy", [])
    out: list[tuple[datetime, datetime]] = []
    for b in busy:
        s = date_parser.isoparse(b["start"])
        e = date_parser.isoparse(b["end"])
        out.append((s, e))
    return out


async def freebusy_busy_intervals_async(
    time_min_rfc3339: str, time_max_rfc3339: str
) -> list[tuple[datetime, datetime]]:
    return await asyncio.to_thread(
        freebusy_busy_intervals, time_min_rfc3339, time_max_rfc3339
    )


def list_timed_events_between(
    time_min_rfc3339: str, time_max_rfc3339: str
) -> list[dict[str, Any]]:
    """Eventos con hora concreta en el rango [timeMin, timeMax)."""
    creds = _credentials()
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    cal = _calendar_id()
    out: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "calendarId": cal,
            "timeMin": time_min_rfc3339,
            "timeMax": time_max_rfc3339,
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 250,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        ev = service.events().list(**kwargs).execute()
        for item in ev.get("items", []):
            if item.get("start", {}).get("dateTime") and item.get("end", {}).get(
                "dateTime"
            ):
                out.append(item)
        page_token = ev.get("nextPageToken")
        if not page_token:
            break
    return out


async def list_timed_events_between_async(
    time_min_rfc3339: str, time_max_rfc3339: str
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        list_timed_events_between, time_min_rfc3339, time_max_rfc3339
    )


def delete_calendar_event(event_id: str) -> None:
    creds = _credentials()
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    cal = _calendar_id()
    service.events().delete(
        calendarId=cal, eventId=event_id, sendUpdates="none"
    ).execute()


async def delete_calendar_event_async(event_id: str) -> None:
    return await asyncio.to_thread(delete_calendar_event, event_id)


def calendar_configured() -> bool:
    if os.getenv("GOOGLE_CALENDAR_ENABLED", "").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return False
    p = os.getenv("GOOGLE_CALENDAR_SERVICE_ACCOUNT_FILE", "").strip()
    if not p:
        return False
    if not os.path.isfile(_resolve_sa_path(p)):
        return False
    return bool(os.getenv("GOOGLE_CALENDAR_ID", "").strip())
