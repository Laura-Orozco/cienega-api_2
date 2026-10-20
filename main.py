import os
from datetime import datetime
from zoneinfo import ZoneInfo
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

# Configuración de CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuración de zona horaria de Colombia
TZ_COLOMBIA = ZoneInfo("America/Bogota")

def obtener_hora_colombia():
    """Devuelve la fecha y hora actual en zona horaria de Colombia (UTC-5)"""
    return datetime.now(TZ_COLOMBIA)

# Configuración de Gemini LLM
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

# Clave de escritura del nuevo canal de ThingSpeak
THINGSPEAK_WRITE_KEY = os.getenv("THINGSPEAK_WRITE_KEY", "6L9PJOXG9FZMFYU")
THINGSPEAK_UPDATE_URL = "https://api.thingspeak.com/update"

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
        "zona_horaria": "America/Bogota",
        "hora_servidor": obtener_hora_colombia().isoformat(),
        "docs": "/docs"
    }

# 1. Ingesta: Guarda en Neon PostgreSQL con hora Colombia y sincroniza hacia ThingSpeak
@app.post("/api/esp32/enviar-datos")
def recibir_datos_esp32(datos: TelemetriaInput, db: Session = Depends(get_db)):
    try:
        # Fecha y hora exacta de Colombia
        ahora_colombia = obtener_hora_colombia()

        # A. Almacenamiento directo en Neon PostgreSQL
        nueva_lectura = LecturaAgua(
            identificador=datos.identificador,
            temperatura=datos.temperatura,
            turbidez=datos.turbidez,
            estado=datos.estado,
            timestamp=ahora_colombia
        )
        db.add(nueva_lectura)
        db.commit()
        db.refresh(nueva_lectura)

        # B. Detección y registro de incidentes en BD
        if datos.estado and datos.estado.lower() in ["critica", "regular", "alerta"]:
            alerta = Incidente(
                nivel="ALERTA",
                descripcion=f"Anomalía en {datos.identificador}: Temp={datos.temperatura}°C, Turb={datos.turbidez}NTU",
                estado="ACTIVO",
                timestamp=ahora_colombia
            )
            db.add(alerta)
            db.commit()

        # C. Mapeo numérico del estado para ThingSpeak (Field 3)
        estado_map = {"normal": 1, "regular": 2, "alerta": 2, "critica": 3}
        estado_codificado = estado_map.get(str(datos.estado).lower(), 1)

        # D. Publicar hacia ThingSpeak mediante POST
        ts_entry_id = "No sincronizado"
        if THINGSPEAK_WRITE_KEY:
            try:
                payload = {
                    "api_key": THINGSPEAK_WRITE_KEY,
                    "field1": str(nueva_lectura.temperatura),
                    "field2": str(nueva_lectura.turbidez),
                    "field3": str(estado_codificado)
                }
                res_ts = requests.post(THINGSPEAK_UPDATE_URL, data=payload, timeout=4)
                ts_entry_id = res_ts.text.strip()
                print(f"[ThingSpeak POST Response]: {ts_entry_id}")
            except Exception as e:
                ts_entry_id = f"Error: {str(e)}"
                print(f"[ThingSpeak Error]: {e}")

        return {
            "status": "success",
            "origen_datos": "Neon PostgreSQL -> ThingSpeak",
            "id_neon": nueva_lectura.id,
            "temperatura_db": nueva_lectura.temperatura,
            "turbidez_db": nueva_lectura.turbidez,
            "estado_db": nueva_lectura.estado,
            "timestamp_colombia": ahora_colombia.strftime("%Y-%m-%d %H:%M:%S"),
            "thingspeak_entry_id": ts_entry_id,
            "frecuencia_actual": configuracion_sistema["intervalo_segundos"]
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al procesar telemetría: {str(e)}")

# 2. Historial de lecturas con timestamp ajustado
@app.get("/api/calidad-agua")
def obtener_calidad_agua(limite: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    lecturas = db.query(LecturaAgua).order_by(desc(LecturaAgua.timestamp)).limit(limite).all()
    
    data = []
    for item in reversed(lecturas):
        ts_str = None
        if item.timestamp:
            # Si el registro no tiene tzinfo asignado en la BD, lo contextualizamos a Colombia
            dt = item.timestamp if item.timestamp.tzinfo else item.timestamp.replace(tzinfo=TZ_COLOMBIA)
            ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")

        data.append({
            "id": item.id,
            "time": ts_str,
            "identificador": item.identificador,
            "temperatura": item.temperatura,
            "turbidez": item.turbidez,
            "estado": item.estado
        })

    return {
        "status": "success",
        "origen": "PostgreSQL Neon",
        "data": data
    }

# 3. Consulta de la última telemetría persistida
@app.get("/api/telemetria")
def obtener_ultima_telemetria(db: Session = Depends(get_db)):
    ultima = db.query(LecturaAgua).order_by(desc(LecturaAgua.timestamp)).first()
    if not ultima:
        return {
            "identificador": "ESP32_Breiner",
            "temperatura": 0.0,
            "turbidez": 0.0,
            "estado": "Sin datos",
            "timestamp": obtener_hora_colombia().strftime("%Y-%m-%d %H:%M:%S")
        }

    ts_str = None
    if ultima.timestamp:
        dt = ultima.timestamp if ultima.timestamp.tzinfo else ultima.timestamp.replace(tzinfo=TZ_COLOMBIA)
        ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")

    return {
        "id": ultima.id,
        "identificador": ultima.identificador,
        "temperatura": ultima.temperatura,
        "turbidez": ultima.turbidez,
        "estado": ultima.estado,
        "timestamp": ts_str
    }

# 4. Listado de incidentes
@app.get("/api/incidentes")
def obtener_incidentes(db: Session = Depends(get_db)):
    incidentes = db.query(Incidente).order_by(desc(Incidente.timestamp)).limit(10).all()
    return [
        {
            "id": inc.id,
            "fecha": (inc.timestamp if inc.timestamp.tzinfo else inc.timestamp.replace(tzinfo=TZ_COLOMBIA)).strftime("%Y-%m-%d %H:%M:%S") if inc.timestamp else None,
            "nivel": inc.nivel,
            "descripcion": inc.descripcion,
            "estado": inc.estado
        }
        for inc in incidentes
    ]

# 5. Configuración de frecuencia
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

# 6. Teclado matricial del ESP32
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

# 7. Diagnóstico técnico con LLM
@app.post("/api/llm/consultar")
def consultar_llm(datos: ConsultaLLMInput, db: Session = Depends(get_db)):
    ultima = db.query(LecturaAgua).order_by(desc(LecturaAgua.timestamp)).first()
    temp = ultima.temperatura if ultima else 25.0
    turb = ultima.turbidez if ultima else 3.0
    estado = ultima.estado if ultima else "Normal"

    pregunta = datos.pregunta or "Realiza una evaluación técnica de los parámetros actuales del agua."

    prompt = f"""
    Eres un asistente experto en calidad de agua e ingeniería sanitaria.
    Parámetros actuales en Neon DB:
    - Temperatura: {temp} °C
    - Turbidez: {turb} NTU
    - Estado clasificado: {estado}

    Pregunta: {pregunta}
    Proporciona un diagnóstico técnico y recomendaciones concisas.
    """

    if not GEMINI_KEY:
        return {
            "status": "mock",
            "contexto": {"temperatura": temp, "turbidez": turb, "estado": estado},
            "respuesta_llm": f"Diagnóstico: Agua en estado {estado}. Temperatura {temp}°C y Turbidez {turb} NTU en rangos previstos."
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
