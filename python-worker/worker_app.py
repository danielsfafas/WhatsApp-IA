"""
Worker FastAPI: BLPOP wa:inbound → recepcionista_reply → RPUSH wa:outbound.
El proceso Node (Baileys) envía mensajes a wa:inbound y consume wa:outbound.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import redis.asyncio as redis
from dotenv import load_dotenv
from fastapi import FastAPI
from redis.exceptions import ConnectionError as RedisConnectionError

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")
load_dotenv()

from google_calendar_client import calendar_configured

from reception_brain import describe_mode, generate_reply

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379")
INBOUND = "wa:inbound"
OUTBOUND = "wa:outbound"


def _reply_tzinfo():
    """En Windows hace falta el paquete `tzdata` para IANA (p. ej. America/Mexico_City)."""
    name = os.getenv("REPLY_TZ", "America/Mexico_City")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        log.warning(
            "Zona horaria %r no disponible (Windows: pip install tzdata). "
            "Usando UTC para repliedAt.",
            name,
        )
        return timezone.utc


# Hora de la contestación del bot (ISO 8601 con offset cuando hay tzdata).
REPLY_TZ = _reply_tzinfo()

log.info("Modo respuesta recepcionista: %s", describe_mode())

_worker_task: asyncio.Task[None] | None = None


async def queue_loop() -> None:
    """Reintenta hasta que Redis esté arriba; evita spam de tracebacks si el puerto 6379 está cerrado."""
    while True:
        r: redis.Redis | None = None
        try:
            r = redis.from_url(REDIS_URL, decode_responses=True)
            await r.ping()
            log.info("Worker conectado a Redis %s", REDIS_URL)
            while True:
                try:
                    item = await r.blpop(INBOUND, timeout=10)
                    if item is None:
                        continue
                    _, raw = item
                    data = json.loads(raw)
                    jid = data.get("remoteJid")
                    if not jid:
                        log.warning("Payload sin remoteJid: %s", raw[:200])
                        continue
                    text_in = data.get("text") or ""
                    log.info("INBOUND recibido jid=%s texto=%r", jid, text_in[:120])
                    reply = await generate_reply(text_in, r, jid)
                    replied_at = datetime.now(REPLY_TZ).isoformat(timespec="seconds")
                    out_payload: dict[str, str] = {
                        "remoteJid": jid,
                        "text": reply,
                        "repliedAt": replied_at,
                    }
                    if data.get("senderPn"):
                        out_payload["senderPn"] = str(data["senderPn"])
                    out = json.dumps(out_payload, ensure_ascii=False)
                    await r.rpush(OUTBOUND, out)
                    olen = await r.llen(OUTBOUND)
                    log.info(
                        "OUTBOUND +1 para %s repliedAt=%s (preview %r) | len outbound=%s",
                        jid,
                        replied_at,
                        reply[:80],
                        olen,
                    )
                except asyncio.CancelledError:
                    raise
                except RedisConnectionError:
                    log.warning("Se perdió Redis; reconectando…")
                    break
                except Exception:
                    log.exception("Error procesando mensaje de cola")
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            if r is not None:
                await r.aclose()
            raise
        except (RedisConnectionError, OSError) as e:
            log.warning(
                "Redis no disponible en %s (%s). Arranca Redis (desde la carpeta del proyecto: "
                "`docker compose up -d`) o revisa REDIS_URL en .env. Reintento en 3s.",
                REDIS_URL,
                e,
            )
            await asyncio.sleep(3)
        finally:
            if r is not None:
                try:
                    await r.aclose()
                except Exception:
                    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker_task
    _worker_task = asyncio.create_task(queue_loop())
    yield
    if _worker_task:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="WhatsApp QR — worker (FastAPI)", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "inbound": INBOUND,
        "outbound": OUTBOUND,
        "reply_mode": describe_mode(),
        "google_calendar": calendar_configured(),
    }


@app.get("/health/queues")
async def health_queues() -> dict[str, int | str]:
    """Comprueba si Node está metiendo mensajes en Redis (inbound) y respuestas (outbound)."""
    r = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        li = int(await r.llen(INBOUND))
        lo = int(await r.llen(OUTBOUND))
        return {"redis": REDIS_URL, "inbound_len": li, "outbound_len": lo}
    finally:
        await r.aclose()


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("worker_app:app", host="0.0.0.0", port=port, reload=False)
