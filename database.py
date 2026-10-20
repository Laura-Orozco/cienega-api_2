import os
from sqlalchemy import create_engine, Column, Integer, Float, String, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_Vs2cnIBZMfy6@ep-divine-mode-aw5ymc3n-pooler.c-12.us-east-1.aws.neon.tech/neondb?sslmode=require"
)

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Tabla de Telemetría (Solo tus sensores y estado)
class LecturaAgua(Base):
    __tablename__ = "lecturas_calidad_agua"

    id = Column(Integer, primary_key=True, index=True)
    identificador = Column(String(50), default="ESP32_Breiner")
    temperatura = Column(Float, nullable=False)
    turbidez = Column(Float, nullable=False)
    estado = Column(String(50), default="Normal")
    timestamp = Column(DateTime, default=datetime.utcnow)

# Tabla de Incidentes / Eventos
class Incidente(Base):
    __tablename__ = "incidentes"

    id = Column(Integer, primary_key=True, index=True)
    nivel = Column(String(20), default="INFO")
    descripcion = Column(String(255), nullable=False)
    estado = Column(String(50), default="REGISTRADO")
    timestamp = Column(DateTime, default=datetime.utcnow)

# Crear tablas en Neon
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
