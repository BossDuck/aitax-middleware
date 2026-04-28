import logging

from fastapi import FastAPI

from app.config import settings
from app.webhooks.router import router as webhooks_router


logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


app = FastAPI(
    title="aitax-webhooks",
    description="Microservicio que recibe webhooks de Syntage y sincroniza con AITAX",
    version="0.1.0",
)


app.include_router(webhooks_router)


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "environment": settings.ENVIRONMENT,
        "version": app.version,
    }


@app.get("/")
def root():
    return {"service": "aitax-webhooks", "docs": "/docs"}