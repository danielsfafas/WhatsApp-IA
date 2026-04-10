/**
 * Prueba solo la cola de SALIDA (sin depender de mensajes entrantes).
 * Con npm start corriendo, esto debe hacer que WhatsApp envíe un texto al JID indicado.
 *
 * Uso:
 *   node probe-outbound.mjs 5211234567890@s.whatsapp.net
 *   node probe-outbound.mjs 5211234567890@s.whatsapp.net "tu texto"
 */
import dotenv from 'dotenv'
import Redis from 'ioredis'
import { dirname, join } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))
dotenv.config({ path: join(__dirname, '..', '.env') })
dotenv.config()

const jid = process.argv[2]
const text = process.argv[3] || 'Prueba probe-outbound: si ves esto, el envío por Redis funciona.'

if (!jid?.includes('@')) {
  console.error('Uso: node probe-outbound.mjs 521XXXXXXXXXX@s.whatsapp.net ["texto opcional"]')
  process.exit(1)
}

const url = process.env.REDIS_URL || 'redis://127.0.0.1:6379'
const r = new Redis(url)
await r.rpush('wa:outbound', JSON.stringify({ remoteJid: jid, text }))
const lo = await r.llen('wa:outbound')
const li = await r.llen('wa:inbound')
console.log('[probe] RPUSH wa:outbound OK | Redis:', url)
console.log('[probe] Longitudes → inbound:', li, 'outbound:', lo)
console.log('[probe] Debe aparecer en la consola de npm start: "Enviado a ..."')
await r.quit()
