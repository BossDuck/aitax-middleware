"""
Conexión a la DB del microservicio (Postgres).

"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


engine = create_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)


# Factory de sesiones
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


# Base para todos los modelos
class Base(DeclarativeBase):
    pass


# Helper para FastAPI: inyecta una sesión por request
def get_db():
    """
    Dependencia de FastAPI.
    Uso:
        @app.post("/algo")
        def mi_endpoint(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()