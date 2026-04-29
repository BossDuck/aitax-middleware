from celery import Celery

from app.config import settings


celery_app = Celery(
    "aitax_webhooks",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks.process_event"],
)


celery_app.conf.update(
    # Serialización: JSON es más portable y legible que pickle.
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Zona horaria UTC para timestamps consistentes.
    timezone="UTC",
    enable_utc=True,

    # Modo eager para tests (controlado por env var).
    task_always_eager=settings.CELERY_TASK_ALWAYS_EAGER,
    # En modo eager, propagar excepciones para que los tests las capturen.
    task_eager_propagates=settings.CELERY_TASK_ALWAYS_EAGER,

    # Reintentos por defecto para tareas que usen autoretry_for:
    # (la tarea individual también puede sobreescribir esto).
    task_acks_late=True,           # Solo confirmar al broker tras éxito.
    task_reject_on_worker_lost=True,

    # Resultados expiran a la hora (no nos interesa guardarlos mucho tiempo).
    result_expires=3600,
)