"""
Historial de conversación por chat (JID) en Redis para la IA.
Sin esto cada mensaje va solo y el modelo no recuerda nombre, fecha, etc.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import redis.asyncio as redis

from reception import PREFIJO_RECEPCIONISTA

log = logging.getLogger(__name__)

_CTX_PREFIX = os.getenv("CHAT_MEMORY_KEY_PREFIX", "wa:chatctx:")
_MAX_MESSAGES = max(4, int(os.getenv("CHAT_MEMORY_MAX_MESSAGES", "24")))
_TTL_SEC = int(os.getenv("CHAT_MEMORY_TTL_SEC", str(7 * 24 * 3600)))


def _ctx_key(jid: str) -> str:
    return _CTX_PREFIX + jid


def _strip_prefijo_guardado(full_reply: str) -> str:
    """Guardamos solo el cuerpo, sin prefijo WhatsApp, para ahorrar tokens."""
    p = PREFIJO_RECEPCIONISTA
    t = (full_reply or "").strip()
    if t.startswith(p):
        return t[len(p) :].strip()
    return t


async def load_turns(r: redis.Redis, jid: str) -> list[dict[str, Any]]:
    raw = await r.get(_ctx_key(jid))
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Historial corrupto para %s; reiniciando", jid[:40])
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            out.append({"role": role, "content": content.strip()})
    return out


async def append_turn(
    r: redis.Redis,
    jid: str,
    user_text: str,
    assistant_reply_with_prefijo: str,
) -> None:
    hist = await load_turns(r, jid)
    u = (user_text or "").strip()
    if u:
        hist.append({"role": "user", "content": u})
    asst = _strip_prefijo_guardado(assistant_reply_with_prefijo)
    if asst:
        hist.append({"role": "assistant", "content": asst})
    if len(hist) > _MAX_MESSAGES:
        hist = hist[-_MAX_MESSAGES:]
    key = _ctx_key(jid)
    payload = json.dumps(hist, ensure_ascii=False)
    if _TTL_SEC > 0:
        await r.set(key, payload, ex=_TTL_SEC)
    else:
        await r.set(key, payload)
