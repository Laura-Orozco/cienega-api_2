import os
from sqlalchemy import create_engine, Column, Integer, Float, String, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

# 1. Obtener la URL de conexión desde la variable de entorno o usar la URL por defecto
URL_DEFAULT = "postgresql://neondb_owner:npg_Vs2cnIBZMfy6@ep-divine-mode-aw5ymc3n-pooler.c-12.us-east-1.aws.neon.tech/neondb?sslmode=require"
DATABASE_URL = os.getenv("DATABASE_URL", URL_DEFAULT)

# 2. Normalizar el prefijo si Render entrega 'postgres://'
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# 3. Especificar explícitamente el driver psycopg2 para compatibilidad con Linux y Python 3.14
if DATABASE_URL.startswith("postgresql://") and not DATABASE_URL.startswith("postgresql+"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

# 4. Crear el motor de conexión
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ====================================================================
# TABLA: lecturas_calidad_agua (Telemetría directa del ESP32)
# ====================================================================
class LecturaAgua(Base):
    __tablename__ = "lecturas_calidad_agua"

    id = Column(Integer, primary_key=True, index=True)
    identificador = Column(String(50), default="ESP32_Breiner")
    temperatura = Column(Float, nullable=False)
    turbidez = Column(Float, nullable=False)
    estado = Column(String(50), default="Normal")
    timestamp = Column(DateTime, default=datetime.utcnow)

# ====================================================================
# TABLA: incidentes (Alertas y comandos del teclado matricial)
# ====================================================================
class Incidente(Base):
    __tablename__ = "incidentes"

    id = Column(Integer, primary_key=True, index=True)
    nivel = Column(String(20), default="INFO")
    descripcion = Column(String(255), nullable=False)
    estado = Column(String(50), default="REGISTRADO")
    timestamp = Column(DateTime, default=datetime.utcnow)

# Crear las tablas en Neon si no existen
Base.metadata.create_all(bind=engine)

# Dependencia para inyectar la sesión de base de datos en las rutas de FastAPI
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
