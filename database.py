import os
from sqlalchemy import create_engine, Column, Integer, Float, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

# 1. Obtener la URL de conexión a la base de datos de Neon desde las variables de entorno
# Si no está definida en el entorno, usa tu cadena de conexión directa
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://neondb_owner:npg_1QpU9XoKqj@ep-weathered-water-a5e01234.us-east-2.aws.neon.tech/neondb?sslmode=require"
)

# Render o Neon a veces entregan la URL iniciando en 'postgres://'
# SQLAlchemy 1.4+ requiere obligatoriamente 'postgresql://'
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# 2. Configurar el motor de conexión (Engine)
# 'pool_pre_ping=True' ayuda a evitar caídas por desconexión en bases de datos serverless como Neon
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300
)

# 3. Crear el generador de sesiones y la clase Base declarativa
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# --- MODELOS ORM (TABLAS DE NEON) ---

class LecturaAgua(Base):
    __tablename__ = "lecturas_agua"

    id = Column(Integer, primary_key=True, index=True)
    identificador = Column(String(50), nullable=True, default="ESP32_Breiner")
    temperatura = Column(Float, nullable=False)
    turbidez = Column(Float, nullable=False)
    estado = Column(String(50), nullable=True, default="Normal")
    # DateTime(timezone=True) genera TIMESTAMPTZ en PostgreSQL para guardar la zona horaria de Colombia
    timestamp = Column(DateTime(timezone=True), index=True)


class Incidente(Base):
    __tablename__ = "incidentes"

    id = Column(Integer, primary_key=True, index=True)
    nivel = Column(String(50), nullable=False)          # ALERTA, CRITICA, etc.
    descripcion = Column(String(255), nullable=False)
    estado = Column(String(50), default="ACTIVO")        # ACTIVO, RESUELTO
    timestamp = Column(DateTime(timezone=True), index=True)


# 4. Crear automáticamente las tablas en Neon si aún no existen
Base.metadata.create_all(bind=engine)


# 5. Dependencia generadora de sesión para FastAPI (usada en main.py)
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
