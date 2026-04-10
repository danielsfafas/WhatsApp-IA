import dotenv from 'dotenv'
import { dirname, join } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))
dotenv.config({ path: join(__dirname, '..', '.env') })
dotenv.config()

/**
 * Puente WhatsApp (Baileys) ↔ Redis.
 * - Escanea el QR con WhatsApp → Dispositivos vinculados (solo pruebas; riesgo de ban).
 * - Mensajes entrantes → lista Redis wa:inbound (JSON).
 * - Respuestas: BLPOP en wa:outbound (conexión Redis aparte) y envía con sendMessage.
 *
 * Variables: REDIS_URL (default redis://127.0.0.1:6379), LOG_LEVEL (p. ej. warn)
 * Si no ves "Encolado inbound": Baileys puede dejar eventos en buffer hasta "ib offline";
 *   forzamos flush a los pocos segundos tras conectar (ver scheduleEventBufferFlush).
 */
import makeWASocket, {
  Browsers,
  DisconnectReason,
  extractMessageContent,
  fetchLatestBaileysVersion,
  isJidNewsletter,
  isLidUser,
  jidNormalizedUser,
  makeCacheableSignalKeyStore,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys'
import { Boom } from '@hapi/boom'
import Redis from 'ioredis'
import pino from 'pino'
import qrcode from 'qrcode-terminal'

const REDIS_URL = process.env.REDIS_URL || 'redis://127.0.0.1:6379'
const INBOUND = 'wa:inbound'
const OUTBOUND = 'wa:outbound'

const logger = pino({ level: process.env.LOG_LEVEL || 'warn' })
const redis = new Redis(REDIS_URL)
/** BLPOP bloquea la conexión; si usas la misma que RPUSH/inbound, ioredis puede encolar comandos y retrasar o bloquear el encolado. */
const redisOutbound = redis.duplicate()
redisOutbound.on('error', (e) => console.error('[Redis] cola outbound:', e.message))
console.log('[Redis] Node usando', REDIS_URL)
redis
  .ping()
  .then((p) => console.log('[Redis] PING →', p))
  .catch((e) => console.error('[Redis] no responde:', e.message))

/** @type {{ current: import('@whiskeysockets/baileys').WASocket | null }} */
const sockRef = { current: null }

let reconnectDelayMs = 3000
const RECONNECT_DELAY_MAX_MS = 60_000
/** Reconexiones seguidas por 440 (otra sesión te desplaza); evita bucle infinito. */
let consecutive440 = 0
const MAX_440_RECONNECTS = parseInt(process.env.WA_MAX_440_RECONNECTS || '5', 10) || 5
const DELAY_MS_ON_440 = 45_000
/** WhatsApp pide reinicio de socket (normal tras emparejar por QR). */
const DELAY_MS_ON_515 = 2000

/** Evita contestar dos veces si Baileys emite el mismo id en notify y append. */
const seenInboundKeys = new Set()

function extractText(msg) {
  const c = extractMessageContent(msg.message)
  if (!c) return ''
  return (
    c.conversation ||
    c.extendedTextMessage?.text ||
    c.imageMessage?.caption ||
    c.videoMessage?.caption ||
    c.buttonsResponseMessage?.selectedButtonId ||
    c.listResponseMessage?.singleSelectReply?.selectedRowId ||
    ''
  )
}

function inboundDedupKey(msg) {
  return `${msg.key.remoteJid ?? ''}|${msg.key.id ?? ''}|${msg.key.participant ?? ''}`
}

/**
 * Chats con identificador local (remoteJid …@lid): hay que responder al mismo @lid;
 * enviar solo a senderPn (@s.whatsapp.net) a menudo no entrega el mensaje en ese hilo.
 */
function jidParaRespuesta(msg) {
  const remote = msg.key?.remoteJid
  if (remote && isLidUser(remote)) return remote
  const pn = msg.key?.senderPn
  if (pn && typeof pn === 'string') return pn
  return remote
}

async function outboundLoop() {
  // eslint-disable-next-line no-constant-condition
  while (true) {
    let remoteJid
    try {
      // FIFO: Python hace RPUSH al final; hay que sacar por la izquierda (BLPOP), no BRPOP.
      const result = await redisOutbound.blpop(OUTBOUND, 0)
      if (!result) continue
      const raw = result[1]
      let payload
      try {
        payload = JSON.parse(raw)
      } catch {
        console.error('JSON inválido en wa:outbound:', raw)
        continue
      }
      const data = payload
      remoteJid = data.remoteJid
      const text = data.text
      if (!remoteJid || !text) continue

      const fallbackPn = typeof data.senderPn === 'string' ? data.senderPn : null
      const preview = String(text).slice(0, 60)

      let waitSock = 0
      while (!sockRef.current) {
        await new Promise((r) => setTimeout(r, 300))
        waitSock += 300
        if (waitSock === 9000) {
          console.warn('[outbound] Sigue sin haber socket WhatsApp conectado (sockRef vacío).')
        }
      }

      console.log(
        '[outbound] Desde Redis →',
        remoteJid,
        fallbackPn && fallbackPn !== remoteJid ? `(fallbackPn ${fallbackPn})` : '',
      )

      const sendOnce = async (jid, tag) => {
        const to = jidNormalizedUser(jid) || jid
        console.log('>>> Enviando WhatsApp', tag, to, '→', preview)
        return sockRef.current.sendMessage(to, { text: String(text) })
      }

      let res
      try {
        res = await sendOnce(remoteJid, '(principal)')
      } catch (e1) {
        const msg1 = e1?.message || e1?.output?.payload?.message || String(e1)
        if (fallbackPn && fallbackPn !== remoteJid) {
          console.warn('[outbound] Fallo envío principal; reintento senderPn. Causa:', msg1)
          res = await sendOnce(fallbackPn, '(senderPn)')
        } else {
          throw e1
        }
      }
      const repliedAt = typeof data.repliedAt === 'string' ? data.repliedAt : null
      const sentAt = new Date().toISOString()
      console.log(
        '<<< Enviado OK, id:',
        res?.key?.id ?? '(sin id)',
        '| contestación (worker):',
        repliedAt ?? '(sin repliedAt)',
        '| enviado (UTC):',
        sentAt,
      )
    } catch (e) {
      const detail = e?.message || e?.output?.payload?.message || String(e)
      console.error('outboundLoop (envío falló):', remoteJid ?? '(sin jid)', detail, e)
      await new Promise((r) => setTimeout(r, 2000))
    }
  }
}

async function pushInbound(obj) {
  await redis.rpush(INBOUND, JSON.stringify(obj))
  const n = await redis.llen(INBOUND)
  console.log('[Redis] wa:inbound +1, elementos en cola:', n)
}

/** Si no llega el paquete ib/offline de WhatsApp, los mensajes quedan en buffer y nunca ves messages.upsert. */
/** Segundos: ignorar solo “append” muy antiguos (historial); no tocar “notify”. */
const APPEND_MAX_AGE_SEC = 86_400

async function handleMessagesUpsert(up) {
  if (up.type !== 'notify' && up.type !== 'append') return

  console.log('[WA] ▶ messages.upsert', up.type, 'cantidad=', up.messages?.length ?? 0)

  const nowSec = Math.floor(Date.now() / 1000)

  for (const msg of up.messages) {
    const jid0 = msg.key.remoteJid
    const text = extractText(msg)
    console.log(
      '[WA]   · revisando msg fromMe=%s jid=%s textLen=%s id=%s senderPn=%s',
      msg.key.fromMe,
      jid0,
      text.length,
      msg.key.id,
      msg.key.senderPn || '-',
    )

    if (msg.key.fromMe) {
      console.log('[WA]   · omitido (es tuyo / fromMe)')
      continue
    }
    const jid = msg.key.remoteJid
    if (!jid || jid.endsWith('@g.us') || jid.endsWith('@broadcast') || isJidNewsletter(jid)) {
      console.log('[WA]   · omitido (grupo/broadcast/newsletter o sin jid):', jid)
      continue
    }

    const ts = Number(msg.messageTimestamp || 0)
    if (up.type === 'append' && ts > 0 && nowSec - ts > APPEND_MAX_AGE_SEC) {
      console.log('[WA]   · omitido (append demasiado antiguo, edad s)', nowSec - ts)
      continue
    }

    if (!text) {
      const keys = msg.message ? Object.keys(msg.message) : []
      const stub = msg.messageStubType ?? ''
      console.warn(
        '[WA IN] Mensaje de OTRO usuario sin texto legible (no podemos contestar).',
        'jid=', jid,
        'id=', msg.key?.id,
        'tipo=', up.type,
        'stub=', stub,
        'keys=', keys.join(',') || '(vacío)',
        'senderPn=', msg.key?.senderPn || '-',
      )
      console.warn(
        '  → Si esto pasa con cada mensaje nuevo: borra baileys_auth_info y vuelve a escanear el QR (sesión Signal corrupta).',
      )
      continue
    }

    const dkey = inboundDedupKey(msg)
    if (seenInboundKeys.has(dkey)) {
      console.log('[WA]   · omitido (duplicado ya procesado):', dkey.slice(0, 80))
      continue
    }
    seenInboundKeys.add(dkey)
    if (seenInboundKeys.size > 4000) {
      const drop = [...seenInboundKeys].slice(0, 2000)
      drop.forEach((k) => seenInboundKeys.delete(k))
    }

    const destino = jidParaRespuesta(msg)
    console.log('[WA]   · → encolando en Redis', INBOUND, 'destino', destino, '«' + text.slice(0, 40) + '»')
    try {
      await pushInbound({
        remoteJid: destino,
        text,
        id: msg.key.id,
        senderPn: msg.key.senderPn || undefined,
      })
      console.log('Encolado inbound', up.type, jid, '→', destino, ':', text.slice(0, 50))
    } catch (e) {
      console.error('pushInbound:', e)
    }
  }
}

function scheduleEventBufferFlush(sock, label = '') {
  const tryFlush = (ms, tag) => {
    setTimeout(() => {
      try {
        const flushFn = sock?.ev?.flush
        const isBuf = sock?.ev?.isBuffering?.()
        if (typeof flushFn === 'function' && isBuf) {
          flushFn.call(sock.ev)
          console.log(`[WA] Flush de buffer de eventos (${tag}, +${ms}ms)${label ? ' ' + label : ''}`)
        }
      } catch (e) {
        console.warn('[WA] flush buffer:', e.message)
      }
    }, ms)
  }
  tryFlush(4000, '4s')
  tryFlush(12000, '12s')
}

async function startSock() {
  const { state, saveCreds } = await useMultiFileAuthState('baileys_auth_info')
  const { version } = await fetchLatestBaileysVersion()

  const sock = makeWASocket({
    version,
    logger,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger),
    },
    browser: Browsers.windows('Chrome'),
    // Evita fallos "bad-request" en fetchProps / executeInitQueries (visto en algunas cuentas/redes).
    // Para un bot de citas suele bastar; si necesitas sync completo de chats, prueba true y actualiza Baileys.
    fireInitQueries: false,
    // Ayuda a que WhatsApp enrute mensajes; si da problemas, pon false.
    markOnlineOnConnect: true,
  })

  sock.ev.on('creds.update', saveCreds)

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update
    if (qr) {
      console.log('Escanea este QR con WhatsApp (Dispositivos vinculados):')
      qrcode.generate(qr, { small: true })
    }
    if (connection === 'open') {
      reconnectDelayMs = 3000
      sockRef.current = sock
      const me = sock.user?.id ?? '(aún sin id)'
      console.log('WhatsApp conectado como', me, '| colas:', INBOUND, OUTBOUND)
      scheduleEventBufferFlush(sock, 'post-open')
    }
    if (connection === 'close') {
      sockRef.current = null
      const err = lastDisconnect?.error
      const statusCode =
        err instanceof Boom ? err.output.statusCode : err?.output?.statusCode
      const errMsg = err instanceof Error ? err.message : String(err)
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut

      console.log('Conexión cerrada', { statusCode, shouldReconnect, mensaje: errMsg })

      if (statusCode === DisconnectReason.restartRequired) {
        console.log(
          '515 “restart required”: WhatsApp cerró el stream para reiniciarlo; suele pasar justo después del QR. Reconectando en unos segundos…',
        )
      }

      if (statusCode === 405) {
        console.error(
          [
            'HTTP/WebSocket 405: el upgrade a wss://web.whatsapp.com fue rechazado.',
            'Suele ser red intermedia (proxy corporativo, antivirus con inspección HTTPS, VPN) o variables HTTP_PROXY/HTTPS_PROXY.',
            'Prueba: desactivar VPN, otro Wi‑Fi (datos móviles), desactivar inspección SSL del antivirus,',
            'o en PowerShell: Remove-Item Env:HTTP_PROXY, Env:HTTPS_PROXY -ErrorAction SilentlyContinue y vuelve a ejecutar npm start.',
          ].join(' '),
        )
      }

      const is440 = statusCode === DisconnectReason.connectionReplaced
      let stopAutoReconnect = false

      if (is440) {
        consecutive440 += 1
        console.error(
          [
            `[440] Otra sesión te desplazó (${consecutive440}/${MAX_440_RECONNECTS}). WhatsApp solo deja un “Web” activo a la vez para esta cuenta.`,
            'Haz ESTO antes de seguir: (1) Cierra TODAS las ventanas de npm/node de este proyecto.',
            '(2) Cierra la pestaña de web.whatsapp.com si está abierta con el mismo número.',
            '(3) En el móvil: Ajustes de WhatsApp → Dispositivos vinculados → desvincula entradas duplicadas o “Chrome/Windows”.',
            '(4) Espera 1 minuto y ejecuta solo UNA vez: npm start.',
            'Si tras limpiar sigue igual, borra la carpeta baileys_auth_info y vuelve a escanear el QR.',
          ].join('\n'),
        )
        if (consecutive440 >= MAX_440_RECONNECTS) {
          stopAutoReconnect = true
          console.error(
            `Se detiene la reconexión automática tras ${MAX_440_RECONNECTS} conflictos 440. Arregla duplicados y vuelve a lanzar npm start (o sube el límite con WA_MAX_440_RECONNECTS).`,
          )
        }
      } else if (statusCode !== undefined) {
        consecutive440 = 0
        reconnectDelayMs = 3000
      }

      const is515 = statusCode === DisconnectReason.restartRequired

      if (shouldReconnect && !stopAutoReconnect) {
        let delay
        if (is440) delay = DELAY_MS_ON_440
        else if (is515) delay = DELAY_MS_ON_515
        else delay = reconnectDelayMs

        if (!is440 && !is515) {
          reconnectDelayMs = Math.min(reconnectDelayMs * 2, RECONNECT_DELAY_MAX_MS)
        }
        console.log(`Reconexión en ${delay / 1000}s…`)
        setTimeout(() => startSock().catch(console.error), delay)
      } else if (!shouldReconnect) {
        console.log('Sesión cerrada (logged out). Borra baileys_auth_info si quieres nuevo QR.')
      } else {
        console.log('Reconexión detenida por conflictos 440. Corrige duplicados y vuelve a ejecutar npm start.')
      }
    }
  })

  // Patrón recomendado por Baileys (además de ev.on): el buffer agrupa eventos en "event".
  sock.ev.process((events) => {
    if (!events['messages.upsert']) return
    handleMessagesUpsert(events['messages.upsert']).catch((e) => {
      console.error('[WA] error en handleMessagesUpsert:', e)
    })
  })
}

outboundLoop().catch((e) => console.error('outbound fatal', e))
startSock().catch((e) => console.error('startSock', e))
