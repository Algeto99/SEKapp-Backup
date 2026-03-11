import os
import sys
import logging
import re
from datetime import timedelta, datetime, timezone, date, time
from io import BytesIO

from flask import Blueprint, current_app, Flask, render_template, request, jsonify, Response, flash, session, redirect, url_for, send_file
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity, get_jwt, unset_jwt_cookies
from flask_cors import CORS
from google.cloud import secretmanager
from google.api_core.exceptions import NotFound

import google.auth.transport.requests
import google.oauth2.id_token
import requests

import psycopg2
from psycopg2 import extras
import urllib.parse as urlparse

import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from functools import wraps
from flask_jwt_extended import get_jwt
from google.cloud import storage

# PDF generation imports
try:
    from weasyprint import HTML
    WEASYPRINT_AVAILABLE = True
except OSError:
    print("WARNING: WeasyPrint could not be loaded. PDF generation will not work locally.")
    WEASYPRINT_AVAILABLE = False
    HTML = None

viewer_bp = Blueprint('viewer', __name__)

def generate_signed_url(gcs_url):
    """Generates a v4 signed URL for a GCS blob."""
    try:
        if not gcs_url or 'storage.googleapis.com' not in gcs_url:
            return gcs_url
        
        # Extract bucket and blob name
        # Format: https://storage.googleapis.com/BUCKET_NAME/BLOB_NAME
        parts = gcs_url.replace("https://storage.googleapis.com/", "").split('/', 1)
        if len(parts) != 2:
            return gcs_url
            
        bucket_name, blob_name = parts
        
        # Explicitly use default credentials which should pick up the service account
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        
        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=60), # 1 hour expiration
            method="GET"
        )
        app_logger.info(f"Generated signed URL for {blob_name}")
        return url
    except Exception as e:
        app_logger.error(f"Error generating signed URL for {gcs_url}: {e}", exc_info=True)
        return gcs_url


# --- Configure Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
app_logger = logging.getLogger(__name__)

# App config and JWT handlers are managed centrally in the monolith's main app_bp

# DB Config
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    app_logger.warning("DATABASE_URL environment variable is not set. Database connection will fail.")

# --- Database Helper Functions ---
def get_db_connection():
    """Establishes and returns a database connection."""
    if not DATABASE_URL:
        app_logger.error("Attempted to connect to DB, but DATABASE_URL is not set.")
        return None
        
    urlparse.uses_netloc.append('postgres')
    parsed_url = urlparse.urlparse(DATABASE_URL)
    query = dict(urlparse.parse_qsl(parsed_url.query))
    
    try:
        app_logger.info(f"Attempting to connect to database using parsed parameters (path: {parsed_url.path})")
        conn = psycopg2.connect(
            dbname=parsed_url.path[1:],
            user=parsed_url.username,
            password=parsed_url.password,
            host=query.get('host', parsed_url.hostname),
            port=query.get('port', parsed_url.port or '5432')
        )
        app_logger.info("Successfully connected to the database.")
        return conn
    except psycopg2.OperationalError as e:
        app_logger.error(f"PostgreSQL Operational Error connecting to database: {e}", exc_info=True)
        if "timeout" in str(e).lower():
            app_logger.error("Possible timeout. Check firewall, Cloud SQL Auth Proxy, or network configuration.")
        elif "no such file or directory" in str(e).lower() and "cloudsql" in str(e).lower():
             app_logger.error("Could not connect to Cloud SQL instance. Ensure 'ADD_CLOUDSQL_INSTANCES' is correctly configured in Cloud Run deployment.")
        return None
    except Exception as e:
        app_logger.error(f"General Error connecting to database: {e}", exc_info=True)
        return None


# --- Form Configurations ---
FORM_CONFIGS = {
    'reporte_incidente': {
        'table': 'reportes_incidentes',
        'id_col': 'id_reporte_incidente',
        'date_col': 'creado_en',
        'user_col': 'user_email',
        'title_prefix': 'Reporte de Incidente',
        'joins': """
            LEFT JOIN users u ON t.user_email = u.email
            LEFT JOIN propiedades p ON t.cliente_instalacion = p.nombre
        """,
        'columns': """
            t.creado_en,
            t.*,
            COALESCE(p.nombre, t.cliente_instalacion) AS propiedad_nombre,
            u.name AS user_name
        """,
        'data_mapping': {
            "Título de Incidencia": "tipo_incidente",
            "Tipo de Cliente": "cliente_instalacion",
            "Lugar del Incidente": "puesto_area_especifica",
            "Propiedad": "propiedad_nombre",
            "Fecha del Incidente": "fecha_hora",
            "Descripción del Incidente": "descripcion_incidente",
            "Nombre del Supervisor": "nombre_responsable",
            "URLs de Imágenes o PDFs": "foto_evidencia_url",
            "Nivel Severidad": "nivel_severidad",
            "Impacto": "impacto",
            "Tiempo Resolución (min)": "tiempo_resolucion_min",
            "Responsable Asignado": "responsable_asignado",
            "Estado": "estado",
            "Descripción Impacto": "descripcion_impacto",
            "Categoría": "categoria",
            "ID Propiedad": "id_propiedad",
            "Firma Responsable": "firma_responsable"
        }
    },
    'mantenimiento_seguridad_fisica': {
        'table': 'mantenimiento_seguridad_fisica',
        'id_col': 'id_mantenimiento',
        'date_col': 'creado_en', 
        'user_col': 'submitted_by_email',
        'title_prefix': 'Mantenimiento Seguridad Física',
        'joins': "LEFT JOIN users u ON t.submitted_by_email = u.email",
        'columns': "t.creado_en, t.*, u.name as user_name",
        'data_mapping': {
            "Fecha": "fecha",
            "Hora": "hora",
            "Sitio": "sitio",
            "Equipo": "equipo",
            "ID Equipo/Serial": "id_equipo_serial",
            "Técnico Responsable": "tecnico_responsable",
            "Tipo de Servicio": "tipo_servicio",
            "Actividad Realizada": "actividad_realizada",
            "Resultado": "resultado",
            "Observaciones": "observaciones",
            "Downtime (Horas)": "downtime_horas",
            "Repuestos Usados": "repuestos_usados",
            "Tipo de Alerta": "tipo_alerta_generada",
            "Descripción Alerta": "descripcion_alerta_critica",
            "Acción Inmediata": "accion_inmediata_critica",
            "Acción Correctiva": "accion_correctiva_recomendada",
            "Responsable Crítica": "responsable_asignado_critica",
            "Fecha Límite": "fecha_limite_cierre_critica",
            "Estado Crítica": "estado_critica",
            "Firma Usuario": "firma_usuario"
        }
    },
    'medicion_experiencia_cliente': {
        'table': 'medicion_experiencia_cliente',
        'id_col': 'id_encuesta',
        'date_col': 'creado_en',
        'user_col': 'submitted_by_email',
        'title_prefix': 'Medición Experiencia Cliente',
        'joins': "LEFT JOIN users u ON t.submitted_by_email = u.email",
        'columns': "t.creado_en, t.*, u.name as user_name",
        'data_mapping': {
            "Cliente/Instalación": "cliente_instalacion",
            "Fecha/Hora": "fecha_hora",
            "Atención al Cliente": "atencion_cliente",
            "Comunicación": "comunicacion",
            "Confiabilidad": "confiabilidad",
            "NPS": "calificacion_global_nps",
            "Observaciones": "observaciones_cliente",
            "Empresa/Sitio": "empresa_sitio",
            "Sitio Local": "sitio_local",
            "Cliente Encargado": "cliente_encargado",
            "Cargo Encuestado": "cargo_encuestado",
            "Recomendaría Servicio": "recomendaria_servicio",
            "Categoría Evaluada": "categoria_evaluada",
            "Encuestado": "encuestado",
            "Firma Encuestado": "firma_encuestado",
            "Firma Responsable": "firma_responsable",
            "Capacidad Reacción": "capacidad_reaccion",
            "Cumplimiento": "cumplimiento",
            "Competencia Personal": "competencia_personal",
            "Actitud Servicio": "actitud_servicio",
            "Atención Quejas": "atencion_quejas"
        }
    },
    'supervision_puesto': {
        'table': 'supervision_puesto',
        'id_col': 'id_supervision',
        'date_col': 'creado_en',
        'user_col': 'submitted_by_email',
        'title_prefix': 'Supervisión de Puesto',
        'joins': "LEFT JOIN users u ON t.submitted_by_email = u.email",
        'columns': "t.creado_en, t.*, u.name as user_name",
        'data_mapping': {
            "Cliente/Instalación": "cliente_instalacion",
            "Fecha/Hora": "fecha_hora",
            "Supervisor": "supervisor",
            "Puesto/Área": "puesto_area_especifica",
            "Nombre Guardia": "nombre_guardia",
            "Observaciones": "observaciones_novedades",
            "Ruta": "ruta",
            "Cliente": "cliente",
            "Dirección": "direccion",
            "Documento Guardia": "documento_guardia",
            "Serie Arma": "serie_arma",
            "Cantidad Munición": "cantidad_municion",
            "Realiza Inducción": "realiza_induccion",
            "Conoce Consignas": "conoce_consignas",
            "Horario Claro": "horario_claro",
            "Asistencia/Puntualidad": "asistencia_puntualidad",
            "Presentación Uniforme": "presentacion_uniforme",
            "Estado Limpieza": "estado_limpieza_puesto",
            "Equipamiento Completo": "equipamiento_completo",
            "Conoce Misión/Visión": "conoce_mision_vision",
            "Estado Bitácora": "estado_bitacora",
            "Firma Guardia": "firma_guardia",
            "Foto Evidencia": "foto_evidencia_url",
            "Conoce Ordenes": "conoce_ordenes_consignas",
            "Horario Detalles": "horario_detalles_claros",
            "Nombre Guardia Firma": "nombre_guardia_firma",
            "Detalles Puestos": "detalles_puestos",
            "Porta Arma": "porta_arma",
            "Conoce Política": "conoce_politica",
            "Número Empleado": "numero_empleado",
            "Rol Aplicador": "rol_aplicador",
            "Firma Supervisor": "firma_supervisor"
        }
    },
    'informe_novedades_disciplinario': {
        'table': 'informe_novedades_disciplinario',
        'id_col': 'id_informe',
        'date_col': 'creado_en',
        'user_col': 'submitted_by_email',
        'title_prefix': 'Informe Disciplinario',
        'joins': "LEFT JOIN users u ON t.submitted_by_email = u.email",
        'columns': "t.creado_en, t.*, u.name as user_name",
        'data_mapping': {
            "Empleado": "empleado_nombre",
            "Cargo": "empleado_cargo",
            "Tipo Novedad": "tipo_novedad",
            "Descripción": "descripcion_novedad",
            "Sitio": "sitio_ocurrencia",
            "Fecha/Hora": "fecha_hora",
            "Responsable": "nombre_responsable",
            "Realizado Por Cargo": "realizado_por_cargo",
            "Dirigido A": "dirigido_a",
            "Empleado Número": "empleado_numero",
            "Empleado Documento": "empleado_documento",
            "Otras Personas": "otras_personas_involucradas",
            "Anexos": "anexos",
            "Firma Responsable": "firma_responsable",
            "Firma Recibido": "firma_recibido_revisado",
            "Rol Aplicador": "rol_aplicador",
            "Turno": "turno",
            "Recibido Por Nombre": "recibido_revisado_por_nombre",
            "Recibido Por Cargo": "recibido_revisado_por_cargo"
        }
    },
    'log_de_patrullas': {
        'table': 'log_de_patrullas',
        'id_col': 'id_patrulla',
        'date_col': 'creado_en',
        'user_col': 'submitted_by_email',
        'title_prefix': 'Log de Patrullas',
        'joins': "LEFT JOIN users u ON t.submitted_by_email = u.email",
        'columns': "t.creado_en, t.*, u.name as user_name",
        'data_mapping': {
            "Guardia": "id_guardia_nombre_guardia",
            "Sitio": "sitio_ubicacion",
            "Fecha": "fecha",
            "Hora Inicio": "hora_inicio",
            "Hora Fin": "hora_fin",
            "Nivel Riesgo": "nivel_riesgo",
            "Estado": "estado_patrulla",
            "Patrulla ID": "id_patrulla_consecutivo",
            "Detalles Incidente": "detalles_incidente",
            "Riesgo Detectado": "riesgo_detectado",
            "Contexto": "contexto_observaciones",
            "Firma Guardia": "firma_guardia",
            "Firma Supervisor": "firma_supervisor"
        }
    },
    'registro_de_capacitaciones': {
        'table': 'registro_de_capacitaciones',
        'id_col': 'id_capacitacion',
        'date_col': 'creado_en',
        'user_col': 'submitted_by_email',
        'title_prefix': 'Registro de Capacitaciones',
        'joins': "LEFT JOIN users u ON t.submitted_by_email = u.email",
        'columns': "t.creado_en, t.*, u.name as user_name",
        'data_mapping': {
            "Capacitación": "nombre_capacitacion",
            "Objetivo": "objetivo_capacitacion",
            "Responsable": "nombre_responsable",
            "Fecha/Hora": "fecha_hora",
            "Nivel Comprensión": "nivel_comprension",
            "Observaciones": "observaciones_retroalimentacion",
            "Lista Asistencia": "lista_asistencia",
            "Práctica Realizada": "practica_simulacro_realizado",
            "Recomendaciones": "recomendaciones"
        }
    },
    'registro_y_acta_de_visita': {
        'table': 'registro_y_acta_de_visita',
        'id_col': 'id_visita',
        'date_col': 'creado_en',
        'user_col': 'submitted_by_email',
        'title_prefix': 'Acta de Visita',
        'joins': "LEFT JOIN users u ON t.submitted_by_email = u.email",
        'columns': "t.creado_en, t.*, u.name as user_name",
        'data_mapping': {
            "Cliente": "cliente_instalacion",
            "Motivo": "motivo_visita",
            "Visitante": "nombre_visitante",
            "Fecha/Hora": "fecha_hora",
            "Temas Tratados": "temas_tratados",
            "Acuerdos": "acuerdos_compromisos",
            "Rol Aplicador": "rol_aplicador",
            "Turno": "turno",
            "Visita Realizada Por": "visita_realizada_por",
            "Firma Visitante": "firma_visitante",
            "Objetivo": "objetivo_reunion",
            "Actividades": "actividades_realizadas",
            "Satisfacción": "satisfaccion_cliente",
            "Comentarios": "comentarios_satisfaccion",
            "Compromisos": "compromisos_adquiridos",
            "Compromisos Responsable": "compromisos_responsable",
            "Compromisos Fecha": "compromisos_fecha_limite",
            "Atendió": "persona_atendio",
            "Cargo Atendió": "cargo_atendio",
            "Teléfono": "telefono_contacto",
            "Detalles Participantes": "detalles_participantes",
            "Cargo Visitante": "cargo_visitante"
        }
    },
    'planilla_vehicular': {
        'table': 'planilla_vehicular',
        'id_col': 'id_planilla_vehicular',
        'date_col': 'creado_en',
        'user_col': 'submitted_by_email',
        'title_prefix': 'Planilla Vehicular',
        'joins': "LEFT JOIN users u ON t.submitted_by_email = u.email",
        'columns': "t.creado_en, t.*, u.name as user_name",
        'data_mapping': {
            "Placa": "placa_vehiculo",
            "Kilometraje": "kilometraje_vehiculo",
            "Responsable": "nombre_responsable",
            "Fecha/Hora": "fecha_hora",
            "Novedades Críticas": "novedades_criticas",
            "Rol Aplicador": "rol_aplicador",
            "Turno": "turno",
            "Firma Responsable": "firma_responsable",
            "Km Entrega": "kilometraje_entrega",
            "Km Salida": "kilometraje_salida",
            "Estado Rines": "estado_rines",
            "Juego Señales": "juego_senales_carretera",
            "Gato Hidráulico": "gato_hidraulico",
            "Palanca Gato": "palanca_gato",
            "Estado Asientos": "estado_asientos",
            "Estado Tapetes": "estado_tapetes_alfombras",
            "Limpieza Carrocería": "limpieza_carroceria",
            "Luces Delanteras": "luces_delanteras",
            "Luces Direccionales": "luces_direccionales",
            "Luces Traseras": "luces_traseras",
            "Parabrisas Delantero": "parabrisas_delantero",
            "Parabrisas Trasero": "parabrisas_trasero",
            "Defensa Delantera": "defensa_delantera",
            "Defensa Trasera": "defensa_trasera",
            "Puertas/Vidrios": "puertas_vidrios",
            "Tapa Radiador": "tapa_radiador",
            "Tapa Aceite": "tapa_aceite_motor",
            "Batería Tapa": "bateria_tapa",
            "Espejo Interno": "espejo_retrovisor_interno",
            "Espejos Externos": "espejos_retrovisores_externos",
            "Limpiabrisas": "limpia_brisas",
            "Antena Radio": "antena_radio",
            "Radio Funciona": "radio_funciona",
            "Llanta Repuesto": "llanta_repuesto",
            "Aire Acondicionado": "aire_acondicionado",
            "Diagrama Daños": "diagrama_danos",
            "Acción Inmediata": "accion_inmediata",
            "Firma Entrega": "firma_entrega",
            "Firma Recibe": "firma_recibe",
            "Oficial Operaciones": "oficial_operaciones_nombre",
            "Firma Oficial": "oficial_operaciones_firma"
        }
    },
    'planilla_motocicletas': {
        'table': 'planilla_motocicletas',
        'id_col': 'id',
        'date_col': 'creado_en',
        'user_col': 'submitted_by_email',
        'title_prefix': 'Planilla Motocicletas',
        'joins': "LEFT JOIN users u ON t.submitted_by_email = u.email",
        'columns': "t.creado_en, t.*, u.name as user_name",
        'data_mapping': {
            "Placa": "placa_motocicleta",
            "Kilometraje": "kilometraje_motocicleta",
            "Responsable": "nombre_responsable",
            "Fecha/Hora": "fecha_hora",
            "Novedades Críticas": "novedades_criticas_detectadas",
            "Rol Aplicador": "rol_aplicador",
            "Turno": "turno",
            "Km Entrega": "kilometraje_entrega",
            "Km Salida": "kilometraje_salida",
            "Estado Neumáticos": "estado_neumaticos",
            "Estado Rines": "estado_rines",
            "Equipo Carretera": "equipo_carretera",
            "Kit Arrastre": "estado_kit_arrastre",
            "Palanca Soporte": "estado_palanca_soporte",
            "Forro Asiento": "estado_forro_asiento",
            "Tapas Derecha": "estado_tapas_derecha",
            "Direccionales Derecha": "estado_luces_direccionales_derecha",
            "Luces Delanteras": "estado_luces_delanteras",
            "Guardafango Delantero": "estado_guarda_fango_delantero",
            "Freno Delantero": "estado_sistema_freno_delantero",
            "Manillar Embrague": "estado_manillar_embrague",
            "Manillar Freno": "estado_manillar_freno_delantero",
            "Manómetros": "estado_manometros_indicadores",
            "Tanque Combustible": "estado_tanque_combustible",
            "Tapa Tanque": "tapa_tanque_combustible",
            "Espejos Retrovisores": "espejos_retrovisores",
            "Tapa Aceite": "tapa_aceite_motor",
            "Batería Tapa": "bateria_tapa",
            "Luces Izquierda": "estado_luces_izquierda",
            "Direccionales Izquierda": "estado_luces_direccionales_izquierda",
            "Luz Trasera": "estado_luz_trasera",
            "Guardafango Trasero": "estado_guarda_fango_trasero",
            "Tubo Escape": "estado_tubo_escape",
            "Palanca Freno": "estado_palanca_freno",
            "Palanca Cambios": "estado_palanca_cambios",
            "Acción Inmediata": "accion_inmediata_tomada",
            "Firma Entrega": "firma_entrega",
            "Firma Recibe": "firma_recibe",
            "Firma Responsable": "firma_responsable",
            "Oficial Operaciones": "oficial_operaciones_nombre",
            "Firma Oficial": "oficial_operaciones_firma"
        }
    },
    'orden_mantenimiento': {
        'table': 'orden_mantenimiento',
        'id_col': 'id_orden',
        'date_col': 'creado_en',
        'user_col': 'submitted_by_email',
        'title_prefix': 'Orden de Mantenimiento',
        'joins': "LEFT JOIN users u ON t.submitted_by_email = u.email",
        'columns': "t.creado_en, t.*, u.name as user_name",
        'data_mapping': {
            "Cliente": "cliente_instalacion",
            "Técnico": "nombre_tecnico",
            "Fecha/Hora": "fecha_hora",
            "Equipo": "equipo",
            "Tipo Servicio": "tipo_servicio",
            "Puesto/Area": "puesto_area",
            "Rol Aplicador": "rol_aplicador",
            "Turno": "turno",
            "Firma Técnico": "firma_tecnico",
            "ID Equipo": "id_equipo_serial",
            "Actividad": "actividad_realizada",
            "Downtime": "downtime_horas",
            "Repuestos": "repuestos_usados",
            "Observaciones": "observaciones",
            "Tipo Clasificación": "tipo_servicio_clasificacion",
            "Resultado Clasificación": "resultado_clasificacion",
            "Alerta Clasificación": "tipo_alerta_clasificacion",
            "Detalles Equipos": "detalles_equipos",
            "Foto Evidencia": "foto_evidencia_url"
        }
    },
    'checklist_cumplimiento': {
        'table': 'checklist_cumplimiento',
        'id_col': 'id',
        'date_col': 'created_at',
        'user_col': 'submitted_by_email',
        'title_prefix': 'Checklist Cumplimiento',
        'joins': "LEFT JOIN users u ON t.submitted_by_email = u.email",
        'columns': "t.created_at, t.*, u.name as user_name",
        'data_mapping': {
            "Cliente": "cliente_instalacion",
            "Auditor": "nombre_auditor",
            "Agente": "agente_nombre_completo",
            "Fecha/Hora": "fecha_hora",
            "Nivel Cumplimiento": "nivel_cumplimiento",
            "Turno": "turno",
            "Firma Auditor": "firma_auditor",
            "Rol Aplicador": "rol_aplicador",
            "Agente Tipo Doc": "agente_tipo_documento",
            "Agente Nro Doc": "agente_numero_documento",
            "Agente Cargo": "agente_cargo_rol",
            "Agente Puesto": "agente_puesto",
            "Curso Certificación": "curso_certificacion",
            "Academia": "academia_certifica",
            "Nro Resolución": "nro_resolucion",
            "Fecha Resolución": "fecha_resolucion",
            "Vigencia Desde": "vigencia_desde",
            "Vigencia Hasta": "vigencia_hasta",
            "Evidencia URL": "evidencia_url",
            "Copia Certificados": "copia_certificados_fisica",
            "Certificados Sistema": "certificados_cargados_sistema",
            "Doc Coincide HV": "documentacion_coincide_hv",
            "Fechas Vigentes": "fechas_vigentes",
            "Firma Guardia": "firma_guarda_supervisado"
        }
    }
}

def fetch_reports(offset, limit, filters=None, form_type='all'):
    conn = None
    cur = None
    all_reports = []
    
    # Determine which form types to fetch
    types_to_fetch = []
    if not form_type or form_type == 'all' or form_type == '':
        types_to_fetch = list(FORM_CONFIGS.keys())
    elif form_type in FORM_CONFIGS:
        types_to_fetch = [form_type]
    else:
        app_logger.warning(f"Invalid form_type: {form_type}. Defaulting to all.")
        types_to_fetch = list(FORM_CONFIGS.keys())

    conn = get_db_connection()
    if not conn:
        app_logger.error("Failed to get database connection in fetch_reports. Returning empty list.")
        return [], 0

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        total_count = 0
        
        for f_type in types_to_fetch:
            config = FORM_CONFIGS[f_type]
            
            # Build WHERE clause based on filters
            where_conditions = []
            query_params = []
            
            if filters:
                # Report ID filter
                if filters.get('report_id'):
                    try:
                        report_id = int(filters['report_id'])
                        where_conditions.append(f"t.{config['id_col']} = %s")
                        query_params.append(report_id)
                    except (ValueError, TypeError):
                        pass # Ignore invalid ID for this type
                
                # Submitted by filter
                if filters.get('submitted_by'):
                    where_conditions.append(f"(LOWER(u.name) LIKE LOWER(%s) OR LOWER(t.{config['user_col']}) LIKE LOWER(%s))")
                    search_term = f"%{filters['submitted_by']}%"
                    query_params.extend([search_term, search_term])
                
                # Date range filters
                if filters.get('start_date'):
                    where_conditions.append(f"t.{config['date_col']} >= %s")
                    query_params.append(filters['start_date'])
                
                if filters.get('end_date'):
                    where_conditions.append(f"t.{config['date_col']} <= %s")
                    query_params.append(filters['end_date'])
                    
                # Property/Location filters (Only for reporte_incidente for now)
                if f_type == 'reporte_incidente':
                     if filters.get('property_id'):
                        where_conditions.append("p.id_propiedad = %s")
                        query_params.append(filters['property_id'])
                     elif filters.get('property'):
                        where_conditions.append("LOWER(t.cliente_instalacion) LIKE LOWER(%s)")
                        query_params.append(f"%{filters['property']}%")
                     
                     if filters.get('location'):
                        where_conditions.append("LOWER(t.puesto_area_especifica) LIKE LOWER(%s)")
                        query_params.append(f"%{filters['location']}%")

            where_clause = ""
            if where_conditions:
                where_clause = "WHERE " + " AND ".join(where_conditions)

            # Count Query
            count_query = f"SELECT COUNT(*) FROM {config['table']} t {config['joins']} {where_clause}"
            cur.execute(count_query, query_params)
            total_count += cur.fetchone()[0]

            # Data Query
            # Fetch enough rows to satisfy the global offset + limit
            fetch_limit = limit + offset
            query = f"""
                SELECT {config['columns']}
                FROM {config['table']} t
                {config['joins']}
                {where_clause}
                ORDER BY t.{config['date_col']} DESC NULLS LAST, t.{config['id_col']} DESC
                LIMIT %s
            """
            
            cur.execute(query, query_params + [fetch_limit])
            rows = cur.fetchall()
            
            for row_dict in rows:
                # Determine display name
                display_name = row_dict.get("user_name") or row_dict.get(config['user_col'], "desconocido")
                
                # Determine date
                date_val = row_dict.get(config['date_col'])
                if isinstance(date_val, datetime):
                    date_str = date_val.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    date_str = str(date_val) if date_val else "N/A"

                # Map data
                mapped_data = {}
                processed_cols = set() # Keep track of columns already processed

                # 1. Process explicit data_mapping first
                for label, col_name in config['data_mapping'].items():
                    val = row_dict.get(col_name)
                    processed_cols.add(col_name) # Mark as processed

                    # Handle GCS URLs signing for specific labels
                    if label == 'URLs de Imágenes o PDFs' and val:
                        signed_urls = []
                        for url in str(val).split('\n'):
                            url = url.strip()
                            if url:
                                signed_urls.append(generate_signed_url(url))
                        val = '\n'.join(signed_urls)
                    # Sign signatures if they are GCS URLs
                    elif 'firma' in col_name.lower() and val and isinstance(val, str) and 'storage.googleapis.com' in val:
                         val = generate_signed_url(val)

                    mapped_data[label] = val

                # 2. Add unmapped fields, filtering out system columns
                system_cols = {config['id_col'], config['date_col'], config['user_col'], 'user_name'}
                for col_name, val in row_dict.items():
                    if col_name not in processed_cols and col_name not in system_cols:
                        # Sign signatures if they are GCS URLs
                        if 'firma' in col_name.lower() and val and isinstance(val, str) and 'storage.googleapis.com' in val:
                             val = generate_signed_url(val)
                             
                        # Convert snake_case to Title Case for display
                        display_label = ' '.join(word.capitalize() for word in col_name.split('_'))
                        
                        # Handle Date/Time objects for JSON serialization
                        if isinstance(val, (datetime, date, time)):
                            val = str(val)
                            
                        mapped_data[display_label] = val
                
                report = {
                    "id": row_dict.get(config['id_col']),
                    "title": f"{config['title_prefix']} #{row_dict.get(config['id_col'])}",
                    "submittedBy": display_name,
                    "dateSubmitted": date_str,
                    "data": mapped_data,
                    "formType": f_type
                }
                all_reports.append(report)

        # Sort all reports by date descending
        all_reports.sort(key=lambda x: x['dateSubmitted'], reverse=True)
        
        # Apply pagination to the combined list
        start = offset
        end = offset + limit
        paginated_reports = all_reports[start:end]
        
        return paginated_reports, total_count

    except Exception as e:
        app_logger.error(f"Error fetching reports: {e}", exc_info=True)
        return [], 0
    finally:
        if conn:
            conn.close()

def fetch_reports_by_ids(report_ids, form_type='reporte_incidente', skip_signing=False):
    conn = None
    cur = None
    reports = []
    if not report_ids:
        app_logger.info("fetch_reports_by_ids received an empty report_ids list.")
        return []
        
    if form_type not in FORM_CONFIGS:
        app_logger.warning(f"Invalid form_type: {form_type}. Defaulting to reporte_incidente.")
        form_type = 'reporte_incidente'
        
    config = FORM_CONFIGS[form_type]

    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in fetch_reports_by_ids. Returning empty list.")
            return reports

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Log the raw report_ids before cleaning
        app_logger.info(f"Raw report_ids received by fetch_reports_by_ids: {report_ids}")

        clean_ids = [int(id) for id in report_ids if isinstance(id, (int, str)) and str(id).isdigit()]
        
        # Log the cleaned report_ids
        app_logger.info(f"Cleaned report_ids for fetch_reports_by_ids: {clean_ids}")

        if not clean_ids:
            app_logger.warning("After cleaning, report_ids list is empty. No reports to fetch.")
            return []

        placeholders = ','.join(['%s'] * len(clean_ids))
        # ORDER BY: Sort by creation date (newest reports first - most recent submissions at the top)
        query = f"""
            SELECT {config['columns']}
            FROM {config['table']} t
            {config['joins']}
            WHERE t.{config['id_col']} IN ({placeholders})
            ORDER BY t.{config['date_col']} DESC NULLS LAST, t.{config['id_col']} DESC
        """
        app_logger.info(f"Executing fetch_reports_by_ids query for IDs: {clean_ids} with form_type: {form_type}.")
        cur.execute(query, clean_ids)
        rows = cur.fetchall()
        app_logger.info(f"Fetched {len(rows)} specific reports.")
        
        for row_dict in rows:
            # Determine display name
            display_name = row_dict.get("user_name") or row_dict.get(config['user_col'], "desconocido")
            
            # Determine date
            date_val = row_dict.get(config['date_col'])
            if isinstance(date_val, datetime):
                date_str = date_val.strftime("%Y-%m-%d %H:%M:%S")
            else:
                date_str = str(date_val) if date_val else "N/A"

            # Map data fields
            data_content = {}
            processed_cols = set()
            
            # 1. Process explicit data_mapping first
            for label, col_name in config['data_mapping'].items():
                val = row_dict.get(col_name)
                processed_cols.add(col_name)
                
                # Generate signed URLs for image/pdf columns
                if val and (label == "URLs de Imágenes o PDFs" or col_name == 'foto_evidencia_url'):
                    if not skip_signing:
                        urls = str(val).split('\n')
                        signed_urls = []
                        for url in urls:
                            signed_urls.append(generate_signed_url(url.strip()))
                        val = '\n'.join(signed_urls)
                # Sign signatures if they are GCS URLs
                elif 'firma' in col_name.lower() and val and isinstance(val, str) and 'storage.googleapis.com' in val:
                     if not skip_signing:
                        val = generate_signed_url(val)

                data_content[label] = str(val) if val is not None else "N/A"

            # 2. Add unmapped fields, filtering out system columns
            system_cols = {config['id_col'], config['date_col'], config['user_col'], 'user_name'}
            for col_name, val in row_dict.items():
                if col_name not in processed_cols and col_name not in system_cols:
                    # Sign signatures if they are GCS URLs
                    if 'firma' in col_name.lower() and val and isinstance(val, str) and 'storage.googleapis.com' in val:
                         if not skip_signing:
                            val = generate_signed_url(val)
                         
                    # Convert snake_case to Title Case for display
                    display_label = ' '.join(word.capitalize() for word in col_name.split('_'))
                    
                    # Handle Date/Time objects for JSON serialization
                    if isinstance(val, (datetime, date, time)):
                        val = str(val)

                    data_content[display_label] = str(val) if val is not None else "N/A"

            forms_data = {
                "id": row_dict[config['id_col']],
                "title": f"{config['title_prefix']} #{row_dict[config['id_col']]}",
                "submittedBy": display_name,
                "dateSubmitted": date_str,
                "data": data_content,
                "formType": form_type
            }
            reports.append(forms_data)

    except psycopg2.Error as e:
        app_logger.error(f"PostgreSQL Error in fetch_reports_by_ids: {e}", exc_info=True)
        reports = []
    except Exception as e:
        app_logger.error(f"An unexpected error occurred in fetch_reports_by_ids: {e}", exc_info=True)
        reports = []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
            app_logger.info("Database connection closed in fetch_reports_by_ids.")
    return reports

def fetch_reports_mixed(items, skip_signing=False):
    """
    Fetches reports from multiple tables based on a list of item dicts.
    Each item in 'items' should be a dict with: {'id': int/str, 'formType': str}
    Returns a unified list of report objects.
    """
    if not items:
        return []

    # Group IDs by formType
    ids_by_type = {}
    for item in items:
        f_type = item.get('formType', 'reporte_incidente')
        r_id = item.get('id')
        
        if not r_id:
            continue
            
        if f_type not in ids_by_type:
            ids_by_type[f_type] = []
        ids_by_type[f_type].append(r_id)

    all_reports = []
    
    # Fetch for each type
    for f_type, ids in ids_by_type.items():
        if not ids:
            continue
        app_logger.info(f"fetch_reports_mixed: Fetching {len(ids)} reports of type {f_type}")
        type_reports = fetch_reports_by_ids(ids, form_type=f_type, skip_signing=skip_signing)
        all_reports.extend(type_reports)

    # Sort combined results by date (submitted at)
    # The 'dateSubmitted' field is a string, so sorting might be imperfect if format varies, but usually YYYY-MM-DD HH:MM:SS
    all_reports.sort(key=lambda x: str(x.get('dateSubmitted', '')), reverse=True)
    
    return all_reports

def send_reports_email(recipient_email, subject, body, is_html=False):
    # Retrieve email credentials - using provided values and Secret Manager for password
    _email_username = "no-reply@tzolkintech.com"
    _smtp_server = "tzolkintech.com"
    _smtp_port = 587
    # FIX: Retrieve password from Secret Manager using the provided secret name
    _email_password = get_secret(project_id, 'admin-email-pass') 

    if not all([_email_username, _email_password, _smtp_server, _smtp_port]):
        app_logger.error("Email sending skipped: Missing one or more email credentials or invalid port.")
        return False, "Missing or invalid email configuration."

    msg = MIMEMultipart()
    msg['From'] = _email_username
    msg['To'] = recipient_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'html' if is_html else 'plain'))

    app_logger.info(f"Attempting to send email to {recipient_email} via {_smtp_server}:{_smtp_port} from {_email_username}.")
    try:
        server = None
        context = ssl.create_default_context()

        if _smtp_port == 465: # SMTP_SSL for port 465
            server = smtplib.SMTP_SSL(_smtp_server, _smtp_port, context=context, timeout=10)
        else: # Standard SMTP with STARTTLS for other ports like 587
            server = smtplib.SMTP(_smtp_server, _smtp_port, timeout=10)
            server.starttls(context=context)

        server.login(_email_username, _email_password)
        server.send_message(msg)
        server.quit()
        app_logger.info(f"Email sent successfully to {recipient_email}.")
        return True, "Email sent successfully."

    except smtplib.SMTPAuthenticationError:
        app_logger.error(f"SMTP Authentication Error: Check email username and password for {_email_username}.", exc_info=True)
        return False, "Authentication failed. Check email credentials."
    except smtplib.SMTPServerDisconnected:
        app_logger.error(f"SMTP Server Disconnected: Server {_smtp_server}:{_smtp_port} disconnected unexpectedly.", exc_info=True)
        return False, "The email server is unavailable. Please try again later."
    except ConnectionRefusedError:
        app_logger.error(f"SMTP Connection Refused: Check SMTP_HOST, SMTP_PORT, and firewall rules for {_smtp_server}:{_smtp_port}.", exc_info=True)
        return False, "Connection refused by the email server."
    except TimeoutError:
        app_logger.error(f"SMTP Connection Timeout: Could not connect to {_smtp_server}:{_smtp_port}. Check network connectivity and firewall rules.", exc_info=True)
        return False, "Connection timed out with the email server."
    except Exception as e:
        app_logger.error(f"An unexpected error occurred while sending email to {recipient_email}: {e}", exc_info=True)
        return False, f"An error occurred while sending email: {e}"

def admin_required(f):
    """
    Decorator that requires the user to be an admin.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            # Get JWT claims
            claims = get_jwt()
            is_admin = claims.get('is_admin', False)
            user_email = claims.get('sub', 'unknown')
            
            app_logger.info(f"Admin check: User {user_email}, is_admin={is_admin}")
            
            if not is_admin:
                app_logger.warning(f"Access denied: User {user_email} attempted to access admin-only resource")
                
                # Check if this is an API request or web request
                if request.path.startswith('/api/') or (request.accept_mimetypes and request.accept_mimetypes.accept_json):
                    return jsonify({
                        "error": "Access denied", 
                        "message": "Solo los administradores pueden acceder a este recurso."
                    }), 403
                else:
                    # Redirect to landing page
                    landing_url = '/landing/'
                    app_logger.info(f"Redirecting non-admin user to: {landing_url}")
                    return redirect(landing_url)
            
            app_logger.info(f"Admin access granted to {user_email} for {request.endpoint}")
            return f(*args, **kwargs)
            
        except Exception as e:
            app_logger.error(f"Error in admin_required decorator: {e}", exc_info=True)
            
            # Return error response
            if request.path.startswith('/api/') or (request.accept_mimetypes and request.accept_mimetypes.accept_json):
                return jsonify({"error": "Authentication error", "details": str(e)}), 500
            else:
                login_url = '/'
                return redirect(login_url)
    
    return decorated_function

# --- Routes ---
@viewer_bp.route('/')
@jwt_required()
@admin_required
def index():
    user_email = get_jwt_identity()
    
    # Get JWT claims and admin status
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
        app_logger.info(f"Admin user {user_email} (is_admin={is_admin}) accessing viewer dashboard")
    except Exception as e:
        app_logger.error(f"Error getting JWT claims: {e}", exc_info=True)
        user_name = user_email.split('@')[0]
        is_admin = False
    
    # Get user name from database as fallback
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute('SELECT "name" FROM "users" WHERE email = %s', (user_email,))
            user_row = cur.fetchone()
            if user_row and user_row[0]:
                user_name = user_row[0]
                app_logger.info(f"User found in DB: {user_name}")
    except Exception as e:
        app_logger.error(f"Error fetching user name: {e}", exc_info=True)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    initial_reports, total_reports_count = fetch_reports(offset=0, limit=10)
    return render_template("index.html", 
                         current_user=user_email, 
                         forms=initial_reports, 
                         user_name=user_name,
                         is_admin=is_admin,  # Pass admin status to template
                         total_reports=total_reports_count,
                         login_service_url='/',
                         landing_service_url='/landing/',
                         forms_service_url='/forms',
                         dashboard_service_url='/dashboard')

@viewer_bp.route('/api/properties', methods=['GET'])
@jwt_required()
def get_properties():
    """Get all properties from the database"""
    conn = None
    cur = None
    properties = []
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_properties.")
            return jsonify({"error": "Database connection failed", "properties": []}), 500

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        query = """
            SELECT DISTINCT id_propiedad, nombre 
            FROM propiedades 
            WHERE nombre IS NOT NULL 
            ORDER BY nombre
        """
        
        app_logger.info("Fetching properties from database")
        cur.execute(query)
        rows = cur.fetchall()
        
        for row in rows:
            properties.append({
                "id": row["id_propiedad"],
                "name": row["nombre"]
            })
        
        app_logger.info(f"Retrieved {len(properties)} properties")
        return jsonify({"properties": properties}), 200
        
    except psycopg2.Error as e:
        app_logger.error(f"PostgreSQL Error in get_properties: {e}", exc_info=True)
        return jsonify({"error": f"Database error: {str(e)}", "properties": []}), 500
    except Exception as e:
        app_logger.error(f"Unexpected error in get_properties: {e}", exc_info=True)
        return jsonify({"error": f"Server error: {str(e)}", "properties": []}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@viewer_bp.route('/api/locations', methods=['GET'])
@jwt_required()
def get_locations():
    """Get locations, optionally filtered by property"""
    property_id = request.args.get('property_id', type=int)
    
    conn = None
    cur = None
    locations = []
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_locations.")
            return jsonify({"error": "Database connection failed", "locations": []}), 500

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        if property_id:
            # Get locations for specific property by checking reports
            query = """
                SELECT DISTINCT li.id_lugar_incidente, li.nombre 
                FROM lugar_incidente li
                INNER JOIN reportes_incidentes ri ON li.id_lugar_incidente = ri.id_lugar_incidente
                WHERE ri.id_propiedad = %s AND li.nombre IS NOT NULL
                ORDER BY li.nombre
            """
            cur.execute(query, (property_id,))
        else:
            # Get all locations
            query = """
                SELECT DISTINCT id_lugar_incidente, nombre 
                FROM lugar_incidente 
                WHERE nombre IS NOT NULL 
                ORDER BY nombre
            """
            cur.execute(query)
        
        rows = cur.fetchall()
        
        for row in rows:
            locations.append({
                "id": row["id_lugar_incidente"],
                "name": row["nombre"]
            })
        
        app_logger.info(f"Retrieved {len(locations)} locations for property_id: {property_id}")
        return jsonify({"locations": locations}), 200
        
    except psycopg2.Error as e:
        app_logger.error(f"PostgreSQL Error in get_locations: {e}", exc_info=True)
        return jsonify({"error": f"Database error: {str(e)}", "locations": []}), 500
    except Exception as e:
        app_logger.error(f"Unexpected error in get_locations: {e}", exc_info=True)
        return jsonify({"error": f"Server error: {str(e)}", "locations": []}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@viewer_bp.route('/api/reports', methods=['GET'])
@jwt_required()
def get_more_reports():
    offset = request.args.get('offset', type=int, default=0)
    limit = request.args.get('limit', type=int, default=10)
    ids_only = request.args.get('ids_only', type=str, default='false').lower() == 'true'

    if offset < 0:
        offset = 0
    
    # Allow unlimited limit when ids_only is true, otherwise cap at 100
    if not ids_only:
        if limit <= 0 or limit > 100:
            limit = 10

    # Extract filter parameters
    filters = {}
    
    # Report ID filter
    if request.args.get('report_id'):
        filters['report_id'] = request.args.get('report_id')

    # Property filters (ID takes precedence over name)
    if request.args.get('property_id'):
        filters['property_id'] = request.args.get('property_id')
    elif request.args.get('property'):
        filters['property'] = request.args.get('property')
    
    # Location filters (ID takes precedence over name)
    if request.args.get('location_id'):
        filters['location_id'] = request.args.get('location_id')
    elif request.args.get('location'):
        filters['location'] = request.args.get('location')
    
    # Other filters
    if request.args.get('submitted_by'):
        filters['submitted_by'] = request.args.get('submitted_by')
    if request.args.get('start_date'):
        filters['start_date'] = request.args.get('start_date')
    if request.args.get('end_date'):
        filters['end_date'] = request.args.get('end_date')

    # Form Type Filter
    form_type = request.args.get('form_type', 'all')

    app_logger.info(f"API request with filters: {filters}, form_type: {form_type}, ids_only: {ids_only}")
    
    try:
        reports, total_count = fetch_reports(offset, limit, filters if filters else None, form_type=form_type)
        
        # If ids_only is true, return simplified response with just IDs and form types
        if ids_only:
            report_ids = [{'id': r['id'], 'formType': r['formType']} for r in reports]
            return jsonify({"report_ids": report_ids, "total_count": total_count})
        
        return jsonify({"reports": reports, "total_count": total_count})
    except psycopg2.Error as e:
        app_logger.error(f"Database error in get_more_reports: {e}", exc_info=True)
        return jsonify({"error": f"Database error: {str(e)}", "reports": [], "total_count": 0}), 500
    except Exception as e:
        app_logger.error(f"Server error in get_more_reports: {e}", exc_info=True)
        return jsonify({"error": f"Server error: {str(e)}", "reports": [], "total_count": 0}), 500

# New route to handle fetching a single report by ID, assumed to be used by "Ver Detalles"
@viewer_bp.route('/api/report/<int:report_id>', methods=['GET'])
@jwt_required()
def get_single_report(report_id):
    form_type = request.args.get('form_type', 'reporte_incidente')
    app_logger.info(f"Attempting to fetch single report with ID: {report_id} via GET /api/report/<id> with form_type: {form_type}")
    # fetch_reports_by_ids expects a list of IDs
    reports = fetch_reports_by_ids([report_id], form_type=form_type)
    
    if reports:
        app_logger.info(f"Successfully fetched report {report_id} for details.")
        return jsonify(reports[0]), 200
    else:
        app_logger.warning(f"Report with ID {report_id} not found for details.")
        return jsonify({"success": False, "message": f"Report with ID {report_id} not found."}), 404


@viewer_bp.route('/api/email-reports', methods=['POST'])
@jwt_required()
def email_selected_reports_api():
    user_email = get_jwt_identity()
    data = request.get_json()
    requests_payload = data.get('reports') # New format: list of {id, formType}
    report_ids = data.get('report_ids') # Old format: list of ids (ints)

    if not requests_payload and not report_ids:
        return jsonify({"success": False, "message": "No reports provided."}), 400
        
    if not recipient_email or not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", recipient_email):
        return jsonify({"success": False, "message": "Invalid recipient email address."}), 400

    app_logger.info(f"User {user_email} requested to email reports to {recipient_email}")

    reports_to_email = []
    if requests_payload:
        reports_to_email = fetch_reports_mixed(requests_payload)
    elif report_ids:
        # Fallback for backward compatibility
        reports_to_email = fetch_reports_by_ids(report_ids)

    if not reports_to_email:
        app_logger.warning(f"No reports found for the provided items during email request.")
        return jsonify({"success": False, "message": "No reports found for the provided IDs."}), 404

    subject = f"Reportes de Incidencias Seleccionados ({len(reports_to_email)} Reportes)"
    
    html_body_parts = [
        f"<html><body style='font-family: Arial, sans-serif; color: #333;'>",
        f"<div style='max-width: 600px; margin: 0 auto; padding: 20px;'>",
        f"<h2 style='color: #2563eb;'>Reportes de Incidencias Seleccionados - Kanan SecApp</h2>",
        f"<p>Hola,</p>",
        f"<p>Adjuntos se encuentran los detalles de los reportes de incidencias seleccionados:</p>"
    ]

    # Group reports by formType
    reports_by_type = {}
    for report in reports_to_email: # Changed from 'reports' to 'reports_to_email'
        f_type = report.get('formType', 'reporte_incidente')
        if f_type not in reports_by_type:
            reports_by_type[f_type] = []
        reports_by_type[f_type].append(report)

    for f_type, type_reports in reports_by_type.items():
        config = FORM_CONFIGS.get(f_type)
        if not config:
            continue
            
        title = config.get('title_prefix', f_type)
        html_body_parts.append(f"<h2>{title}s</h2>") # Changed from html_parts to html_body_parts

        for report in type_reports:
            html_body_parts.append(f"<div class='report-container'>") # Changed from html_parts to html_body_parts
            html_body_parts.append(f"<div class='report-header'>") # Changed from html_parts to html_body_parts
            html_body_parts.append(f"<h2>{report['title']}</h2>") # Changed from html_parts to html_body_parts
            html_body_parts.append(f"<p><strong>Enviado por:</strong> {report['submittedBy']} | <strong>Fecha:</strong> {report['dateSubmitted']}</p>") # Changed from html_parts to html_body_parts
            html_body_parts.append(f"</div>") # Changed from html_parts to html_body_parts
            
            html_body_parts.append(f"<div class='report-body'>") # Changed from html_parts to html_body_parts
            
            # Dynamic fields based on config
            for label, col_name in config['data_mapping'].items():
                # Get value from report data using label as key
                value = report['data'].get(label)
                display_value = value if value and str(value).strip() != 'N/A' and str(value).strip() != 'None' else 'No especificado'
                
                # Handle Attachments
                if label == 'URLs de Imágenes o PDFs' and display_value != 'No especificado':
                    urls = str(display_value).split('\n')
                    valid_urls = [u.strip() for u in urls if u.strip()]
                    
                    if valid_urls:
                        html_body_parts.append(f"<p><strong>Archivos Adjuntos:</strong></p><div style='display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px;'>") # Changed from html_parts to html_body_parts
                        for url in valid_urls:
                            # Generate signed URL for PDF/Email if needed (though PDF generation happens server side, so signed URL is good)
                            # Note: fetch_reports_by_ids already signed them
                            
                            lower_url = url.lower()
                            filename = url.split('?')[0].split('/')[-1]
                            
                            if lower_url.endswith(('.jpeg', '.jpg', '.png', '.gif', '.webp')):
                                html_body_parts.append(f"""
                                    <div style='margin-bottom: 10px;'>
                                        <a href="{url}" target="_blank" style="text-decoration: none;">
                                            <img src="{url}" alt="Imagen" style="max-width: 200px; height: auto; border-radius: 4px; border: 1px solid #ccc;">
                                        </a>
                                    </div>
                                """)
                            else:
                                html_body_parts.append(f"""
                                    <div style='margin-bottom: 10px;'>
                                        <p style="margin: 0;">Archivo: <a href="{url}" target="_blank" style="color: #2563eb; text-decoration: none;">{filename}</a></p>
                                    </div>
                                """)
                        html_body_parts.append(f"</div>") # Changed from html_parts to html_body_parts
                else:
                    html_body_parts.append(f"<p><strong>{label}:</strong> {display_value}</p>") # Changed from html_parts to html_body_parts
            
            html_body_parts.append(f"</div></div>") # Close body and container # Changed from html_parts to html_body_parts

    html_body_parts.append(f"<p style='margin-top: 20px;'>Generado por {user_email} desde Kanan SecApp.</p>")
    html_body_parts.append(f"</div></body></html>")
    email_html_body = "\n".join(html_body_parts)

    success, message = send_reports_email(recipient_email, subject, email_html_body, is_html=True)

    if success:
        return jsonify({"success": True, "message": "Reportes enviados por correo electrónico exitosamente!"}), 200
    else:
        app_logger.error(f"Failed to send email: {message}")
        return jsonify({"success": False, "message": f"Error al enviar correo electrónico: {message}"}), 500


@viewer_bp.route('/api/export-excel', methods=['POST'])
@jwt_required()
def export_excel():
    """Export selected reports to Excel format"""
    user_email = get_jwt_identity()
    data = request.get_json()
    requests_payload = data.get('reports') # New format: list of {id, formType}
    report_ids = data.get('report_ids')

    if not requests_payload and not report_ids:
        return jsonify({"success": False, "message": "No reports provided."}), 400

    app_logger.info(f"User {user_email} requested Excel export")

    try:
        # Import openpyxl for Excel generation
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
            from openpyxl.utils import get_column_letter
        except ImportError:
            app_logger.error("openpyxl not installed. Please install with: pip install openpyxl")
            return jsonify({"success": False, "message": "Excel export not available. Missing required dependencies."}), 500

        # Fetch the reports
        reports_to_export = []
        if requests_payload:
             reports_to_export = fetch_reports_mixed(requests_payload, skip_signing=True)
        elif report_ids:
             reports_to_export = fetch_reports_by_ids(report_ids, skip_signing=True)

        if not reports_to_export:
            app_logger.warning(f"No reports found for the provided IDs during Excel export.")
            return jsonify({"success": False, "message": "No reports found for the provided IDs."}), 404

        # Create Excel workbook
        wb = Workbook()
        # Remove default sheet
        default_ws = wb.active
        wb.remove(default_ws)

        # Define styles
        header_font = Font(bold=True, color="FFFFFF", size=12)
        header_fill = PatternFill(start_color="1D4ED8", end_color="1D4ED8", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        border = Border(
            left=Side(style="thin"),
            right=Side(style="thin"), 
            top=Side(style="thin"),
            bottom=Side(style="thin")
        )

        # Group reports by formType
        reports_by_type = {}
        for report in reports_to_export:
            f_type = report.get('formType', 'reporte_incidente')
            if f_type not in reports_by_type:
                reports_by_type[f_type] = []
            reports_by_type[f_type].append(report)

        # Create a sheet for each form type
        for f_type, type_reports in reports_by_type.items():
            # Get config for this form type to determine headers
            config = FORM_CONFIGS.get(f_type)
            if not config:
                app_logger.warning(f"No configuration found for form type: {f_type}. Skipping.")
                continue

            # Create sheet
            sheet_title = config.get('title_prefix', f_type)[:30] # Excel sheet names max 31 chars
            ws = wb.create_sheet(title=sheet_title)

            # Define headers dynamically based on data_mapping
            # Standard headers first
            headers = ["ID Reporte", "Enviado Por", "Fecha Envío"]
            
            # Dynamic headers from mapping
            dynamic_headers = list(config['data_mapping'].keys())
            headers.extend(dynamic_headers)

            # Write headers
            for col, header in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col, value=header)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_alignment
                cell.border = border

            # Write data
            for row, report in enumerate(type_reports, 2):
                # Standard data
                ws.cell(row=row, column=1, value=report['id']).border = border
                ws.cell(row=row, column=2, value=report['submittedBy']).border = border
                ws.cell(row=row, column=3, value=report['dateSubmitted']).border = border

                # Dynamic data
                max_row_height = 25 # Default height
                
                for i, header_key in enumerate(dynamic_headers):
                    # Map header key to data key using config
                    data_key = header_key # The keys in report['data'] match the keys in data_mapping (labels)
                    val = report['data'].get(data_key, '')
                    
                    col_index = 4 + i
                    cell = ws.cell(row=row, column=col_index, value=str(val) if val else '')
                    cell.border = border
                    cell.alignment = Alignment(wrap_text=True, vertical="top")

                    # Image Embedding Logic
                    is_image_field = any(keyword in header_key.lower() for keyword in ['firma', 'foto', 'evidencia', 'diagrama', 'imagen'])
                    
                    if is_image_field and val and isinstance(val, str):
                        try:
                            from openpyxl.drawing.image import Image as OpenpyxlImage
                            from PIL import Image as PILImage
                            import base64
                            
                            img_file = None
                            
                            # Case A: Base64 Data URI
                            if val.strip().startswith('data:image'):
                                try:
                                    # Format: data:image/png;base64,.....
                                    header, encoded = val.strip().split(',', 1)
                                    img_bytes = base64.b64decode(encoded)
                                    
                                    # Convert to PNG if needed (for WebP or other formats)
                                    pil_img = PILImage.open(BytesIO(img_bytes))
                                    if pil_img.format not in ['PNG', 'JPEG', 'JPG', 'GIF', 'BMP']:
                                        png_buffer = BytesIO()
                                        pil_img.convert('RGB').save(png_buffer, format='PNG')
                                        png_buffer.seek(0)
                                        img_file = png_buffer
                                    else:
                                        img_file = BytesIO(img_bytes)
                                    
                                    cell.value = "Firma Digital" # Set friendly text
                                    cell.hyperlink = None # No external link for base64
                                except Exception as e:
                                    app_logger.error(f"Error processing base64 image: {e}")
                                    
                            # Case B: URL (HTTP/GCS)
                            elif 'http' in val or 'storage.googleapis.com' in val:
                                # Handle multiple images (newlines)
                                urls = val.split('\n')
                                
                                valid_url = None
                                for u in urls:
                                    if u.strip():
                                        valid_url = u.strip()
                                        break
                                
                                if valid_url:
                                    # Just set the hyperlink, do not attempt to download or embed
                                    cell.value = "Ver Imagen Original"
                                    cell.hyperlink = valid_url
                                    cell.style = "Hyperlink"

                            # If we successfully got an image file, resize and place it
                            if img_file:
                                img = OpenpyxlImage(img_file)
                                
                                # Resize image (thumbnail)
                                # Target height ~80px
                                aspect_ratio = img.width / img.height
                                new_height = 80
                                new_width = int(new_height * aspect_ratio)
                                
                                img.height = new_height
                                img.width = new_width
                                
                                # Anchor to cell
                                col_letter = get_column_letter(col_index)
                                cell_address = f"{col_letter}{row}"
                                img.anchor = cell_address
                                
                                ws.add_image(img)
                                
                                # Update max row height needed
                                max_row_height = max(max_row_height, 90)

                        except Exception as e:
                            app_logger.error(f"Error embedding image for field {header_key}: {e}")
                            # Keep original text value
                            pass

                # Set row height
                ws.row_dimensions[row].height = max_row_height

            # Auto-adjust column widths
            for col in range(1, len(headers) + 1):
                column_letter = get_column_letter(col)
                max_length = 0
                # Check header length
                max_length = max(max_length, len(headers[col-1]))
                # Check data lengths (sample first 50 rows)
                for row in range(2, min(52, ws.max_row + 1)):
                    cell_value = ws[f"{column_letter}{row}"].value
                    if cell_value:
                        max_length = max(max_length, len(str(cell_value)))
                
                adjusted_width = min(max(max_length + 2, 10), 50)
                ws.column_dimensions[column_letter].width = adjusted_width
            
            # Set header row height
            ws.row_dimensions[1].height = 25

        # Set row height for header
        ws.row_dimensions[1].height = 25

        # Create filename
        custom_filename = data.get('filename')
        if custom_filename:
            # Sanitize filename
            custom_filename = re.sub(r'[^\w\-_]', '', custom_filename)
            filename = f"{custom_filename}.xlsx"
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"reportes_incidencias_{timestamp}.xlsx"

        # Save to BytesIO
        excel_buffer = BytesIO()
        wb.save(excel_buffer)
        excel_buffer.seek(0)

        app_logger.info(f"Excel file generated successfully for {len(reports_to_export)} reports")

        return send_file(
            excel_buffer,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )

    except Exception as e:
        app_logger.error(f"Error generating Excel file: {e}", exc_info=True)
        return jsonify({"success": False, "message": f"Error generating Excel file: {str(e)}"}), 500

@viewer_bp.route('/api/generate-pdf', methods=['POST'])
@jwt_required()
def generate_pdf():
    """Generate PDF for selected reports using WeasyPrint"""
    user_email = get_jwt_identity()
    data = request.get_json()
    requests_payload = data.get('reports') # New format: list of {id, formType}
    report_ids = data.get('report_ids')

    if not requests_payload and not report_ids:
        return jsonify({"success": False, "message": "No reports provided."}), 400

    app_logger.info(f"User {user_email} requested PDF generation")

    try:
        # Fetch the reports
        reports_to_pdf = []
        if requests_payload:
            reports_to_pdf = fetch_reports_mixed(requests_payload)
        elif report_ids:
            reports_to_pdf = fetch_reports_by_ids(report_ids)

        if not reports_to_pdf:
            app_logger.warning(f"No reports found for the provided IDs during PDF request.")
            return jsonify({"success": False, "message": "No reports found for the provided IDs."}), 404

        # Generate HTML content for PDF
        html_content = generate_reports_html(reports_to_pdf)
        
        # Create PDF using WeasyPrint
        pdf_buffer = BytesIO()
        if WEASYPRINT_AVAILABLE:
            HTML(string=html_content).write_pdf(pdf_buffer)
        else:
            pdf_buffer.write(b"%PDF-1.4\n%Mock PDF\n1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n2 0 obj <</Type /Pages /Kids [] /Count 0>> endobj\nxref\n0 3\n0000000000 65535 f \n0000000021 00000 n \n0000000071 00000 n \ntrailer <</Size 3 /Root 1 0 R>>\nstartxref\n120\n%%EOF")
        pdf_buffer.seek(0)

        # Create filename
        custom_filename = data.get('filename')
        if custom_filename:
            # Sanitize filename
            custom_filename = re.sub(r'[^\w\-_]', '', custom_filename)
            filename = f"{custom_filename}.pdf"
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"reportes_incidencias_{timestamp}.pdf"

        app_logger.info(f"PDF generated successfully for {len(reports_to_pdf)} reports using WeasyPrint")

        return send_file(
            pdf_buffer,
            as_attachment=True,
            download_name=filename,
            mimetype='application/pdf'
        )

    except Exception as e:
        app_logger.error(f"Error generating PDF: {e}", exc_info=True)
        return jsonify({"success": False, "message": f"Error generating PDF: {str(e)}"}), 500


def generate_reports_html(reports):
    """Generate HTML content for PDF generation - matching print layout"""
    html_parts = ["""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <style>
            body {
                font-family: 'Roboto', Arial, sans-serif;
                margin: 40px;
                color: #333;
                line-height: 1.6;
                font-size: 12pt;
            }
            .header {
                text-align: center;
                margin-bottom: 40px;
                border-bottom: 2px solid #1d4ed8;
                padding-bottom: 20px;
            }
            .header h1 {
                color: #1d4ed8;
                font-size: 24px;
                margin: 0;
                font-family: 'Merriweather', serif;
            }
            .report-block {
                page-break-before: always;
                margin-bottom: 2rem;
                padding: 1rem;
                background: white;
                border: 1px solid #ddd;
            }
            .report-block:first-child {
                page-break-before: avoid;
            }
            .report-header {
                margin-bottom: 1rem;
            }
            .report-title {
                font-size: 16pt;
                font-weight: bold;
                color: #212529;
                margin-bottom: 0.5rem;
                font-family: 'Merriweather', serif;
            }
            .report-meta {
                color: #666;
                font-size: 11pt;
                margin-bottom: 1rem;
            }
            .report-summary {
                margin-bottom: 1rem;
                padding-bottom: 1rem;
                border-bottom: 1px solid #eee;
            }
            .report-summary p {
                margin-bottom: 0.5rem;
                color: #212529;
                font-size: 11pt;
            }
            .report-details {
                margin-top: 1rem;
                padding-top: 1rem;
                border-top: 1px solid #eee;
            }
            .report-details ul {
                list-style-type: none;
                padding: 0;
                margin: 0;
            }
            .report-details li {
                margin-bottom: 0.5rem;
                padding: 0;
                font-size: 11pt;
                color: #212529;
            }
            .report-details strong {
                font-weight: bold;
                color: #212529;
            }
            .attachment-section {
                margin: 1rem 0;
            }
            .attachment-grid {
                display: flex;
                flex-wrap: wrap;
                gap: 10px;
                margin-top: 10px;
            }
            .attachment-item {
                margin-bottom: 10px;
                text-align: center;
            }
            .attachment-item img {
                max-width: 200px;
                max-height: 200px;
                object-fit: contain;
                border-radius: 4px;
                border: 1px solid #ccc;
                page-break-inside: avoid;
            }
            .attachment-item p {
                font-size: 10pt;
                color: #555;
                margin-top: 5px;
                margin-bottom: 0;
            }
            .pdf-link {
                color: #2563eb;
                text-decoration: none;
                font-size: 11pt;
            }
            .pdf-link:hover {
                text-decoration: underline;
            }
            @page {
                margin: 2cm;
                @bottom-center {
                    content: "Página " counter(page) " - Kanan SecApp";
                    font-size: 10px;
                    color: #666;
                }
            }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>Reportes de Incidencias - Kanan SecApp</h1>
            <p style="margin: 10px 0 0 0; color: #666;">Generado el """ + datetime.now().strftime("%d/%m/%Y a las %H:%M") + """</p>
        </div>
    """]

    for i, report in enumerate(reports):
        html_parts.append(f"""
        <div class="report-block">
            <div class="report-header">
                <h2 class="report-title">{report['title']}</h2>
                <p class="report-meta">Enviado por: {report['submittedBy']} el {report['dateSubmitted']}</p>
            </div>
            
            <div class="report-summary">
                <p><strong>Título de Incidencia:</strong> {report['data'].get('Título de Incidencia', 'No especificado')}</p>
                <p><strong>Lugar del Incidente:</strong> {report['data'].get('Lugar del Incidente', 'No especificado')}</p>
                <p><strong>Fecha del Incidente:</strong> {report['data'].get('Fecha del Incidente', 'No especificado')}</p>
            </div>
            
            <div class="report-details">
                <ul>
        """)

        # Add all report data except URLs (we'll handle those separately)
        for key, value in report['data'].items():
            if value and str(value).strip() not in ['N/A', 'None', ''] and key != 'URLs de Imágenes o PDFs':
                clean_value = str(value).replace('\n', '<br>')
                html_parts.append(f"""
                    <li><strong>{key}:</strong> {clean_value}</li>
                """)

        html_parts.append("""
                </ul>
        """)

        # Handle attachments separately
        if report['data'].get('URLs de Imágenes o PDFs') and str(report['data']['URLs de Imágenes o PDFs']).strip() not in ['N/A', 'None', '']:
            urls = str(report['data']['URLs de Imágenes o PDFs']).split('\n')
            image_urls = []
            pdf_urls = []
            other_urls = []
            
            for url in urls:
                url = url.strip()
                if url:
                    lower_url = url.lower()
                    filename = os.path.basename(url)
                    if lower_url.endswith(('.jpeg', '.jpg', '.png', '.gif', '.webp')):
                        image_urls.append(url)
                    elif lower_url.endswith('.pdf'):
                        pdf_urls.append(url)
                    else:
                        other_urls.append(url)
            
            if image_urls or pdf_urls or other_urls:
                html_parts.append("""
                <div class="attachment-section">
                    <strong>Archivos Adjuntos:</strong>
                    <div class="attachment-grid">
                """)
                
                # Add images
                for url in image_urls:
                    filename = url.split('/')[-1] if '/' in url else url
                    html_parts.append(f"""
                        <div class="attachment-item">
                            <img src="{url}" alt="Imagen del reporte">
                            <p>{filename}</p>
                        </div>
                    """)
                
                # Add PDF links
                for url in pdf_urls:
                    filename = url.split('/')[-1] if '/' in url else url
                    html_parts.append(f"""
                        <div class="attachment-item">
                            <p>PDF: <a href="{url}" class="pdf-link">{filename}</a></p>
                        </div>
                    """)
                
                # Add other file links
                for url in other_urls:
                    filename = url.split('/')[-1] if '/' in url else url
                    html_parts.append(f"""
                        <div class="attachment-item">
                            <p>Archivo: <a href="{url}" class="pdf-link">{filename}</a></p>
                        </div>
                    """)
                
                html_parts.append("""
                    </div>
                </div>
                """)

        html_parts.append("""
            </div>
        </div>
        """)

    html_parts.append("""
    </body>
    </html>
    """)

    return ''.join(html_parts)


@viewer_bp.route('/logout')
def logout():
    try:
        app_logger.info("User logout requested")
        response = redirect('/')
        unset_jwt_cookies(response)
        app_logger.info("JWT cookies cleared, redirecting to login service")
        return response
    except Exception as e:
        app_logger.error(f"Error during logout: {e}", exc_info=True)
        # Fallback: just redirect without cookie clearing if there's an error
        return redirect('/')

# Viewer routes initialized