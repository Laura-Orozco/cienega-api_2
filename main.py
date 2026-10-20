import os
from fastapi import FastAPI, Depends, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import desc
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import google.generativeai as genai
from database import get_db, LecturaAgua, Incidente

# Configuración LLM (Gemini API para Punto 4)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

app = FastAPI(title="API Central IoT - Monitoreo Calidad del Agua (Neon DB)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRECUENCIA_MUESTREO = {"intervalo_segundos": 10}

# Esquema idéntico al diccionario que envía el ESP32
class TelemetriaIn(BaseModel):
    identificador: Optional[str] = "ESP32_Breiner"
    temperatura: float
    turbidez: float
    estado: Optional[str] = "Normal"

class TecladoIn(BaseModel):
    tecla: str

@app.get("/")
def raiz():
    return {"status": "online", "motor_bd": "PostgreSQL Neon", "version": "2.1.0"}

# ====================================================================
# 1. INGESTA DIRECTA DESDE EL ESP32
# ====================================================================
@app.post("/api/esp32/enviar-datos")
def recibir_datos_esp32(payload: TelemetriaIn, db: Session = Depends(get_db)):
    estado_final = payload.estado if payload.estado else "Normal"

    nueva_lectura = LecturaAgua(
        identificador=payload.identificador,
        temperatura=payload.temperatura,
        turbidez=payload.turbidez,
        estado=estado_final,
        timestamp=datetime.utcnow()
    )
    db.add(nueva_lectura)

    # Registrar en incidentes si la condición es Crítica o Regular
    if estado_final.strip().capitalize() in ["Critica", "Regular", "Alerta"]:
        nuevo_incidente = Incidente(
            nivel="ALERTA" if estado_final.strip().capitalize() == "Critica" else "ADVERTENCIA",
            descripcion=f"Anomalía en {payload.identificador}: Temp={payload.temperatura}°C, Turb={payload.turbidez}NTU",
            estado="ACTIVO"
        )
        db.add(nuevo_incidente)

    db.commit()
    db.refresh(nueva_lectura)

    return {
        "status": "success",
        "mensaje": "Dato persistido en Neon PostgreSQL",
        "frecuencia_actual": FRECUENCIA_MUESTREO["intervalo_segundos"]
    }

# ====================================================================
# 2. PUNTO 3: CONSUMO DESDE NEON PARA EL DASHBOARD
# ====================================================================
@app.get("/api/calidad-agua")
def obtener_calidad_agua(limite: int = 20, db: Session = Depends(get_db)):
    lecturas = db.query(LecturaAgua).order_by(desc(LecturaAgua.timestamp)).limit(limite).all()
    lecturas.reverse()

    resultado = [
        {
            "time": l.timestamp.isoformat() + "Z",
            "identificador": l.identificador,
            "temperatura": l.temperatura,
            "turbidez": l.turbidez,
            "estado": l.estado
        }
        for l in lecturas
    ]

    return {
        "status": "success",
        "origen": "PostgreSQL Neon Direct",
        "data": resultado
    }

@app.get("/api/telemetria")
def obtener_ultima_telemetria(db: Session = Depends(get_db)):
    ultima = db.query(LecturaAgua).order_by(desc(LecturaAgua.timestamp)).first()
    if not ultima:
        return {"temperatura": 24.0, "turbidez": 2.5, "estado": "Normal"}
    return {
        "identificador": ultima.identificador,
        "temperatura": ultima.temperatura,
        "turbidez": ultima.turbidez,
        "estado": ultima.estado,
        "timestamp": ultima.timestamp.isoformat()
    }

@app.get("/api/incidentes")
def obtener_incidentes(db: Session = Depends(get_db)):
    incidentes = db.query(Incidente).order_by(desc(Incidente.timestamp)).limit(10).all()
    return [
        {
            "fecha": inc.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "nivel": inc.nivel,
            "descripcion": inc.descripcion,
            "estado": inc.estado
        }
        for inc in incidentes
    ]

# ====================================================================
# 3. CONTROL DE FRECUENCIA Y TECLADO MATRICIAL
# ====================================================================
@app.post("/api/control/frecuencia")
def configurar_frecuencia(payload: dict = Body(...)):
    if "intervalo_segundos" in payload:
        FRECUENCIA_MUESTREO["intervalo_segundos"] = int(payload["intervalo_segundos"])
    return {"status": "success", "frecuencia_configurada": FRECUENCIA_MUESTREO["intervalo_segundos"]}

@app.get("/api/control/frecuencia")
def obtener_frecuencia():
    return FRECUENCIA_MUESTREO

# ====================================================================
# 4. PUNTO 4: CONSULTAS LLM Y TECLADO MATRICIAL
# ====================================================================
@app.post("/api/control/teclado")
def procesar_tecla_esp32(payload: TecladoIn, db: Session = Depends(get_db)):
    tecla = payload.tecla.strip().upper()
    ultima = db.query(LecturaAgua).order_by(desc(LecturaAgua.timestamp)).first()

    temp = ultima.temperatura if ultima else 24.0
    turb = ultima.turbidez if ultima else 3.0
    estado = ultima.estado if ultima else "Normal"

    db.add(Incidente(
        nivel="INFO",
        descripcion=f"Comando de teclado: Tecla {tecla}",
        estado="PROCESADO"
    ))
    db.commit()

    if tecla == "1":
        return {
            "status": "success",
            "tecla": tecla,
            "accion": "CONSULTA_RAPIDA",
            "mensaje_lcd": f"T:{temp}C Turb:{turb} {estado[:4]}"
        }
    elif tecla == "2":
        FRECUENCIA_MUESTREO["intervalo_segundos"] = 5
        return {
            "status": "success",
            "tecla": tecla,
            "accion": "CAMBIO_FRECUENCIA",
            "mensaje_lcd": "Muestreo: 5 seg"
        }
    elif tecla == "3":
        diagnostico_llm = consultar_llm_diagnostico(temp, turb, estado)
        return {
            "status": "success",
            "tecla": tecla,
            "accion": "DIAGNOSTICO_LLM",
            "mensaje_lcd": diagnostico_llm[:32],
            "diagnostico_completo": diagnostico_llm
        }
    else:
        return {
            "status": "success",
            "tecla": tecla,
            "accion": "DESCONOCIDO",
            "mensaje_lcd": "Opcion no valida"
        }

@app.post("/api/llm/consultar")
def endpoint_consultar_llm(pregunta: Optional[str] = Body(None, embed=True), db: Session = Depends(get_db)):
    ultima = db.query(LecturaAgua).order_by(desc(LecturaAgua.timestamp)).first()
    temp = ultima.temperatura if ultima else 25.0
    turb = ultima.turbidez if ultima else 4.0
    estado = ultima.estado if ultima else "Normal"

    prompt_contexto = f"Datos del tanque: Temperatura = {temp}°C, Turbidez = {turb} NTU, Estado = {estado}."
    if pregunta:
        prompt_final = f"{prompt_contexto}\nPregunta: {pregunta}\nResponde de forma técnica y concisa."
    else:
        prompt_final = f"{prompt_contexto}\nGenera un dictamen conciso sobre la calidad y tratamiento sugerido."

    respuesta_texto = ejecutar_gemini(prompt_final)
    return {
        "status": "success",
        "contexto_usado": {"temperatura": temp, "turbidez": turb, "estado": estado},
        "respuesta_llm": respuesta_texto
    }

def consultar_llm_diagnostico(temperatura: float, turbidez: float, estado: str) -> str:
    prompt = (
        f"Actúa como experto en calidad del agua. Datos: {temperatura}°C y {turbidez} NTU (Estado: {estado}). "
        f"Genera un diagnóstico directo en máximo 2 oraciones sobre la condición del agua."
    )
    return ejecutar_gemini(prompt)

def ejecutar_gemini(prompt: str) -> str:
    if not GEMINI_API_KEY:
        return "LLM no configurado (Falta GEMINI_API_KEY). Parámetros estables según sensores."
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return f"Error en inferencia LLM: {str(e)}"
