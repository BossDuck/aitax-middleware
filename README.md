# aitax-webhooks

Microservicio que recibe webhooks de Syntage y sincroniza datos con AITAX.

## Stack

- Python 3.14+
- FastAPI
- SQLAlchemy + Alembic
- Celery + Redis
- PostgreSQL

## Setup local

1. Clonar el repo y entrar a la carpeta:
```bash
   git clone <repo-url>
   cd aitax-webhooks
```

2. Crear venv:
```bash
   python -m venv venv
   .\venv\Scripts\Activate.ps1   # Windows
```

3. Instalar dependencias:
```bash
   pip install -r requirements.txt
```

4. Copiar `.env.example` a `.env` y llenar valores:
```bash
   copy .env.example .env
```

5. Crear DB local:
```bash
   psql -U postgres -c "CREATE DATABASE aitax_webhooks_local;"
```

## Estado del proyecto

🚧 En desarrollo (Bloque 0 completado)

## Arquitectura

Ver `docs/arquitectura.md` (próximamente).

## Correr el microservicio en local

Necesitas 3 terminales simultáneas:

### Terminal 1: Memurai (Redis)
Memurai corre como servicio de Windows, así que ya está corriendo.
Verifica con:
```powershell
Get-Service Memurai
```

### Terminal 2: FastAPI server
```powershell
.\venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8001
```

### Terminal 3: Celery worker
```powershell
.\venv\Scripts\Activate.ps1
celery -A app.celery_app worker --loglevel=info --pool=solo
```

> En Windows usamos `--pool=solo` porque el pool por defecto (prefork) no
> funciona bien en Windows. En producción Linux usaríamos prefork o gevent.