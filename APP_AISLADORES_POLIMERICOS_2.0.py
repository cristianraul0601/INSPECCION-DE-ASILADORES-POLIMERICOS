import os
import json
import time
import base64
import requests
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st
import pandas as pd
from PIL import Image
from google import genai
from google.genai import types
from streamlit_gsheets import GSheetsConnection

# ──────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ──────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Diagnóstico de aisladores", page_icon="🔌", layout="wide")

MODELO = "gemini-3.1-flash-lite"
URL_APPSCRIPT ="https://script.google.com/macros/s/AKfycbx3mLvIF9rq1dhYzzDi5cO9wX_J8e-dWIfLIGoRrrmp0Cm9MxlxrCdwTtOE304LPFm7/exec"
RESULTADOS_CSV = "resultados_diagnostico.csv"
CORRECCIONES_CSV = "correcciones_criterio.csv"

# La API key NUNCA va escrita aquí. En Streamlit Cloud: Settings → Secrets →
#   GEMINI_API_KEY = "tu_key_aqui"
# En local (para probar antes de subir a GitHub), crea un archivo
# .streamlit/secrets.toml con la misma línea, o usa una variable de entorno.
API_KEY = st.secrets.get("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY"))

if not API_KEY:
    st.error(
        "No se encontró GEMINI_API_KEY. Configúrala en Settings → Secrets "
        "(si está en Streamlit Cloud) o como variable de entorno (si es local)."
    )
    st.stop()

client = genai.Client(api_key=API_KEY.strip())

SYSTEM_INSTRUCTION = """
Eres un especialista técnico en alta tensión e inspección visual de aisladores poliméricos,
siguiendo la Guía de inspección visual de aisladores poliméricos del área de Mantenimiento AT de Pluz Energía.

Clasificación general de prioridad del daño:
- Grado 1: buen estado, bajo grado de daño/desgaste/deterioro. Vigilar daños futuros.
- Grado 2: grado moderado de daño/desgaste/deterioro. Considerar reemplazo.
- Grado 3: alto grado de daño/desgaste/deterioro. Retirar el aislante de polímero del servicio.

Identifica el tipo de daño específico visible en la imagen y asigna el grado y la acción según esta tabla de referencia:

1. Contaminación general (polvo, polución): Grado 1-2 según el nivel. Acción: limpieza/lavado del aislador dependiendo del grado.
2. Contaminación causada por aves: Grado 2 (reduce la capacidad del aislante). Acción: lavado del aislador.
3. Contaminación biológica (hongos o moho): Grado 1-2 según acumulación. Acción: lavado dependiendo del grado.
4. Daño al cobertizo/vaina causado por aves o roedores:
   - Pequeños daños en el cobertizo: Grado 1. Acción: ninguna acción, monitorizar.
   - Daño en la vaina, varilla NO expuesta: Grado 2. Acción: retirar el aislante en la próxima oportunidad.
   - Varilla expuesta: Grado 3. Acción: retirar el aislante del servicio.
5. Daño al cobertizo (fisuras menores que pueden reducir ligeramente el rendimiento): Grado 2. Acción: vigilar el crecimiento de las fisuras y la posible progresión hacia la vaina.
6. Caimanería (patrón de agrietamiento superficial tipo "piel de caimán", señal de envejecimiento por rayos UV y pérdida de hidrofobicidad): Grado 2-3. Acción: monitorizar el aumento de la degradación; considerar el retiro del servicio.
7. Erosión superficial (escala propia):
   - Leve: Grado 1. Acción: monitorizar la progresión del daño.
   - Profunda: Grado 2. Acción: considerar el retiro del servicio.
   - Excesiva (indicio de descarga eléctrica excesiva): Grado 3. Acción: retiro del servicio.
8. Blanqueo/tiza (polvo o película blanca sobre la superficie): Grado 2. Acción: reemplazar en la próxima oportunidad si está en servicio.
9. Agrietamiento (crazing: pistas finas por actividad eléctrica superficial, agravada por UV y corona; distinto de una grieta abierta en la vaina): Grado 2-3. Acción: monitorizar la degradación; considerar reemplazo o retiro.
10. Grietas en el cobertizo: Grado 2. Acción: reemplazar en la próxima oportunidad si está en servicio.
11. Grietas en la vaina (con posible penetración de humedad al núcleo): Grado 3. Acción: retirar el aislador del servicio.
12. Daño por arma de fuego:
    - Vaina dañada, varilla NO expuesta: Grado 2. Acción: retirar en la próxima oportunidad.
    - Varilla expuesta o balas alojadas en la vaina: Grado 3. Acción: retirar del servicio.
13. Orificios en la vaina (efecto corona, posible penetración de humedad): Grado 3. Acción: retirar del servicio.
14. Tracking (trayectoria conductora carbonizada): Grado 3. Acción: retirar del servicio.
15. Varilla expuesta (sin otra causa evidente): Grado 3. Acción: retirar del servicio.
16. Mala adherencia en la interfaz (permite ingreso de humedad; suele notarse en cortes, huellas carbonizadas o termografía): Grado 3. Acción: retirar del servicio.

Si la imagen muestra más de un tipo de daño, reporta el de mayor grado (el más severo) y menciona los demás en la observación.

Responde obligatoriamente en formato JSON válido con estas 4 claves:
"tipo_dano", "grado", "accion", "observacion".
"""

GRADO_COLOR = {"1": "#3FB68B", "2": "#F5A623", "3": "#E5484D"}


# ──────────────────────────────────────────────────────────────────────────
# ESTADO PERSISTENTE EN SESIÓN (se recarga desde los CSV al iniciar la app)
# ──────────────────────────────────────────────────────────────────────────
def cargar_csv(path, columnas):
    if os.path.exists(path):
        try:
            return pd.read_csv(path, sep=';', encoding='utf-8-sig').to_dict('records')
        except Exception:
            return []
    return []


if "resultados" not in st.session_state:
    st.session_state.resultados = cargar_csv(
        RESULTADOS_CSV, ["archivo", "grado", "tipo_dano", "accion", "observacion"]
    )

conn = st.connection("gsheets", type=GSheetsConnection)

def cargar_correcciones_gsheet():
    try:
        df = conn.read(ttl="0s")
        if df is not None and not df.empty:
            return df.dropna(how="all").to_dict("records")
    except Exception:
        pass
    return []

if "correcciones" not in st.session_state:
    st.session_state.correcciones = cargar_correcciones_gsheet()

if "procesados" not in st.session_state:
    st.session_state.procesados = set(r["archivo"] for r in st.session_state.resultados)
    
if "imagenes_almacenadas" not in st.session_state:
    st.session_state.imagenes_almacenadas = {}

def guardar_resultados():
    pd.DataFrame(st.session_state.resultados).to_csv(
        RESULTADOS_CSV, sep=';', index=False, encoding='utf-8-sig'
    )


def guardar_correcciones(registro):
    try:
        requests.post(URL_APPSCRIPT, json=registro, timeout=5)
    except Exception as e:
        st.error(f"Error al enviar a Google Sheets: {e}")
            
def exportar_a_excel(datos):
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        pd.DataFrame(datos).to_excel(writer, index=False, sheet_name='Diagnostico')
    return output.getvalue()

def construir_bloque_correcciones():
    if not st.session_state.correcciones:
        return ""
    recientes = st.session_state.correcciones[-30:]
    lineas = []
    for i, c in enumerate(recientes):
        ia_g = c.get('ia_grado', '')
        ia_d = c.get('ia_tipo_dano', '')
        # Se usan los nombres originales de tu hoja: 'grado' y 'tipo_dano'
        usr_g = c.get('grado', c.get('usuario_grado', ''))
        usr_d = c.get('tipo_dano', c.get('usuario_tipo_dano', ''))
        obs = c.get('observacion')
        
        texto_obs = ""
        if obs is not None and str(obs).strip() not in ("", "None", "nan"):
            texto_obs = f" Nota: {obs}"
            
        lineas.append(
            f"{i+1}. La IA había dicho: grado {ia_g} ({ia_d}). "
            f"El experto corrigió a: grado {usr_g} ({usr_d}).{texto_obs}"
        )
    ejemplos = "\n".join(lineas)
    return (
        "\n\nEl equipo ha corregido evaluaciones previas de la IA. Usa estos ejemplos "
        f"para ajustar tu criterio y no repetir los mismos errores:\n{ejemplos}"
    )


# ──────────────────────────────────────────────────────────────────────────
# ANÁLISIS CON LA IA (misma lógica de reintentos que la versión de escritorio)
# ──────────────────────────────────────────────────────────────────────────
def optimizar_imagen(uploaded_file, max_dim=1024):
    img = Image.open(uploaded_file)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
    return img


def clasificar_tipo_error(error_texto):
    et = error_texto.lower()
    if "404" in error_texto or "not_found" in et:
        return "MODELO_INVALIDO"
    elif "429" in error_texto or "resource_exhausted" in et:
        if "perday" in et.replace(" ", "") or "per day" in et:
            return "CUOTA_DIARIA"
        elif "perminute" in et.replace(" ", "") or "per minute" in et:
            return "CUOTA_MINUTO"
        else:
            return "CUOTA_DIARIA"
    elif "503" in error_texto or "unavailable" in et:
        return "SERVIDOR"
    return "OTRO"


def analizar_aislador(img, max_intentos=4):
    ultimo_error = ""
    system_prompt = SYSTEM_INSTRUCTION + construir_bloque_correcciones()

    for _ in range(max_intentos):
        try:
            response = client.models.generate_content(
                model=MODELO,
                contents=["Analiza este aislador polimérico y devuelve el JSON de diagnóstico:", img],
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                ),
            )
            return json.loads(response.text)
        except Exception as e:
            ultimo_error = str(e)
            tipo = clasificar_tipo_error(ultimo_error)

            if tipo == "MODELO_INVALIDO":
                st.error(f"El modelo '{MODELO}' no existe o fue retirado por Google. Revisa la variable MODELO.")
                st.stop()
            elif tipo == "CUOTA_DIARIA":
                break
            elif tipo == "CUOTA_MINUTO":
                time.sleep(20)
            elif tipo == "SERVIDOR":
                time.sleep(8)
            else:
                time.sleep(4)

    return {
        "grado": "ERROR",
        "tipo_dano": "ERROR_RESPUESTA",
        "accion": "REINTENTAR",
        "observacion": f"Detalle: {ultimo_error[:200]}",
    }


# ──────────────────────────────────────────────────────────────────────────
# INTERFAZ
# ──────────────────────────────────────────────────────────────────────────
st.title("🔌 Diagnóstico de aisladores poliméricos")
st.caption("Mantenimiento AT — evaluación con IA según la guía del área")

n_corr = len(st.session_state.correcciones)
if n_corr == 0:
    st.info("Aún no hay correcciones registradas — la IA usa solo el criterio base de la guía.")
else:
    st.success(f"{n_corr} corrección(es) acumulada(s) · usándose para ajustar el criterio.")

archivos = st.file_uploader(
    "Sube una o varias fotos de aisladores (JPG o PNG)",
    type=["jpg", "jpeg", "png"],
    accept_multiple_files=True,
)

if archivos:
    nuevos = [f for f in archivos if f.name not in st.session_state.procesados]
    if nuevos:
        barra = st.progress(0, text="Analizando imágenes...")
        for i, archivo in enumerate(nuevos):
            st.session_state.imagenes_almacenadas[archivo.name] = archivo.getvalue()
            img = optimizar_imagen(archivo)
            data = analizar_aislador(img)
            registro = {
                "archivo": archivo.name,
                "grado": str(data.get("grado", "")),
                "tipo_dano": data.get("tipo_dano", ""),
                "accion": data.get("accion", ""),
                "observacion": data.get("observacion", ""),
            }
            st.session_state.resultados.append(registro)
            if registro.get("grado") != "ERROR" and registro.get("tipo_dano") != "ERROR_RESPUESTA":
                st.session_state.procesados.add(archivo.name)
            barra.progress((i + 1) / len(nuevos), text=f"Analizando {archivo.name}...")
        guardar_resultados()
        barra.empty()

st.divider()
st.subheader("Resultados")

if not st.session_state.resultados:
    st.caption("Aún no se ha subido ninguna imagen.")
else:
    # Mapa de archivo -> UploadedFile de esta sesión, para mostrar miniatura si está disponible
    mapa_archivos = {f.name: f for f in (archivos or [])}

    for idx, r in enumerate(reversed(st.session_state.resultados)):
        real_idx = len(st.session_state.resultados) - 1 - idx
        color = GRADO_COLOR.get(str(r["grado"]), "#8CA0B3")
        col_img, col_info = st.columns([1, 5])
        
        with col_img:
                img_bytes = st.session_state.imagenes_almacenadas.get(r["archivo"])
                if img_bytes:
                    st.image(img_bytes, width=100)
                    with st.popover("🔍 Ver"):
                        st.image(img_bytes, caption=r["archivo"], use_container_width=True)
        with col_info:
            st.markdown(
                f"**{r['archivo']}** &nbsp;"
                f"<span style='background:{color}22;color:{color};padding:2px 8px;"
                f"border-radius:4px;font-size:12.5px;font-weight:600;'>GRADO {r['grado']}</span>",
                unsafe_allow_html=True,
            )
            st.write(f"**{r['tipo_dano']}**")
            st.caption(r["accion"])
            st.write(r["observacion"])
            # Botón para reintentar si dio error de API/servidor
            if r.get("grado") == "ERROR" or r.get("tipo_dano") == "ERROR_RESPUESTA":
                if st.button("🔄 Reintentar análisis", key=f"retry_{real_idx}_{r['archivo']}"):
                    # 1. Elimina de procesados para que Streamlit acepte analizarlo de nuevo
                    st.session_state.procesados.discard(r["archivo"])
                    # 2. Quita el resultado fallido de la lista
                    st.session_state.resultados.pop(real_idx)
                    guardar_resultados()
                    st.rerun()

            ya_corregido = any(c["archivo"] == r["archivo"] for c in st.session_state.correcciones)
            if ya_corregido:
                st.caption("✓ Corregido por el equipo")
            else:
                with st.expander("¿No es correcto? Corregir"):
                    with st.form(key=f"form_{real_idx}_{r['archivo']}"):
                        nuevo_grado = st.selectbox(
                            "Grado correcto", ["1", "2", "3"],
                            index=["1", "2", "3"].index(str(r["grado"])) if str(r["grado"]) in ["1", "2", "3"] else 0,
                        )
                        nuevo_tipo = st.text_input("Tipo de daño correcto", value=r["tipo_dano"])
                        nueva_accion = st.text_input("Acción correcta", value=r["accion"])
                        nota = st.text_area(
                            "⚠️ Nota técnica para la IA (Obligatoria)",
                            placeholder="Ej: 'Fisura transversal en falda superior', 'Solo polvo superficial sin daño', 'Tizado blanco avanzado'.",
                            help="Describe el rasgo visual observado para que la IA aprenda el criterio del equipo.",
                        )
                        if st.form_submit_button("Guardar corrección"):
                            if not nota.strip():
                                st.warning("⚠️ Por favor escribe una nota describiendo el daño observado antes de guardar.")
                            else:
                                nueva_fila = {
                                    "archivo": r["archivo"],
                                    "ia_grado": r["grado"],
                                    "ia_tipo_dano": r["tipo_dano"],
                                    "grado": nuevo_grado,
                                    "tipo_dano": nuevo_tipo,
                                    "accion": nueva_accion,
                                    "observacion": nota,
                                    "fecha": datetime.now(ZoneInfo("America/Lima")).strftime("%Y-%m-%d %H:%M:%S"),
                                }
                                st.session_state.correcciones.append(nueva_fila)
                                st.session_state.resultados[real_idx]["grado"] = nuevo_grado
                                st.session_state.resultados[real_idx]["tipo_dano"] = nuevo_tipo
                                st.session_state.resultados[real_idx]["accion"] = nueva_accion
                                guardar_resultados()
                                guardar_correcciones(nueva_fila)
                                st.rerun()
                            
                            st.markdown("---")

with st.sidebar:
    st.markdown("### 📥 Descargas")
    if st.session_state.resultados:
        excel_res = exportar_a_excel(st.session_state.resultados)
        st.download_button(
            "⬇️ Descargar resultados (Excel)",
            data=excel_res,
            file_name="resultados_diagnostico.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    if st.session_state.correcciones:
        excel_corr = exportar_a_excel(st.session_state.correcciones)
        st.download_button(
            "⬇️ Descargar correcciones (Excel)",
            data=excel_corr,
            file_name="correcciones_criterio_ia.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

st.caption(
    "Los resultados y correcciones se guardan en esta instancia de la app. "
    "Si Streamlit Cloud reinicia la app por inactividad prolongada, descarga los CSV "
    "periódicamente para no perder el historial, o migra el guardado a Google Sheets "
    "más adelante para que sea 100% permanente."
)
