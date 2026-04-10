"""Lógica de recepcionista (pruebas). Sustituir por IA + BD cuando toque."""

import re

# Todas las respuestas llevan prefijo para que en WhatsApp se distinga del resto del chat.
_PREFIJO = "*Recepción virtual:* "
# Mismo texto; exportado para la capa IA (`reception_llm`).
PREFIJO_RECEPCIONISTA = _PREFIJO

_MESES = (
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
_DIAS_SEMANA = (
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


def _parece_fecha_u_hora_cita(t: str) -> bool:
    """Detecta propuestas del estilo «el 10 de abril a las 10» sin decir explícitamente «cita»."""
    if any(m in t for m in _MESES):
        return True
    if any(d in t for d in _DIAS_SEMANA):
        return True
    if "mañana" in t or "manana" in t or "pasado mañana" in t or "pasado manana" in t:
        return True
    if re.search(r"\b\d{1,2}\s*(am|pm)\b", t):
        return True
    if re.search(r"\b\d{1,2}:\d{2}\b", t):
        return True
    if "a las" in t and re.search(r"\d", t):
        return True
    if re.search(r"\bel\s+\d{1,2}\b", t):
        return True
    if re.search(r"\b\d{1,2}\s+de\s+", t):
        return True
    return False


def recepcionista_reply(user_text: str) -> str:
    t = (user_text or "").strip().lower()
    if not t:
        return _PREFIJO + (
            "Hola, buen día. ¿En qué puedo ayudarte? "
            "Puedes escribir *agendar cita*, *horarios* o *cancelar*."
        )
    if "hola" in t or "buenos" in t:
        return _PREFIJO + (
            "¡Hola! Gracias por escribirnos. ¿Te gustaría agendar una cita? "
            "Necesito tu *nombre completo* (nombre y apellido), motivo de la consulta "
            "y día con hora (Ciudad de México)."
        )
    if "horario" in t or "horarios" in t:
        return _PREFIJO + (
            "Atendemos de lunes a viernes de 9:00 a 18:00 (hora Ciudad de México). "
            "¿Qué día te conviene?"
        )
    if "agendar" in t or "cita" in t or _parece_fecha_u_hora_cita(t):
        if _parece_fecha_u_hora_cita(t):
            return _PREFIJO + (
                "Perfecto, tomé nota de la fecha y hora que indicas. "
                "Para *confirmar* la cita envíame tu *nombre completo* y un *motivo breve* "
                "de la consulta (si quieres una segunda opción de horario, indícala también)."
            )
        return _PREFIJO + (
            "Con gusto. Envíame: nombre completo, motivo breve de la consulta "
            "y dos opciones de fecha/hora."
        )
    if "cancelar" in t:
        return _PREFIJO + (
            "Para cancelar necesito el nombre de la cita y la fecha. ¿Me los compartes?"
        )
    return _PREFIJO + (
        "Gracias por tu mensaje. Para agendar: *nombre completo* (nombre y apellido), "
        "motivo, día y hora, o escribe *horarios*."
    )
