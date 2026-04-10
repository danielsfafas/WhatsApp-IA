# WhatsApp QR Bridge (Node + Python + Redis)

Puente de WhatsApp basado en QR (Baileys) con un worker en Python para responder como recepcionista virtual y sincronizar agenda con Google Calendar.

## Arquitectura

- `node-bridge`: conecta WhatsApp Web por QR, envía entrantes a Redis (`wa:inbound`) y consume salidas de Redis (`wa:outbound`).
- `python-worker`: consume `wa:inbound`, genera respuesta (reglas o IA), publica en `wa:outbound`.
- `redis`: cola intermedia entre ambos procesos.
- (Opcional) Google Calendar para validar disponibilidad, crear, reagendar y cancelar citas.

## Requisitos

- Node.js 18+ (recomendado 20+)
- Python 3.11+ (en Windows funciona bien con 3.11/3.12)
- Redis 7+
- Cuenta OpenAI (opcional)
- Cuenta de servicio de Google Calendar (opcional)

## Instalacion de Redis

### Opcion A (recomendada): Docker Compose

Desde la raiz del proyecto:

```bash
docker compose up -d
```

Verificar:

```bash
docker compose ps
```

El `docker-compose.yml` ya viene preparado para Redis con persistencia (`redis_data`).

### Opcion B: Redis nativo

Si lo instalas localmente, deja Redis escuchando en `127.0.0.1:6379` o ajusta `REDIS_URL` en `.env`.

Comprobacion rapida:

```bash
redis-cli ping
```

Debe responder `PONG`.

## Configuracion

1. Copia plantilla:

```bash
cp .env.example .env
```

2. Ajusta como minimo:

- `REDIS_URL=redis://127.0.0.1:6379`

3. Si usaras IA:

- `OPENAI_API_KEY`
- opcional `OPENAI_MODEL`, `RECEPCIONISTA_USE_AI=1`

4. Si usaras Google Calendar:

- `GOOGLE_CALENDAR_ENABLED=1`
- `GOOGLE_CALENDAR_SERVICE_ACCOUNT_FILE=google-calendar-sa.json`
- `GOOGLE_CALENDAR_ID=<id o correo del calendario>`
- `CALENDAR_TIMEZONE=America/Mexico_City`

5. Horarios de la clinica (opcional pero recomendado):

- `CLINIC_OPEN_HOUR`, `CLINIC_CLOSE_HOUR`
- `CLINIC_WEEKDAYS`
- `CLINIC_LUNCH_START_HOUR`, `CLINIC_LUNCH_END_HOUR`

## Instalacion de dependencias

### Node bridge

```bash
cd node-bridge
npm install
```

### Python worker

```bash
cd python-worker
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Ejecucion

Abre dos terminales:

### 1) Node bridge

```bash
cd node-bridge
npm start
```

Escanea el QR en WhatsApp (Dispositivos vinculados).

### 2) Python worker

```bash
cd python-worker
.venv\Scripts\activate
python worker_app.py
```

Por defecto levanta FastAPI en `http://localhost:8080`.

Health checks:

- `GET /health`
- `GET /health/queues`

## Flujo de colas Redis

- Entrante: `wa:inbound` (RPUSH desde Node, BLPOP en Python)
- Saliente: `wa:outbound` (RPUSH desde Python, BLPOP en Node)

## Modificaciones recientes realizadas en este proyecto

Se incorporaron mejoras importantes sobre agenda y consistencia conversacional:

1. Validacion de disponibilidad mas estricta:
   - verifica dia habil, horario de clinica y receso de comida
   - consulta FreeBusy de Google Calendar antes de confirmar
2. Reloj del sistema en prompt:
   - evita errores de "hoy", dia de semana y ano
3. Fallback de parseo de fecha/hora:
   - si `dateparser` falla, usa extraccion JSON con modelo para recuperar `start_local`
4. Reagendar robusto:
   - evita insertar evento cuando la recepcion solo pregunta confirmacion
   - excluye temporalmente la cita actual al evaluar hueco nuevo
   - tras confirmar nueva cita, elimina la cita anterior
5. Cancelacion real en Google Calendar:
   - no solo responde "cancelada", tambien intenta borrar evento en Calendar
   - usa `eventId` guardado en Redis por chat y fallback por ventana horaria + `Chat JID`
6. Persistencia operativa:
   - guarda ultimo evento por chat (`CALENDAR_LAST_EVENT_KEY_PREFIX`, `CALENDAR_LAST_EVENT_TTL_SEC`)

## Variables nuevas de agenda (si aplican)

- `CALENDAR_DATE_PARSE_FALLBACK=1`
- `OPENAI_CALENDAR_DATE_MODEL=gpt-4o-mini`
- `CALENDAR_LAST_EVENT_KEY_PREFIX=wa:calevt:last:`
- `CALENDAR_LAST_EVENT_TTL_SEC=604800`

## Notas

- `.env` y `google-calendar-sa.json` estan ignorados por git para evitar fugas de secretos.
- Si `GOOGLE_CALENDAR_ENABLED` esta apagado, el sistema responde sin sincronizar calendario.

