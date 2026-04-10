"""
Enruta la respuesta: IA (OpenAI / compatible) si está configurada, si no reglas locales.
Con IA, el historial por JID vive en Redis (`chat_memory`).
"""

from __future__ import annotations

import logging
import os

import redis.asyncio as redis

from calendar_availability import (
    build_availability_appendix_for_llm,
    build_closed_day_hint_appendix,
    build_system_clock_appendix,
)
from calendar_booking import try_cancel_event_from_thread, try_create_event_from_thread
from chat_memory import append_turn, load_turns
from reception import recepcionista_reply as rules_reply
from reception_llm import openai_reply_conversation

log = logging.getLogger(__name__)


def use_llm() -> bool:
    if os.getenv("RECEPCIONISTA_USE_AI", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


async def generate_reply(user_text: str, redis_client: redis.Redis, chat_jid: str) -> str:
    if use_llm():
        try:
            prior = await load_turns(redis_client, chat_jid)
            clock = build_system_clock_appendix()
            avail = await build_availability_appendix_for_llm(prior, user_text)
            closed_day = build_closed_day_hint_appendix(prior, user_text)
            appendix = "\n\n".join(
                x.strip() for x in (clock, avail, closed_day) if (x or "").strip()
            )
            reply = await openai_reply_conversation(prior, user_text, appendix)
            await append_turn(redis_client, chat_jid, user_text, reply)
            await try_cancel_event_from_thread(
                redis_client, chat_jid, prior, user_text, reply
            )
            await try_create_event_from_thread(
                redis_client, chat_jid, prior, user_text, reply
            )
            return reply
        except Exception:
            log.exception("IA no disponible o error; respuesta por reglas")
            return rules_reply(user_text)
    return rules_reply(user_text)


def describe_mode() -> str:
    return "openai" if use_llm() else "rules"
