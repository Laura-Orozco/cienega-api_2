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

# Conexión y modelos de base de datos
from database import get_db, LecturaAgua, Incidente

app = FastAPI(
    title="API Central IoT - Monitoreo Calidad del Agua (Neon DB & ThingSpeak)",
    version="1.0.0"
)

# Configuración de CORS para permitir solicitudes desde el frontend local o remoto
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuración de zona horaria oficial (Colombia UTC-5)
TZ_COLOMBIA = ZoneInfo("America/Bogota")

def obtener_hora_colombia():
    """Devuelve la fecha y hora actual con zona horaria de Colombia."""
    return datetime.now(TZ_COLOMBIA)

# Configuración de la API de Gemini
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

# Configuración confirmada de ThingSpeak (Canal 3543191)
THINGSPEAK_WRITE_KEY = os.getenv("THINGSPEAK_WRITE_KEY", "H20B180D9AB4YGYH")
THINGSPEAK_UPDATE_URL = "https://api.thingspeak.com/update"

configuracion_sistema = {
    "intervalo_segundos": 15
}

# --- MODELOS PYDANTIC (VALIDACIÓN DE ENTRADA) ---

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


# --- ENDPOINTS DE LA API ---

@app.get("/")
def raiz():
    return {
        "status": "online",
        "mensaje": "API IoT Calidad del Agua conectada a Neon PostgreSQL",
        "tabla_activa": LecturaAgua.__tablename__,
        "zona_horaria": "America/Bogota",
        "hora_servidor": obtener_hora_colombia().isoformat(),
        "docs": "/docs"
    }

# 1. Ingesta: Guarda inmediatamente en Neon DB y luego sincroniza en ThingSpeak
@app.post("/api/esp32/enviar-datos")
def recibir_datos_esp32(datos: TelemetriaInput, db: Session = Depends(get_db)):
    try:
        ahora_colombia = obtener_hora_colombia()

        # A. Crear y persistir el registro en Neon PostgreSQL
        nueva_lectura = LecturaAgua(
            identificador=datos.identificador,
            temperatura=datos.temperatura,
            turbidez=datos.turbidez,
            estado=datos.estado,
            timestamp=ahora_colombia
        )
        db.add(nueva_lectura)
        db.commit()          # Confirmación obligatoria para guardar en disco
        db.refresh(nueva_lectura) # Extrae el ID asignado por la secuencia de PostgreSQL

        print(f"[NEON DB OK] ID={nueva_lectura.id} guardado en tabla '{LecturaAgua.__tablename__}'")

        # B. Detección y persistencia de incidentes
        if datos.estado and datos.estado.lower() in ["critica", "crítica", "regular", "alerta"]:
            alerta = Incidente(
                nivel="ALERTA" if "regular" in datos.estado.lower() or "alerta" in datos.estado.lower() else "CRITICA",
                descripcion=f"Parámetro anómalo en {datos.identificador}: Temp={datos.temperatura}°C, Turb={datos.turbidez}NTU",
                estado="ACTIVO",
                timestamp=ahora_colombia
            )
            db.add(alerta)
            db.commit()

        # C. Mapeo del estado numérico para Field 3 de ThingSpeak
        # 1: Normal, 2: Regular/Alerta, 3: Crítica
        estado_map = {"normal": 1, "regular": 2, "alerta": 2, "critica": 3, "crítica": 3}
        estado_codificado = estado_map.get(str(datos.estado).lower(), 1)

        # D. Publicación hacia ThingSpeak
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
            except Exception as e:
                ts_entry_id = f"Error: {str(e)}"

        return {
            "status": "success",
            "tabla_afectada": LecturaAgua.__tablename__,
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
        print(f"[ERROR NEON DB]: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error al procesar telemetría: {str(e)}")

# 2. Historial de lecturas de la base de datos
@app.get("/api/calidad-agua")
def obtener_calidad_agua(limite: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    lecturas = db.query(LecturaAgua).order_by(desc(LecturaAgua.timestamp)).limit(limite).all()
    
    data = []
    for item in reversed(lecturas):
        ts_str = None
        if item.timestamp:
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
        "tabla": LecturaAgua.__tablename__,
        "total_registros": len(data),
        "data": data
    }

# 3. Consulta de la última telemetría persistida (consumido por index.html)
@app.get("/api/telemetria")
def obtener_ultima_telemetria(db: Session = Depends(get_db)):
    ultima = db.query(LecturaAgua).order_by(desc(LecturaAgua.timestamp)).first()
    
    if not ultima:
        return {
            "identificador": "ESP32_Breiner",
            "temperatura": 0.0,
            "turbidez": 0.0,
            "estado": "Normal",
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

# 4. Consulta de incidentes
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

# 5. Configuración de frecuencia de muestreo del ESP32
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

# 6. Procesamiento de teclado matricial
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
            "diagnostico_completo": f"Telemetría en BD: Temp={temp}°C, Turbidez={turb}NTU."
        }
    else:
        return {
            "status": "ignored",
            "tecla": tecla,
            "mensaje_lcd": f"Tecla {tecla} sin accion"
        }

# 7. Diagnóstico asistido con Gemini
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
