"""
Punto de entrada del microservicio.

Para correrlo:
    uvicorn app.main:app --reload --port 8001
"""

from fastapi import FastAPI

from app.config import settings


app = FastAPI(
    title="aitax-webhooks",
    description="Microservicio que recibe webhooks de Syntage y sincroniza con AITAX",
    version="0.1.0",
)


@app.get("/health")
def health_check():
    """
    Endpoint para verificar que el servicio está vivo.
    Útil para load balancers, monitoring, y para confirmar el deploy.
    """
    return {
        "status": "ok",
        "environment": settings.ENVIRONMENT,
        "version": app.version,
    }


@app.get("/")
def root():
    return {
        "service": "aitax-webhooks",
        "docs": "/docs",
    }