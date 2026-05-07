# aitax-webhooks

Microservicio que recibe webhooks de Syntage cuando termina una extracción de
facturas, valida la firma, los procesa de forma asíncrona y notifica a AITAX
(la app principal en Django) para que dispare la sincronización de datos.

## Stack

- **FastAPI** (uvicorn) sirve el endpoint de webhooks.
- **Celery + Memurai (Redis para Windows)** procesan los eventos en background.
- **PostgreSQL** persiste el log de eventos recibidos (idempotencia + auditoría).
- **httpx** para llamadas salientes (a Syntage para consultar la extracción y
  a AITAX para notificar el "extraction-completed").

## Por qué un microservicio (en vez de meter esto dentro de AITAX)

Syntage requiere que el endpoint de webhooks responda en menos de ~3 segundos,
si no marca el delivery como fallido. AITAX corre con `runserver` en desarrollo
y workers normales en producción; meter ahí un endpoint que tiene que responder
rápido y luego procesar en background mete complejidad innecesaria al monolito.

Separarlo en un microservicio nos da:
1. Endpoint ligero que responde 200 inmediato y delega al worker.
2. Reintentos en Celery sin acoplar al ciclo de request de Django.
3. Posibilidad de desplegarlo aparte cuando llegue el momento (sin tocar AITAX).

## Flujo end-to-end
Syntage termina la extracción
│
▼
POST https://<host>/webhooks/syntage/   ← endpoint del microservicio
│
├─ Valida firma HMAC-SHA256 (header X-Syntage-Signature)
├─ Guarda el evento en DB (event_id como UNIQUE → idempotencia)
├─ Encola tarea Celery: process_webhook_event(internal_id)
└─ Responde 200 OK rápido (< 1s)
│
▼  (en otro proceso)
Celery worker toma la tarea
│
├─ Lee el evento de la DB
├─ GET https://api.syntage.com/extractions/{id}  ← consulta estado real
├─ Si status != "finished": marca como SKIPPED, termina (no es error)
├─ Si status == "finished":
│     POST http://<aitax>/api/internal/sync/extraction-completed/
│       Authorization: Bearer <INTERNAL_TOKEN>
│       body: { rfc, extraction_id }
│     ▼
│     AITAX corre sync_all() y devuelve resumen
│
└─ Marca evento como PROCESSED

Estados intermedios de Syntage (`running`, `pending`) llegan como
`extraction.updated` y se SKIPean — solo `finished` dispara el sync.

## Convenciones del proyecto

### Estructura de carpetas
- `app/main.py` — entrypoint FastAPI.
- `app/webhooks/router.py` — endpoint POST /webhooks/syntage/.
- `app/tasks.py` — tareas Celery.
- `app/celery_app.py` — configuración Celery.
- `app/syntage_client.py` — cliente HTTP a Syntage.
- `app/aitax_client.py` — cliente HTTP a AITAX.
- `app/models.py` — modelos SQLAlchemy.
- `app/db.py` — sesión de DB.
- `app/config.py` — settings cargadas desde `.env`.

### Idempotencia
**Cada webhook recibido se persiste con `event_id` UNIQUE.** Si Syntage
reenvía el mismo evento (lo hace, los duplicados son normales), se rechaza
silenciosamente con un log "Webhook duplicado recibido". **Nunca cambies
esta lógica sin pensarlo dos veces** — Syntage manda duplicados de verdad.

### Logging
Logs siempre en español, con prefijo del módulo:
- `app.webhooks.router: Webhook guardado (event_id=..., type=...)`
- `app.tasks: Procesando evento ... (intento N)`
- `app.tasks: Extracción no relevante ...: status='running'. Saltando.`

### Manejo de errores
- Si Syntage devuelve 5xx: Celery reintenta con backoff exponencial (max 5 intentos).
- Si AITAX no responde: Celery reintenta igual (es transitorio).
- Si la firma HMAC no coincide: 401 inmediato, NO se guarda el evento.
- Si el JSON del body es inválido: 400 inmediato.

## Integración con AITAX

AITAX expone `POST /api/internal/sync/extraction-completed/` protegido con
token Bearer. El micro lo llama con:

```json
{
  "rfc": "GIN200414CC8",
  "extraction_id": "a1b3414f-..."
}
```

AITAX responde 200 con resumen al terminar `sync_all`. **Es síncrono**: la
llamada puede tardar varios minutos (sync_all corre completo antes de
responder). El cliente HTTP a AITAX usa timeout largo (~30 min) para esto.

> Para feedback en vivo del usuario, AITAX maneja su propia tabla
> `ExtractionSyncStatus` con un endpoint de polling. El microservicio NO
> se ocupa de eso.

## Comandos útiles

Arrancar local (necesita 4 terminales):

```powershell
# 1. ngrok (expone uvicorn al internet para que Syntage llegue)
ngrok http 8001

# 2. uvicorn (FastAPI)
.\venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8001

# 3. Celery worker (en Windows: --pool=solo es obligatorio)
.\venv\Scripts\Activate.ps1
celery -A app.celery_app worker --loglevel=info --pool=solo

# 4. AITAX (la app principal, en otra carpeta)
cd ..\aitax
python manage.py runserver
```

Migraciones (si usas Alembic):

```powershell
alembic revision --autogenerate -m "descripción"
alembic upgrade head
```

## Variables de entorno (.env)

Variables que existen en `.env`:
- `SYNTAGE_API_URL` — sandbox o prod.
- `SYNTAGE_API_KEY` — la del entorno correspondiente.
- `SYNTAGE_WEBHOOK_SIGNING_SECRET` — para validar HMAC. **Distinto entre
  sandbox y prod** — error común usar el de sandbox en prod.
- `AITAX_BASE_URL` — http://localhost:8000 en local.
- `AITAX_INTERNAL_TOKEN` — Bearer token, debe coincidir con el que AITAX
  espera en `apps/sat/internal_api/auth.py`.
- `DATABASE_URL` — Postgres del micro.
- `CELERY_BROKER_URL` — redis://localhost:6379/0.

`.env.example` tiene la plantilla. **Nunca commitear `.env`.**

## Limitaciones conocidas

### ngrok plan free (solo en desarrollo)
- La URL cambia cada vez que reinicias ngrok → hay que actualizar el
  webhook en Syntage cada vez (o usar un webhook URL fijo de pago).
- Latencia variable causa ocasionalmente `ClientDisconnect` de Starlette
  (Syntage corta a los 3s). Syntage lo cuenta como fallo y manda email
  de "alta tasa de error". **No es bug del código**, desaparece al
  desplegar a un servidor real.
- En producción se elimina ngrok completamente: el micro corre en un
  servidor con dominio propio.

### Windows + Celery
- Celery requiere `--pool=solo` en Windows. No usar `prefork` (default),
  rompe en Windows 10+.
- Hay que tener Memurai corriendo como servicio.

## Cosas que NO hacer

- **No proceses el webhook síncronamente** en el endpoint POST. Tiene que
  ser: validar → guardar → encolar → 200 OK. Si procesas inline, Syntage
  da timeout.
- **No confíes en los datos del body del webhook** para tomar decisiones.
  El payload puede traer info estática del momento que se generó. Siempre
  haz GET a `/extractions/{id}` para ver el estado actual.
- **No quites la idempotencia.** Syntage manda duplicados, es por diseño.
- **No mezcles tokens de sandbox y prod.** El `SYNTAGE_WEBHOOK_SIGNING_SECRET`
  en particular: si están cruzados, todos los webhooks fallan validación
  HMAC con un error genérico difícil de debuggear.

## Pendientes / próximos pasos

- [ ] Desplegar a un servidor real (eliminar dependencia de ngrok y de la PC local).
- [ ] Configurar monitoreo de la cola Celery (cuántas tareas pendientes, cuántas fallaron).
- [ ] Considerar replicar la encriptación AES del proyecto JS original
      (`terminal-pesquero-payment-webhook`) si el cliente lo requiere.
      Por ahora se usa solo el token Bearer + URL única.
- [ ] Tarea programada que limpie eventos viejos de la DB (>30 días).

## Documentación adicional

- API de Syntage: <https://docs.syntage.com> (revisar autenticación de webhooks).
- AITAX (lado servidor del integration): ver `apps/sat/internal_api/` en el
  repo de AITAX.