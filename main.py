import os
from datetime import datetime
from typing import Optional, List
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import desc
import requests
import google.generativeai as genai

# Modelos ORM y conexión de base de datos
from database import get_db, LecturaAgua, Incidente

app = FastAPI(
    title="API Central IoT - Monitoreo Calidad del Agua (Neon DB & ThingSpeak)",
    version="1.0.0"
)

# Configuración de CORS para peticiones desde el dashboard web
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuración de Gemini LLM
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

# Clave de escritura de ThingSpeak (Canal 3540837)
THINGSPEAK_WRITE_KEY = os.getenv("THINGSPEAK_WRITE_KEY", "IWE7C67EMHPZP8C")
THINGSPEAK_UPDATE_URL = "https://api.thingspeak.com/update"

# Configuración interna del sistema
configuracion_sistema = {
    "intervalo_segundos": 15
}

# --- ESQUEMAS PYDANTIC ---

class TelemetriaInput(BaseModel):
    identificador: Optional[str] = "ESP32_Breiner"
    temperatura: float
    turbidez: float
    estado: Optional[str] = "Normal"

class TeclaInput(BaseModel):
    tecla: str

class FrecuenciaInput(BaseModel):
    intervalo_segundos: int

class ConsultaLLMInput(BaseModel):
    pregunta: Optional[str] = None


# --- RUTAS DE LA API ---

@app.get("/")
def raiz():
    return {
        "status": "online",
        "mensaje": "API IoT Calidad del Agua operativa",
        "docs": "/docs"
    }

# 1. Ingesta: Guarda en Neon PostgreSQL y sincroniza desde la BD hacia ThingSpeak
@app.post("/api/esp32/enviar-datos")
def recibir_datos_esp32(datos: TelemetriaInput, db: Session = Depends(get_db)):
    try:
        # A. Almacenamiento directo en Neon PostgreSQL
        nueva_lectura = LecturaAgua(
            identificador=datos.identificador,
            temperatura=datos.temperatura,
            turbidez=datos.turbidez,
            estado=datos.estado,
            timestamp=datetime.utcnow()
        )
        db.add(nueva_lectura)
        db.commit()
        db.refresh(nueva_lectura)

        # B. Detección y registro de incidentes en BD
        if datos.estado and datos.estado.lower() in ["critica", "regular", "alerta"]:
            alerta = Incidente(
                nivel="ALERTA",
                descripcion=f"Anomalía detectada en {datos.identificador}: Temp={datos.temperatura}°C, Turb={datos.turbidez}NTU",
                estado="ACTIVO",
                timestamp=datetime.utcnow()
            )
            db.add(alerta)
            db.commit()

        # C. Reenvío a ThingSpeak con el dato extraído de Neon PostgreSQL (usando POST)
        ts_entry_id = "No sincronizado"
        if THINGSPEAK_WRITE_KEY:
            try:
                payload = {
                    "api_key": THINGSPEAK_WRITE_KEY,
                    "field1": str(nueva_lectura.temperatura),
                    "field2": str(nueva_lectura.turbidez)
                }
                res_ts = requests.post(THINGSPEAK_UPDATE_URL, data=payload, timeout=4)
                ts_entry_id = res_ts.text.strip()
                print(f"[ThingSpeak POST Response]: {ts_entry_id}")
            except Exception as e:
                ts_entry_id = f"Error de conexión: {str(e)}"
                print(f"[ThingSpeak Error]: {e}")

        return {
            "status": "success",
            "origen_datos": "Neon PostgreSQL -> ThingSpeak",
            "id_neon": nueva_lectura.id,
            "temperatura_db": nueva_lectura.temperatura,
            "turbidez_db": nueva_lectura.turbidez,
            "thingspeak_entry_id": ts_entry_id,
            "frecuencia_actual": configuracion_sistema["intervalo_segundos"]
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al procesar la telemetría: {str(e)}")

# 2. Historial de lecturas almacenadas en Neon
@app.get("/api/calidad-agua")
def obtener_calidad_agua(limite: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    lecturas = db.query(LecturaAgua).order_by(desc(LecturaAgua.timestamp)).limit(limite).all()
    data = [
        {
            "id": item.id,
            "time": item.timestamp.isoformat() if item.timestamp else None,
            "identificador": item.identificador,
            "temperatura": item.temperatura,
            "turbidez": item.turbidez,
            "estado": item.estado
        }
        for item in reversed(lecturas)
    ]
    return {
        "status": "success",
        "origen": "PostgreSQL Neon",
        "data": data
    }

# 3. Consulta de la última telemetría persistida en base de datos
@app.get("/api/telemetria")
def obtener_ultima_telemetria(db: Session = Depends(get_db)):
    ultima = db.query(LecturaAgua).order_by(desc(LecturaAgua.timestamp)).first()
    if not ultima:
        return {
            "identificador": "ESP32_Breiner",
            "temperatura": 0.0,
            "turbidez": 0.0,
            "estado": "Sin datos",
            "timestamp": datetime.utcnow().isoformat()
        }
    return {
        "id": ultima.id,
        "identificador": ultima.identificador,
        "temperatura": ultima.temperatura,
        "turbidez": ultima.turbidez,
        "estado": ultima.estado,
        "timestamp": ultima.timestamp.isoformat() if ultima.timestamp else None
    }

# 4. Listado de incidentes registrados en Neon
@app.get("/api/incidentes")
def obtener_incidentes(db: Session = Depends(get_db)):
    incidentes = db.query(Incidente).order_by(desc(Incidente.timestamp)).limit(10).all()
    return [
        {
            "id": inc.id,
            "fecha": inc.timestamp.strftime("%Y-%m-%d %H:%M:%S") if inc.timestamp else None,
            "nivel": inc.nivel,
            "descripcion": inc.descripcion,
            "estado": inc.estado
        }
        for inc in incidentes
    ]

# 5. Obtención y modificación de la frecuencia de muestreo
@app.get("/api/control/frecuencia")
def obtener_frecuencia():
    return configuracion_sistema

@app.post("/api/control/frecuencia")
def configurar_frecuencia(datos: FrecuenciaInput):
    configuracion_sistema["intervalo_segundos"] = datos.intervalo_segundos
    return {
        "status": "success",
        "intervalo_segundos": configuracion_sistema["intervalo_segundos"]
    }

# 6. Procesamiento de eventos del teclado matricial
@app.post("/api/control/teclado")
def procesar_tecla_esp32(datos: TeclaInput, db: Session = Depends(get_db)):
    tecla = datos.tecla.strip()
    ultima = db.query(LecturaAgua).order_by(desc(LecturaAgua.timestamp)).first()
    temp = ultima.temperatura if ultima else 0.0
    turb = ultima.turbidez if ultima else 0.0

    if tecla == "1":
        return {
            "status": "success",
            "tecla": "1",
            "accion": "CONSULTA_RAPIDA",
            "mensaje_lcd": f"T:{temp:.1f}C Tu:{turb:.1f}NTU"
        }
    elif tecla == "2":
        configuracion_sistema["intervalo_segundos"] = 5
        return {
            "status": "success",
            "tecla": "2",
            "accion": "CAMBIO_FRECUENCIA",
            "mensaje_lcd": "Muestreo: 5s"
        }
    elif tecla == "3":
        return {
            "status": "success",
            "tecla": "3",
            "accion": "DIAGNOSTICO_LLM",
            "mensaje_lcd": "Analisis listo",
            "diagnostico_completo": f"Telemetría actual en Neon: Temp={temp}°C, Turbidez={turb}NTU."
        }
    else:
        return {
            "status": "ignored",
            "tecla": tecla,
            "mensaje_lcd": f"Tecla {tecla} sin accion"
        }

# 7. Diagnóstico técnico mediante Gemini LLM
@app.post("/api/llm/consultar")
def consultar_llm(datos: ConsultaLLMInput, db: Session = Depends(get_db)):
    ultima = db.query(LecturaAgua).order_by(desc(LecturaAgua.timestamp)).first()
    temp = ultima.temperatura if ultima else 25.0
    turb = ultima.turbidez if ultima else 3.0
    estado = ultima.estado if ultima else "Normal"

    pregunta = datos.pregunta or "Realiza una evaluación técnica de los parámetros actuales del agua."

    prompt = f"""
    Eres un asistente experto en calidad de agua e ingeniería sanitaria.
    Parámetros actuales registrados en la base de datos:
    - Temperatura: {temp} °C
    - Turbidez: {turb} NTU
    - Estado clasificado: {estado}

    Pregunta del usuario: {pregunta}
    Proporciona un diagnóstico técnico y recomendaciones en un párrafo concreto.
    """

    if not GEMINI_KEY:
        return {
            "status": "mock",
            "contexto": {"temperatura": temp, "turbidez": turb, "estado": estado},
            "respuesta_llm": f"Diagnóstico local: Agua en estado {estado}. Temperatura {temp}°C y Turbidez {turb} NTU dentro de rangos previstos."
        }

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        res = model.generate_content(prompt)
        return {
            "status": "success",
            "contexto": {"temperatura": temp, "turbidez": turb, "estado": estado},
            "respuesta_llm": res.text
        }
    except Exception as e:
        return {
            "status": "error",
            "detalle": str(e),
            "respuesta_llm": f"Evaluación de contingencia: Temp {temp}°C, Turbidez {turb} NTU."
        }
