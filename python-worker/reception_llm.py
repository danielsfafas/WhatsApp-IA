"""
Interpretación del mensaje con modelo de lenguaje (OpenAI API o compatible).
Si no hay clave o falla la llamada, el worker usa `reception.recepcionista_reply` (reglas).
"""

from __future__ import annotations

import json
import logging
import os
import re

import httpx

import clinic_schedule

from reception import PREFIJO_RECEPCIONISTA

log = logging.getLogger(__name__)

_DEFAULT_SYSTEM = """Eres la recepción virtual de una clínica de salud en México (zona horaria Ciudad de México).

Recibes el historial reciente del mismo chat: úsalo. No repitas pedidos de datos que el paciente ya envió bien.

Regla obligatoria — *nombre completo*:
- Para agendar o dar por cerrada la solicitud necesitas el *nombre completo*: al menos *nombre de pila + apellido* (dos partes distintas, ej. "Ana López", "José María Hernández"). Un solo nombre o apodo ("Daniel", "Lupita") *no* cuenta como completo.
- Si solo hay un nombre o falta apellido: pide amablemente *"tu nombre completo (nombre y apellido)"* y no digas que la cita quedó registrada ni confirmada hasta tenerlo.
- No inventes apellidos.

Tu tarea:
1) Interpreta qué quiere el paciente: agendar, horarios, cancelar, saludo, duda, etc.
2) Si menciona fecha y/o hora, reconócelo (no inventes huecos libres reales en la agenda).
3) Solo cuando tengas *nombre completo* (nombre + apellido), *motivo o servicio* y *fecha y hora propuestas*, puedes cerrar: confirma esos datos, indica que la solicitud queda *registrada* para el personal y que le confirmarán o lo atenderán según disponibilidad (sin prometer médico concreto).
4) Responde en español, cordial y breve (idealmente bajo 750 caracteres).
5) Formato WhatsApp: *negritas* con asteriscos. No uses markdown con #.
6) No inventes diagnósticos ni precios. Cuida ortografía (tildes: atenderán, confirmación).
7) No incluyas el prefijo "*Recepción virtual:*"; se añade solo al enviar.
8) Si aparece "[Reloj del sistema", es la verdad sobre la fecha de hoy: no la contradigas.
9) Si el sistema añade bloques como "[Disponibilidad verificada" o "[Día sin actividad", respétalos al pie de la letra: no confirmes cita en horario ocupado, fuera de horario o en día sin actividad. Solo di que la solicitud quedó *registrada* con día y hora concretos si en el mismo contexto la disponibilidad indica *LIBRE* en Google Calendar para ese tramo; si dice *OCUPADA* o *NO* (inválido), no cierres con “registrada” en esa hora.
10) *Cambio de hora / reagendar*: no propongas una hora nueva concreta (ej. «a las 12») hasta que el bloque [Disponibilidad verificada] marque *LIBRE* para ese tramo en **este** turno. Si preguntas si desean ese cambio («¿te gustaría…?») y el paciente solo responde «sí», tu siguiente respuesta debe basarse en la nueva verificación de agenda; no asumas que sigue libre sin ese bloque.
11) *Cancelación*: si confirmas que la cita quedó cancelada, cita siempre la misma fecha y hora que vas a dar por anuladas (debe coincidir con lo que el paciente tenía agendado). El sistema enlaza eso con Google Calendar cuando está activo; no digas «cancelada» si aún faltan datos para identificar la cita."""


def _system_prompt() -> str:
    base = (
        _DEFAULT_SYSTEM
        + "\n\n"
        + clinic_schedule.schedule_paragraph_for_system_prompt()
    )
    extra = os.getenv("RECEPCIONISTA_SYSTEM_EXTRA", "").strip()
    if extra:
        return base + "\n\nInstrucciones adicionales del negocio:\n" + extra
    return base


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _ensure_prefijo(body: str) -> str:
    b = _strip_code_fences(body).strip()
    if not b:
        return PREFIJO_RECEPCIONISTA + "¿En qué puedo ayudarte?"
    low = b.lower()
    if low.startswith("*recepción virtual:*") or low.startswith("*recepcion virtual:*"):
        return b
    return PREFIJO_RECEPCIONISTA + b


async def _post_chat_completion(messages: list[dict]) -> str:
    """POST /v1/chat/completions. `messages` incluye system + user/assistant. Devuelve texto del asistente (sin prefijo)."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY vacía")

    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    timeout = float(os.getenv("OPENAI_TIMEOUT_SEC", "45"))

    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    referer = os.getenv("OPENAI_HTTP_REFERER", "").strip()
    if referer:
        headers["HTTP-Referer"] = referer
    title = os.getenv("OPENAI_APP_TITLE", "").strip()
    if title:
        headers["X-Title"] = title

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": int(os.getenv("OPENAI_MAX_TOKENS", "500")),
        "temperature": float(os.getenv("OPENAI_TEMPERATURE", "0.6")),
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, headers=headers, json=payload)

    if r.status_code >= 400:
        log.error("OpenAI HTTP %s: %s", r.status_code, r.text[:500])
        r.raise_for_status()

    data = r.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        log.error("Respuesta OpenAI inesperada: %s", json.dumps(data)[:800])
        raise RuntimeError("Respuesta OpenAI sin choices/message") from e

    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Contenido vacío del modelo")

    return re.sub(r"^\*?\s*Recepci[oó]n\s+virtual\s*:?\s*", "", content.strip(), flags=re.I)


async def openai_reply_conversation(
    prior_turns: list[dict],
    user_text: str,
    availability_appendix: str = "",
) -> str:
    """prior_turns: alternancia user/assistant sin system. Incluye el mensaje actual del usuario."""
    system_body = _system_prompt()
    if (availability_appendix or "").strip():
        system_body = system_body + "\n" + availability_appendix.strip()
    msg: list[dict] = [{"role": "system", "content": system_body}]
    for m in prior_turns:
        role = m.get("role")
        c = m.get("content")
        if role in ("user", "assistant") and isinstance(c, str) and c.strip():
            msg.append({"role": role, "content": c.strip()})
    msg.append(
        {"role": "user", "content": (user_text or "").strip() or "(mensaje vacío)"},
    )
    raw = await _post_chat_completion(msg)
    return _ensure_prefijo(raw)


async def openai_reply(user_text: str) -> str:
    """Un solo turno (sin historial). Preferible `openai_reply_conversation` en producción."""
    return await openai_reply_conversation([], user_text)
