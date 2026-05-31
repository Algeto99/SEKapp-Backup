import os
import sys
import logging
import re
from datetime import timedelta, datetime, timezone
import calendar

from flask import Blueprint, current_app, Flask, render_template, request, jsonify, Response, flash, session, redirect, url_for
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity, get_jwt, unset_jwt_cookies
from flask_cors import CORS
from google.cloud import secretmanager, storage as gcs_storage
from google.api_core.exceptions import NotFound

import google.auth.transport.requests
import google.oauth2.id_token
import requests

import psycopg2
from psycopg2 import extras
import urllib.parse as urlparse
from functools import wraps

# --- Configure Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
app_logger = logging.getLogger(__name__)

_GCS_BUCKET_NAME = 'smt-uploads'

def _upload_file_to_gcs(file_storage):
    """Upload a werkzeug FileStorage to GCS and return the public URL."""
    if not file_storage or not file_storage.filename:
        return None
    import uuid
    from werkzeug.utils import secure_filename
    try:
        client = gcs_storage.Client()
        bucket = client.bucket(_GCS_BUCKET_NAME)
        unique_name = f"{uuid.uuid4()}_{secure_filename(file_storage.filename)}"
        blob = bucket.blob(unique_name)
        blob.upload_from_file(file_storage, content_type=file_storage.content_type)
        return f"https://storage.googleapis.com/{_GCS_BUCKET_NAME}/{unique_name}"
    except Exception as e:
        app_logger.error(f"_upload_file_to_gcs error: {e}", exc_info=True)
        return None

# --- Initialize Flask App ---
dashboard_bp = Blueprint("dashboard_bp", __name__)
is_production = os.environ.get('K_SERVICE') is not None
app_logger.info(f"Starting Dashboard Service in {'production' if is_production else 'development'} mode")

# In the monolith, main app.py handles Secrets and JWT configuration.

# DB Config
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    app_logger.warning("DATABASE_URL environment variable is not set. Database connections will fail.")

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


INCIDENT_DATE_EXPR = "CAST(COALESCE(ri.fecha_hora, ri.creado_en) AS date)"
INCIDENT_TIME_EXPR = "CAST(ri.fecha_hora AS time)"
INCIDENT_TYPE_EXPR = "COALESCE(NULLIF(TRIM(ri.tipo_incidente), ''), 'Sin Tipo')"
INCIDENT_CLIENT_EXPR = "COALESCE(NULLIF(TRIM(ri.cliente_instalacion), ''), '')"
INCIDENT_LOCATION_EXPR = "COALESCE(NULLIF(TRIM(ri.puesto_area_especifica), ''), '')"
INCIDENT_SUPERVISOR_EXPR = "COALESCE(NULLIF(TRIM(ri.nombre_responsable), ''), '')"
INCIDENT_ORDER_EXPR = "COALESCE(ri.fecha_hora, ri.creado_en) DESC NULLS LAST, ri.id_reporte_incidente DESC"


def _build_incident_select():
    return f"""
            ri.id_reporte_incidente,
            {INCIDENT_DATE_EXPR} AS fecha_incidente,
            {INCIDENT_TIME_EXPR} AS hora_incidente,
            ri.descripcion_incidente,
            ''::text AS nombre_persona,
            ri.user_email,
            ''::text AS telefono_persona,
            ''::text AS numero_identidad_persona,
            ''::text AS numero_local,
            p.direccion AS direccion,
            NULL::numeric AS valor_aproximado,
            ri.creado_en,
            ''::text AS pertenencias_sustraidas,
            ''::text AS descripcion_zona_comun,
            ri.foto_evidencia_url AS imagenes_pdfs,
            COALESCE(p.nombre, ri.cliente_instalacion) AS propiedad_nombre,
            p.direccion AS propiedad_direccion,
            p.descripcion AS propiedad_descripcion,
            {INCIDENT_TYPE_EXPR} AS tipo_incidencia,
            {INCIDENT_CLIENT_EXPR} AS tipo_cliente,
            {INCIDENT_LOCATION_EXPR} AS lugar_incidente,
            {INCIDENT_SUPERVISOR_EXPR} AS supervisor_name
    """


def _serialize_incident_report_row(row):
    return {
        'id_reporte': row['id_reporte_incidente'],
        'fecha_incidente': row['fecha_incidente'].strftime('%Y-%m-%d') if row['fecha_incidente'] else '',
        'hora_incidente': str(row['hora_incidente']) if row['hora_incidente'] else '',
        'descripcion_incidente': row['descripcion_incidente'] or '',
        'nombre_reportante': row['user_email'] or '',
        'email_reportante': row['user_email'] or '',
        'telefono_reportante': row['telefono_persona'] or '',
        'nombre_usuario_afectado': row['nombre_persona'] or '',
        'cedula_usuario_afectado': row['numero_identidad_persona'] or '',
        'numero_apartamento': row['numero_local'] or '',
        'propiedad_nombre': row['propiedad_nombre'] or '',
        'tipo_incidencia': row['tipo_incidencia'] or '',
        'tipo_cliente': row['tipo_cliente'] or '',
        'lugar_incidente': row['lugar_incidente'] or '',
        'supervisor_name': row['supervisor_name'] or '',
        'valor_aproximado': float(row['valor_aproximado']) if row['valor_aproximado'] else 0.0,
        'direccion': row['direccion'] or ''
    }

def get_properties():
    """Get all available properties"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_properties.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        query = """
            SELECT id_propiedad, nombre
            FROM propiedades
            WHERE COALESCE(activa, TRUE) = TRUE
            ORDER BY nombre;
        """
        
        cur.execute(query)
        rows = cur.fetchall()
        
        properties = []
        for row in rows:
            properties.append({
                'id': row['id_propiedad'],
                'name': row['nombre']
            })
        
        return properties
        
    except Exception as e:
        app_logger.error(f"Error in get_properties: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_report_details(report_id):
    """Get detailed information for a specific report"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_report_details.")
            return None

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        query = f"""
            SELECT
                {_build_incident_select()}
            FROM reportes_incidentes ri
            LEFT JOIN propiedades p ON ri.id_propiedad = p.id_propiedad
            WHERE ri.id_reporte_incidente = %s;
        """
        
        cur.execute(query, (report_id,))
        row = cur.fetchone()
        
        if not row:
            app_logger.warning(f"Report with ID {report_id} not found")
            return None
        
        # Format the report data based on actual database schema
        report = {
            'id_reporte': row['id_reporte_incidente'],
            'fecha_incidente': row['fecha_incidente'].strftime('%Y-%m-%d') if row['fecha_incidente'] else '',
            'hora_incidente': str(row['hora_incidente']) if row['hora_incidente'] else '',
            'descripcion_incidente': row['descripcion_incidente'] or '',
            'descripcion_zona_comun': row['descripcion_zona_comun'] or '',
            'pertenencias_sustraidas': row['pertenencias_sustraidas'] or '',
            'imagenes_pdfs': row['imagenes_pdfs'] or '',
            'estado_reporte': 'Reportado',  # Default status since it's not in the schema
            
            # Person details
            'nombre_persona': row['nombre_persona'] or '',
            'user_email': row['user_email'] or '',
            'telefono_persona': row['telefono_persona'] or '',
            'numero_identidad_persona': row['numero_identidad_persona'] or '',
            'numero_local': row['numero_local'] or '',
            'direccion': row['direccion'] or '',
            
            # Property details
            'propiedad_nombre': row['propiedad_nombre'] or '',
            'propiedad_direccion': row['propiedad_direccion'] or '',
            'propiedad_descripcion': row['propiedad_descripcion'] or '',
            
            # Incident classification
            'tipo_incidencia': row['tipo_incidencia'] or '',
            'tipo_cliente': row['tipo_cliente'] or '',
            'lugar_incidente': row['lugar_incidente'] or '',
            
            # Supervisor details
            'supervisor_name': row['supervisor_name'] or '',
            
            # Financial
            'valor_aproximado': float(row['valor_aproximado']) if row['valor_aproximado'] else 0.0,
            
            # Timestamps
            'created_at': row['creado_en'].strftime('%Y-%m-%d %H:%M:%S') if row['creado_en'] else '',
            'updated_at': ''  # Not available in current schema
        }
        
        app_logger.info(f"Successfully retrieved details for report {report_id}")
        return report
        
    except Exception as e:
        app_logger.error(f"Error in get_report_details: {e}", exc_info=True)
        return None
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_this_week_count(property_id=None):
    """Get count of incidents for current calendar week"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_this_week_count.")
            return 0

        cur = conn.cursor()
        
        # Build query with optional property filter
        where_clause = f"""WHERE {INCIDENT_DATE_EXPR} >= DATE_TRUNC('week', CURRENT_DATE)
              AND {INCIDENT_DATE_EXPR} < DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '1 week'"""
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            SELECT COUNT(*) as incident_count
            FROM reportes_incidentes ri
            {where_clause}
        """
        
        cur.execute(query, params)
        result = cur.fetchone()
        
        return result[0] if result else 0
        
    except Exception as e:
        app_logger.error(f"Error in get_this_week_count: {e}", exc_info=True)
        return 0
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_this_month_count(property_id=None):
    """Get count of incidents for current calendar month"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_this_month_count.")
            return 0

        cur = conn.cursor()
        
        # Build query with optional property filter
        where_clause = f"""WHERE DATE_TRUNC('month', {INCIDENT_DATE_EXPR}) = DATE_TRUNC('month', CURRENT_DATE)"""
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            SELECT COUNT(*) as incident_count
            FROM reportes_incidentes ri
            {where_clause}
        """
        
        cur.execute(query, params)
        result = cur.fetchone()
        
        return result[0] if result else 0
        
    except Exception as e:
        app_logger.error(f"Error in get_this_month_count: {e}", exc_info=True)
        return 0
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_total_count(property_id=None):
    """Get total count of incidents"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_total_count.")
            return 0

        cur = conn.cursor()
        
        # Build query with optional property filter
        where_clause = ""
        params = []
        
        if property_id:
            where_clause = "WHERE ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            SELECT COUNT(*) as incident_count
            FROM reportes_incidentes ri
            {where_clause}
        """
        
        cur.execute(query, params)
        result = cur.fetchone()
        
        return result[0] if result else 0
        
    except Exception as e:
        app_logger.error(f"Error in get_total_count: {e}", exc_info=True)
        return 0
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_reports_for_stat(stat_type, property_id=None, limit=100):
    """Get detailed reports for a specific statistic"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_reports_for_stat.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Base query using correct column names from reportes_incidentes table
        base_query = f"""
            SELECT
                {_build_incident_select()}
            FROM reportes_incidentes ri
            LEFT JOIN propiedades p ON ri.id_propiedad = p.id_propiedad
        """
        
        where_conditions = []
        params = []
        
        # Add property filter if specified
        if property_id:
            where_conditions.append("ri.id_propiedad = %s")
            params.append(property_id)
        
        # Add conditions based on stat type - FIXED: Use consistent date calculations
        if stat_type == 'total':
            # No additional date filters for total
            pass
        elif stat_type == 'thisMonth':
            where_conditions.append("""
                DATE_TRUNC('month', {INCIDENT_DATE_EXPR}) = DATE_TRUNC('month', CURRENT_DATE)
            """)
        elif stat_type == 'thisWeek':
            # FIXED: Use calendar week consistently
            where_conditions.append("""
                {INCIDENT_DATE_EXPR} >= DATE_TRUNC('week', CURRENT_DATE)
                AND {INCIDENT_DATE_EXPR} < DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '1 week'
            """)
            # DEBUG: Add logging for thisWeek
            app_logger.info("ThisWeek date range: week start = DATE_TRUNC('week', CURRENT_DATE)")
        elif stat_type == 'incidentTypes':
            # FIXED: Use same calendar week calculation for consistency
            where_conditions.append("""
                {INCIDENT_DATE_EXPR} >= DATE_TRUNC('week', CURRENT_DATE)
                AND {INCIDENT_DATE_EXPR} < DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '1 week'
            """)
            # DEBUG: Add logging for incidentTypes
            app_logger.info("IncidentTypes date range: week start = DATE_TRUNC('week', CURRENT_DATE)")
        elif stat_type == 'incidentTypesMonthly':
            # Last 30 days for monthly incident types
            where_conditions.append("""
                {INCIDENT_DATE_EXPR} >= CURRENT_DATE - INTERVAL '30 days'
                AND {INCIDENT_DATE_EXPR} < CURRENT_DATE + INTERVAL '1 day'
            """)
        elif stat_type == 'incidentTypesYearly':
            # Last 365 days for yearly incident types
            where_conditions.append("""
                {INCIDENT_DATE_EXPR} >= CURRENT_DATE - INTERVAL '365 days'
                AND {INCIDENT_DATE_EXPR} < CURRENT_DATE + INTERVAL '1 day'
            """)
        
        # Build final query
        if where_conditions:
            query = base_query + " WHERE " + " AND ".join(where_conditions)
        else:
            query = base_query
            
        query += f" ORDER BY {INCIDENT_ORDER_EXPR}"
        query += f" LIMIT {limit}"
        
        app_logger.info(f"Executing query for stat_type {stat_type}: {query}")
        app_logger.info(f"Query parameters: {params}")
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        app_logger.info(f"Found {len(rows)} reports for stat_type {stat_type}")
        
        # DEBUG: Log details about the found reports
        for i, row in enumerate(rows):
            app_logger.info(f"Report {i+1}: ID={row['id_reporte_incidente']}, "
                          f"Date={row['fecha_incidente']}, "
                          f"Type={row['tipo_incidencia']}, "
                          f"Property={row['propiedad_nombre']}")
        
        reports = []
        for row in rows:
            reports.append(_serialize_incident_report_row(row))
        
        return reports
        
    except Exception as e:
        app_logger.error(f"Error in get_reports_for_stat: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_reports_for_incident_type(incident_type, stat_type='weekly', property_id=None, limit=100):
    """Get detailed reports for a specific incident type and timeframe"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_reports_for_incident_type.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Base query using correct column names
        base_query = f"""
            SELECT
                {_build_incident_select()}
            FROM reportes_incidentes ri
            LEFT JOIN propiedades p ON ri.id_propiedad = p.id_propiedad
        """

        where_conditions = [f"{INCIDENT_TYPE_EXPR} = %s"]
        params = [incident_type]
        
        # Add property filter if specified
        if property_id:
            where_conditions.append("ri.id_propiedad = %s")
            params.append(property_id)
        
        # FIXED: Add date conditions based on stat type with consistent calculations
        if stat_type == 'weekly':
            # Use calendar week for consistency with "Esta Semana"
            where_conditions.append("""
                {INCIDENT_DATE_EXPR} >= DATE_TRUNC('week', CURRENT_DATE)
                AND {INCIDENT_DATE_EXPR} < DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '1 week'
            """)
        elif stat_type == 'monthly':
            # Last 30 days
            where_conditions.append("""
                {INCIDENT_DATE_EXPR} >= CURRENT_DATE - INTERVAL '30 days'
                AND {INCIDENT_DATE_EXPR} < CURRENT_DATE + INTERVAL '1 day'
            """)
        elif stat_type == 'yearly':
            # Last 365 days
            where_conditions.append("""
                {INCIDENT_DATE_EXPR} >= CURRENT_DATE - INTERVAL '365 days'
                AND {INCIDENT_DATE_EXPR} < CURRENT_DATE + INTERVAL '1 day'
            """)
        
        # Build final query
        query = base_query + " WHERE " + " AND ".join(where_conditions)
        query += f" ORDER BY {INCIDENT_ORDER_EXPR}"
        query += f" LIMIT {limit}"
        
        app_logger.info(f"Executing query for incident_type {incident_type}, stat_type {stat_type}: {query}")
        app_logger.info(f"Query parameters: {params}")
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        app_logger.info(f"Found {len(rows)} reports for incident_type {incident_type}")
        
        reports = []
        for row in rows:
            reports.append(_serialize_incident_report_row(row))
        
        return reports
        
    except Exception as e:
        app_logger.error(f"Error in get_reports_for_incident_type: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incidents_by_week_with_types(property_id=None):
    """Get incident counts grouped by 7-day periods with type breakdown for KPI alerts, optionally filtered by property"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incidents_by_week_with_types.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # FIXED: Corrected query structure
        params = []
        property_filter = ""
        
        if property_id:
            property_filter = "AND ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            WITH seven_day_periods AS (
                SELECT 
                    CURRENT_DATE - (generate_series(0, 11) * 7) as period_end,
                    CURRENT_DATE - (generate_series(0, 11) * 7) - 6 as period_start
            ),
            all_types AS (
                SELECT unnest(ARRAY['Hurto', 'Olvido', 'Recuperacion', 'Robo']) as incident_type
            ),
            period_type_combinations AS (
                SELECT 
                    sdp.period_start,
                    sdp.period_end,
                    at.incident_type
                FROM seven_day_periods sdp
                CROSS JOIN all_types at
            ),
            actual_data AS (
                SELECT 
                    sdp.period_start,
                    sdp.period_end,
                    {INCIDENT_TYPE_EXPR} as incident_type,
                    COUNT(ri.id_reporte_incidente) as incident_count
                FROM seven_day_periods sdp
                LEFT JOIN reportes_incidentes ri ON (
                    {INCIDENT_DATE_EXPR} >= sdp.period_start 
                    AND {INCIDENT_DATE_EXPR} <= sdp.period_end
                    {property_filter}
                )
                WHERE {INCIDENT_TYPE_EXPR} IN ('Hurto', 'Olvido', 'Recuperacion', 'Robo', 'Sin Tipo')
                GROUP BY sdp.period_start, sdp.period_end, {INCIDENT_TYPE_EXPR}
            )
            SELECT 
                ptc.period_start,
                ptc.period_end,
                ptc.incident_type,
                COALESCE(ad.incident_count, 0) as incident_count
            FROM period_type_combinations ptc
            LEFT JOIN actual_data ad ON (
                ptc.period_start = ad.period_start 
                AND ptc.period_end = ad.period_end 
                AND ptc.incident_type = ad.incident_type
            )
            ORDER BY ptc.period_start DESC, ptc.incident_type;
        """
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        # KPI threshold for weekly: 4 incidents per 7-day period
        weekly_kpi_threshold = 4
        
        # Group by period and check for KPI violations
        periods_data = {}
        for row in rows:
            period_start = row['period_start']
            period_end = row['period_end']
            incident_type = row['incident_type']
            count = row['incident_count']
            
            period_key = (period_start, period_end)
            
            if period_key not in periods_data:
                periods_data[period_key] = {
                    'period': f"{period_start.strftime('%d/%m')} - {period_end.strftime('%d/%m/%Y')}",
                    'total_count': 0,
                    'has_kpi_violation': False,
                    'types': {},
                    'date_range': {
                        'start': period_start.isoformat(),
                        'end': period_end.isoformat()
                    },
                    'period_start': period_start,
                    'period_end': period_end
                }
            
            # Always set the count, even if it's 0
            periods_data[period_key]['types'][incident_type] = count
            if count > 0:
                periods_data[period_key]['total_count'] += count
        
        # Check KPI violations
        for period_data in periods_data.values():
            if period_data['total_count'] >= weekly_kpi_threshold:
                period_data['has_kpi_violation'] = True
        
        # Convert to list and sort chronologically (oldest first)
        result = []
        for period_key in sorted(periods_data.keys(), key=lambda x: x[0]):
            result.append(periods_data[period_key])
        
        return result[-12:]  # Return last 12 periods
        
    except Exception as e:
        app_logger.error(f"Error in get_incidents_by_week_with_types: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incidents_by_week(property_id=None):
    """Get incident counts grouped by 7-day periods (last 7 days, previous 7 days, etc.), optionally filtered by property"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incidents_by_week.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Build query with optional property filter
        params = []
        property_filter = ""
        
        if property_id:
            property_filter = "AND ri.id_propiedad = %s"
            params.append(property_id)
        
        # Use same 7-day periods as the KPI function
        query = f"""
            WITH seven_day_periods AS (
                SELECT 
                    CURRENT_DATE - (generate_series(0, 11) * 7) as period_end,
                    CURRENT_DATE - (generate_series(0, 11) * 7) - 6 as period_start
            )
            SELECT 
                sdp.period_start,
                sdp.period_end,
                COUNT(ri.id_reporte_incidente) as incident_count
            FROM seven_day_periods sdp
            LEFT JOIN reportes_incidentes ri ON (
                {INCIDENT_DATE_EXPR} >= sdp.period_start 
                AND {INCIDENT_DATE_EXPR} <= sdp.period_end
                {property_filter}
            )
            GROUP BY sdp.period_start, sdp.period_end
            ORDER BY sdp.period_start;
        """
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        data = []
        for row in rows:
            period_start = row['period_start']
            period_end = row['period_end']
            data.append({
                'period': f"{period_start.strftime('%d/%m')} - {period_end.strftime('%d/%m/%Y')}",
                'count': row['incident_count'],
                'date_range': {
                    'start': period_start.isoformat(),
                    'end': period_end.isoformat()
                }
            })
        
        return data
        
    except Exception as e:
        app_logger.error(f"Error in get_incidents_by_week: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incidents_by_month(property_id=None):
    """Get incident counts grouped by month, optionally filtered by property"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incidents_by_month.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Build query with optional property filter
        where_clause = f"WHERE {INCIDENT_DATE_EXPR} IS NOT NULL"
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            SELECT 
                DATE_TRUNC('month', {INCIDENT_DATE_EXPR}) as month_start,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            {where_clause}
            GROUP BY DATE_TRUNC('month', {INCIDENT_DATE_EXPR})
            ORDER BY month_start DESC
            LIMIT 12;
        """
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        # Spanish month names
        month_names = {
            1: 'Enero', 2: 'Febrero', 3: 'Marzo', 4: 'Abril',
            5: 'Mayo', 6: 'Junio', 7: 'Julio', 8: 'Agosto',
            9: 'Septiembre', 10: 'Octubre', 11: 'Noviembre', 12: 'Diciembre'
        }
        
        # KPI threshold for monthly: 4 weeks * 4 incidents = 16 incidents per month
        monthly_kpi_threshold = 16
        
        data = []
        for row in rows:
            month_start = row['month_start']
            month_name = month_names[month_start.month]
            incident_count = row['incident_count']
            
            # Calculate the last day of the month for the date range
            _, last_day = calendar.monthrange(month_start.year, month_start.month)
            month_end = month_start.replace(day=last_day)
            
            data.append({
                'period': f"{month_name} {month_start.year}",
                'count': incident_count,
                'has_kpi_violation': incident_count >= monthly_kpi_threshold,
                'date_range': {
                    'start': month_start.strftime('%Y-%m-%d'),
                    'end': month_end.strftime('%Y-%m-%d')
                }
            })
        
        return list(reversed(data))
        
    except Exception as e:
        app_logger.error(f"Error in get_incidents_by_month: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incidents_by_year(property_id=None):
    """Get incident counts grouped by year, optionally filtered by property"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incidents_by_year.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Build query with optional property filter
        where_clause = f"WHERE {INCIDENT_DATE_EXPR} IS NOT NULL"
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            SELECT 
                EXTRACT(YEAR FROM {INCIDENT_DATE_EXPR}) as year,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            {where_clause}
            GROUP BY EXTRACT(YEAR FROM {INCIDENT_DATE_EXPR})
            ORDER BY year;
        """
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        # KPI threshold for yearly: 52 weeks * 4 incidents = 208 incidents per year
        yearly_kpi_threshold = 208
        
        data = []
        for row in rows:
            incident_count = row['incident_count']
            year = int(row['year'])
            data.append({
                'period': str(year),
                'count': incident_count,
                'has_kpi_violation': incident_count >= yearly_kpi_threshold,
                'date_range': {
                    'start': f"{year}-01-01",
                    'end': f"{year}-12-31"
                }
            })
        
        return data
        
    except Exception as e:
        app_logger.error(f"Error in get_incidents_by_year: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incident_types_stats(property_id=None):
    """Get incident counts by type for current calendar week, optionally filtered by property"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incident_types_stats.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # FIXED: Use exact same date calculation as thisWeek and remove ti.nombre IS NOT NULL filter
        where_clause = f"""WHERE {INCIDENT_DATE_EXPR} >= DATE_TRUNC('week', CURRENT_DATE)
              AND {INCIDENT_DATE_EXPR} < DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '1 week'"""
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        # DEBUG: First let's see all incidents in this week
        debug_query = f"""
            SELECT 
                ri.id_reporte_incidente,
                {INCIDENT_DATE_EXPR} as fecha_incidente,
                {INCIDENT_TYPE_EXPR} as tipo_incidencia,
                p.nombre as propiedad_nombre
            FROM reportes_incidentes ri
            LEFT JOIN propiedades p ON ri.id_propiedad = p.id_propiedad
            {where_clause}
            ORDER BY {INCIDENT_DATE_EXPR} DESC;
        """
        
        app_logger.info(f"DEBUG: Executing debug query: {debug_query}")
        app_logger.info(f"DEBUG: Query parameters: {params}")
        
        cur.execute(debug_query, params)
        debug_rows = cur.fetchall()
        
        app_logger.info(f"DEBUG: Found {len(debug_rows)} total incidents this week")
        for row in debug_rows:
            app_logger.info(f"DEBUG: Incident ID={row['id_reporte_incidente']}, "
                          f"Date={row['fecha_incidente']}, "
                          f"Type={row['tipo_incidencia']}, "
                          f"Property={row['propiedad_nombre']}")
        
        # Now the actual query for incident types
        query = f"""
            SELECT 
                {INCIDENT_TYPE_EXPR} as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            {where_clause}
            GROUP BY {INCIDENT_TYPE_EXPR}
            ORDER BY {INCIDENT_TYPE_EXPR};
        """
        
        app_logger.info(f"Executing incident types query: {query}")
        app_logger.info(f"Query parameters: {params}")
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        app_logger.info(f"Raw incident type results: {len(rows)} types found")
        for row in rows:
            app_logger.info(f"Type: {row['incident_type']}, Count: {row['incident_count']}")
        
        # Initialize all incident types with 0 count
        incident_types = {
            'Hurto': 0,
            'Olvido': 0,
            'Recuperacion': 0,
            'Robo': 0
        }
        
        # Update with actual counts
        for row in rows:
            incident_type = row['incident_type']
            if incident_type in incident_types:
                incident_types[incident_type] = row['incident_count']
            else:
                app_logger.warning(f"Found incident with unknown type: {incident_type} (Count: {row['incident_count']})")
        
        # Convert to list format for frontend
        result = []
        for incident_type, count in incident_types.items():
            result.append({
                'type': incident_type,
                'count': count,
                'is_critical': count >= 4  # KPI threshold
            })
        
        app_logger.info(f"Final incident types result: {result}")
        
        return result
        
    except Exception as e:
        app_logger.error(f"Error in get_incident_types_stats: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incident_types_monthly(property_id=None):
    """Get incident counts by type for the last 30 days, optionally filtered by property"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incident_types_monthly.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Build query with optional property filter - Changed to last 30 days
        where_clause = f"""WHERE {INCIDENT_DATE_EXPR} >= CURRENT_DATE - INTERVAL '30 days'
              AND {INCIDENT_DATE_EXPR} < CURRENT_DATE + INTERVAL '1 day'"""
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            SELECT 
                {INCIDENT_TYPE_EXPR} as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            {where_clause}
            GROUP BY {INCIDENT_TYPE_EXPR}
            ORDER BY {INCIDENT_TYPE_EXPR};
        """
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        # Initialize all incident types with 0 count
        incident_types = {
            'Hurto': 0,
            'Olvido': 0,
            'Recuperacion': 0,
            'Robo': 0
        }
        
        # Update with actual counts
        for row in rows:
            incident_type = row['incident_type']
            if incident_type in incident_types:
                incident_types[incident_type] = row['incident_count']
        
        # Convert to list format for frontend
        result = []
        # KPI threshold for monthly: 16 incidents per month / 4 types = 4 per type per month
        monthly_kpi_threshold = 16
        for incident_type, count in incident_types.items():
            result.append({
                'type': incident_type,
                'count': count,
                'is_critical': count >= monthly_kpi_threshold  # KPI threshold for monthly
            })
        
        return result
        
    except Exception as e:
        app_logger.error(f"Error in get_incident_types_monthly: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incident_types_yearly(property_id=None):
    """Get incident counts by type for the last 365 days, optionally filtered by property"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incident_types_yearly.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Build query with optional property filter - Changed to last 365 days
        where_clause = f"""WHERE {INCIDENT_DATE_EXPR} >= CURRENT_DATE - INTERVAL '365 days'
              AND {INCIDENT_DATE_EXPR} < CURRENT_DATE + INTERVAL '1 day'"""
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            SELECT 
                {INCIDENT_TYPE_EXPR} as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            {where_clause}
            GROUP BY {INCIDENT_TYPE_EXPR}
            ORDER BY {INCIDENT_TYPE_EXPR};
        """
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        # Initialize all incident types with 0 count
        incident_types = {
            'Hurto': 0,
            'Olvido': 0,
            'Recuperacion': 0,
            'Robo': 0
        }
        
        # Update with actual counts
        for row in rows:
            incident_type = row['incident_type']
            if incident_type in incident_types:
                incident_types[incident_type] = row['incident_count']
        
        # Convert to list format for frontend
        result = []
        # KPI threshold for yearly: 208 incidents per year / 4 types = 52 per type per year
        yearly_kpi_threshold = 208
        for incident_type, count in incident_types.items():
            result.append({
                'type': incident_type,
                'count': count,
                'is_critical': count >= yearly_kpi_threshold  # KPI threshold for yearly
            })
        
        return result
        
    except Exception as e:
        app_logger.error(f"Error in get_incident_types_yearly: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incidents_by_month_with_types(property_id=None):
    """Get incident counts grouped by month with type breakdown, optionally filtered by property"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incidents_by_month_with_types.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Build query with optional property filter
        where_clause = f"""WHERE {INCIDENT_DATE_EXPR} IS NOT NULL
              AND {INCIDENT_TYPE_EXPR} IN ('Hurto', 'Olvido', 'Recuperacion', 'Robo')"""
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            SELECT 
                DATE_TRUNC('month', {INCIDENT_DATE_EXPR}) as month_start,
                {INCIDENT_TYPE_EXPR} as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            {where_clause}
            GROUP BY DATE_TRUNC('month', {INCIDENT_DATE_EXPR}), {INCIDENT_TYPE_EXPR}
            ORDER BY month_start DESC, {INCIDENT_TYPE_EXPR}
            LIMIT 48; -- 12 months * 4 types max
        """
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        # Spanish month names
        month_names = {
            1: 'Enero', 2: 'Febrero', 3: 'Marzo', 4: 'Abril',
            5: 'Mayo', 6: 'Junio', 7: 'Julio', 8: 'Agosto',
            9: 'Septiembre', 10: 'Octubre', 11: 'Noviembre', 12: 'Diciembre'
        }
        
        # KPI threshold for monthly: 16 incidents per month
        monthly_kpi_threshold = 16
        
        # Group by month and check for KPI violations
        months_data = {}
        for row in rows:
            month_start = row['month_start']
            incident_type = row['incident_type']
            count = row['incident_count']
            
            if month_start not in months_data:
                month_name = month_names[month_start.month]
                months_data[month_start] = {
                    'period': f"{month_name} {month_start.year}",
                    'total_count': 0,
                    'has_kpi_violation': False,
                    'types': {}
                }
            
            months_data[month_start]['types'][incident_type] = count
            months_data[month_start]['total_count'] += count
            
            # Check KPI violation (16 or more incidents total per month)
            if months_data[month_start]['total_count'] >= monthly_kpi_threshold:
                months_data[month_start]['has_kpi_violation'] = True
        
        # Convert to list and sort chronologically
        result = []
        for month_start in sorted(months_data.keys(), reverse=True)[:12]:  # Last 12 months
            result.append(months_data[month_start])
        
        return list(reversed(result))  # Reverse to show chronological order
        
    except Exception as e:
        app_logger.error(f"Error in get_incidents_by_month_with_types: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incidents_by_year_with_types(property_id=None):
    """Get incident counts grouped by year with type breakdown, optionally filtered by property"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incidents_by_year_with_types.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Build query with optional property filter
        where_clause = f"""WHERE {INCIDENT_DATE_EXPR} IS NOT NULL
              AND {INCIDENT_TYPE_EXPR} IN ('Hurto', 'Olvido', 'Recuperacion', 'Robo')"""
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            SELECT 
                EXTRACT(YEAR FROM {INCIDENT_DATE_EXPR}) as year,
                {INCIDENT_TYPE_EXPR} as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            {where_clause}
            GROUP BY EXTRACT(YEAR FROM {INCIDENT_DATE_EXPR}), {INCIDENT_TYPE_EXPR}
            ORDER BY year DESC, {INCIDENT_TYPE_EXPR};
        """
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        # KPI threshold for yearly: 208 incidents per year
        yearly_kpi_threshold = 208
        
        # Group by year and check for KPI violations
        years_data = {}
        for row in rows:
            year = int(row['year'])
            incident_type = row['incident_type']
            count = row['incident_count']
            
            if year not in years_data:
                years_data[year] = {
                    'period': str(year),
                    'total_count': 0,
                    'has_kpi_violation': False,
                    'types': {}
                }
            
            years_data[year]['types'][incident_type] = count
            years_data[year]['total_count'] += count
            
            # Check KPI violation (208 or more incidents total per year)
            if years_data[year]['total_count'] >= yearly_kpi_threshold:
                years_data[year]['has_kpi_violation'] = True
        
        # Convert to list and sort chronologically
        result = []
        for year in sorted(years_data.keys()):
            result.append(years_data[year])
        
        return result
        
    except Exception as e:
        app_logger.error(f"Error in get_incidents_by_year_with_types: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incident_types_for_period(start_date, end_date, property_id=None):
    """Get incident counts by type for a specific date range"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incident_types_for_period.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # FIXED: Use exact same date filtering logic with property filter
        where_clause = f"""WHERE {INCIDENT_DATE_EXPR} >= %s
              AND {INCIDENT_DATE_EXPR} <= %s"""
        params = [start_date, end_date]
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        # FIXED: Include all incident types, even with 0 counts
        query = f"""
            WITH all_types AS (
                SELECT unnest(ARRAY['Hurto', 'Olvido', 'Recuperacion', 'Robo']) as incident_type
            ),
            actual_counts AS (
                SELECT 
                    {INCIDENT_TYPE_EXPR} as incident_type,
                    COUNT(*) as incident_count
                FROM reportes_incidentes ri
                {where_clause}
                    AND {INCIDENT_TYPE_EXPR} IN ('Hurto', 'Olvido', 'Recuperacion', 'Robo')
                GROUP BY {INCIDENT_TYPE_EXPR}
            )
            SELECT 
                at.incident_type,
                COALESCE(ac.incident_count, 0) as incident_count
            FROM all_types at
            LEFT JOIN actual_counts ac ON at.incident_type = ac.incident_type
            ORDER BY at.incident_type;
        """
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        # Convert to list format for frontend
        result = []
        for row in rows:
            result.append({
                'type': row['incident_type'],
                'count': row['incident_count'],
                'is_critical': row['incident_count'] >= 4  # KPI threshold for 7-day period
            })
        
        return result
        
    except Exception as e:
        app_logger.error(f"Error in get_incident_types_for_period: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

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

# --- Helper to resolve user name from JWT + DB ---
def _get_user_info(user_email):
    """Returns (user_name, is_admin) for the given email."""
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
    except Exception as e:
        app_logger.error(f"Error getting JWT claims: {e}", exc_info=True)
        user_name = user_email.split('@')[0]
        is_admin = False

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
    except Exception as e:
        app_logger.error(f"Error fetching user name: {e}", exc_info=True)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    return user_name, is_admin


# --- Routes ---
@dashboard_bp.route('/')
@jwt_required()
@admin_required
def dashboard_home():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    app_logger.info(f"Admin user {user_email} accessing dashboard home")
    return render_template("dashboard_home.html",
                           current_user=user_email,
                           user_name=user_name,
                           is_admin=is_admin)


@dashboard_bp.route('/incidentes/')
@jwt_required()
def dashboard_incidentes():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    app_logger.info(f"User {user_email} accessing incidentes dashboard")
    return render_template("dashboard_incidentes.html",
                           current_user=user_email,
                           user_name=user_name,
                           is_admin=is_admin)


# ── Module configurations ────────────────────────────────────────────────────
# Each entry: (route_slug, function_name, display_name, description, accent_class, svg_icon)
_MODULE_CONFIGS = {
    'satisfaccion': {
        'name': 'Satisfacción',
        'desc': 'Para medir la percepción y satisfacción del cliente con el servicio de seguridad.',
        'accent': 'accent-green',
        'icon': '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.828 14.828a4 4 0 01-5.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>',
    },
    'supervision': {
        'name': 'Supervisión',
        'desc': 'Para que los supervisores evalúen el estado y desempeño de los puestos de seguridad.',
        'accent': 'accent-blue',
        'icon': '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" /></svg>',
    },
    'cumplimiento': {
        'name': 'Cumplimiento',
        'desc': 'Auditoría de cumplimiento para SST y Seguridad Física.',
        'accent': 'accent-purple',
        'icon': '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" /></svg>',
    },
    'capacitacion': {
        'name': 'Capacitación',
        'desc': 'Para registrar la asistencia y los detalles de las capacitaciones impartidas.',
        'accent': 'accent-amber',
        'icon': '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 14l9-5-9-5-9 5 9 5zm0 0l6.16-3.422a12.083 12.083 0 01.665 6.479A11.952 11.952 0 0112 20.055a11.952 11.952 0 00-6.824-2.998 12.078 12.078 0 01.665-6.479L12 14zm-4 6v-7.5l4-2.222" /></svg>',
    },
    'disciplina': {
        'name': 'Disciplina',
        'desc': 'Para reportar novedades y faltas disciplinarias de los empleados.',
        'accent': 'accent-orange',
        'icon': '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" /></svg>',
    },
    'visitas': {
        'name': 'Visitas',
        'desc': 'Para documentar las visitas a clientes y los acuerdos alcanzados.',
        'accent': 'accent-teal',
        'icon': '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" /></svg>',
    },
    'vehiculos': {
        'name': 'Vehículos',
        'desc': 'Inspección pre-operacional de vehículos y motocicletas de la flota.',
        'accent': 'accent-indigo',
        'icon': '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4" /></svg>',
    },
    'equipos': {
        'name': 'Equipos',
        'desc': 'Evaluación del estado y confiabilidad de los equipos del sistema de seguridad.',
        'accent': 'accent-slate',
        'icon': '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2V9M9 21H5a2 2 0 01-2-2V9m0 0h18" /></svg>',
    },
    'gestion': {
        'name': 'Gestión y Resultados',
        'desc': 'Vista consolidada de todos los módulos — KPIs, tendencias y métricas de desempeño global.',
        'accent': 'accent-blue-purple',
        'icon': '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" /></svg>',
    },
}


def _module_view(slug):
    """Generic handler for all module dashboard pages."""
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    cfg = _MODULE_CONFIGS[slug]
    app_logger.info(f"Admin user {user_email} accessing {slug} dashboard")
    return render_template(
        "dashboard_module.html",
        current_user=user_email,
        user_name=user_name,
        is_admin=is_admin,
        module_name=cfg['name'],
        module_desc=cfg['desc'],
        module_accent=cfg['accent'],
        module_icon=cfg['icon'],
    )


@dashboard_bp.route('/satisfaccion/')
@jwt_required()
@admin_required
def dashboard_satisfaccion():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    return render_template("dashboard_satisfaccion.html",
                           current_user=user_email,
                           user_name=user_name,
                           is_admin=is_admin)

@dashboard_bp.route('/supervision/')
@jwt_required()
@admin_required
def dashboard_supervision():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    app_logger.info(f"Admin user {user_email} accessing supervision dashboard")
    return render_template("dashboard_supervision.html",
                           current_user=user_email,
                           user_name=user_name,
                           is_admin=is_admin)

@dashboard_bp.route('/cumplimiento/')
@jwt_required()
@admin_required
def dashboard_cumplimiento():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    app_logger.info(f"Admin user {user_email} accessing cumplimiento dashboard")
    return render_template("dashboard_cumplimiento.html",
                           current_user=user_email,
                           user_name=user_name,
                           is_admin=is_admin)

@dashboard_bp.route('/capacitacion/')
@jwt_required()
@admin_required
def dashboard_capacitacion():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    app_logger.info(f"Admin user {user_email} accessing capacitacion dashboard")
    return render_template("dashboard_capacitacion.html",
                           current_user=user_email,
                           user_name=user_name,
                           is_admin=is_admin)

@dashboard_bp.route('/disciplina/')
@jwt_required()
@admin_required
def dashboard_disciplina():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    app_logger.info(f"Admin user {user_email} accessing disciplina dashboard")
    return render_template("dashboard_disciplina.html",
                           current_user=user_email,
                           user_name=user_name,
                           is_admin=is_admin)

@dashboard_bp.route('/visitas/')
@jwt_required()
def dashboard_visitas():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    app_logger.info(f"User {user_email} accessing visitas dashboard")
    return render_template("dashboard_visitas.html",
                           current_user=user_email,
                           user_name=user_name,
                           is_admin=is_admin)

@dashboard_bp.route('/vehiculos/')
@jwt_required()
@admin_required
def dashboard_vehiculos():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    app_logger.info(f"Admin user {user_email} accessing vehiculos dashboard")
    return render_template("dashboard_vehiculos.html",
                           current_user=user_email,
                           user_name=user_name,
                           is_admin=is_admin)

@dashboard_bp.route('/motocicletas/')
@jwt_required()
@admin_required
def dashboard_motocicletas():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    app_logger.info(f"Admin user {user_email} accessing motocicletas dashboard")
    return render_template("dashboard_motocicletas.html",
                           current_user=user_email,
                           user_name=user_name,
                           is_admin=is_admin)

@dashboard_bp.route('/equipos/')
@jwt_required()
@admin_required
def dashboard_equipos():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    app_logger.info(f"Admin user {user_email} accessing equipos dashboard")
    return render_template("dashboard_equipos.html",
                           current_user=user_email,
                           user_name=user_name,
                           is_admin=is_admin)

@dashboard_bp.route('/gestion/')
@jwt_required()
@admin_required
def dashboard_gestion():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    app_logger.info(f"Admin user {user_email} accessing gestion dashboard")
    return render_template("dashboard_gestion.html",
                           current_user=user_email,
                           user_name=user_name,
                           is_admin=is_admin)


def _gestion_clamp(value, lo=0, hi=100):
    if value is None:
        return None
    return max(lo, min(hi, value))


def _gestion_status(score):
    if score is None:
        return {
            'label': 'Sin datos',
            'color': '#64748b',
            'tone': 'slate',
            'rank': 0,
        }
    score = float(score)
    if score >= 85:
        return {'label': 'Saludable', 'color': '#16a34a', 'tone': 'green', 'rank': 3}
    if score >= 70:
        return {'label': 'En seguimiento', 'color': '#eab308', 'tone': 'yellow', 'rank': 2}
    if score >= 55:
        return {'label': 'Alerta', 'color': '#f97316', 'tone': 'orange', 'rank': 1}
    return {'label': 'Crítico', 'color': '#dc2626', 'tone': 'red', 'rank': 0}


def _gestion_date_prefix(year, month, day):
    if not year:
        return None
    prefix = f"{year}"
    if month:
        prefix += f"-{month:02d}"
        if day:
            prefix += f"-{day:02d}"
    return prefix


def _gestion_add_like_date_filter(conds, params, text_expr, year, month, day):
    prefix = _gestion_date_prefix(year, month, day)
    if prefix:
        conds.append(f"{text_expr} LIKE %s")
        params.append(prefix + "%")


def _parse_multi(value):
    """Parse comma-separated string → list of non-empty stripped strings."""
    if not value:
        return []
    return [v.strip() for v in str(value).split(',') if v.strip()]


def _gestion_add_multi_date_filter(conds, params, text_expr, years, months, days):
    """Build (expr LIKE '...' OR expr LIKE '...') for multi-select year/month/day.
    Falls back gracefully to single-value behaviour when only one value is given.
    """
    def _to_ints(val):
        if isinstance(val, (int, float)):
            return [int(val)]
        if isinstance(val, str):
            return [int(x) for x in _parse_multi(val) if x.lstrip('-').isdigit()]
        if val is None:
            return []
        # already a list/tuple
        result = []
        for x in val:
            try:
                result.append(int(x))
            except (TypeError, ValueError):
                pass
        return result

    y_list = _to_ints(years)
    m_list = _to_ints(months)
    d_list = _to_ints(days)

    if not y_list:
        return  # no year filter → no date constraint

    prefixes = []
    for y in y_list:
        if m_list:
            for m in m_list:
                if d_list:
                    for d in d_list:
                        prefixes.append(f"{y}-{m:02d}-{d:02d}")
                else:
                    prefixes.append(f"{y}-{m:02d}")
        else:
            prefixes.append(f"{y}")

    if len(prefixes) == 1:
        conds.append(f"{text_expr} LIKE %s")
        params.append(prefixes[0] + "%")
    elif prefixes:
        placeholders = " OR ".join(f"{text_expr} LIKE %s" for _ in prefixes)
        conds.append(f"({placeholders})")
        params.extend(p + "%" for p in prefixes)


def _gestion_add_extract_date_filter(conds, params, date_expr, year, month, day):
    if year:
        conds.append(f"EXTRACT(YEAR FROM {date_expr}) = %s")
        params.append(year)
    if month:
        conds.append(f"EXTRACT(MONTH FROM {date_expr}) = %s")
        params.append(month)
    if day:
        conds.append(f"EXTRACT(DAY FROM {date_expr}) = %s")
        params.append(day)


def _gestion_where(conds):
    return ('WHERE ' + ' AND '.join(conds)) if conds else ''


def _gestion_score_payload(module_key, title, route, score, primary_value, primary_label,
                           secondary_value=None, secondary_label=None):
    score = round(score, 1) if score is not None else None
    status = _gestion_status(score)
    return {
        'key': module_key,
        'title': title,
        'route': route,
        'score': score,
        'status': status,
        'primary_value': primary_value,
        'primary_label': primary_label,
        'secondary_value': secondary_value,
        'secondary_label': secondary_label,
    }


@dashboard_bp.route('/api/gestion/filtros')
@jwt_required()
@admin_required
def api_gestion_filtros():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'clientes': [], 'proyectos': [], 'paises': [], 'turnos': ['Diurno', 'Nocturno']})

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("""
            SELECT DISTINCT cliente FROM (
                SELECT TRIM(cliente_instalacion) AS cliente FROM medicion_experiencia_cliente
                UNION
                SELECT TRIM(cliente_instalacion) AS cliente FROM reportes_incidentes
                UNION
                SELECT TRIM(cliente)             AS cliente FROM supervision_puesto
                UNION
                SELECT TRIM(cliente_instalacion) AS cliente FROM checklist_cumplimiento
                UNION
                SELECT TRIM(cliente_instalacion) AS cliente FROM registro_de_capacitaciones
                UNION
                SELECT TRIM(cliente_instalacion) AS cliente FROM informe_novedades_disciplinario
                UNION
                SELECT TRIM(cliente_instalacion) AS cliente FROM registro_y_acta_de_visita
                UNION
                SELECT TRIM(cliente_instalacion) AS cliente FROM planilla_vehicular
                UNION
                SELECT TRIM(cliente_instalacion) AS cliente FROM confiabilidad_equipos
            ) q
            WHERE cliente IS NOT NULL AND cliente <> ''
            ORDER BY cliente
        """)
        clientes = [r['cliente'] for r in cur.fetchall()]

        cur.execute("""
            SELECT DISTINCT proyecto FROM (
                SELECT TRIM(puesto_area_especifica) AS proyecto FROM reportes_incidentes
                UNION
                SELECT TRIM(puesto_area_especifica) AS proyecto FROM checklist_cumplimiento
                UNION
                SELECT TRIM(puesto_area_especifica) AS proyecto FROM registro_de_capacitaciones
                UNION
                SELECT TRIM(puesto_area_especifica) AS proyecto FROM informe_novedades_disciplinario
                UNION
                SELECT TRIM(puesto_area_especifica) AS proyecto FROM registro_y_acta_de_visita
                UNION
                SELECT TRIM(puesto_area_especifica) AS proyecto FROM planilla_vehicular
                UNION
                SELECT TRIM(sitio) AS proyecto FROM confiabilidad_equipos
            ) q
            WHERE proyecto IS NOT NULL AND proyecto <> ''
            ORDER BY proyecto
        """)
        proyectos = [r['proyecto'] for r in cur.fetchall()]

        cur.execute("""
            SELECT DISTINCT turno FROM (
                SELECT TRIM(turno) AS turno FROM reportes_incidentes
                UNION
                SELECT TRIM(turno) AS turno FROM checklist_cumplimiento
                UNION
                SELECT TRIM(turno) AS turno FROM registro_de_capacitaciones
                UNION
                SELECT TRIM(turno) AS turno FROM informe_novedades_disciplinario
                UNION
                SELECT TRIM(turno) AS turno FROM registro_y_acta_de_visita
                UNION
                SELECT TRIM(turno) AS turno FROM planilla_vehicular
            ) q
            WHERE turno IS NOT NULL AND turno <> ''
            ORDER BY turno
        """)
        turnos = [r['turno'] for r in cur.fetchall()] or ['Diurno', 'Nocturno']

        cur.execute("""
            SELECT DISTINCT rol_aplicador AS responsable
            FROM (
                SELECT TRIM(rol_aplicador) AS rol_aplicador FROM supervision_puesto
                UNION
                SELECT TRIM(rol_aplicador) AS rol_aplicador FROM medicion_experiencia_cliente
            ) q
            WHERE rol_aplicador IS NOT NULL AND rol_aplicador <> ''
            ORDER BY rol_aplicador
        """)
        responsables = [r['responsable'] for r in cur.fetchall()]

        return jsonify({
            'clientes': clientes,
            'proyectos': proyectos,
            'paises': [],
            'turnos': turnos,
            'responsables': responsables,
        })
    except Exception as e:
        app_logger.error(f"api_gestion_filtros error: {e}", exc_info=True)
        return jsonify({'clientes': [], 'proyectos': [], 'paises': [], 'turnos': ['Diurno', 'Nocturno'], 'responsables': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/gestion/nombres-por-rol')
@jwt_required()
@admin_required
def api_gestion_nombres_por_rol():
    """Returns distinct person names for a given rol_aplicador across all relevant tables."""
    rol = request.args.get('rol') or None
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'nombres': []})
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        rol_cond = "AND TRIM(rol_aplicador) = %s" if rol else ""
        rol_params = [rol] if rol else []

        cur.execute(f"""
            SELECT DISTINCT nombre FROM (
                SELECT TRIM(nombre_responsable) AS nombre
                FROM medicion_experiencia_cliente
                WHERE nombre_responsable IS NOT NULL AND TRIM(nombre_responsable) <> ''
                {rol_cond}
                UNION
                SELECT TRIM(supervisor) AS nombre
                FROM supervision_puesto
                WHERE supervisor IS NOT NULL AND TRIM(supervisor) <> ''
                {rol_cond}
            ) q
            WHERE nombre IS NOT NULL AND nombre <> ''
            ORDER BY nombre
        """, rol_params + rol_params)
        nombres = [r['nombre'] for r in cur.fetchall()]
        return jsonify({'nombres': nombres})
    except Exception as e:
        app_logger.error(f"api_gestion_nombres_por_rol error: {e}", exc_info=True)
        return jsonify({'nombres': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/gestion/data')
@jwt_required()
@admin_required
def api_gestion_data():
    cliente = request.args.get('cliente') or None
    proyecto = request.args.get('proyecto') or None
    turno = request.args.get('turno') or None
    # Accept comma-separated multi-values for year and month
    year  = request.args.get('year')  or None  # kept as string; may be "2024,2025"
    month = request.args.get('month') or None  # kept as string; may be "1,2,3"
    day   = int(request.args.get('day')) if request.args.get('day') else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        sat_conds, sat_params = [], []
        if cliente:
            sat_conds.append("cliente_instalacion = %s")
            sat_params.append(cliente)
        if turno:
            sat_conds.append("LOWER(COALESCE(rol_aplicador, '')) = %s")
            sat_params.append(turno.lower())
        _gestion_add_multi_date_filter(sat_conds, sat_params, "fecha_hora::TEXT", year, month, day)
        sat_where = _gestion_where(sat_conds)
        cur.execute(f"""
            SELECT
                AVG(NULLIF(calificacion_global_nps::TEXT, '')::NUMERIC) AS avg_global,
                COUNT(*) AS total,
                SUM(CASE WHEN LOWER(COALESCE(recomendaria_servicio::TEXT, '')) IN ('sí','si','yes','s') THEN 1 ELSE 0 END) AS recomienda
            FROM medicion_experiencia_cliente
            {sat_where}
        """, sat_params)
        sat_row = cur.fetchone()
        sat_avg = float(sat_row['avg_global']) if sat_row and sat_row['avg_global'] is not None else None
        sat_total = int(sat_row['total'] or 0) if sat_row else 0
        sat_recomienda = int(sat_row['recomienda'] or 0) if sat_row else 0
        sat_recomienda_pct = round(sat_recomienda / sat_total * 100, 1) if sat_total else None
        sat_score = _gestion_clamp((sat_avg / 40) * 100 if sat_avg is not None else None)

        inc_conds, inc_params = [], []
        if cliente:
            inc_conds.append("cliente_instalacion = %s")
            inc_params.append(cliente)
        if proyecto:
            inc_conds.append("puesto_area_especifica = %s")
            inc_params.append(proyecto)
        if turno:
            inc_conds.append("LOWER(COALESCE(turno, '')) = %s")
            inc_params.append(turno.lower())
        _gestion_add_multi_date_filter(inc_conds, inc_params, "fecha_hora::TEXT", year, month, day)
        inc_where = _gestion_where(inc_conds)
        cur.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN LOWER(COALESCE(nivel_severidad, '')) = 'alto' THEN 1 ELSE 0 END) AS alto,
                AVG(CASE WHEN tiempo_resolucion_min IS NOT NULL AND tiempo_resolucion_min > 0 THEN tiempo_resolucion_min END) AS resolucion
            FROM reportes_incidentes
            {inc_where}
        """, inc_params)
        inc_row = cur.fetchone()
        inc_total = int(inc_row['total'] or 0) if inc_row else 0
        inc_alto = int(inc_row['alto'] or 0) if inc_row else 0
        inc_res = float(inc_row['resolucion']) if inc_row and inc_row['resolucion'] is not None else None
        inc_pct_alto = round(inc_alto / inc_total * 100, 1) if inc_total else 0
        inc_score = _gestion_clamp(100 - min(inc_total * 4, 60) - min(inc_pct_alto * 1.2, 40))

        sup_safe = r"""CASE
            WHEN TRIM(%s::TEXT) ~ '^[0-9]+(\\.[0-9]+)?$' THEN TRIM(%s::TEXT)::NUMERIC
            WHEN LOWER(TRIM(%s::TEXT)) = 'excelente' THEN 5
            WHEN LOWER(TRIM(%s::TEXT)) IN ('bueno','bien') THEN 4
            WHEN LOWER(TRIM(%s::TEXT)) IN ('regular','aceptable') THEN 3
            WHEN LOWER(TRIM(%s::TEXT)) IN ('malo','deficiente') THEN 2
            WHEN LOWER(TRIM(%s::TEXT)) IN ('pesimo','pésimo','muy malo') THEN 1
            ELSE NULL
        END"""
        def _sup_expr(col):
            return sup_safe % (col, col, col, col, col, col)
        sup_score_expr = " + ".join(f"COALESCE(({_sup_expr(f)}),0)" for f, _ in _SUP_CRITERIA)
        sup_conds, sup_params = [], []
        if cliente:
            sup_conds.append("cliente = %s")
            sup_params.append(cliente)
        if turno:
            sup_conds.append("LOWER(COALESCE(rol_aplicador, '')) = %s")
            sup_params.append(turno.lower())
        _gestion_add_multi_date_filter(sup_conds, sup_params, "fecha_hora::TEXT", year, month, day)
        sup_where = _gestion_where(sup_conds)
        cur.execute(f"""
            SELECT
                COUNT(*) AS total,
                AVG({sup_score_expr}) AS avg_score,
                SUM(CASE WHEN ({sup_score_expr}) <= 15 AND ({sup_score_expr}) > 0 THEN 1 ELSE 0 END) AS criticos
            FROM supervision_puesto
            {sup_where}
        """, sup_params)
        sup_row = cur.fetchone()
        sup_total = int(sup_row['total'] or 0) if sup_row else 0
        sup_avg = float(sup_row['avg_score']) if sup_row and sup_row['avg_score'] is not None else None
        sup_criticos = int(sup_row['criticos'] or 0) if sup_row else 0
        sup_score = _gestion_clamp((sup_avg / 25) * 100 if sup_avg is not None else None)

        cum_conds, cum_params = [], []
        if cliente:
            cum_conds.append("cliente_instalacion = %s")
            cum_params.append(cliente)
        if proyecto:
            cum_conds.append("puesto_area_especifica = %s")
            cum_params.append(proyecto)
        if turno:
            cum_conds.append("LOWER(COALESCE(turno, '')) = %s")
            cum_params.append(turno.lower())
        _gestion_add_extract_date_filter(cum_conds, cum_params, "fecha_hora", year, month, day)
        cum_where = _gestion_where(cum_conds)
        cur.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN LOWER(TRIM(nivel_cumplimiento)) = 'cumple' THEN 1 ELSE 0 END) AS cumple,
                SUM(CASE WHEN vigencia_hasta IS NOT NULL AND vigencia_hasta < CURRENT_DATE THEN 1 ELSE 0 END) AS vencidos
            FROM checklist_cumplimiento
            {cum_where}
        """, cum_params)
        cum_row = cur.fetchone()
        cum_total = int(cum_row['total'] or 0) if cum_row else 0
        cum_cumple = int(cum_row['cumple'] or 0) if cum_row else 0
        cum_vencidos = int(cum_row['vencidos'] or 0) if cum_row else 0
        cum_pct = round(cum_cumple / cum_total * 100, 1) if cum_total else None
        cum_score = cum_pct

        cap_date_expr = _capac_date_expr()
        cap_safe_len = _capac_safe_len()
        cap_conds, cap_params = [], []
        if cliente:
            cap_conds.append("cliente_instalacion = %s")
            cap_params.append(cliente)
        if proyecto:
            cap_conds.append("puesto_area_especifica = %s")
            cap_params.append(proyecto)
        if turno:
            cap_conds.append("LOWER(COALESCE(turno, '')) = %s")
            cap_params.append(turno.lower())
        _gestion_add_extract_date_filter(cap_conds, cap_params, cap_date_expr, year, month, day)
        cap_where = _gestion_where(cap_conds)
        cur.execute(f"""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM({cap_safe_len}), 0) AS asistentes,
                ROUND(COALESCE(AVG(NULLIF({cap_safe_len}, 0)), 0), 1) AS promedio
            FROM registro_de_capacitaciones
            {cap_where}
        """, cap_params)
        cap_row = cur.fetchone()
        cap_total = int(cap_row['total'] or 0) if cap_row else 0
        cap_asist = int(cap_row['asistentes'] or 0) if cap_row else 0
        cap_prom = float(cap_row['promedio'] or 0) if cap_row else 0
        cap_score = _gestion_clamp(cap_prom * 5 if cap_total else 0)

        disc_conds, disc_params = [], []
        if cliente:
            disc_conds.append("cliente_instalacion = %s")
            disc_params.append(cliente)
        if proyecto:
            disc_conds.append("puesto_area_especifica = %s")
            disc_params.append(proyecto)
        if turno:
            disc_conds.append("LOWER(COALESCE(turno, '')) = %s")
            disc_params.append(turno.lower())
        _gestion_add_multi_date_filter(disc_conds, disc_params, "fecha_hora::TEXT", year, month, day)
        disc_where = _gestion_where(disc_conds)
        cur.execute(f"""
            WITH emp_counts AS (
                SELECT COALESCE(NULLIF(TRIM(empleado_numero), ''), empleado_nombre) AS emp_key,
                       COUNT(*) AS cnt
                FROM informe_novedades_disciplinario
                {disc_where}
                GROUP BY emp_key
            )
            SELECT
                COALESCE(SUM(cnt), 0) AS total,
                COUNT(*) AS total_empleados,
                COALESCE(ROUND(
                    100.0 * SUM(CASE WHEN cnt > 1 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0)
                , 1), 0) AS pct_reincidencia,
                SUM(CASE WHEN cnt > 3 THEN 1 ELSE 0 END) AS criticos
            FROM emp_counts
        """, disc_params)
        disc_row = cur.fetchone()
        disc_total = int(disc_row['total'] or 0) if disc_row else 0
        disc_reinc = float(disc_row['pct_reincidencia'] or 0) if disc_row else 0
        disc_crit = int(disc_row['criticos'] or 0) if disc_row else 0
        disc_score = _gestion_clamp(100 - (disc_reinc * 1.1) - (disc_crit * 8))

        vis_date_expr = _visita_date_expr()
        vis_conds, vis_params = [], []
        if cliente:
            vis_conds.append("cliente_instalacion = %s")
            vis_params.append(cliente)
        if proyecto:
            vis_conds.append("puesto_area_especifica = %s")
            vis_params.append(proyecto)
        if turno:
            vis_conds.append("LOWER(COALESCE(turno, '')) = %s")
            vis_params.append(turno.lower())
        _gestion_add_extract_date_filter(vis_conds, vis_params, vis_date_expr, year, month, day)
        vis_where = _gestion_where(vis_conds)
        cur.execute(f"""
            SELECT
                COUNT(*) AS total_visitas,
                COUNT(DISTINCT NULLIF(TRIM(cliente_instalacion), '')) AS clientes_visitados
            FROM registro_y_acta_de_visita
            {vis_where}
        """, vis_params)
        vis_row = cur.fetchone()
        vis_total = int(vis_row['total_visitas'] or 0) if vis_row else 0
        vis_clientes = int(vis_row['clientes_visitados'] or 0) if vis_row else 0
        vis_promedio = round(vis_total / vis_clientes, 1) if vis_clientes else 0
        vis_score = _gestion_clamp(55 + (vis_promedio * 15) if vis_total else 0)

        veh_date_expr = _veh_date_expr()
        veh_conds, veh_params = [], []
        if cliente:
            veh_conds.append("cliente_instalacion = %s")
            veh_params.append(cliente)
        if proyecto:
            veh_conds.append("puesto_area_especifica = %s")
            veh_params.append(proyecto)
        if turno:
            veh_conds.append("LOWER(COALESCE(turno, '')) = %s")
            veh_params.append(turno.lower())
        _gestion_add_extract_date_filter(veh_conds, veh_params, veh_date_expr, year, month, day)
        veh_where = _gestion_where(veh_conds)
        cur.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN {_VEH_FAULT_EXPR} THEN 1 ELSE 0 END) AS no_aptas,
                SUM({_VEH_FAULT_SUM}) AS total_fallas
            FROM planilla_vehicular
            {veh_where}
        """, veh_params)
        veh_row = cur.fetchone()
        veh_total = int(veh_row['total'] or 0) if veh_row else 0
        veh_no_aptas = int(veh_row['no_aptas'] or 0) if veh_row else 0
        veh_fallas = int(veh_row['total_fallas'] or 0) if veh_row else 0
        veh_aptas_pct = round((veh_total - veh_no_aptas) / veh_total * 100, 1) if veh_total else None
        veh_score = veh_aptas_pct

        eq_conds, eq_params = [], []
        if cliente:
            eq_conds.append("c.cliente_instalacion = %s")
            eq_params.append(cliente)
        if proyecto:
            eq_conds.append("c.sitio = %s")
            eq_params.append(proyecto)
        if year:
            eq_conds.append("EXTRACT(YEAR FROM c.fecha) = %s")
            eq_params.append(year)
        if month:
            eq_conds.append("EXTRACT(MONTH FROM c.fecha) = %s")
            eq_params.append(month)
        if day:
            eq_conds.append("EXTRACT(DAY FROM c.fecha) = %s")
            eq_params.append(day)
        eq_where = _eq_where(eq_conds)
        eq_lateral = f"""
            FROM confiabilidad_equipos c,
                 LATERAL jsonb_array_elements(c.inventario) AS elem
            {eq_where}
        """
        cur.execute(f"""
            SELECT
                COUNT(DISTINCT c.id) AS total_registros,
                SUM({_EQ_TOTAL_SQL}) AS total_equipos,
                SUM({_EQ_FUNC_SQL}) AS funcionando
            {eq_lateral}
        """, eq_params)
        eq_row = cur.fetchone()
        eq_regs = int(eq_row['total_registros'] or 0) if eq_row else 0
        eq_total = int(eq_row['total_equipos'] or 0) if eq_row else 0
        eq_func = int(eq_row['funcionando'] or 0) if eq_row else 0
        eq_pct = round(eq_func / eq_total * 100, 1) if eq_total else None
        eq_score = eq_pct

        personal_score = None
        if sup_score is not None or disc_score is not None:
            vals = [v for v in [sup_score, disc_score] if v is not None]
            personal_score = round(sum(vals) / len(vals), 1) if vals else None

        cards = [
            _gestion_score_payload('satisfaccion', 'Satisfacción', '/dashboard/satisfaccion/', sat_score,
                                   round(sat_avg, 1) if sat_avg is not None else '—', 'Promedio global NPS',
                                   f"{sat_recomienda_pct}%" if sat_recomienda_pct is not None else '—', '% recomienda'),
            _gestion_score_payload('incidentes', 'Incidentes', '/dashboard/incidentes/', inc_score,
                                   inc_total, 'Total incidentes',
                                   f"{inc_pct_alto}%", 'Severidad alta'),
            _gestion_score_payload('supervision', 'Supervisión', '/dashboard/supervision/', sup_score,
                                   round(sup_avg, 1) if sup_avg is not None else '—', 'Score promedio',
                                   sup_criticos, 'Puestos críticos'),
            _gestion_score_payload('cumplimiento', 'Cumplimiento', '/dashboard/cumplimiento/', cum_score,
                                   f"{cum_pct}%" if cum_pct is not None else '—', '% cumplimiento',
                                   cum_vencidos, 'Certificados vencidos'),
            _gestion_score_payload('capacitacion', 'Capacitación', '/dashboard/capacitacion/', cap_score,
                                   cap_total, 'Sesiones',
                                   cap_asist, 'Asistentes'),
            _gestion_score_payload('disciplina', 'Disciplina', '/dashboard/disciplina/', disc_score,
                                   disc_total, 'Novedades',
                                   f"{disc_reinc}%", 'Reincidencia'),
            _gestion_score_payload('visitas', 'Visitas', '/dashboard/visitas/', vis_score,
                                   vis_total, 'Visitas',
                                   vis_clientes, 'Clientes visitados'),
            _gestion_score_payload('vehiculos', 'Vehículos', '/dashboard/vehiculos/', veh_score,
                                   f"{veh_aptas_pct}%" if veh_aptas_pct is not None else '—', '% aptas',
                                   veh_fallas, 'Fallas registradas'),
            _gestion_score_payload('equipos', 'Equipos', '/dashboard/equipos/', eq_score,
                                   f"{eq_pct}%" if eq_pct is not None else '—', 'Confiabilidad',
                                   eq_total, 'Equipos evaluados'),
        ]
        ranking = sorted(cards, key=lambda item: item['score'] if item['score'] is not None else -1)

        executive_values = [c['score'] for c in cards if c['score'] is not None]
        executive_score = round(sum(executive_values) / len(executive_values), 1) if executive_values else None
        executive_status = _gestion_status(executive_score)

        status_distribution = {'green': 0, 'yellow': 0, 'orange': 0, 'red': 0, 'slate': 0}
        for item in cards:
            tone = item['status']['tone']
            status_distribution[tone] = status_distribution.get(tone, 0) + 1

        highlights = []
        if sat_score is not None and sat_score < 70:
            highlights.append({
                'level': 'warning',
                'module': 'Satisfacción',
                'title': 'Percepción del cliente por debajo del objetivo',
                'detail': f"El promedio global es {round(sat_avg, 1) if sat_avg is not None else 's/d'} y la recomendación llega a {sat_recomienda_pct if sat_recomienda_pct is not None else 's/d'}%.",
                'route': '/dashboard/satisfaccion/',
            })
        if inc_total > 0:
            highlights.append({
                'level': 'danger' if inc_pct_alto >= 25 else 'warning',
                'module': 'Incidentes',
                'title': 'Se requiere seguimiento de incidentes',
                'detail': f"Se registran {inc_total} incidentes y {inc_alto} de severidad alta para el período filtrado.",
                'route': '/dashboard/incidentes/',
            })
        if cum_vencidos > 0:
            highlights.append({
                'level': 'danger',
                'module': 'Cumplimiento',
                'title': 'Hay certificados vencidos',
                'detail': f"Se identifican {cum_vencidos} registros vencidos que impactan el cumplimiento normativo.",
                'route': '/dashboard/cumplimiento/',
            })
        if disc_crit > 0:
            highlights.append({
                'level': 'warning',
                'module': 'Disciplina',
                'title': 'Existen reincidencias críticas',
                'detail': f"Se detectan {disc_crit} colaboradores con más de tres novedades disciplinarias.",
                'route': '/dashboard/disciplina/',
            })
        if veh_no_aptas > 0:
            highlights.append({
                'level': 'warning',
                'module': 'Vehículos',
                'title': 'La flota presenta unidades no aptas',
                'detail': f"Hay {veh_no_aptas} inspecciones con resultado no apto y {veh_fallas} fallas acumuladas.",
                'route': '/dashboard/vehiculos/',
            })
        if eq_pct is not None and eq_pct < 85:
            highlights.append({
                'level': 'warning',
                'module': 'Equipos',
                'title': 'Confiabilidad técnica en seguimiento',
                'detail': f"La confiabilidad general de equipos se ubica en {eq_pct}%.",
                'route': '/dashboard/equipos/',
            })
        if cap_total > 0 and cap_score >= 85:
            highlights.append({
                'level': 'success',
                'module': 'Capacitación',
                'title': 'Buen ritmo de formación',
                'detail': f"Se reportan {cap_total} sesiones y {cap_asist} asistentes en total.",
                'route': '/dashboard/capacitacion/',
            })
        if not highlights:
            highlights.append({
                'level': 'neutral',
                'module': 'General',
                'title': 'Sin alertas críticas para el período',
                'detail': 'Los indicadores consolidados no muestran desvíos relevantes con los filtros aplicados.',
                'route': '/dashboard/gestion/',
            })

        trend_labels = []
        sat_trend_map = {}
        inc_trend_map = {}
        cum_trend_map = {}
        sup_trend_map = {}
        veh_trend_map = {}
        eq_trend_map = {}

        if month and year:
            sat_period_expr = "SUBSTRING(fecha_hora::TEXT, 9, 2)"
            sat_period_label = "SUBSTRING(fecha_hora::TEXT, 9, 2)"
            sat_period_order = "SUBSTRING(fecha_hora::TEXT, 9, 2)"
            extract_period_expr = "LPAD(EXTRACT(DAY FROM {expr})::TEXT, 2, '0')"
            extract_period_label = extract_period_expr
            extract_period_order = extract_period_expr
        else:
            sat_period_expr = "SUBSTRING(fecha_hora::TEXT, 1, 7)"
            sat_period_label = "SUBSTRING(fecha_hora::TEXT, 1, 7)"
            sat_period_order = "SUBSTRING(fecha_hora::TEXT, 1, 7)"
            extract_period_expr = "TO_CHAR(DATE_TRUNC('month', {expr}), 'YYYY-MM')"
            extract_period_label = extract_period_expr
            extract_period_order = extract_period_expr

        cur.execute(f"""
            SELECT {sat_period_label} AS label,
                   ROUND(AVG(NULLIF(calificacion_global_nps::TEXT, '')::NUMERIC)::NUMERIC, 2) AS value
            FROM medicion_experiencia_cliente
            {sat_where}
            GROUP BY {sat_period_expr}
            ORDER BY {sat_period_order}
            LIMIT 24
        """, sat_params)
        for row in cur.fetchall():
            label = row['label']
            sat_trend_map[label] = round((_gestion_clamp((float(row['value']) / 40) * 100)), 1) if row['value'] is not None else None
            trend_labels.append(label)

        cur.execute(f"""
            SELECT {sat_period_label} AS label,
                   COUNT(*) AS value
            FROM reportes_incidentes
            {inc_where}
            GROUP BY {sat_period_expr}
            ORDER BY {sat_period_order}
            LIMIT 24
        """, inc_params)
        for row in cur.fetchall():
            label = row['label']
            inc_trend_map[label] = int(row['value'] or 0)
            if label not in trend_labels:
                trend_labels.append(label)

        cum_period_expr = extract_period_expr.format(expr="fecha_hora")
        cur.execute(f"""
            SELECT {cum_period_label.format(expr="fecha_hora")} AS label,
                   ROUND(
                       100.0 * SUM(CASE WHEN LOWER(TRIM(nivel_cumplimiento)) = 'cumple' THEN 1 ELSE 0 END)
                       / NULLIF(COUNT(*), 0)
                   , 1) AS value
            FROM checklist_cumplimiento
            {cum_where}
            GROUP BY {cum_period_expr}
            ORDER BY {cum_period_order.format(expr="fecha_hora")}
            LIMIT 24
        """, cum_params)
        for row in cur.fetchall():
            label = row['label']
            cum_trend_map[label] = float(row['value']) if row['value'] is not None else None
            if label not in trend_labels:
                trend_labels.append(label)

        cur.execute(f"""
            SELECT {sat_period_label} AS label,
                   ROUND(AVG({sup_score_expr})::NUMERIC, 2) AS value
            FROM supervision_puesto
            {sup_where}
            GROUP BY {sat_period_expr}
            ORDER BY {sat_period_order}
            LIMIT 24
        """, sup_params)
        for row in cur.fetchall():
            label = row['label']
            sup_trend_map[label] = round((_gestion_clamp((float(row['value']) / 25) * 100)), 1) if row['value'] is not None else None
            if label not in trend_labels:
                trend_labels.append(label)

        cur.execute(f"""
            SELECT {extract_period_label.format(expr=veh_date_expr)} AS label,
                   ROUND(
                       100.0 * SUM(CASE WHEN NOT ({_VEH_FAULT_EXPR}) THEN 1 ELSE 0 END)
                       / NULLIF(COUNT(*), 0)
                   , 1) AS value
            FROM planilla_vehicular
            {veh_where}
            GROUP BY {extract_period_expr.format(expr=veh_date_expr)}
            ORDER BY {extract_period_order.format(expr=veh_date_expr)}
            LIMIT 24
        """, veh_params)
        for row in cur.fetchall():
            label = row['label']
            veh_trend_map[label] = float(row['value']) if row['value'] is not None else None
            if label not in trend_labels:
                trend_labels.append(label)

        cur.execute(f"""
            SELECT {extract_period_label.format(expr='c.fecha')} AS label,
                   ROUND(SUM({_EQ_FUNC_SQL})::numeric / NULLIF(SUM({_EQ_TOTAL_SQL}), 0) * 100, 1) AS value
            {eq_lateral}
            GROUP BY {extract_period_expr.format(expr='c.fecha')}
            ORDER BY {extract_period_order.format(expr='c.fecha')}
            LIMIT 24
        """, eq_params)
        for row in cur.fetchall():
            label = row['label']
            eq_trend_map[label] = float(row['value']) if row['value'] is not None else None
            if label not in trend_labels:
                trend_labels.append(label)

        trend_labels = sorted(set(trend_labels))

        return jsonify({
            'filters_meta': {
                'cliente': cliente,
                'proyecto': proyecto,
                'pais': request.args.get('pais') or None,
                'turno': turno,
                'year': year,
                'month': month,
                'day': day,
                'country_supported': False,
                'project_supported_as_site_area': True,
            },
            'executive': {
                'score': executive_score,
                'status': executive_status,
                'total_modules': len(cards),
                'healthy_modules': sum(1 for c in cards if c['status']['tone'] == 'green'),
                'critical_modules': sum(1 for c in cards if c['status']['tone'] in ('red', 'orange')),
                'satisfaccion': {
                    'value': round(sat_avg, 1) if sat_avg is not None else None,
                    'recommendation': sat_recomienda_pct,
                },
                'incidentes': {
                    'value': inc_total,
                    'alto_pct': inc_pct_alto,
                },
                'cumplimiento': {
                    'value': cum_pct,
                    'vencidos': cum_vencidos,
                },
                'equipos': {
                    'value': eq_pct,
                    'total': eq_total,
                },
                'capacitacion': {
                    'value': cap_total,
                    'asistentes': cap_asist,
                },
                'gestion_personal': {
                    'value': personal_score,
                    'criticos': disc_crit + sup_criticos,
                },
            },
            'cards': cards,
            'ranking': ranking,
            'status_distribution': status_distribution,
            'highlights': highlights,
            'trend': {
                'labels': trend_labels,
                'series': [
                    {'label': 'Satisfacción', 'data': [sat_trend_map.get(label) for label in trend_labels], 'type': 'line', 'color': '#16a34a'},
                    {'label': 'Cumplimiento', 'data': [cum_trend_map.get(label) for label in trend_labels], 'type': 'line', 'color': '#0f766e'},
                    {'label': 'Supervisión', 'data': [sup_trend_map.get(label) for label in trend_labels], 'type': 'line', 'color': '#2563eb'},
                    {'label': 'Vehículos aptos', 'data': [veh_trend_map.get(label) for label in trend_labels], 'type': 'line', 'color': '#7c3aed'},
                    {'label': 'Equipos confiables', 'data': [eq_trend_map.get(label) for label in trend_labels], 'type': 'line', 'color': '#475569'},
                ],
                'incidentes': [inc_trend_map.get(label, 0) for label in trend_labels],
            },
        })
    except Exception as e:
        app_logger.error(f"api_gestion_data error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ── Satisfacción Dashboard ────────────────────────────────────────────────────

_SAT_CRITERIA = [
    ('atencion_cliente',     'Atención al cliente'),
    ('comunicacion',         'Comunicación'),
    ('confiabilidad',        'Confiabilidad'),
    ('capacidad_reaccion',   'Cap. de reacción'),
    ('cumplimiento',         'Cumplimiento'),
    ('competencia_personal', 'Competencia personal'),
    ('actitud_servicio',     'Actitud del servicio'),
    ('atencion_quejas',      'Atención quejas'),
]

def _sat_color_criteria(avg):
    if avg is None: return '#6b7280'
    if avg >= 4.25: return '#22c55e'
    if avg >= 3.25: return '#eab308'
    if avg >= 2.25: return '#f97316'
    return '#ef4444'

def _sat_color_global(score):
    # score is now a Likert average (1.0–5.0)
    if score is None: return '#6b7280'
    if score >= 4.5: return '#22c55e'
    if score >= 3.5: return '#eab308'
    if score >= 2.5: return '#f97316'
    if score >= 1.5: return '#ef4444'
    return '#e11d48'

def _sat_label_global(score):
    if score is None: return 'Sin datos'
    if score >= 4.5: return 'Totalmente satisfecho'
    if score >= 3.5: return 'Satisfecho'
    if score >= 2.5: return 'Oportunidades de mejora'
    if score >= 1.5: return 'Insatisfecho'
    return 'Muy insatisfecho'

def _sat_date_prefix(year, month, day):
    """Build an ISO date prefix string for LIKE-based filtering.
    Works regardless of whether fecha_hora is stored as TEXT or TIMESTAMP,
    since casting either to TEXT produces an ISO-sortable string.
    """
    if not year:
        return None
    prefix = f"{year}"
    if month:
        prefix += f"-{month:02d}"
        if day:
            prefix += f"-{day:02d}"
    return prefix


def _sat_add_multi_date_filter(conds, params, text_expr, year, month, day):
    """Multi-value date filter for sub-module _xxx_where helpers.
    Delegates to _gestion_add_multi_date_filter — same logic, shared implementation.
    """
    _gestion_add_multi_date_filter(conds, params, text_expr, year, month, day)


def _sat_where(cliente, year, month, day, responsable=None, nombre_usuario=None):
    conds, params = [], []
    if cliente:
        conds.append("cliente_instalacion = %s")
        params.append(cliente)
    _sat_add_multi_date_filter(conds, params, "fecha_hora::TEXT", year, month, day)
    if responsable:
        conds.append("TRIM(rol_aplicador) = %s")
        params.append(responsable)
    if nombre_usuario:
        conds.append("TRIM(nombre_responsable) = %s")
        params.append(nombre_usuario)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


def _sat_prev_where(cliente, year, month, day):
    """Build WHERE clause for the previous comparison period.
    Accepts comma-separated year/month but uses only the first value
    (prev-period comparison only makes sense for a single period).
    """
    # Coerce to single int
    try:
        year  = int(str(year).split(',')[0].strip())  if year  else None
        month = int(str(month).split(',')[0].strip()) if month else None
    except (ValueError, TypeError):
        year = month = None
    if not year:
        return None, None

    conds, params = [], []
    if cliente:
        conds.append("cliente_instalacion = %s")
        params.append(cliente)

    now = datetime.now(timezone.utc)
    if year and month and day:
        prev = datetime(year, month, day) - timedelta(days=1)
        prefix = f"{prev.year}-{prev.month:02d}-{prev.day:02d}"
    elif year and month:
        prev_month = month - 1 or 12
        prev_year  = year if month > 1 else year - 1
        prefix = f"{prev_year}-{prev_month:02d}"
    elif year:
        prefix = str(year - 1)
    else:
        prefix = str(now.year - 1)

    conds.append("fecha_hora::TEXT LIKE %s")
    params.append(prefix + "%")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


@dashboard_bp.route('/api/satisfaccion/filtros')
@jwt_required()
def api_satisfaccion_filtros():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'responsables': []})
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT TRIM(rol_aplicador) AS responsable
            FROM medicion_experiencia_cliente
            WHERE rol_aplicador IS NOT NULL AND TRIM(rol_aplicador) <> ''
            ORDER BY responsable
        """)
        return jsonify({'responsables': [r[0] for r in cur.fetchall()]})
    except Exception as e:
        app_logger.error(f"api_satisfaccion_filtros error: {e}", exc_info=True)
        return jsonify({'responsables': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/satisfaccion/clientes')
@jwt_required()
def api_satisfaccion_clientes():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'clientes': []})
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT cliente_instalacion
            FROM medicion_experiencia_cliente
            WHERE cliente_instalacion IS NOT NULL AND cliente_instalacion <> ''
            ORDER BY cliente_instalacion
        """)
        return jsonify({'clientes': [r[0] for r in cur.fetchall()]})
    except Exception as e:
        app_logger.error(f"api_satisfaccion_clientes error: {e}", exc_info=True)
        return jsonify({'clientes': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/satisfaccion/debug')
@jwt_required()
@admin_required
def api_satisfaccion_debug():
    """Diagnostic endpoint — shows table shape, column types, sample rows, and a safe count."""
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # Column types
        cur.execute("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'medicion_experiencia_cliente'
            ORDER BY ordinal_position
        """)
        columns = [dict(r) for r in cur.fetchall()]

        # Row count
        cur.execute("SELECT COUNT(*) AS cnt FROM medicion_experiencia_cliente")
        total = cur.fetchone()['cnt']

        # Most recent 3 rows (only safe TEXT columns to avoid cast errors)
        cur.execute("""
            SELECT
                id,
                cliente_instalacion,
                fecha_hora,
                calificacion_global_nps,
                recomendaria_servicio,
                atencion_cliente,
                comunicacion,
                confiabilidad
            FROM medicion_experiencia_cliente
            ORDER BY id DESC
            LIMIT 3
        """)
        sample = []
        for r in cur.fetchall():
            sample.append({k: str(v) if v is not None else None for k, v in r.items()})

        # Test the LIKE date filter
        cur.execute("""
            SELECT COUNT(*) AS cnt
            FROM medicion_experiencia_cliente
            WHERE fecha_hora::TEXT LIKE %s
        """, (f"{datetime.now().year}%",))
        this_year = cur.fetchone()['cnt']

        return jsonify({
            'total_rows': total,
            'this_year_rows': this_year,
            'columns': columns,
            'sample_rows': sample,
        })
    except Exception as e:
        app_logger.error(f"api_satisfaccion_debug error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/satisfaccion/data')
@jwt_required()
def api_satisfaccion_data():
    cliente     = request.args.get('cliente')     or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day         = int(request.args.get('day'))    if request.args.get('day')   else None
    responsable    = request.args.get('responsable')    or None
    nombre_usuario = request.args.get('nombre_usuario') or None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        where,      params      = _sat_where(cliente, year, month, day, responsable=responsable, nombre_usuario=nombre_usuario)
        where_prev, params_prev = _sat_prev_where(cliente, year, month, day)

        # ── Main summary ─────────────────────────────────────────────────
        # Use ::TEXT before NULLIF so it works for both INTEGER and TEXT columns.
        # Use ::NUMERIC for math; PostgreSQL handles '5'::NUMERIC just fine.
        def _safe_avg(col):
            return f"AVG(NULLIF({col}::TEXT, '')::NUMERIC)"
        def _safe_int(col):
            return f"NULLIF({col}::TEXT, '')::NUMERIC"

        criteria_sql_parts = []
        for f, _ in _SAT_CRITERIA:
            criteria_sql_parts.append(f"{_safe_avg(f)} as avg_{f}")
            for rating in range(1, 6):
                criteria_sql_parts.append(f"SUM(CASE WHEN {_safe_int(f)} = {rating} THEN 1 ELSE 0 END) as {f}_{rating}")
        
        criteria_sql = ",\n                ".join(criteria_sql_parts)

        rec_check = """LOWER(COALESCE(recomendaria_servicio::TEXT, '')) IN ('sí','si','yes','s')"""

        cur.execute(f"""
            SELECT
                {_safe_avg('calificacion_global_nps')}                             AS avg_global,
                COUNT(*)                                                            AS total,
                SUM(CASE WHEN {rec_check} THEN 1 ELSE 0 END)                       AS total_si,
                SUM(CASE WHEN {_safe_int('calificacion_global_nps')} >= 4.5 THEN 1 ELSE 0 END) AS dist_satisfecho,
                SUM(CASE WHEN {_safe_int('calificacion_global_nps')} >= 3.5 AND {_safe_int('calificacion_global_nps')} < 4.5 THEN 1 ELSE 0 END) AS dist_oportunidad,
                SUM(CASE WHEN {_safe_int('calificacion_global_nps')} >= 2.5 AND {_safe_int('calificacion_global_nps')} < 3.5 THEN 1 ELSE 0 END) AS dist_baja,
                SUM(CASE WHEN {_safe_int('calificacion_global_nps')} >= 1.5 AND {_safe_int('calificacion_global_nps')} < 2.5 THEN 1 ELSE 0 END) AS dist_insatisfecho,
                SUM(CASE WHEN {_safe_int('calificacion_global_nps')} > 0 AND {_safe_int('calificacion_global_nps')} < 1.5 THEN 1 ELSE 0 END) AS dist_muy_insatisfecho,
                {criteria_sql}
            FROM medicion_experiencia_cliente
            {where}
        """, params)
        row = cur.fetchone()

        avg_global = float(row['avg_global']) if row['avg_global'] else None
        total      = int(row['total'])        if row['total']      else 0
        total_si   = int(row['total_si'])     if row['total_si']   else 0

        # ── Previous period ───────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                {_safe_avg('calificacion_global_nps')} AS avg_global,
                COUNT(*) AS total,
                SUM(CASE WHEN {rec_check} THEN 1 ELSE 0 END) AS total_si
            FROM medicion_experiencia_cliente
            {where_prev}
        """, params_prev)
        prev = cur.fetchone()
        avg_global_prev = float(prev['avg_global']) if prev and prev['avg_global'] else None
        total_prev      = int(prev['total'])        if prev and prev['total']      else 0
        total_si_prev   = int(prev['total_si'])     if prev and prev['total_si']   else 0

        def pct_change(curr, prev_val):
            if curr is None or not prev_val or prev_val == 0: return None
            return round((curr - prev_val) / prev_val * 100, 1)

        pct_rec      = round(total_si / total * 100, 1)           if total      else None
        pct_rec_prev = round(total_si_prev / total_prev * 100, 1) if total_prev else None

        # ── Criteria (sorted ascending) ───────────────────────────────────
        criteria = []
        for field, label in _SAT_CRITERIA:
            avg = float(row[f'avg_{field}']) if row[f'avg_{field}'] else None
            
            counts = {r: int(row[f'{field}_{r}']) if row[f'{field}_{r}'] else 0 for r in range(1, 6)}
            total_c = sum(counts.values())
            pcts = {r: round((counts[r] / total_c) * 100, 1) if total_c > 0 else 0 for r in range(1, 6)}
            
            criteria.append({
                'label': label,
                'avg':   round(avg, 2) if avg is not None else None,
                'color': _sat_color_criteria(avg),
                'pcts':  pcts,
            })
        criteria.sort(key=lambda x: x['avg'] if x['avg'] is not None else 0)

        # ── Trend — use SUBSTRING on TEXT to avoid TIMESTAMP cast issues ───
        # fecha_hora is stored as 'YYYY-MM-DDTHH:MM' so SUBSTRING works safely.
        if month and year:
            # Daily: group by day number within the month
            group_expr = "SUBSTRING(fecha_hora::TEXT, 9, 2)"   # 'DD'
            label_expr = "SUBSTRING(fecha_hora::TEXT, 9, 2)"   # 'DD'
        elif year:
            # Monthly: group by 'YYYY-MM'
            group_expr = "SUBSTRING(fecha_hora::TEXT, 1, 7)"   # 'YYYY-MM'
            label_expr = "SUBSTRING(fecha_hora::TEXT, 1, 7)"
        else:
            # All time: group by 'YYYY-MM'
            group_expr = "SUBSTRING(fecha_hora::TEXT, 1, 7)"
            label_expr = "SUBSTRING(fecha_hora::TEXT, 1, 7)"

        trend_criteria_sql = ", ".join(
            f"{_safe_avg(f)} as avg_{f}"
            for f, _ in _SAT_CRITERIA
        )

        cur.execute(f"""
            SELECT
                {group_expr}                                              AS period_num,
                {label_expr}                                              AS period_label,
                {_safe_avg('calificacion_global_nps')}                    AS avg_global,
                {trend_criteria_sql},
                COUNT(*)                                                   AS cnt
            FROM medicion_experiencia_cliente
            {where}
            GROUP BY period_num
            ORDER BY period_num
            LIMIT 24
        """, params)
        trend_rows = cur.fetchall()

        return jsonify({
            'kpi': {
                'avg_global':            round(avg_global, 1) if avg_global is not None else None,
                'avg_global_color':      _sat_color_global(avg_global),
                'avg_global_label':      _sat_label_global(avg_global),
                'pct_change_global':     pct_change(avg_global, avg_global_prev),
                'total':                 total,
                'pct_change_total':      pct_change(total, total_prev),
                'pct_recomienda':        pct_rec,
                'pct_change_recomienda': pct_change(pct_rec, pct_rec_prev),
            },
            'criteria': criteria,
            'distribution': {
                'satisfecho':        int(row['dist_satisfecho'])        if row['dist_satisfecho']        else 0,
                'oportunidad':       int(row['dist_oportunidad'])       if row['dist_oportunidad']       else 0,
                'baja':              int(row['dist_baja'])              if row['dist_baja']              else 0,
                'insatisfecho':      int(row['dist_insatisfecho'])      if row['dist_insatisfecho']      else 0,
                'muy_insatisfecho':  int(row['dist_muy_insatisfecho'])  if row['dist_muy_insatisfecho']  else 0,
            },
            'recommendation': {
                'si': total_si,
                'no': total - total_si,
            },
            'trend': {
                'labels': [r['period_label'] for r in trend_rows],
                'global': [round(float(r['avg_global']), 2) if r['avg_global'] else None for r in trend_rows],
                'criteria': [
                    {
                        'label': label,
                        'data': [round(float(r[f'avg_{f}']), 2) if r[f'avg_{f}'] else None for r in trend_rows]
                    }
                    for f, label in _SAT_CRITERIA
                ],
                'counts': [int(r['cnt']) for r in trend_rows],
            },
        })
    except Exception as e:
        app_logger.error(f"api_satisfaccion_data error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/satisfaccion/detalles')
@jwt_required()
def api_satisfaccion_detalles():
    cliente        = request.args.get('cliente')        or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day            = int(request.args.get('day'))    if request.args.get('day')   else None
    responsable    = request.args.get('responsable')    or None
    nombre_usuario = request.args.get('nombre_usuario') or None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        where, params = _sat_where(cliente, year, month, day, responsable=responsable, nombre_usuario=nombre_usuario)
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'medicion_experiencia_cliente'
        """)
        available_columns = {r['column_name'] for r in cur.fetchall()}
        detail_criteria = [(col, label) for col, label in _SAT_CRITERIA if col in available_columns]
        criteria_select = ",\n                ".join(col for col, _ in detail_criteria)
        criteria_select_sql = f",\n                {criteria_select}" if criteria_select else ""

        cur.execute(f"""
            SELECT 
                id_encuesta as id, 
                fecha_hora, 
                cliente_instalacion, 
                encuestado,
                recomendaria_servicio,
                calificacion_global_nps
                {criteria_select_sql}
            FROM medicion_experiencia_cliente 
            {where}
            ORDER BY fecha_hora DESC NULLS LAST, id_encuesta DESC
            LIMIT 500
        """, params)
        rows = cur.fetchall()
        
        detalles = []
        for r in rows:
            fh = r['fecha_hora']
            fh_str = fh.strftime('%Y-%m-%d %H:%M') if hasattr(fh, 'strftime') else str(fh)
            if fh_str == 'None': fh_str = '—'

            calc = r['calificacion_global_nps']
            calc_str = str(calc) if calc is not None and str(calc).strip() != '' else '—'
            try:
                calc_val = float(calc) if calc is not None and str(calc).strip() != '' else None
            except (TypeError, ValueError):
                calc_val = None

            if calc_val is None:
                calc_bucket = 'sin_dato'
            elif calc_val >= 34:
                calc_bucket = 'satisfecho'
            elif calc_val >= 26:
                calc_bucket = 'oportunidad'
            elif calc_val >= 18:
                calc_bucket = 'baja'
            else:
                calc_bucket = 'insatisfecho'

            recomienda_raw = (r['recomendaria_servicio'] or '').strip()
            recomienda_norm = recomienda_raw.lower()

            criterios = {}
            for col, label in _SAT_CRITERIA:
                if col not in available_columns:
                    criterios[label] = None
                    continue
                raw_val = r[col]
                try:
                    criterios[label] = float(raw_val) if raw_val is not None and str(raw_val).strip() != '' else None
                except (TypeError, ValueError):
                    criterios[label] = None

            detalles.append({
                'id': r['id'] if r['id'] else 0,
                'fecha_hora': fh_str,
                'cliente': r['cliente_instalacion'] if r['cliente_instalacion'] else '—',
                'encuestado': r['encuestado'] if 'encuestado' in r and r['encuestado'] else '—',
                'recomendaria': r['recomendaria_servicio'] if r['recomendaria_servicio'] else '—',
                'recomienda_bool': recomienda_norm in ('si', 'sí', 'yes', 's'),
                'calificacion': calc_str,
                'calificacion_valor': calc_val,
                'calificacion_bucket': calc_bucket,
                'criterios': criterios,
            })
            
        return jsonify({'detalles': detalles})
    except Exception as e:
        app_logger.error(f"api_satisfaccion_detalles error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ── Incidentes Dashboard ─────────────────────────────────────────────────────

def _inc_where(cliente, year, month, day, categoria=None, severidad=None, turno=None, responsable=None):
    conds, params = [], []
    if cliente:
        conds.append("cliente_instalacion = %s")
        params.append(cliente)
    _sat_add_multi_date_filter(conds, params, "fecha_hora::TEXT", year, month, day)
    if categoria:
        conds.append("categoria = %s")
        params.append(categoria)
    if severidad:
        conds.append("nivel_severidad = %s")
        params.append(severidad)
    if turno:
        conds.append("turno = %s")
        params.append(turno)
    if responsable:
        conds.append("TRIM(nombre_responsable) = %s")
        params.append(responsable)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


def _inc_prev_where(cliente, year, month, day):
    # Coerce to single int — prev-period comparison only works for a single period
    try:
        year  = int(str(year).split(',')[0].strip())  if year  else None
        month = int(str(month).split(',')[0].strip()) if month else None
    except (ValueError, TypeError):
        year = month = None

    if not year:
        return None, None
    conds, params = [], []
    if cliente:
        conds.append("cliente_instalacion = %s")
        params.append(cliente)
    now = datetime.now(timezone.utc)
    if year and month and day:
        prev = datetime(year, month, day) - timedelta(days=1)
        prefix = f"{prev.year}-{prev.month:02d}-{prev.day:02d}"
    elif year and month:
        prev_month = month - 1 or 12
        prev_year  = year if month > 1 else year - 1
        prefix = f"{prev_year}-{prev_month:02d}"
    elif year:
        prefix = str(year - 1)
    else:
        prefix = str(now.year - 1)
    conds.append("fecha_hora::TEXT LIKE %s")
    params.append(prefix + "%")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


@dashboard_bp.route('/api/incidentes/filtros')
@jwt_required()
def api_incidentes_filtros():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'responsables': []})
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT TRIM(nombre_responsable) AS responsable
            FROM reportes_incidentes
            WHERE nombre_responsable IS NOT NULL AND TRIM(nombre_responsable) <> ''
            ORDER BY responsable
        """)
        return jsonify({'responsables': [r[0] for r in cur.fetchall()]})
    except Exception as e:
        app_logger.error(f"api_incidentes_filtros error: {e}", exc_info=True)
        return jsonify({'responsables': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/incidentes/clientes')
@jwt_required()
def api_incidentes_clientes():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'clientes': []})
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT cliente_instalacion
            FROM reportes_incidentes
            WHERE cliente_instalacion IS NOT NULL AND cliente_instalacion <> ''
            ORDER BY cliente_instalacion
        """)
        return jsonify({'clientes': [r[0] for r in cur.fetchall()]})
    except Exception as e:
        app_logger.error(f"api_incidentes_clientes error: {e}", exc_info=True)
        return jsonify({'clientes': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/incidentes/data')
@jwt_required()
def api_incidentes_data():
    cliente     = request.args.get('cliente')     or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day         = int(request.args.get('day'))    if request.args.get('day')   else None
    categoria   = request.args.get('categoria')   or None
    severidad   = request.args.get('severidad')   or None
    responsable = request.args.get('responsable') or None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        where, params           = _inc_where(cliente, year, month, day, categoria, severidad, responsable=responsable)
        where_prev, params_prev = _inc_prev_where(cliente, year, month, day)

        # ── KPI summary ───────────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN nivel_severidad = 'Alto' THEN 1 ELSE 0 END) AS total_alto,
                AVG(CASE WHEN tiempo_resolucion_min IS NOT NULL AND tiempo_resolucion_min > 0
                         THEN tiempo_resolucion_min END) AS avg_resolucion,
                SUM(CASE WHEN nivel_severidad = 'Bajo'  THEN 1 ELSE 0 END) AS sev_bajo,
                SUM(CASE WHEN nivel_severidad = 'Medio' THEN 1 ELSE 0 END) AS sev_medio,
                SUM(CASE WHEN nivel_severidad = 'Alto'  THEN 1 ELSE 0 END) AS sev_alto,
                SUM(CASE WHEN turno = 'Diurno'   THEN 1 ELSE 0 END) AS turno_diurno,
                SUM(CASE WHEN turno = 'Nocturno' THEN 1 ELSE 0 END) AS turno_nocturno
            FROM reportes_incidentes
            {where}
        """, params)
        row = cur.fetchone()

        total         = int(row['total'])          if row['total']         else 0
        total_alto    = int(row['total_alto'])      if row['total_alto']    else 0
        avg_resolucion = float(row['avg_resolucion']) if row['avg_resolucion'] else None

        # ── Previous period ───────────────────────────────────────────────
        total_prev = 0
        total_alto_prev = 0
        avg_resolucion_prev = None
        if where_prev is not None:
            cur.execute(f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN nivel_severidad = 'Alto' THEN 1 ELSE 0 END) AS total_alto,
                    SUM(CASE WHEN nivel_severidad = 'Medio' THEN 1 ELSE 0 END) AS total_medio,
                    SUM(CASE WHEN nivel_severidad = 'Bajo' THEN 1 ELSE 0 END) AS total_bajo,
                    AVG(CASE WHEN tiempo_resolucion_min IS NOT NULL AND tiempo_resolucion_min > 0
                             THEN tiempo_resolucion_min END) AS avg_resolucion
                FROM reportes_incidentes
                {where_prev}
            """, params_prev)
            prev = cur.fetchone()
            if prev:
                total_prev          = int(prev['total'])           if prev['total']          else 0
                total_alto_prev     = int(prev['total_alto'])      if prev['total_alto']     else 0
                total_medio_prev    = int(prev['total_medio'])     if prev['total_medio']    else 0
                total_bajo_prev     = int(prev['total_bajo'])      if prev['total_bajo']     else 0
                avg_resolucion_prev = float(prev['avg_resolucion']) if prev['avg_resolucion'] else None

        def pct_change(curr, prev_val):
            if curr is None or not prev_val or prev_val == 0: return None
            return round((curr - prev_val) / prev_val * 100, 1)

        pct_alto      = round(total_alto / total * 100, 1) if total else None
        pct_alto_prev = round(total_alto_prev / total_prev * 100, 1) if total_prev else None

        pct_medio      = round(int(row['sev_medio']) / total * 100, 1) if total else None
        pct_medio_prev = round(total_medio_prev / total_prev * 100, 1) if total_prev else None

        pct_bajo      = round(int(row['sev_bajo']) / total * 100, 1) if total else None
        pct_bajo_prev = round(total_bajo_prev / total_prev * 100, 1) if total_prev else None

        # ── Category breakdown ────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                COALESCE(NULLIF(TRIM(categoria), ''), 'Sin categoría') AS cat,
                COUNT(*) AS cnt
            FROM reportes_incidentes
            {where}
            GROUP BY cat
            ORDER BY cnt DESC
            LIMIT 10
        """, params)
        categorias = [{'label': r['cat'], 'count': int(r['cnt'])} for r in cur.fetchall()]

        # ── Trend ─────────────────────────────────────────────────────────
        if month and year:
            group_expr = "SUBSTRING(fecha_hora::TEXT, 9, 2)"
            label_expr = "SUBSTRING(fecha_hora::TEXT, 9, 2)"
        else:
            group_expr = "SUBSTRING(fecha_hora::TEXT, 1, 7)"
            label_expr = "SUBSTRING(fecha_hora::TEXT, 1, 7)"

        cur.execute(f"""
            SELECT
                {group_expr} AS period_num,
                {label_expr} AS period_label,
                COUNT(*) AS cnt
            FROM reportes_incidentes
            {where}
            GROUP BY period_num
            ORDER BY period_num
            LIMIT 24
        """, params)
        trend_rows = cur.fetchall()

        return jsonify({
            'kpi': {
                'total':                 total,
                'pct_change_total':      pct_change(total, total_prev),
                'total_alto':            total_alto,
                'pct_alto':              pct_alto,
                'pct_change_alto':       pct_change(pct_alto, pct_alto_prev),
                'total_medio':           int(row['sev_medio']) if row['sev_medio'] else 0,
                'pct_medio':             pct_medio,
                'pct_change_medio':      pct_change(pct_medio, pct_medio_prev),
                'total_bajo':            int(row['sev_bajo']) if row['sev_bajo'] else 0,
                'pct_bajo':              pct_bajo,
                'pct_change_bajo':       pct_change(pct_bajo, pct_bajo_prev),
                'avg_resolucion':        round(avg_resolucion, 0) if avg_resolucion is not None else None,
                'pct_change_resolucion': pct_change(avg_resolucion, avg_resolucion_prev),
            },
            'severidad': {
                'bajo':   int(row['sev_bajo'])   if row['sev_bajo']   else 0,
                'medio':  int(row['sev_medio'])  if row['sev_medio']  else 0,
                'alto':   int(row['sev_alto'])   if row['sev_alto']   else 0,
            },
            'turno': {
                'diurno':   int(row['turno_diurno'])   if row['turno_diurno']   else 0,
                'nocturno': int(row['turno_nocturno']) if row['turno_nocturno'] else 0,
            },
            'categorias': categorias,
            'trend': {
                'labels': [r['period_label'] for r in trend_rows],
                'counts': [int(r['cnt'])      for r in trend_rows],
            },
        })
    except Exception as e:
        app_logger.error(f"api_incidentes_data error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/incidentes/detalles')
@jwt_required()
def api_incidentes_detalles():
    cliente   = request.args.get('cliente')   or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day       = int(request.args.get('day'))   if request.args.get('day')   else None
    categoria = request.args.get('categoria') or None
    severidad = request.args.get('severidad') or None
    turno     = request.args.get('turno')     or None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        where, params = _inc_where(cliente, year, month, day, categoria, severidad, turno)

        # Fetch with optional tracking columns (may not exist yet)
        try:
            cur.execute(f"""
                SELECT
                    id_reporte_incidente,
                    fecha_hora,
                    cliente_instalacion,
                    categoria,
                    tipo_incidente,
                    nivel_severidad,
                    turno,
                    estado,
                    nombre_responsable,
                    accion_seguimiento,
                    evidencia_seguimiento_url
                FROM reportes_incidentes
                {where}
                ORDER BY fecha_hora DESC NULLS LAST, id_reporte_incidente DESC
                LIMIT 500
            """, params)
        except Exception:
            conn.rollback()
            cur.execute(f"""
                SELECT
                    id_reporte_incidente,
                    fecha_hora,
                    cliente_instalacion,
                    categoria,
                    tipo_incidente,
                    nivel_severidad,
                    turno,
                    estado,
                    nombre_responsable
                FROM reportes_incidentes
                {where}
                ORDER BY fecha_hora DESC NULLS LAST, id_reporte_incidente DESC
                LIMIT 500
            """, params)
        rows = cur.fetchall()

        detalles = []
        for r in rows:
            fh = r['fecha_hora']
            fh_str = fh.strftime('%Y-%m-%d %H:%M') if hasattr(fh, 'strftime') else str(fh)
            if fh_str == 'None': fh_str = '—'
            detalles.append({
                'id':              r['id_reporte_incidente'] or 0,
                'fecha_hora':      fh_str,
                'cliente':         r['cliente_instalacion'] or '—',
                'categoria':       r['categoria']            or '—',
                'tipo':            r['tipo_incidente']       or '—',
                'severidad':       r['nivel_severidad']      or '—',
                'turno':           r['turno']                or '—',
                'estado':          r['estado']               or 'Reportado',
                'responsable':     r['nombre_responsable']   or '—',
                'accion_tomada':   dict(r).get('accion_seguimiento') or '',
                'evidencia_url':   dict(r).get('evidencia_seguimiento_url') or '',
            })
        return jsonify({'detalles': detalles})
    except Exception as e:
        app_logger.error(f"api_incidentes_detalles error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/incidentes/<int:id_reporte>/estado', methods=['PUT'])
@jwt_required()
def api_incidentes_update_estado(id_reporte):
    """Update estado, accion_seguimiento and evidencia_seguimiento_url on a reportes_incidentes row."""
    if request.content_type and 'multipart' in request.content_type:
        nuevo_estado = (request.form.get('estado') or '').strip()
        accion_seguimiento = (request.form.get('accion_tomada') or '').strip()
        evidencia_file = request.files.get('evidencia')
    else:
        body = request.get_json(silent=True) or {}
        nuevo_estado = (body.get('estado') or '').strip()
        accion_seguimiento = (body.get('accion_tomada') or '').strip()
        evidencia_file = None

    if not nuevo_estado:
        return jsonify({'error': 'Parámetros inválidos'}), 400

    evidencia_url = _upload_file_to_gcs(evidencia_file) if evidencia_file else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # Ensure tracking columns exist (idempotent)
        for col, col_type in [('accion_seguimiento', 'TEXT'), ('evidencia_seguimiento_url', 'TEXT')]:
            cur.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name='reportes_incidentes' AND column_name=%s
            """, (col,))
            if not cur.fetchone():
                cur.execute(f"ALTER TABLE reportes_incidentes ADD COLUMN {col} {col_type}")

        if evidencia_url:
            cur.execute(
                "UPDATE reportes_incidentes SET estado=%s, accion_seguimiento=%s, evidencia_seguimiento_url=%s WHERE id_reporte_incidente=%s",
                (nuevo_estado, accion_seguimiento, evidencia_url, id_reporte)
            )
        else:
            cur.execute(
                "UPDATE reportes_incidentes SET estado=%s, accion_seguimiento=%s WHERE id_reporte_incidente=%s",
                (nuevo_estado, accion_seguimiento, id_reporte)
            )
        conn.commit()
        return jsonify({'ok': True, 'estado': nuevo_estado, 'evidencia_url': evidencia_url})
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"api_incidentes_update_estado error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ── Disciplina Dashboard ─────────────────────────────────────────────────────

def _disc_where(cliente, year, month, day, tipo=None, empleado_num=None):
    conds, params = [], []
    if cliente:
        conds.append("cliente_instalacion = %s")
        params.append(cliente)
    _sat_add_multi_date_filter(conds, params, "fecha_hora::TEXT", year, month, day)
    if tipo:
        conds.append("tipo_novedad = %s")
        params.append(tipo)
    if empleado_num:
        conds.append("empleado_numero = %s")
        params.append(empleado_num)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


def _disc_prev_where(cliente, year, month, day):
    # Coerce to single int — prev-period comparison only works for a single period
    try:
        year  = int(str(year).split(',')[0].strip())  if year  else None
        month = int(str(month).split(',')[0].strip()) if month else None
    except (ValueError, TypeError):
        year = month = None

    if not year:
        return None, None
    conds, params = [], []
    if cliente:
        conds.append("cliente_instalacion = %s")
        params.append(cliente)
    now = datetime.now(timezone.utc)
    if year and month and day:
        prev = datetime(year, month, day) - timedelta(days=1)
        prefix = f"{prev.year}-{prev.month:02d}-{prev.day:02d}"
    elif year and month:
        prev_month = month - 1 or 12
        prev_year  = year if month > 1 else year - 1
        prefix = f"{prev_year}-{prev_month:02d}"
    elif year:
        prefix = str(year - 1)
    else:
        prefix = str(now.year - 1)
    conds.append("fecha_hora::TEXT LIKE %s")
    params.append(prefix + "%")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


@dashboard_bp.route('/api/disciplina/clientes')
@jwt_required()
def api_disciplina_clientes():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'clientes': []})
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT cliente_instalacion
            FROM informe_novedades_disciplinario
            WHERE cliente_instalacion IS NOT NULL AND cliente_instalacion <> ''
            ORDER BY cliente_instalacion
        """)
        return jsonify({'clientes': [r[0] for r in cur.fetchall()]})
    except Exception as e:
        app_logger.error(f"api_disciplina_clientes error: {e}", exc_info=True)
        return jsonify({'clientes': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/disciplina/data')
@jwt_required()
def api_disciplina_data():
    cliente = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day     = int(request.args.get('day'))   if request.args.get('day')   else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        where, params           = _disc_where(cliente, year, month, day)
        where_prev, params_prev = _disc_prev_where(cliente, year, month, day)

        # ── KPIs via CTE on employee counts ──────────────────────────────
        # Use empleado_numero as the canonical employee key, fall back to name
        # when number is missing so we don't lose records.
        cte_filter = where.replace("WHERE ", "AND ") if where else ""

        cur.execute(f"""
            WITH emp_counts AS (
                SELECT
                    COALESCE(NULLIF(TRIM(empleado_numero), ''), empleado_nombre) AS emp_key,
                    COUNT(*) AS cnt
                FROM informe_novedades_disciplinario
                {where}
                GROUP BY emp_key
            )
            SELECT
                COALESCE(SUM(cnt), 0)                                       AS total,
                COUNT(*)                                                      AS total_empleados,
                ROUND(AVG(cnt)::NUMERIC, 2)                                   AS avg_novedades,
                ROUND(
                    100.0 * SUM(CASE WHEN cnt > 1 THEN 1 ELSE 0 END)
                    / NULLIF(COUNT(*), 0)
                , 1)                                                          AS pct_reincidencia,
                SUM(CASE WHEN cnt > 3 THEN 1 ELSE 0 END)                     AS criticos
            FROM emp_counts
        """, params)
        row = cur.fetchone()

        total             = int(row['total'])             if row['total']             else 0
        total_empleados   = int(row['total_empleados'])   if row['total_empleados']   else 0
        avg_novedades     = float(row['avg_novedades'])   if row['avg_novedades']     else 0.0
        pct_reincidencia  = float(row['pct_reincidencia'])if row['pct_reincidencia']  else 0.0
        criticos          = int(row['criticos'])          if row['criticos']          else 0

        # ── Previous period KPIs ──────────────────────────────────────────
        total_prev, total_emp_prev = 0, 0
        avg_prev, pct_reinc_prev   = None, None
        if where_prev is not None:
            cur.execute(f"""
                WITH emp_counts AS (
                    SELECT
                        COALESCE(NULLIF(TRIM(empleado_numero), ''), empleado_nombre) AS emp_key,
                        COUNT(*) AS cnt
                    FROM informe_novedades_disciplinario
                    {where_prev}
                    GROUP BY emp_key
                )
                SELECT
                    COALESCE(SUM(cnt), 0)                                           AS total,
                    COUNT(*)                                                          AS total_empleados,
                    ROUND(AVG(cnt)::NUMERIC, 2)                                       AS avg_novedades,
                    ROUND(
                        100.0 * SUM(CASE WHEN cnt > 1 THEN 1 ELSE 0 END)
                        / NULLIF(COUNT(*), 0)
                    , 1)                                                              AS pct_reincidencia
                FROM emp_counts
            """, params_prev)
            prev = cur.fetchone()
            if prev:
                total_prev      = int(prev['total'])             if prev['total']             else 0
                total_emp_prev  = int(prev['total_empleados'])   if prev['total_empleados']   else 0
                avg_prev        = float(prev['avg_novedades'])   if prev['avg_novedades']     else None
                pct_reinc_prev  = float(prev['pct_reincidencia'])if prev['pct_reincidencia']  else None

        def pct_change(curr, prev_val):
            if curr is None or prev_val is None or prev_val == 0: return None
            return round((curr - prev_val) / prev_val * 100, 1)

        # ── Novedades por tipo (sorted desc) ──────────────────────────────
        cur.execute(f"""
            SELECT
                COALESCE(NULLIF(TRIM(tipo_novedad), ''), 'Sin tipo') AS tipo,
                COUNT(*) AS cnt
            FROM informe_novedades_disciplinario
            {where}
            GROUP BY tipo
            ORDER BY cnt DESC
            LIMIT 20
        """, params)
        por_tipo = [{'label': r['tipo'], 'count': int(r['cnt'])} for r in cur.fetchall()]

        # ── Novedades por cliente/instalación (sorted desc) ───────────────
        cur.execute(f"""
            SELECT
                COALESCE(NULLIF(TRIM(cliente_instalacion), ''), 'Sin cliente') AS cliente,
                COUNT(*) AS cnt
            FROM informe_novedades_disciplinario
            {where}
            GROUP BY cliente
            ORDER BY cnt DESC
            LIMIT 15
        """, params)
        por_cliente = [{'label': r['cliente'], 'count': int(r['cnt'])} for r in cur.fetchall()]

        # ── Trend ─────────────────────────────────────────────────────────
        if month and year:
            group_expr = "SUBSTRING(fecha_hora::TEXT, 9, 2)"
        else:
            group_expr = "SUBSTRING(fecha_hora::TEXT, 1, 7)"

        cur.execute(f"""
            SELECT
                {group_expr} AS period_label,
                COUNT(*) AS cnt
            FROM informe_novedades_disciplinario
            {where}
            GROUP BY period_label
            ORDER BY period_label
            LIMIT 24
        """, params)
        trend_rows = cur.fetchall()

        # ── Top empleados ranking ─────────────────────────────────────────
        cur.execute(f"""
            SELECT
                COALESCE(NULLIF(TRIM(empleado_numero), ''), '—')  AS emp_numero,
                MAX(empleado_nombre)                                AS emp_nombre,
                COUNT(*)                                            AS cnt
            FROM informe_novedades_disciplinario
            {where}
            GROUP BY emp_numero
            ORDER BY cnt DESC
            LIMIT 20
        """, params)
        emp_rows = cur.fetchall()

        empleados = []
        for r in emp_rows:
            cnt = int(r['cnt'])
            pct_of_total = round(cnt / total * 100, 1) if total else 0
            critico = cnt > 3
            empleados.append({
                'numero':       r['emp_numero'],
                'nombre':       r['emp_nombre'] or '—',
                'count':        cnt,
                'pct_total':    pct_of_total,
                'critico':      critico,
            })

        # ── Critical employees list for alert banner ──────────────────────
        criticos_list = [e for e in empleados if e['critico']]

        return jsonify({
            'kpi': {
                'total':                total,
                'pct_change_total':     pct_change(total, total_prev),
                'total_empleados':      total_empleados,
                'pct_change_empleados': pct_change(total_empleados, total_emp_prev),
                'avg_novedades':        round(avg_novedades, 2),
                'pct_change_avg':       pct_change(avg_novedades, avg_prev),
                'pct_reincidencia':     round(pct_reincidencia, 1),
                'pct_change_reinc':     pct_change(pct_reincidencia, pct_reinc_prev),
                'criticos':             criticos,
            },
            'por_tipo':    por_tipo,
            'por_cliente': por_cliente,
            'trend': {
                'labels': [r['period_label'] for r in trend_rows],
                'counts': [int(r['cnt'])      for r in trend_rows],
            },
            'empleados':        empleados,
            'criticos_list':    criticos_list,
        })
    except Exception as e:
        app_logger.error(f"api_disciplina_data error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/disciplina/detalles')
@jwt_required()
def api_disciplina_detalles():
    cliente      = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day          = int(request.args.get('day'))   if request.args.get('day')   else None
    tipo         = request.args.get('tipo')         or None
    empleado_num = request.args.get('empleado_num') or None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        where, params = _disc_where(cliente, year, month, day, tipo, empleado_num)

        cur.execute(f"""
            SELECT
                id_informe,
                fecha_hora,
                cliente_instalacion,
                empleado_numero,
                empleado_nombre,
                empleado_cargo,
                tipo_novedad,
                turno,
                nombre_responsable
            FROM informe_novedades_disciplinario
            {where}
            ORDER BY fecha_hora DESC NULLS LAST, id_informe DESC
            LIMIT 500
        """, params)
        rows = cur.fetchall()

        detalles = []
        for r in rows:
            fh = r['fecha_hora']
            fh_str = fh.strftime('%Y-%m-%d %H:%M') if hasattr(fh, 'strftime') else str(fh)
            if fh_str == 'None': fh_str = '—'
            detalles.append({
                'id':           r['id_informe'] or 0,
                'fecha_hora':   fh_str,
                'cliente':      r['cliente_instalacion'] or '—',
                'emp_numero':   r['empleado_numero']     or '—',
                'emp_nombre':   r['empleado_nombre']     or '—',
                'emp_cargo':    r['empleado_cargo']      or '—',
                'tipo':         r['tipo_novedad']        or '—',
                'turno':        r['turno']               or '—',
                'responsable':  r['nombre_responsable']  or '—',
            })
        return jsonify({'detalles': detalles})
    except Exception as e:
        app_logger.error(f"api_disciplina_detalles error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ── Supervision Dashboard ─────────────────────────────────────────────────────
# Scoring: 5 criteria (asistencia_puntualidad, presentacion_uniforme,
#   estado_limpieza_puesto, equipamiento_completo, estado_bitacora) × 1-5 = max 25
# Thresholds: ≤15 → crítico, 16-20 → seguimiento, 21-25 → excelente

_SUP_CRITERIA = [
    ('asistencia_puntualidad',  'Asistencia y puntualidad'),
    ('presentacion_uniforme',   'Presentación y uniforme'),
    ('estado_limpieza_puesto',  'Estado y limpieza del puesto'),
    ('equipamiento_completo',   'Equipamiento'),
    ('estado_bitacora',         'Estado de bitácora'),
]

def _sup_score_label(score):
    if score is None:   return 'Sin datos'
    if score >= 21:     return 'Excelente'
    if score >= 16:     return 'Con oportunidades de mejora'
    if score > 0:       return 'Requiere acción inmediata'
    return 'Sin datos'

def _sup_score_color(score):
    if score is None:  return '#6b7280'
    if score >= 21:    return '#22c55e'
    if score >= 16:    return '#eab308'
    return '#ef4444'

def _sup_where(cliente, year, month, day, responsable=None, nombre_usuario=None):
    conds, params = [], []
    if cliente:
        conds.append("cliente = %s")
        params.append(cliente)
    if responsable:
        conds.append("TRIM(rol_aplicador) = %s")
        params.append(responsable)
    if nombre_usuario:
        conds.append("TRIM(supervisor) = %s")
        params.append(nombre_usuario)
    _sat_add_multi_date_filter(conds, params, "fecha_hora::TEXT", year, month, day)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params

def _sup_prev_where(cliente, year, month, day):
    # Coerce to single int — prev-period comparison only works for a single period
    try:
        year  = int(str(year).split(',')[0].strip())  if year  else None
        month = int(str(month).split(',')[0].strip()) if month else None
    except (ValueError, TypeError):
        year = month = None

    if not year:
        return None, None
    conds, params = [], []
    if cliente:
        conds.append("cliente = %s")
        params.append(cliente)
    now = datetime.now(timezone.utc)
    if year and month and day:
        prev = datetime(year, month, day) - timedelta(days=1)
        prefix = f"{prev.year}-{prev.month:02d}-{prev.day:02d}"
    elif year and month:
        prev_month = month - 1 or 12
        prev_year  = year if month > 1 else year - 1
        prefix = f"{prev_year}-{prev_month:02d}"
    elif year:
        prefix = str(year - 1)
    else:
        prefix = str(now.year - 1)
    conds.append("fecha_hora::TEXT LIKE %s")
    params.append(prefix + "%")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


@dashboard_bp.route('/api/supervision/filtros')
@jwt_required()
def api_supervision_filtros():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'responsables': []})
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT TRIM(rol_aplicador) AS responsable
            FROM supervision_puesto
            WHERE rol_aplicador IS NOT NULL AND TRIM(rol_aplicador) <> ''
            ORDER BY responsable
        """)
        return jsonify({'responsables': [r[0] for r in cur.fetchall()]})
    except Exception as e:
        app_logger.error(f"api_supervision_filtros error: {e}", exc_info=True)
        return jsonify({'responsables': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/supervision/clientes')
@jwt_required()
def api_supervision_clientes():
    """Return active properties as the canonical list of supervision sites."""
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'clientes': []})
        cur = conn.cursor()
        cur.execute("""
            SELECT nombre
            FROM propiedades
            WHERE COALESCE(activa, TRUE) = TRUE
            ORDER BY nombre
        """)
        return jsonify({'clientes': [r[0] for r in cur.fetchall()]})
    except Exception as e:
        app_logger.error(f"api_supervision_clientes error: {e}", exc_info=True)
        return jsonify({'clientes': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/supervision/data')
@jwt_required()
def api_supervision_data():
    cliente     = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day         = int(request.args.get('day'))   if request.args.get('day')   else None
    nivel          = request.args.get('nivel') or None  # 'excelente' | 'seguimiento' | 'critico'
    responsable    = request.args.get('responsable')    or None
    nombre_usuario = request.args.get('nombre_usuario') or None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        where, params           = _sup_where(cliente, year, month, day, responsable, nombre_usuario=nombre_usuario)
        where_prev, params_prev = _sup_prev_where(cliente, year, month, day)

        # ── Helper: cast 1-5 field to numeric, mapping text labels too ──────
        # Some records store 'Excelente'/'Bueno'/etc. instead of 1-5.
        def _safe(col):
            return fr"""CASE
                WHEN TRIM({col}::TEXT) ~ '^[0-9]+(\.[0-9]+)?$' THEN TRIM({col}::TEXT)::NUMERIC
                WHEN LOWER(TRIM({col}::TEXT)) = 'excelente'            THEN 5
                WHEN LOWER(TRIM({col}::TEXT)) IN ('bueno','bien')       THEN 4
                WHEN LOWER(TRIM({col}::TEXT)) IN ('regular','aceptable')THEN 3
                WHEN LOWER(TRIM({col}::TEXT)) IN ('malo','deficiente')  THEN 2
                WHEN LOWER(TRIM({col}::TEXT)) IN ('pesimo','pésimo','muy malo') THEN 1
                ELSE NULL
            END"""

        score_expr = " + ".join(f"COALESCE(({_safe(f)}),0)" for f, _ in _SUP_CRITERIA)
        avg_criteria = ", ".join(
            f"AVG({_safe(f)}) AS avg_{f}" for f, _ in _SUP_CRITERIA
        )

        # Apply nivel filter if requested — wrap base where with score condition
        if nivel in ('excelente', 'seguimiento', 'critico'):
            nivel_cond = {
                'excelente':   f"({score_expr}) >= 21",
                'seguimiento': f"({score_expr}) BETWEEN 16 AND 20",
                'critico':     f"({score_expr}) > 0 AND ({score_expr}) <= 15",
            }[nivel]
            connector = "AND" if where else "WHERE"
            where = f"{where} {connector} {nivel_cond}".strip()
            # where_prev not filtered by nivel (we keep prev-period unfiltered for comparison)

        # ── KPI summary ───────────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                COUNT(*)                                                    AS total,
                AVG({score_expr})                                           AS avg_score,
                SUM(CASE WHEN ({score_expr}) >= 21 THEN 1 ELSE 0 END)     AS cnt_excelente,
                SUM(CASE WHEN ({score_expr}) BETWEEN 16 AND 20
                         THEN 1 ELSE 0 END)                                AS cnt_seguimiento,
                SUM(CASE WHEN ({score_expr}) <= 15
                         AND ({score_expr}) > 0 THEN 1 ELSE 0 END)        AS cnt_critico,
                {avg_criteria}
            FROM supervision_puesto
            {where}
        """, params)
        row = cur.fetchone()

        total          = int(row['total'])         if row['total']     else 0
        avg_score      = float(row['avg_score'])   if row['avg_score'] else None
        cnt_excelente  = int(row['cnt_excelente'])  if row['cnt_excelente']  else 0
        cnt_seguimiento= int(row['cnt_seguimiento'])if row['cnt_seguimiento'] else 0
        cnt_critico    = int(row['cnt_critico'])    if row['cnt_critico']     else 0

        # ── Previous period ───────────────────────────────────────────────
        avg_score_prev, total_prev = None, 0
        if where_prev is not None:
            cur.execute(f"""
                SELECT COUNT(*) AS total, AVG({score_expr}) AS avg_score
                FROM supervision_puesto
                {where_prev}
            """, params_prev)
            prev = cur.fetchone()
            if prev:
                total_prev     = int(prev['total'])       if prev['total']     else 0
                avg_score_prev = float(prev['avg_score']) if prev['avg_score'] else None

        def pct_change(curr, prev_val):
            if curr is None or prev_val is None or prev_val == 0: return None
            return round((curr - prev_val) / prev_val * 100, 1)

        pct_critico    = round(cnt_critico     / total * 100, 1) if total else 0
        pct_seguimiento= round(cnt_seguimiento / total * 100, 1) if total else 0
        pct_excelente  = round(cnt_excelente   / total * 100, 1) if total else 0

        # ── Per-criterion averages (for criteria bar chart) ───────────────
        criteria = []
        for field, label in _SUP_CRITERIA:
            avg = float(row[f'avg_{field}']) if row[f'avg_{field}'] else None
            color = _sup_score_color(avg * 5 if avg else None)  # scale 1-5 → 5-25
            # Use raw 1-5 scale for criteria bars
            crit_color = '#22c55e' if avg and avg >= 4.0 else '#eab308' if avg and avg >= 2.5 else '#ef4444'
            criteria.append({
                'label': label,
                'avg':   round(avg, 2) if avg is not None else None,
                'color': crit_color,
            })
        # Sort ascending (worst first) per requirements
        criteria.sort(key=lambda x: x['avg'] if x['avg'] is not None else 0)

        # ── Result per puesto (by numero_empleado) ────────────────────────
        cur.execute(f"""
            SELECT
                COALESCE(NULLIF(TRIM(numero_empleado),''), nombre_guardia, 'Sin ID') AS emp_key,
                MAX(nombre_guardia)                                                    AS guardia,
                COUNT(*)                                                               AS cnt,
                ROUND(AVG({score_expr})::NUMERIC, 1)                                  AS avg_score
            FROM supervision_puesto
            {where}
            GROUP BY emp_key
            ORDER BY avg_score ASC
            LIMIT 30
        """, params)
        puestos = []
        for r in cur.fetchall():
            s = float(r['avg_score']) if r['avg_score'] else None
            puestos.append({
                'emp_key':  r['emp_key'],
                'guardia':  r['guardia'] or '—',
                'cnt':      int(r['cnt']),
                'avg_score': s,
                'color':    _sup_score_color(s),
                'label':    _sup_score_label(s),
                'alerta':   s is not None and s <= 15,
            })

        # ── Critical puestos for alert banner ─────────────────────────────
        criticos   = [p for p in puestos if p['alerta']]
        seguimiento= [p for p in puestos if p['avg_score'] is not None and 16 <= p['avg_score'] <= 20]

        # ── Trend by period ───────────────────────────────────────────────
        if month and year:
            grp = "SUBSTRING(fecha_hora::TEXT, 9, 2)"
        else:
            grp = "SUBSTRING(fecha_hora::TEXT, 1, 7)"

        cur.execute(f"""
            SELECT
                {grp} AS period_label,
                ROUND(AVG({score_expr})::NUMERIC, 2) AS avg_score,
                COUNT(*) AS cnt
            FROM supervision_puesto
            {where}
            GROUP BY period_label
            ORDER BY period_label
            LIMIT 24
        """, params)
        trend_rows = cur.fetchall()

        # ── Trend by rol_aplicador (turno not stored as a top-level column) ─
        cur.execute(f"""
            SELECT
                {grp} AS period_label,
                COALESCE(NULLIF(TRIM(rol_aplicador),''), 'Sin rol') AS turno,
                ROUND(AVG({score_expr})::NUMERIC, 2) AS avg_score
            FROM supervision_puesto
            {where}
            GROUP BY period_label, turno
            ORDER BY period_label, turno
            LIMIT 72
        """, params)
        turno_rows = cur.fetchall()

        # Build per-turno series
        turno_periods = sorted(set(r['period_label'] for r in turno_rows))
        turno_map = {}
        for r in turno_rows:
            t = r['turno']
            if t not in turno_map:
                turno_map[t] = {}
            turno_map[t][r['period_label']] = float(r['avg_score']) if r['avg_score'] else None
        turno_series = [
            {'turno': t, 'data': [turno_map[t].get(p) for p in turno_periods]}
            for t in sorted(turno_map.keys())
        ]

        return jsonify({
            'kpi': {
                'total':                total,
                'pct_change_total':     pct_change(total, total_prev),
                'avg_score':            round(avg_score, 1) if avg_score is not None else None,
                'avg_score_prev':       round(avg_score_prev, 1) if avg_score_prev else None,
                'pct_change_score':     pct_change(avg_score, avg_score_prev),
                'avg_score_color':      _sup_score_color(avg_score),
                'avg_score_label':      _sup_score_label(avg_score),
                'cnt_critico':          cnt_critico,
                'pct_critico':          pct_critico,
                'cnt_seguimiento':      cnt_seguimiento,
                'pct_seguimiento':      pct_seguimiento,
                'cnt_excelente':        cnt_excelente,
                'pct_excelente':        pct_excelente,
            },
            'distribution': {
                'excelente':   cnt_excelente,
                'seguimiento': cnt_seguimiento,
                'critico':     cnt_critico,
            },
            'criteria':   criteria,
            'puestos':    puestos,
            'criticos':   criticos,
            'seguimiento_list': seguimiento,
            'trend': {
                'labels':    [r['period_label'] for r in trend_rows],
                'avg_score': [float(r['avg_score']) if r['avg_score'] else None for r in trend_rows],
                'counts':    [int(r['cnt']) for r in trend_rows],
            },
            'trend_turno': {
                'labels':  turno_periods,
                'series':  turno_series,
            },
        })
    except Exception as e:
        app_logger.error(f"api_supervision_data error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/supervision/detalles')
@jwt_required()
def api_supervision_detalles():
    cliente        = request.args.get('cliente')        or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day            = int(request.args.get('day'))    if request.args.get('day')   else None
    nivel          = request.args.get('nivel')          or None
    empleado_num   = request.args.get('empleado_num')   or None
    turno_filter   = request.args.get('turno')          or None
    responsable    = request.args.get('responsable')    or None
    nombre_usuario = request.args.get('nombre_usuario') or None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        where, params = _sup_where(cliente, year, month, day, responsable=responsable, nombre_usuario=nombre_usuario)
        if empleado_num:
            where = (where + " AND " if where else "WHERE ") + "COALESCE(NULLIF(TRIM(numero_empleado),''), nombre_guardia, 'Sin ID') = %s"
            params = list(params) + [empleado_num]
        if turno_filter:
            where = (where + " AND " if where else "WHERE ") + "rol_aplicador = %s"
            params = list(params) + [turno_filter]

        def _safe(col):
            return fr"""CASE
                WHEN TRIM({col}::TEXT) ~ '^[0-9]+(\.[0-9]+)?$' THEN TRIM({col}::TEXT)::NUMERIC
                WHEN LOWER(TRIM({col}::TEXT)) = 'excelente'            THEN 5
                WHEN LOWER(TRIM({col}::TEXT)) IN ('bueno','bien')       THEN 4
                WHEN LOWER(TRIM({col}::TEXT)) IN ('regular','aceptable')THEN 3
                WHEN LOWER(TRIM({col}::TEXT)) IN ('malo','deficiente')  THEN 2
                WHEN LOWER(TRIM({col}::TEXT)) IN ('pesimo','pésimo','muy malo') THEN 1
                ELSE NULL
            END"""

        score_expr = " + ".join(f"COALESCE(({_safe(f)}),0)" for f, _ in _SUP_CRITERIA)

        cur.execute(f"""
            SELECT
                id_supervision,
                fecha_hora,
                cliente,
                supervisor,
                numero_empleado,
                nombre_guardia,
                rol_aplicador,
                ({score_expr})                AS total_score
            FROM supervision_puesto
            {where}
            ORDER BY fecha_hora DESC NULLS LAST, id_supervision DESC
            LIMIT 500
        """, params)
        rows = cur.fetchall()

        detalles = []
        for r in rows:
            fh = r['fecha_hora']
            fh_str = fh.strftime('%Y-%m-%d %H:%M') if hasattr(fh, 'strftime') else str(fh)
            if fh_str == 'None': fh_str = '—'
            score = float(r['total_score']) if r['total_score'] else 0
            detalles.append({
                'id':          r['id_supervision'] or 0,
                'fecha_hora':  fh_str,
                'cliente':     r['cliente']          or '—',
                'supervisor':  r['supervisor']        or '—',
                'emp_numero':  r['numero_empleado']   or '—',
                'guardia':     r['nombre_guardia']    or '—',
                'turno':       r['rol_aplicador']     or '—',
                'score':       score,
                'score_label': _sup_score_label(score),
                'score_color': _sup_score_color(score),
            })
        if nivel == 'critico':
            detalles = [d for d in detalles if d['score'] <= 15]
        elif nivel == 'seguimiento':
            detalles = [d for d in detalles if 16 <= d['score'] <= 20]
        elif nivel == 'excelente':
            detalles = [d for d in detalles if d['score'] >= 21]
        return jsonify({'detalles': detalles})
    except Exception as e:
        app_logger.error(f"api_supervision_detalles error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ── Cumplimiento Dashboard ─────────────────────────────────────────────────────

_CUMPL_CRITERIA = [
    ('copia_certificados_fisica',     'Copia de Certificado'),
    ('certificados_cargados_sistema', 'Certificados Cargados'),
    ('documentacion_coincide_hv',     'Coincide Documentación'),
    ('fechas_vigentes',               'Vigente'),
]

def _cumpl_conds(cliente, year, month, day, responsable=None):
    conds, params = [], []
    if cliente:
        conds.append('cliente_instalacion = %s'); params.append(cliente)
    if year:
        conds.append('EXTRACT(YEAR  FROM fecha_hora) = %s'); params.append(year)
    if month:
        conds.append('EXTRACT(MONTH FROM fecha_hora) = %s'); params.append(month)
    if day:
        conds.append('EXTRACT(DAY   FROM fecha_hora) = %s'); params.append(day)
    if responsable:
        conds.append("TRIM(rol_aplicador) = %s"); params.append(responsable)
    return conds, params

def _cumpl_where(conds):
    return ('WHERE ' + ' AND '.join(conds)) if conds else ''


@dashboard_bp.route('/api/cumplimiento/filtros')
@jwt_required()
def api_cumplimiento_filtros():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'responsables': []})
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT TRIM(rol_aplicador) AS responsable
            FROM checklist_cumplimiento
            WHERE rol_aplicador IS NOT NULL AND TRIM(rol_aplicador) <> ''
            ORDER BY responsable
        """)
        return jsonify({'responsables': [r[0] for r in cur.fetchall()]})
    except Exception as e:
        app_logger.error(f"api_cumplimiento_filtros error: {e}", exc_info=True)
        return jsonify({'responsables': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/cumplimiento/clientes')
@jwt_required()
def api_cumplimiento_clientes():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'clientes': []})
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT cliente_instalacion
            FROM checklist_cumplimiento
            WHERE cliente_instalacion IS NOT NULL AND cliente_instalacion <> ''
            ORDER BY cliente_instalacion
        """)
        return jsonify({'clientes': [r[0] for r in cur.fetchall()]})
    except Exception as e:
        app_logger.error(f"api_cumplimiento_clientes error: {e}", exc_info=True)
        return jsonify({'clientes': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/cumplimiento/data')
@jwt_required()
def api_cumplimiento_data():
    cliente     = request.args.get('cliente')     or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day         = int(request.args.get('day'))    if request.args.get('day')   else None
    responsable = request.args.get('responsable') or None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        base_conds, base_params = _cumpl_conds(cliente, year, month, day, responsable=responsable)
        where = _cumpl_where(base_conds)

        # ── KPI summary ────────────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                COUNT(*)                                                                AS total,
                SUM(CASE WHEN LOWER(TRIM(nivel_cumplimiento)) = 'cumple'
                         THEN 1 ELSE 0 END)                                             AS cnt_cumple,
                SUM(CASE WHEN LOWER(TRIM(nivel_cumplimiento)) = 'no cumple'
                         THEN 1 ELSE 0 END)                                             AS cnt_no_cumple,
                SUM(CASE WHEN LOWER(TRIM(nivel_cumplimiento)) LIKE 'cumple con%%'
                         THEN 1 ELSE 0 END)                                             AS cnt_hallazgos,
                SUM(CASE WHEN vigencia_hasta IS NOT NULL
                              AND vigencia_hasta < CURRENT_DATE
                         THEN 1 ELSE 0 END)                                             AS cnt_vencidos,
                SUM(CASE WHEN vigencia_hasta IS NOT NULL
                              AND vigencia_hasta >= CURRENT_DATE
                              AND vigencia_hasta < CURRENT_DATE + INTERVAL '30 days'
                         THEN 1 ELSE 0 END)                                             AS cnt_proximos
            FROM checklist_cumplimiento
            {where}
        """, base_params)
        row = cur.fetchone()

        total         = int(row['total'])         if row['total']         else 0
        cnt_cumple    = int(row['cnt_cumple'])    if row['cnt_cumple']    else 0
        cnt_no_cumple = int(row['cnt_no_cumple']) if row['cnt_no_cumple'] else 0
        cnt_hallazgos = int(row['cnt_hallazgos']) if row['cnt_hallazgos'] else 0
        cnt_vencidos  = int(row['cnt_vencidos'])  if row['cnt_vencidos']  else 0
        cnt_proximos  = int(row['cnt_proximos'])  if row['cnt_proximos']  else 0

        pct_cumple    = round(cnt_cumple    / total * 100, 1) if total else 0
        pct_no_cumple = round(cnt_no_cumple / total * 100, 1) if total else 0
        pct_vencidos  = round(cnt_vencidos  / total * 100, 1) if total else 0

        # ── Per-criterion Si/No counts ─────────────────────────────────────
        crit_select = ", ".join(
            f"SUM(CASE WHEN LOWER(TRIM({col})) = 'si' THEN 1 ELSE 0 END) AS si_{col},"
            f" SUM(CASE WHEN LOWER(TRIM({col})) = 'no' THEN 1 ELSE 0 END) AS no_{col}"
            for col, _ in _CUMPL_CRITERIA
        )
        cur.execute(f"SELECT {crit_select} FROM checklist_cumplimiento {where}", base_params)
        crit_row = cur.fetchone()
        criteria = []
        for col, label in _CUMPL_CRITERIA:
            si  = int(crit_row[f'si_{col}']) if crit_row[f'si_{col}'] else 0
            no  = int(crit_row[f'no_{col}']) if crit_row[f'no_{col}'] else 0
            tot = si + no
            pct = round(si / tot * 100, 1) if tot else 0
            criteria.append({'label': label, 'si': si, 'no': no, 'pct': pct})
        criteria.sort(key=lambda x: x['pct'])  # ascending — worst first

        # ── Certification status (donut) ───────────────────────────────────
        cur.execute(f"""
            SELECT
                SUM(CASE WHEN vigencia_hasta IS NOT NULL
                              AND vigencia_hasta >= CURRENT_DATE + INTERVAL '30 days'
                         THEN 1 ELSE 0 END)                                             AS vigente,
                SUM(CASE WHEN vigencia_hasta IS NOT NULL
                              AND vigencia_hasta >= CURRENT_DATE
                              AND vigencia_hasta < CURRENT_DATE + INTERVAL '30 days'
                         THEN 1 ELSE 0 END)                                             AS proximo,
                SUM(CASE WHEN vigencia_hasta IS NOT NULL
                              AND vigencia_hasta < CURRENT_DATE
                         THEN 1 ELSE 0 END)                                             AS vencido
            FROM checklist_cumplimiento
            {where}
        """, base_params)
        cert_row   = cur.fetchone()
        cert_status = {
            'vigente': int(cert_row['vigente']) if cert_row['vigente'] else 0,
            'proximo': int(cert_row['proximo']) if cert_row['proximo'] else 0,
            'vencido': int(cert_row['vencido']) if cert_row['vencido'] else 0,
        }

        # ── Compliance by employee (numero_documento, sorted asc) ──────────
        cur.execute(f"""
            SELECT
                COALESCE(NULLIF(TRIM(agente_numero_documento), ''), 'Sin ID') AS num_doc,
                MAX(agente_nombre_completo)                                    AS nombre,
                COUNT(*)                                                        AS total,
                SUM(CASE WHEN LOWER(TRIM(nivel_cumplimiento)) = 'cumple'
                         THEN 1 ELSE 0 END)                                    AS cumple
            FROM checklist_cumplimiento
            {where}
            GROUP BY num_doc
            ORDER BY
                ROUND(COALESCE(SUM(CASE WHEN LOWER(TRIM(nivel_cumplimiento)) = 'cumple'
                               THEN 1 ELSE 0 END), 0)::NUMERIC
                      / NULLIF(COUNT(*), 0) * 100, 1) ASC
            LIMIT 30
        """, base_params)
        empleados = []
        for r in cur.fetchall():
            tot    = int(r['total'])
            cumple = int(r['cumple']) if r['cumple'] else 0
            pct    = round(cumple / tot * 100, 1) if tot else 0
            color  = '#22c55e' if pct >= 90 else '#eab308' if pct >= 70 else '#ef4444'
            empleados.append({
                'num_doc':    r['num_doc'],
                'nombre':     r['nombre'] or '—',
                'total':      tot,
                'pct_cumple': pct,
                'color':      color,
            })

        # ── Compliance by site ──────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                COALESCE(NULLIF(TRIM(cliente_instalacion), ''), 'Sin sitio') AS sitio,
                COUNT(*)                                                       AS total,
                SUM(CASE WHEN LOWER(TRIM(nivel_cumplimiento)) = 'cumple'
                         THEN 1 ELSE 0 END)                                   AS cumple
            FROM checklist_cumplimiento
            {where}
            GROUP BY sitio
            ORDER BY
                ROUND(COALESCE(SUM(CASE WHEN LOWER(TRIM(nivel_cumplimiento)) = 'cumple'
                               THEN 1 ELSE 0 END), 0)::NUMERIC
                      / NULLIF(COUNT(*), 0) * 100, 1) ASC
            LIMIT 20
        """, base_params)
        sitios = []
        for r in cur.fetchall():
            tot    = int(r['total'])
            cumple = int(r['cumple']) if r['cumple'] else 0
            pct    = round(cumple / tot * 100, 1) if tot else 0
            color  = '#22c55e' if pct >= 90 else '#eab308' if pct >= 70 else '#ef4444'
            sitios.append({
                'sitio':      r['sitio'],
                'total':      tot,
                'pct_cumple': pct,
                'color':      color,
            })

        # ── Alert: vencidos ────────────────────────────────────────────────
        v_conds = base_conds + ['vigencia_hasta IS NOT NULL', 'vigencia_hasta < CURRENT_DATE']
        cur.execute(f"""
            SELECT agente_nombre_completo, agente_numero_documento, curso_certificacion,
                   vigencia_hasta, cliente_instalacion,
                   (CURRENT_DATE - vigencia_hasta) AS dias_vencido
            FROM checklist_cumplimiento
            {_cumpl_where(v_conds)}
            ORDER BY dias_vencido DESC
            LIMIT 20
        """, base_params)
        alertas_vencidos = [{
            'nombre':        r['agente_nombre_completo'] or '—',
            'num_doc':       r['agente_numero_documento'] or '—',
            'curso':         r['curso_certificacion'] or '—',
            'vigencia_hasta':r['vigencia_hasta'].isoformat() if r['vigencia_hasta'] else None,
            'sitio':         r['cliente_instalacion'] or '—',
            'dias_vencido':  int(r['dias_vencido']) if r['dias_vencido'] else 0,
        } for r in cur.fetchall()]

        # ── Alert: próximos a vencer (<30 days) ────────────────────────────
        p_conds = base_conds + [
            'vigencia_hasta IS NOT NULL',
            'vigencia_hasta >= CURRENT_DATE',
            "vigencia_hasta < CURRENT_DATE + INTERVAL '30 days'",
        ]
        cur.execute(f"""
            SELECT agente_nombre_completo, agente_numero_documento, curso_certificacion,
                   vigencia_hasta, cliente_instalacion,
                   (vigencia_hasta - CURRENT_DATE) AS dias_restantes
            FROM checklist_cumplimiento
            {_cumpl_where(p_conds)}
            ORDER BY dias_restantes ASC
            LIMIT 20
        """, base_params)
        alertas_proximos = [{
            'nombre':         r['agente_nombre_completo'] or '—',
            'num_doc':        r['agente_numero_documento'] or '—',
            'curso':          r['curso_certificacion'] or '—',
            'vigencia_hasta': r['vigencia_hasta'].isoformat() if r['vigencia_hasta'] else None,
            'sitio':          r['cliente_instalacion'] or '—',
            'dias_restantes': int(r['dias_restantes']) if r['dias_restantes'] else 0,
        } for r in cur.fetchall()]

        return jsonify({
            'kpi': {
                'total':         total,
                'cnt_cumple':    cnt_cumple,
                'cnt_no_cumple': cnt_no_cumple,
                'cnt_hallazgos': cnt_hallazgos,
                'cnt_vencidos':  cnt_vencidos,
                'cnt_proximos':  cnt_proximos,
                'pct_cumple':    pct_cumple,
                'pct_no_cumple': pct_no_cumple,
                'pct_vencidos':  pct_vencidos,
            },
            'criteria':         criteria,
            'cert_status':      cert_status,
            'por_empleado':     empleados,
            'por_sitio':        sitios,
            'alertas_vencidos': alertas_vencidos,
            'alertas_proximos': alertas_proximos,
        })
    except Exception as e:
        app_logger.error(f"api_cumplimiento_data error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/cumplimiento/detalles')
@jwt_required()
def api_cumplimiento_detalles():
    cliente = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day     = int(request.args.get('day'))   if request.args.get('day')   else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        base_conds, base_params = _cumpl_conds(cliente, year, month, day)
        where = _cumpl_where(base_conds)

        cur.execute(f"""
            SELECT id, fecha_hora, cliente_instalacion, nombre_auditor,
                   agente_numero_documento, agente_nombre_completo,
                   curso_certificacion, nivel_cumplimiento, vigencia_hasta,
                   copia_certificados_fisica, certificados_cargados_sistema,
                   documentacion_coincide_hv, fechas_vigentes
            FROM checklist_cumplimiento
            {where}
            ORDER BY fecha_hora DESC
            LIMIT 200
        """, base_params)
        detalles = [{
            'id':              r['id'],
            'fecha_hora':      r['fecha_hora'].strftime('%Y-%m-%d %H:%M') if r['fecha_hora'] else '—',
            'cliente':         r['cliente_instalacion'] or '—',
            'auditor':         r['nombre_auditor'] or '—',
            'num_doc':         r['agente_numero_documento'] or '—',
            'agente':          r['agente_nombre_completo'] or '—',
            'curso':           r['curso_certificacion'] or '—',
            'nivel':           r['nivel_cumplimiento'] or '—',
            'vigencia_hasta':  r['vigencia_hasta'].isoformat() if r['vigencia_hasta'] else '—',
            'copia_fisica':    r['copia_certificados_fisica'] or '—',
            'cargado_sistema': r['certificados_cargados_sistema'] or '—',
            'coincide_hv':     r['documentacion_coincide_hv'] or '—',
            'vigente':         r['fechas_vigentes'] or '—',
        } for r in cur.fetchall()]
        return jsonify({'detalles': detalles})
    except Exception as e:
        app_logger.error(f"api_cumplimiento_detalles error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ── Capacitaciones Dashboard ───────────────────────────────────────────────────

def _capac_safe_len(col='lista_asistencia'):
    """SQL expression for safe JSON array length."""
    return (
        f"CASE WHEN {col} IS NOT NULL AND {col} NOT IN ('', '[]', 'null') "
        f"THEN json_array_length({col}::json) ELSE 0 END"
    )

def _capac_date_expr():
    """Normalized timestamp used by filters and trends.

    Older training rows may have `fecha_hora` empty because the form posted
    separate date/time fields, but `creado_en` is still present.
    """
    return "COALESCE(fecha_hora, creado_en::timestamp)"

def _capac_conds(cliente, year, month, day):
    conds, params = [], []
    date_expr = _capac_date_expr()
    if cliente: conds.append('cliente_instalacion = %s'); params.append(cliente)
    if year:    conds.append(f'EXTRACT(YEAR  FROM {date_expr}) = %s'); params.append(year)
    if month:   conds.append(f'EXTRACT(MONTH FROM {date_expr}) = %s'); params.append(month)
    if day:     conds.append(f'EXTRACT(DAY   FROM {date_expr}) = %s'); params.append(day)
    return conds, params

def _capac_where(conds):
    return ('WHERE ' + ' AND '.join(conds)) if conds else ''


@dashboard_bp.route('/api/capacitacion/clientes')
@jwt_required()
def api_capacitacion_clientes():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT DISTINCT cliente_instalacion
            FROM registro_de_capacitaciones
            WHERE cliente_instalacion IS NOT NULL
            ORDER BY cliente_instalacion
        """)
        clientes = [r['cliente_instalacion'] for r in cur.fetchall()]
        return jsonify({'clientes': clientes})
    except Exception as e:
        app_logger.error(f"api_capacitacion_clientes error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/capacitacion/data')
@jwt_required()
def api_capacitacion_data():
    cliente = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day     = int(request.args.get('day'))   if request.args.get('day')   else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        base_conds, base_params = _capac_conds(cliente, year, month, day)
        where = _capac_where(base_conds)
        safe_len = _capac_safe_len()
        date_expr = _capac_date_expr()

        # ── KPIs ──────────────────────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                COUNT(*)                       AS total_capacitaciones,
                COALESCE(SUM({safe_len}), 0)   AS total_asistentes,
                ROUND(COALESCE(AVG(
                    NULLIF({safe_len}, 0)
                ), 0), 1)                      AS promedio_asistentes
            FROM registro_de_capacitaciones
            {where}
        """, base_params)
        kpi = cur.fetchone()
        total_cap   = int(kpi['total_capacitaciones'])   if kpi else 0
        total_asist = int(kpi['total_asistentes'])       if kpi else 0
        promedio    = float(kpi['promedio_asistentes'])  if kpi else 0.0

        # ── Asistentes por capacitación (sorted ASC for chart) ────────────────
        tema_conds = base_conds + ['nombre_capacitacion IS NOT NULL']
        cur.execute(f"""
            SELECT
                nombre_capacitacion,
                COUNT(*)                     AS total_sesiones,
                COALESCE(SUM({safe_len}), 0) AS total_asistentes
            FROM registro_de_capacitaciones
            {_capac_where(tema_conds)}
            GROUP BY nombre_capacitacion
            ORDER BY total_asistentes ASC
            LIMIT 20
        """, base_params)
        por_tema = [{
            'tema':      r['nombre_capacitacion'],
            'sesiones':  int(r['total_sesiones']),
            'asistentes': int(r['total_asistentes']),
        } for r in cur.fetchall()]

        # ── Tendencia mensual ─────────────────────────────────────────────────
        trend_conds = base_conds + [f'{date_expr} IS NOT NULL']
        cur.execute(f"""
            SELECT
                DATE_TRUNC('month', {date_expr})            AS month_start,
                TO_CHAR(DATE_TRUNC('month', {date_expr}), 'YYYY-MM')   AS mes,
                TO_CHAR(DATE_TRUNC('month', {date_expr}), 'Mon YYYY')  AS mes_label,
                COUNT(*)                                    AS capacitaciones,
                COALESCE(SUM({safe_len}), 0)                AS asistentes
            FROM registro_de_capacitaciones
            {_capac_where(trend_conds)}
            GROUP BY DATE_TRUNC('month', {date_expr})
            ORDER BY DATE_TRUNC('month', {date_expr})
        """, base_params)
        tendencia = [{
            'mes':            r['mes'],
            'mes_label':      r['mes_label'].strip() if r['mes_label'] else r['mes'],
            'month_start':    r['month_start'].strftime('%Y-%m-%d') if r['month_start'] else None,
            'capacitaciones': int(r['capacitaciones']),
            'asistentes':     int(r['asistentes']),
        } for r in cur.fetchall()]

        # ── Por área/puesto ───────────────────────────────────────────────────
        area_conds = base_conds + ['puesto_area_especifica IS NOT NULL']
        cur.execute(f"""
            SELECT
                puesto_area_especifica       AS area,
                COUNT(*)                     AS total_sesiones,
                COALESCE(SUM({safe_len}), 0) AS total_asistentes
            FROM registro_de_capacitaciones
            {_capac_where(area_conds)}
            GROUP BY puesto_area_especifica
            ORDER BY total_asistentes ASC
            LIMIT 15
        """, base_params)
        por_area = [{
            'area':      r['area'],
            'sesiones':  int(r['total_sesiones']),
            'asistentes': int(r['total_asistentes']),
        } for r in cur.fetchall()]

        # ── Por cliente/sitio ─────────────────────────────────────────────────
        sitio_conds = base_conds + ['cliente_instalacion IS NOT NULL']
        cur.execute(f"""
            SELECT
                cliente_instalacion          AS sitio,
                COUNT(*)                     AS total_sesiones,
                COALESCE(SUM({safe_len}), 0) AS total_asistentes
            FROM registro_de_capacitaciones
            {_capac_where(sitio_conds)}
            GROUP BY cliente_instalacion
            ORDER BY total_asistentes ASC
            LIMIT 15
        """, base_params)
        por_sitio = [{
            'sitio':     r['sitio'],
            'sesiones':  int(r['total_sesiones']),
            'asistentes': int(r['total_asistentes']),
        } for r in cur.fetchall()]

        # ── Top asistentes (JSON expansion via lateral) ───────────────────────
        top_conds = base_conds + [
            "lista_asistencia IS NOT NULL",
            "lista_asistencia NOT IN ('', '[]', 'null')",
        ]
        cur.execute(f"""
            SELECT
                att->>'nombre' AS nombre,
                att->>'cargo'  AS cargo,
                COUNT(*)       AS sesiones
            FROM (
                SELECT lista_asistencia
                FROM registro_de_capacitaciones
                {_capac_where(top_conds)}
            ) sub,
            LATERAL json_array_elements(sub.lista_asistencia::json) AS att
            WHERE (att->>'nombre') IS NOT NULL AND (att->>'nombre') != ''
            GROUP BY att->>'nombre', att->>'cargo'
            ORDER BY sesiones DESC
            LIMIT 15
        """, base_params)
        top_asistentes = [{
            'nombre':  r['nombre'],
            'cargo':   r['cargo'] or '—',
            'sesiones': int(r['sesiones']),
        } for r in cur.fetchall()]

        return jsonify({
            'kpi': {
                'total_capacitaciones': total_cap,
                'total_asistentes':     total_asist,
                'promedio_asistentes':  promedio,
            },
            'por_tema':       por_tema,
            'tendencia':      tendencia,
            'por_area':       por_area,
            'por_sitio':      por_sitio,
            'top_asistentes': top_asistentes,
        })
    except Exception as e:
        app_logger.error(f"api_capacitacion_data error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/capacitacion/detalles')
@jwt_required()
def api_capacitacion_detalles():
    cliente = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day     = int(request.args.get('day'))   if request.args.get('day')   else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        base_conds, base_params = _capac_conds(cliente, year, month, day)
        where = _capac_where(base_conds)
        safe_len = _capac_safe_len()
        date_expr = _capac_date_expr()

        cur.execute(f"""
            SELECT
                id_capacitacion,
                {date_expr} AS fecha_evento,
                cliente_instalacion,
                puesto_area_especifica,
                nombre_responsable,
                nombre_capacitacion,
                lista_asistencia,
                {safe_len} AS num_asistentes
            FROM registro_de_capacitaciones
            {where}
            ORDER BY {date_expr} DESC NULLS LAST, id_capacitacion DESC
            LIMIT 200
        """, base_params)

        import json as _json
        detalles = []
        for r in cur.fetchall():
            raw = r['lista_asistencia']
            try:
                asistentes = _json.loads(raw) if raw and raw not in ('', '[]', 'null') else []
            except Exception:
                asistentes = []
            detalles.append({
                'id':            r['id_capacitacion'],
                'fecha':         r['fecha_evento'].strftime('%Y-%m-%d %H:%M') if r['fecha_evento'] else '—',
                'cliente':       r['cliente_instalacion'] or '—',
                'area':          r['puesto_area_especifica'] or '—',
                'responsable':   r['nombre_responsable'] or '—',
                'tema':          r['nombre_capacitacion'] or '—',
                'num_asistentes': int(r['num_asistentes']),
                'asistentes':    [
                    {'nombre': a.get('nombre',''), 'cargo': a.get('cargo',''), 'documento': a.get('documento','')}
                    for a in asistentes
                ],
            })
        return jsonify({'detalles': detalles})
    except Exception as e:
        app_logger.error(f"api_capacitacion_detalles error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ── Visitas Dashboard ───────────────────────────────────────────────────────────

def _visita_date_expr():
    return "COALESCE(fecha_hora, creado_en::timestamp)"

def _visita_conds(cliente, year, month, day):
    conds, params = [], []
    date_expr = _visita_date_expr()
    if cliente:
        conds.append('cliente_instalacion = %s'); params.append(cliente)
    if year:
        conds.append(f'EXTRACT(YEAR FROM {date_expr}) = %s'); params.append(year)
    if month:
        conds.append(f'EXTRACT(MONTH FROM {date_expr}) = %s'); params.append(month)
    if day:
        conds.append(f'EXTRACT(DAY FROM {date_expr}) = %s'); params.append(day)
    return conds, params

def _visita_where(conds):
    return ('WHERE ' + ' AND '.join(conds)) if conds else ''

def _visita_split_blocks(raw_value):
    if not raw_value:
        return []
    return [part.strip() for part in re.split(r'\n\s*---\s*\n', raw_value) if part and part.strip()]

def _visita_parse_responsables(raw_value):
    if not raw_value:
        return []
    import json as _json
    try:
        parsed = _json.loads(raw_value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []

def _visita_status(acuerdo_text, fecha_limite):
    text = (acuerdo_text or '').lower()
    done_markers = ('cumplido', 'completado', 'realizado', 'ejecutado', 'cerrado', 'finalizado')
    if any(marker in text for marker in done_markers):
        return 'cumplido'
    if fecha_limite and fecha_limite < datetime.now().date():
        return 'vencido'
    return 'pendiente'

def _visita_parse_compromisos(rows):
    import json as _json
    compromisos = []
    for row in rows:
        acuerdos = _visita_split_blocks(row['acuerdos_compromisos'])
        responsables = _visita_parse_responsables(row['compromisos_responsable'])
        temas = _visita_split_blocks(row['temas_tratados'])

        estados_override = {}
        raw_estados = row.get('compromisos_estados')
        if raw_estados:
            try:
                estados_override = _json.loads(raw_estados)
            except Exception:
                pass

        max_len = max(len(acuerdos), len(responsables), len(temas), 1)
        for idx in range(max_len):
            acuerdo = acuerdos[idx] if idx < len(acuerdos) else ''
            responsable_item = responsables[idx] if idx < len(responsables) and isinstance(responsables[idx], dict) else {}
            responsable = (
                responsable_item.get('nombre')
                or row.get('nombre_responsable')
                or row.get('nombre_visitante')
                or 'Por definir'
            )

            fecha_limite = None
            raw_fecha = responsable_item.get('fecha') if responsable_item else None
            if raw_fecha:
                try:
                    fecha_limite = datetime.fromisoformat(raw_fecha).date()
                except Exception:
                    fecha_limite = None
            if not fecha_limite and row.get('fecha_cumplimiento'):
                fecha_limite = row['fecha_cumplimiento']
            if not fecha_limite and row.get('compromisos_fecha_limite'):
                fecha_limite = row['compromisos_fecha_limite']

            tema = temas[idx] if idx < len(temas) else (temas[0] if temas else 'Sin tema registrado')
            if not acuerdo and not tema and not fecha_limite and not responsable_item:
                continue

            estado_from_form = responsable_item.get('estado') or ''
            override_val = estados_override.get(str(idx))
            if isinstance(override_val, dict):
                estado = override_val.get('estado') or estado_from_form or _visita_status(acuerdo, fecha_limite)
                accion_tomada = override_val.get('accion_tomada') or ''
                evidencia_url = override_val.get('evidencia_url') or ''
            else:
                estado = override_val or estado_from_form or _visita_status(acuerdo, fecha_limite)
                accion_tomada = ''
                evidencia_url = ''
            compromisos.append({
                'id_visita': row['id_visita'],
                'bloque_idx': idx,
                'cliente': row['cliente_instalacion'] or 'Sin cliente',
                'fecha_visita': row['fecha_evento'].strftime('%Y-%m-%d %H:%M') if row['fecha_evento'] else '—',
                'fecha_sort': row['fecha_evento'].strftime('%Y-%m-%dT%H:%M:%S') if row['fecha_evento'] else '',
                'motivo_visita': row['motivo_visita'] or 'Sin motivo',
                'tema': tema or 'Sin tema registrado',
                'acuerdo': acuerdo or 'Sin acuerdo detallado',
                'responsable': responsable,
                'fecha_cumplimiento': fecha_limite.isoformat() if fecha_limite else '—',
                'estado': estado,
                'accion_tomada': accion_tomada,
                'evidencia_url': evidencia_url,
            })
    return compromisos


@dashboard_bp.route('/api/visitas/clientes')
@jwt_required()
def api_visitas_clientes():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'clientes': []})
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT DISTINCT cliente_instalacion
            FROM registro_y_acta_de_visita
            WHERE cliente_instalacion IS NOT NULL AND cliente_instalacion <> ''
            ORDER BY cliente_instalacion
        """)
        return jsonify({'clientes': [r['cliente_instalacion'] for r in cur.fetchall()]})
    except Exception as e:
        app_logger.error(f"api_visitas_clientes error: {e}", exc_info=True)
        return jsonify({'clientes': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/visitas/data')
@jwt_required()
def api_visitas_data():
    cliente = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day     = int(request.args.get('day'))   if request.args.get('day')   else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        date_expr = _visita_date_expr()
        base_conds, base_params = _visita_conds(cliente, year, month, day)
        where = _visita_where(base_conds)

        cur.execute(f"""
            SELECT
                COUNT(*) AS total_visitas,
                COUNT(DISTINCT NULLIF(TRIM(cliente_instalacion), '')) AS clientes_visitados
            FROM registro_y_acta_de_visita
            {where}
        """, base_params)
        kpi_row = cur.fetchone()
        total_visitas = int(kpi_row['total_visitas']) if kpi_row and kpi_row['total_visitas'] else 0
        clientes_visitados = int(kpi_row['clientes_visitados']) if kpi_row and kpi_row['clientes_visitados'] else 0
        promedio_visitas = round(total_visitas / clientes_visitados, 1) if clientes_visitados else 0

        total_clientes_base = 0
        if cliente:
            total_clientes_base = 1
        else:
            cur.execute("""
                SELECT COUNT(*)
                FROM propiedades
                WHERE activa = TRUE
            """)
            total_clientes_base = int(cur.fetchone()[0] or 0)
            if total_clientes_base == 0:
                cur.execute("""
                    SELECT COUNT(DISTINCT NULLIF(TRIM(cliente_instalacion), ''))
                    FROM registro_y_acta_de_visita
                """)
                total_clientes_base = int(cur.fetchone()[0] or 0)

        clientes_sin_visita = max(total_clientes_base - clientes_visitados, 0)
        pct_sin_visita = round((clientes_sin_visita / total_clientes_base) * 100, 1) if total_clientes_base else 0

        freq_conds = base_conds + ["cliente_instalacion IS NOT NULL", "cliente_instalacion <> ''"]
        cur.execute(f"""
            SELECT
                cliente_instalacion AS cliente,
                COUNT(*) AS visitas
            FROM registro_y_acta_de_visita
            {_visita_where(freq_conds)}
            GROUP BY cliente_instalacion
            ORDER BY visitas DESC, cliente_instalacion
            LIMIT 12
        """, base_params)
        frecuencia_clientes = [{
            'cliente': r['cliente'] or 'Sin cliente',
            'visitas': int(r['visitas']),
        } for r in cur.fetchall()]

        trend_conds = base_conds + [f'{date_expr} IS NOT NULL']
        cur.execute(f"""
            SELECT
                DATE_TRUNC('month', {date_expr}) AS month_start,
                TO_CHAR(DATE_TRUNC('month', {date_expr}), 'YYYY-MM') AS periodo,
                TO_CHAR(DATE_TRUNC('month', {date_expr}), 'Mon YYYY') AS periodo_label,
                COUNT(*) AS visitas
            FROM registro_y_acta_de_visita
            {_visita_where(trend_conds)}
            GROUP BY DATE_TRUNC('month', {date_expr})
            ORDER BY DATE_TRUNC('month', {date_expr})
        """, base_params)
        tendencia = [{
            'periodo': r['periodo'],
            'periodo_label': r['periodo_label'].strip() if r['periodo_label'] else r['periodo'],
            'month_start': r['month_start'].strftime('%Y-%m-%d') if r['month_start'] else None,
            'visitas': int(r['visitas']),
        } for r in cur.fetchall()]

        cur.execute(f"""
            SELECT
                COALESCE(NULLIF(TRIM(motivo_visita), ''), 'Sin motivo') AS motivo,
                COUNT(*) AS visitas
            FROM registro_y_acta_de_visita
            {where}
            GROUP BY motivo
            ORDER BY visitas DESC, motivo
        """, base_params)
        motivos = [{
            'motivo': r['motivo'],
            'visitas': int(r['visitas']),
        } for r in cur.fetchall()]

        cur.execute(f"""
            SELECT
                id_visita,
                cliente_instalacion,
                {date_expr} AS fecha_evento,
                motivo_visita,
                temas_tratados,
                acuerdos_compromisos,
                compromisos_responsable,
                compromisos_fecha_limite,
                nombre_responsable,
                fecha_cumplimiento,
                nombre_visitante,
                compromisos_estados
            FROM registro_y_acta_de_visita
            {_visita_where(trend_conds)}
            ORDER BY {date_expr} DESC NULLS LAST, id_visita DESC
        """, base_params)
        compromisos = _visita_parse_compromisos(cur.fetchall())

        estado_counts = {'cumplido': 0, 'pendiente': 0, 'vencido': 0}
        resolucion_dias = []
        for item in compromisos:
            estado_counts[item['estado']] = estado_counts.get(item['estado'], 0) + 1
            if item['estado'] == 'cumplido' and item['fecha_cumplimiento'] != '—' and item['fecha_sort']:
                try:
                    from datetime import date as _date
                    f_creacion = datetime.fromisoformat(item['fecha_sort']).date()
                    f_cierre = _date.fromisoformat(item['fecha_cumplimiento'])
                    dias = (f_cierre - f_creacion).days
                    if dias >= 0:
                        resolucion_dias.append(dias)
                except Exception:
                    pass
        avg_dias_resolucion = round(sum(resolucion_dias) / len(resolucion_dias), 1) if resolucion_dias else None

        return jsonify({
            'kpi': {
                'total_visitas': total_visitas,
                'clientes_visitados': clientes_visitados,
                'promedio_visitas': promedio_visitas,
                'pct_sin_visita': pct_sin_visita,
                'clientes_sin_visita': clientes_sin_visita,
                'avg_dias_resolucion': avg_dias_resolucion,
                'resolucion_muestra': len(resolucion_dias),
            },
            'frecuencia_clientes': frecuencia_clientes,
            'tendencia': tendencia,
            'motivos': motivos,
            'estado_compromisos': estado_counts,
            'vencidos_total': estado_counts['vencido'],
        })
    except Exception as e:
        app_logger.error(f"api_visitas_data error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/visitas/detalles')
@jwt_required()
def api_visitas_detalles():
    cliente = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day     = int(request.args.get('day'))   if request.args.get('day')   else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        date_expr = _visita_date_expr()
        base_conds, base_params = _visita_conds(cliente, year, month, day)
        trend_conds = base_conds + [f'{date_expr} IS NOT NULL']

        cur.execute(f"""
            SELECT
                id_visita,
                cliente_instalacion,
                {date_expr} AS fecha_evento,
                motivo_visita,
                temas_tratados,
                acuerdos_compromisos,
                compromisos_responsable,
                compromisos_fecha_limite,
                nombre_responsable,
                fecha_cumplimiento,
                nombre_visitante,
                compromisos_estados
            FROM registro_y_acta_de_visita
            {_visita_where(trend_conds)}
            ORDER BY {date_expr} DESC NULLS LAST, id_visita DESC
            LIMIT 200
        """, base_params)
        compromisos = _visita_parse_compromisos(cur.fetchall())
        compromisos.sort(key=lambda item: (item['fecha_sort'], item['cliente']), reverse=True)
        return jsonify({'detalles': compromisos})
    except Exception as e:
        app_logger.error(f"api_visitas_detalles error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/visitas/<int:id_visita>/estado', methods=['PUT'])
@jwt_required()
def api_visitas_update_estado(id_visita):
    import json as _json
    # Accept either multipart/form-data (with file) or JSON
    if request.content_type and 'multipart' in request.content_type:
        bloque_idx = request.form.get('bloque_idx')
        nuevo_estado = (request.form.get('estado') or '').strip().lower()
        accion_tomada = (request.form.get('accion_tomada') or '').strip()
        evidencia_file = request.files.get('evidencia')
    else:
        body = request.get_json(silent=True) or {}
        bloque_idx = body.get('bloque_idx')
        nuevo_estado = body.get('estado', '').strip().lower()
        accion_tomada = (body.get('accion_tomada') or '').strip()
        evidencia_file = None

    try:
        bloque_idx = int(bloque_idx)
    except (TypeError, ValueError):
        return jsonify({'error': 'Parámetros inválidos'}), 400

    if nuevo_estado not in ('cumplido', 'pendiente', 'vencido'):
        return jsonify({'error': 'Parámetros inválidos'}), 400

    evidencia_url = _upload_file_to_gcs(evidencia_file) if evidencia_file else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute(
            "SELECT compromisos_estados FROM registro_y_acta_de_visita WHERE id_visita = %s",
            (id_visita,)
        )
        row = cur.fetchone()
        if row is None:
            return jsonify({'error': 'Registro no encontrado'}), 404

        estados = {}
        if row['compromisos_estados']:
            try:
                estados = _json.loads(row['compromisos_estados'])
            except Exception:
                pass

        # Preserve existing evidencia_url if no new file uploaded
        existing = estados.get(str(bloque_idx))
        existing_evidencia = ''
        if isinstance(existing, dict):
            existing_evidencia = existing.get('evidencia_url') or ''

        estados[str(bloque_idx)] = {
            'estado': nuevo_estado,
            'accion_tomada': accion_tomada,
            'evidencia_url': evidencia_url or existing_evidencia,
        }

        cur.execute(
            "UPDATE registro_y_acta_de_visita SET compromisos_estados = %s WHERE id_visita = %s",
            (_json.dumps(estados), id_visita)
        )
        conn.commit()
        return jsonify({'ok': True, 'estado': nuevo_estado, 'evidencia_url': evidencia_url or existing_evidencia})
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"api_visitas_update_estado error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ── Motocicletas Dashboard ────────────────────────────────────────────────────

_MOTO_COMPONENTS = [
    ('estado_neumaticos',                    'Estado Neumáticos'),
    ('estado_rines',                         'Estado Rines'),
    ('equipo_carretera',                     'Equipo Carretera'),
    ('estado_kit_arrastre',                  'Kit Arrastre'),
    ('estado_palanca_soporte',               'Palanca Soporte'),
    ('estado_forro_asiento',                 'Forro Asiento'),
    ('estado_tapas_derecha',                 'Tapas Derecha'),
    ('estado_luces_direccionales_derecha',   'Luces Dir. Derecha'),
    ('estado_luces_delanteras',              'Luces Delanteras'),
    ('estado_guarda_fango_delantero',        'Guarda Fango Del.'),
    ('estado_sistema_freno_delantero',       'Freno Delantero'),
    ('estado_manillar_embrague',             'Manillar Embrague'),
    ('estado_manillar_freno_delantero',      'Manillar Freno Del.'),
    ('estado_manometros_indicadores',        'Manómetros'),
    ('estado_tanque_combustible',            'Tanque Combustible'),
    ('tapa_tanque_combustible',              'Tapa Tanque'),
    ('espejos_retrovisores',                 'Espejos Retrovisores'),
    ('tapa_aceite_motor',                    'Tapa Aceite'),
    ('bateria_tapa',                         'Batería'),
    ('estado_luces_izquierda',               'Luces Izquierda'),
    ('estado_luces_direccionales_izquierda', 'Luces Dir. Izquierda'),
    ('estado_luz_trasera',                   'Luz Trasera'),
    ('estado_guarda_fango_trasero',          'Guarda Fango Tras.'),
    ('estado_tubo_escape',                   'Tubo Escape'),
    ('estado_palanca_freno',                 'Palanca Freno'),
    ('estado_palanca_cambios',               'Palanca Cambios'),
]

_MOTO_FAULT_EXPR = " OR ".join(
    [f"LOWER(COALESCE({col},''))='malo'" for col, _ in _MOTO_COMPONENTS]
)
_MOTO_FAULT_SUM = " + ".join(
    [f"CASE WHEN LOWER(COALESCE({col},''))='malo' THEN 1 ELSE 0 END"
     for col, _ in _MOTO_COMPONENTS]
)


def _moto_date_expr():
    return "COALESCE(fecha_hora, creado_en)"


def _moto_conds(cliente, year, month, day):
    conds, params = [], []
    if cliente:
        conds.append("cliente_instalacion = %s")
        params.append(cliente)
    de = _moto_date_expr()
    if year:
        conds.append(f"EXTRACT(YEAR  FROM {de}) = %s")
        params.append(year)
    if month:
        conds.append(f"EXTRACT(MONTH FROM {de}) = %s")
        params.append(month)
    if day:
        conds.append(f"EXTRACT(DAY   FROM {de}) = %s")
        params.append(day)
    return conds, params


def _moto_where(conds):
    return ("WHERE " + " AND ".join(conds)) if conds else ""


@dashboard_bp.route('/api/motocicletas/clientes')
@jwt_required()
def api_motocicletas_clientes():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'clientes': []})
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT DISTINCT cliente_instalacion
            FROM planilla_motocicletas
            WHERE cliente_instalacion IS NOT NULL AND TRIM(cliente_instalacion) <> ''
            ORDER BY cliente_instalacion
        """)
        return jsonify({'clientes': [r['cliente_instalacion'] for r in cur.fetchall()]})
    except Exception as e:
        app_logger.error(f"api_motocicletas_clientes error: {e}", exc_info=True)
        return jsonify({'clientes': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/motocicletas/data')
@jwt_required()
def api_motocicletas_data():
    cliente = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day     = int(request.args.get('day'))   if request.args.get('day')   else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        base_conds, base_params = _moto_conds(cliente, year, month, day)
        where = _moto_where(base_conds)

        # ── KPIs ──────────────────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                COUNT(*)                                                      AS total,
                SUM(CASE WHEN {_MOTO_FAULT_EXPR} THEN 1 ELSE 0 END)         AS no_aptas,
                SUM({_MOTO_FAULT_SUM})                                        AS total_fallas
            FROM planilla_motocicletas {where}
        """, base_params)
        kpi = cur.fetchone()
        total        = int(kpi['total']        or 0)
        no_aptas     = int(kpi['no_aptas']     or 0)
        total_fallas = int(kpi['total_fallas'] or 0)
        aptas        = total - no_aptas
        pct_aptas    = round(aptas    / total * 100, 1) if total else 0
        pct_no_aptas = round(no_aptas / total * 100, 1) if total else 0

        # ── Fallas por componente ──────────────────────────────────────────
        comp_cases = ", ".join([
            f"SUM(CASE WHEN LOWER(COALESCE({col},''))='malo' THEN 1 ELSE 0 END) AS \"{col}\""
            for col, _ in _MOTO_COMPONENTS
        ])
        cur.execute(f"SELECT {comp_cases} FROM planilla_motocicletas {where}", base_params)
        comp_row = cur.fetchone()
        fallas_comp = [
            {'componente': label, 'fallas': int(comp_row[col] or 0)}
            for col, label in _MOTO_COMPONENTS
        ]
        fallas_comp.sort(key=lambda x: x['fallas'])   # ascending: fewest first

        # ── Tendencia mensual ──────────────────────────────────────────────
        de = _moto_date_expr()
        tend_conds = base_conds + [f"{de} IS NOT NULL"]
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', {de}), 'YYYY-MM')   AS periodo,
                TO_CHAR(DATE_TRUNC('month', {de}), 'Mon YYYY')  AS periodo_label,
                COUNT(*)                                          AS inspecciones,
                SUM(CASE WHEN {_MOTO_FAULT_EXPR} THEN 1 ELSE 0 END) AS no_aptas
            FROM planilla_motocicletas
            {_moto_where(tend_conds)}
            GROUP BY DATE_TRUNC('month', {de})
            ORDER BY DATE_TRUNC('month', {de})
        """, base_params)
        tendencia = [
            {
                'periodo':       r['periodo'],
                'periodo_label': (r['periodo_label'] or '').strip(),
                'inspecciones':  int(r['inspecciones'] or 0),
                'no_aptas':      int(r['no_aptas']     or 0),
            }
            for r in cur.fetchall()
        ]

        # ── Novedades por número de placa ─────────────────────────────────
        placa_nov_conds = base_conds + [
            "placa_motocicleta IS NOT NULL",
            "TRIM(placa_motocicleta) <> ''"
        ]
        cur.execute(f"""
            SELECT
                TRIM(placa_motocicleta) AS placa,
                COUNT(*) AS inspecciones,
                SUM(CASE WHEN {_MOTO_FAULT_EXPR} THEN 1 ELSE 0 END) AS no_aptas
            FROM planilla_motocicletas {_moto_where(placa_nov_conds)}
            GROUP BY TRIM(placa_motocicleta)
            ORDER BY no_aptas DESC, inspecciones DESC
            LIMIT 15
        """, base_params)
        por_placa = [
            {
                'placa':        r['placa'],
                'inspecciones': int(r['inspecciones'] or 0),
                'no_aptas':     int(r['no_aptas']     or 0),
            }
            for r in cur.fetchall()
        ]

        # ── Alertas ────────────────────────────────────────────────────────
        alertas_conds = base_conds + [
            "novedades_criticas_detectadas IS NOT NULL",
            "TRIM(novedades_criticas_detectadas) <> ''"
        ]
        cur.execute(f"""
            SELECT COUNT(*) FROM planilla_motocicletas {_moto_where(alertas_conds)}
        """, base_params)
        alertas_count = int(cur.fetchone()[0] or 0)

        # ── Placas recurrentes ─────────────────────────────────────────────
        placa_conds = base_conds + [
            "placa_motocicleta IS NOT NULL",
            "TRIM(placa_motocicleta) <> ''"
        ]
        cur.execute(f"""
            SELECT placa_motocicleta AS placa,
                   COUNT(*) AS inspecciones,
                   SUM(CASE WHEN {_MOTO_FAULT_EXPR} THEN 1 ELSE 0 END) AS no_aptas
            FROM planilla_motocicletas {_moto_where(placa_conds)}
            GROUP BY placa_motocicleta
            HAVING SUM(CASE WHEN {_MOTO_FAULT_EXPR} THEN 1 ELSE 0 END) >= 2
            ORDER BY no_aptas DESC, inspecciones DESC
            LIMIT 5
        """, base_params)
        placas_recurrentes = [
            {
                'placa':        r['placa'],
                'inspecciones': int(r['inspecciones'] or 0),
                'no_aptas':     int(r['no_aptas']     or 0),
            }
            for r in cur.fetchall()
        ]

        return jsonify({
            'kpi': {
                'total_inspecciones': total,
                'aptas':              aptas,
                'pct_aptas':          pct_aptas,
                'no_aptas':           no_aptas,
                'pct_no_aptas':       pct_no_aptas,
                'total_fallas':       total_fallas,
            },
            'fallas_componente':  fallas_comp,
            'tendencia':          tendencia,
            'por_placa':          por_placa,
            'alertas_count':      alertas_count,
            'placas_recurrentes': placas_recurrentes,
        })
    except Exception as e:
        app_logger.error(f"api_motocicletas_data error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/motocicletas/detalles')
@jwt_required()
def api_motocicletas_detalles():
    cliente = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day     = int(request.args.get('day'))   if request.args.get('day')   else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        de = _moto_date_expr()
        base_conds, base_params = _moto_conds(cliente, year, month, day)
        nov_conds = base_conds + [
            "novedades_criticas_detectadas IS NOT NULL",
            "TRIM(novedades_criticas_detectadas) <> ''"
        ]
        cur.execute(f"""
            SELECT
                id,
                COALESCE(NULLIF(TRIM(cliente_instalacion),''), '—') AS cliente,
                COALESCE(NULLIF(TRIM(placa_motocicleta),''), '—')   AS placa,
                COALESCE(NULLIF(TRIM(nombre_responsable),''), '—')  AS responsable,
                {de} AS fecha_evento,
                novedades_criticas_detectadas,
                accion_inmediata_tomada
            FROM planilla_motocicletas
            {_moto_where(nov_conds)}
            ORDER BY {de} DESC NULLS LAST, id DESC
            LIMIT 200
        """, base_params)
        rows = cur.fetchall()
        detalles = [
            {
                'id':                 r['id'],
                'cliente':            r['cliente'],
                'placa':              r['placa'],
                'responsable':        r['responsable'],
                'fecha':              r['fecha_evento'].strftime('%d/%m/%Y %H:%M') if r['fecha_evento'] else '—',
                'novedades_criticas': r['novedades_criticas_detectadas'] or '—',
                'accion_inmediata':   r['accion_inmediata_tomada']       or '—',
            }
            for r in rows
        ]

        all_cols = ",\n                ".join(
            [f"COALESCE(NULLIF(TRIM({col}), ''), '—') AS {col}" for col, _ in _MOTO_COMPONENTS]
        )
        cur.execute(f"""
            SELECT
                id,
                COALESCE(NULLIF(TRIM(cliente_instalacion),''), '—') AS cliente,
                COALESCE(NULLIF(TRIM(placa_motocicleta),''), '—')   AS placa,
                COALESCE(NULLIF(TRIM(nombre_responsable),''), '—')  AS responsable,
                {de} AS fecha_evento,
                {all_cols}
            FROM planilla_motocicletas
            {_moto_where(base_conds)}
            ORDER BY {de} DESC NULLS LAST, id DESC
            LIMIT 300
        """, base_params)
        inspecciones = []
        for r in cur.fetchall():
            componentes_malos = [
                label for col, label in _MOTO_COMPONENTS
                if (r[col] or '').strip().lower() == 'malo'
            ]
            inspecciones.append({
                'id': r['id'],
                'cliente': r['cliente'],
                'placa': r['placa'],
                'responsable': r['responsable'],
                'fecha': r['fecha_evento'].strftime('%d/%m/%Y %H:%M') if r['fecha_evento'] else '—',
                'periodo': r['fecha_evento'].strftime('%Y-%m') if r['fecha_evento'] else '',
                'no_apta': len(componentes_malos) > 0,
                'fallas': len(componentes_malos),
                'componentes_malos': componentes_malos,
            })
        return jsonify({'detalles': detalles, 'inspecciones': inspecciones})
    except Exception as e:
        app_logger.error(f"api_motocicletas_detalles error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ── Vehiculos Dashboard ───────────────────────────────────────────────────────

_VEH_COMPONENTS = [
    ('estado_rines',                  'Estado Rines'),
    ('juego_senales_carretera',       'Señales Carretera'),
    ('gato_hidraulico',               'Gato Hidráulico'),
    ('palanca_gato',                  'Palanca Gato'),
    ('estado_asientos',               'Estado Asientos'),
    ('estado_tapetes_alfombras',      'Tapetes/Alfombras'),
    ('limpieza_carroceria',           'Limpieza Carrocería'),
    ('luces_delanteras',              'Luces Delanteras'),
    ('luces_direccionales',           'Luces Direccionales'),
    ('luces_traseras',                'Luces Traseras'),
    ('parabrisas_delantero',          'Parabrisas Del.'),
    ('parabrisas_trasero',            'Parabrisas Tras.'),
    ('defensa_delantera',             'Defensa Delantera'),
    ('defensa_trasera',               'Defensa Trasera'),
    ('puertas_vidrios',               'Puertas/Vidrios'),
    ('tapa_radiador',                 'Tapa Radiador'),
    ('tapa_aceite_motor',             'Tapa Aceite'),
    ('bateria_tapa',                  'Batería'),
    ('espejo_retrovisor_interno',     'Retrovisor Interno'),
    ('espejos_retrovisores_externos', 'Retrovisores Ext.'),
    ('limpia_brisas',                 'Limpia Brisas'),
    ('antena_radio',                  'Antena/Radio'),
    ('radio_funciona',                'Radio'),
    ('llanta_repuesto',               'Llanta Repuesto'),
    ('aire_acondicionado',            'Aire Acondicionado'),
]

_VEH_FAULT_EXPR = " OR ".join(
    [f"LOWER(COALESCE({col},''))='malo'" for col, _ in _VEH_COMPONENTS]
)
_VEH_FAULT_SUM = " + ".join(
    [f"CASE WHEN LOWER(COALESCE({col},''))='malo' THEN 1 ELSE 0 END"
     for col, _ in _VEH_COMPONENTS]
)


def _veh_date_expr():
    return "COALESCE(fecha_hora::timestamp, creado_en::timestamp)"


def _veh_conds(cliente, year, month, day):
    conds, params = [], []
    if cliente:
        conds.append("cliente_instalacion = %s")
        params.append(cliente)
    de = _veh_date_expr()
    if year:
        conds.append(f"EXTRACT(YEAR  FROM {de}) = %s")
        params.append(year)
    if month:
        conds.append(f"EXTRACT(MONTH FROM {de}) = %s")
        params.append(month)
    if day:
        conds.append(f"EXTRACT(DAY   FROM {de}) = %s")
        params.append(day)
    return conds, params


def _veh_where(conds):
    return ("WHERE " + " AND ".join(conds)) if conds else ""


@dashboard_bp.route('/api/vehiculos/clientes')
@jwt_required()
def api_vehiculos_clientes():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'clientes': []})
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT DISTINCT cliente_instalacion
            FROM planilla_vehicular
            WHERE cliente_instalacion IS NOT NULL AND TRIM(cliente_instalacion) <> ''
            ORDER BY cliente_instalacion
        """)
        return jsonify({'clientes': [r['cliente_instalacion'] for r in cur.fetchall()]})
    except Exception as e:
        app_logger.error(f"api_vehiculos_clientes error: {e}", exc_info=True)
        return jsonify({'clientes': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/vehiculos/data')
@jwt_required()
def api_vehiculos_data():
    cliente = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day     = int(request.args.get('day'))   if request.args.get('day')   else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        base_conds, base_params = _veh_conds(cliente, year, month, day)
        where = _veh_where(base_conds)

        # ── KPIs ──────────────────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                COUNT(*)                                                      AS total,
                SUM(CASE WHEN {_VEH_FAULT_EXPR} THEN 1 ELSE 0 END)          AS no_aptas,
                SUM({_VEH_FAULT_SUM})                                         AS total_fallas
            FROM planilla_vehicular {where}
        """, base_params)
        kpi = cur.fetchone()
        total        = int(kpi['total']        or 0)
        no_aptas     = int(kpi['no_aptas']     or 0)
        total_fallas = int(kpi['total_fallas'] or 0)
        aptas        = total - no_aptas
        pct_aptas    = round(aptas    / total * 100, 1) if total else 0
        pct_no_aptas = round(no_aptas / total * 100, 1) if total else 0

        # ── Fallas por componente ──────────────────────────────────────────
        comp_cases = ", ".join([
            f"SUM(CASE WHEN LOWER(COALESCE({col},''))='malo' THEN 1 ELSE 0 END) AS \"{col}\""
            for col, _ in _VEH_COMPONENTS
        ])
        cur.execute(f"SELECT {comp_cases} FROM planilla_vehicular {where}", base_params)
        comp_row = cur.fetchone()
        fallas_comp = [
            {'componente': label, 'fallas': int(comp_row[col] or 0)}
            for col, label in _VEH_COMPONENTS
        ]
        fallas_comp.sort(key=lambda x: x['fallas'])   # ascending: fewest first

        # ── Tendencia mensual ──────────────────────────────────────────────
        de = _veh_date_expr()
        tend_conds = base_conds + [f"{de} IS NOT NULL"]
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', {de}), 'YYYY-MM')   AS periodo,
                TO_CHAR(DATE_TRUNC('month', {de}), 'Mon YYYY')  AS periodo_label,
                COUNT(*)                                          AS inspecciones,
                SUM(CASE WHEN {_VEH_FAULT_EXPR} THEN 1 ELSE 0 END) AS no_aptas
            FROM planilla_vehicular
            {_veh_where(tend_conds)}
            GROUP BY DATE_TRUNC('month', {de})
            ORDER BY DATE_TRUNC('month', {de})
        """, base_params)
        tendencia = [
            {
                'periodo':       r['periodo'],
                'periodo_label': (r['periodo_label'] or '').strip(),
                'inspecciones':  int(r['inspecciones'] or 0),
                'no_aptas':      int(r['no_aptas']     or 0),
            }
            for r in cur.fetchall()
        ]

        # ── Novedades por número de placa ─────────────────────────────────
        placa_nov_conds = base_conds + [
            "placa_vehiculo IS NOT NULL",
            "TRIM(placa_vehiculo) <> ''"
        ]
        cur.execute(f"""
            SELECT
                TRIM(placa_vehiculo) AS placa,
                COUNT(*) AS inspecciones,
                SUM(CASE WHEN {_VEH_FAULT_EXPR} THEN 1 ELSE 0 END) AS no_aptas
            FROM planilla_vehicular {_veh_where(placa_nov_conds)}
            GROUP BY TRIM(placa_vehiculo)
            ORDER BY no_aptas DESC, inspecciones DESC
            LIMIT 15
        """, base_params)
        por_placa = [
            {
                'placa':        r['placa'],
                'inspecciones': int(r['inspecciones'] or 0),
                'no_aptas':     int(r['no_aptas']     or 0),
            }
            for r in cur.fetchall()
        ]

        # ── Alertas: inspecciones con novedades críticas ───────────────────
        alertas_conds = base_conds + [
            "novedades_criticas IS NOT NULL",
            "TRIM(novedades_criticas) <> ''"
        ]
        cur.execute(f"""
            SELECT COUNT(*) FROM planilla_vehicular {_veh_where(alertas_conds)}
        """, base_params)
        alertas_count = int(cur.fetchone()[0] or 0)

        # ── Placas con fallas recurrentes ──────────────────────────────────
        placa_conds = base_conds + [
            "placa_vehiculo IS NOT NULL",
            "TRIM(placa_vehiculo) <> ''"
        ]
        cur.execute(f"""
            SELECT placa_vehiculo AS placa,
                   COUNT(*) AS inspecciones,
                   SUM(CASE WHEN {_VEH_FAULT_EXPR} THEN 1 ELSE 0 END) AS no_aptas
            FROM planilla_vehicular {_veh_where(placa_conds)}
            GROUP BY placa_vehiculo
            HAVING SUM(CASE WHEN {_VEH_FAULT_EXPR} THEN 1 ELSE 0 END) >= 2
            ORDER BY no_aptas DESC, inspecciones DESC
            LIMIT 5
        """, base_params)
        placas_recurrentes = [
            {
                'placa':        r['placa'],
                'inspecciones': int(r['inspecciones'] or 0),
                'no_aptas':     int(r['no_aptas']     or 0),
            }
            for r in cur.fetchall()
        ]

        return jsonify({
            'kpi': {
                'total_inspecciones': total,
                'aptas':              aptas,
                'pct_aptas':          pct_aptas,
                'no_aptas':           no_aptas,
                'pct_no_aptas':       pct_no_aptas,
                'total_fallas':       total_fallas,
            },
            'fallas_componente':  fallas_comp,
            'tendencia':          tendencia,
            'por_placa':          por_placa,
            'alertas_count':      alertas_count,
            'placas_recurrentes': placas_recurrentes,
        })
    except Exception as e:
        app_logger.error(f"api_vehiculos_data error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/vehiculos/detalles')
@jwt_required()
def api_vehiculos_detalles():
    cliente = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day     = int(request.args.get('day'))   if request.args.get('day')   else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        de = _veh_date_expr()
        base_conds, base_params = _veh_conds(cliente, year, month, day)
        nov_conds = base_conds + [
            "novedades_criticas IS NOT NULL",
            "TRIM(novedades_criticas) <> ''"
        ]
        cur.execute(f"""
            SELECT
                id_planilla_vehicular,
                COALESCE(NULLIF(TRIM(cliente_instalacion),''), '—') AS cliente,
                COALESCE(NULLIF(TRIM(placa_vehiculo),''), '—')      AS placa,
                COALESCE(NULLIF(TRIM(nombre_responsable),''), '—')  AS responsable,
                {de} AS fecha_evento,
                novedades_criticas,
                accion_inmediata
            FROM planilla_vehicular
            {_veh_where(nov_conds)}
            ORDER BY {de} DESC NULLS LAST, id_planilla_vehicular DESC
            LIMIT 200
        """, base_params)
        rows = cur.fetchall()
        detalles = [
            {
                'id':                 r['id_planilla_vehicular'],
                'cliente':            r['cliente'],
                'placa':              r['placa'],
                'responsable':        r['responsable'],
                'fecha':              r['fecha_evento'].strftime('%d/%m/%Y %H:%M') if r['fecha_evento'] else '—',
                'novedades_criticas': r['novedades_criticas'] or '—',
                'accion_inmediata':   r['accion_inmediata']   or '—',
            }
            for r in rows
        ]

        all_cols = ",\n                ".join(
            [f"COALESCE(NULLIF(TRIM({col}), ''), '—') AS {col}" for col, _ in _VEH_COMPONENTS]
        )
        cur.execute(f"""
            SELECT
                id_planilla_vehicular,
                COALESCE(NULLIF(TRIM(cliente_instalacion),''), '—') AS cliente,
                COALESCE(NULLIF(TRIM(placa_vehiculo),''), '—')      AS placa,
                COALESCE(NULLIF(TRIM(nombre_responsable),''), '—')  AS responsable,
                {de} AS fecha_evento,
                {all_cols}
            FROM planilla_vehicular
            {_veh_where(base_conds)}
            ORDER BY {de} DESC NULLS LAST, id_planilla_vehicular DESC
            LIMIT 300
        """, base_params)
        inspecciones = []
        for r in cur.fetchall():
            componentes_malos = [
                label for col, label in _VEH_COMPONENTS
                if (r[col] or '').strip().lower() == 'malo'
            ]
            inspecciones.append({
                'id': r['id_planilla_vehicular'],
                'cliente': r['cliente'],
                'placa': r['placa'],
                'responsable': r['responsable'],
                'fecha': r['fecha_evento'].strftime('%d/%m/%Y %H:%M') if r['fecha_evento'] else '—',
                'periodo': r['fecha_evento'].strftime('%Y-%m') if r['fecha_evento'] else '',
                'no_apta': len(componentes_malos) > 0,
                'fallas': len(componentes_malos),
                'componentes_malos': componentes_malos,
            })
        return jsonify({'detalles': detalles, 'inspecciones': inspecciones})
    except Exception as e:
        app_logger.error(f"api_vehiculos_detalles error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ── Equipos / Confiabilidad Dashboard ────────────────────────────────────────

# Safe int extraction from JSONB text fields
_EQ_TOTAL_SQL = "CASE WHEN elem->>'total_equipos' ~ '^[0-9]+$' THEN (elem->>'total_equipos')::int ELSE 0 END"
_EQ_FUNC_SQL  = "CASE WHEN elem->>'equipos_operativos' ~ '^[0-9]+$' THEN (elem->>'equipos_operativos')::int ELSE 0 END"

_EQ_BASE = [
    "c.inventario IS NOT NULL",
    "c.inventario != 'null'::jsonb",
    "jsonb_array_length(c.inventario) > 0",
]


def _eq_conds(cliente, year, month, day, responsable=None):
    conds, params = [], []
    if cliente:
        conds.append("c.cliente_instalacion = %s")
        params.append(cliente)
    if year:
        conds.append("EXTRACT(YEAR  FROM c.fecha) = %s")
        params.append(year)
    if month:
        conds.append("EXTRACT(MONTH FROM c.fecha) = %s")
        params.append(month)
    if day:
        conds.append("EXTRACT(DAY   FROM c.fecha) = %s")
        params.append(day)
    if responsable:
        conds.append("TRIM(c.rol_aplicador) = %s")
        params.append(responsable)
    return conds, params


def _eq_where(extra=None):
    parts = list(_EQ_BASE) + (extra or [])
    return "WHERE " + " AND ".join(parts)


def _eq_estado(pct):
    if pct is None: return 'Sin datos'
    f = float(pct)
    if f >= 95: return 'Operativo'
    if f >= 85: return 'Operativo c/obs.'
    if f >= 70: return 'Riesgo operativo'
    return 'No confiable'


@dashboard_bp.route('/api/equipos/filtros')
@jwt_required()
def api_equipos_filtros():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'responsables': []})
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT TRIM(rol_aplicador) AS responsable
            FROM confiabilidad_equipos
            WHERE rol_aplicador IS NOT NULL AND TRIM(rol_aplicador) <> ''
            ORDER BY responsable
        """)
        return jsonify({'responsables': [r[0] for r in cur.fetchall()]})
    except Exception as e:
        app_logger.error(f"api_equipos_filtros error: {e}", exc_info=True)
        return jsonify({'responsables': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/equipos/clientes')
@jwt_required()
def api_equipos_clientes():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'clientes': []})
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT DISTINCT cliente_instalacion
            FROM confiabilidad_equipos
            WHERE cliente_instalacion IS NOT NULL AND TRIM(cliente_instalacion) <> ''
            ORDER BY cliente_instalacion
        """)
        return jsonify({'clientes': [r['cliente_instalacion'] for r in cur.fetchall()]})
    except Exception as e:
        app_logger.error(f"api_equipos_clientes error: {e}", exc_info=True)
        return jsonify({'clientes': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/equipos/data')
@jwt_required()
def api_equipos_data():
    cliente     = request.args.get('cliente')     or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day         = int(request.args.get('day'))    if request.args.get('day')   else None
    responsable = request.args.get('responsable') or None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        base_conds, base_params = _eq_conds(cliente, year, month, day, responsable=responsable)
        where = _eq_where(base_conds)
        lateral = f"""
            FROM confiabilidad_equipos c,
                 LATERAL jsonb_array_elements(c.inventario) AS elem
            {where}
        """

        # ── KPIs ──────────────────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                COUNT(DISTINCT c.id)          AS total_registros,
                SUM({_EQ_TOTAL_SQL})           AS sum_total,
                SUM({_EQ_FUNC_SQL})            AS sum_func
            {lateral}
        """, base_params)
        krow = cur.fetchone()
        total_registros = int(krow['total_registros'] or 0)
        sum_total       = int(krow['sum_total'] or 0)
        sum_func        = int(krow['sum_func']  or 0)
        pct_general     = round(sum_func / sum_total * 100, 1) if sum_total else None

        # ── Distribution per record (for donut) ───────────────────────────
        cur.execute(f"""
            WITH per_rec AS (
                SELECT c.id,
                    ROUND(SUM({_EQ_FUNC_SQL})::numeric
                        / NULLIF(SUM({_EQ_TOTAL_SQL}), 0) * 100, 1) AS pct
                {lateral}
                GROUP BY c.id
                HAVING SUM({_EQ_TOTAL_SQL}) > 0
            )
            SELECT
                COUNT(*) FILTER (WHERE pct >= 95)             AS verde,
                COUNT(*) FILTER (WHERE pct >= 85 AND pct < 95) AS amarillo,
                COUNT(*) FILTER (WHERE pct >= 70 AND pct < 85) AS naranja,
                COUNT(*) FILTER (WHERE pct < 70)               AS rojo,
                COUNT(*)                                        AS total
            FROM per_rec WHERE pct IS NOT NULL
        """, base_params)
        dist = cur.fetchone()
        distribucion = {
            'verde':    int(dist['verde']    or 0),
            'amarillo': int(dist['amarillo'] or 0),
            'naranja':  int(dist['naranja']  or 0),
            'rojo':     int(dist['rojo']     or 0),
            'total':    int(dist['total']    or 0),
        }

        # ── Confiabilidad por tipo (bar chart, sorted asc) ─────────────────
        cur.execute(f"""
            SELECT
                COALESCE(NULLIF(TRIM(elem->>'tipo'), ''), 'Sin tipo') AS tipo,
                SUM({_EQ_TOTAL_SQL})  AS sum_total,
                SUM({_EQ_FUNC_SQL})   AS sum_func,
                ROUND(SUM({_EQ_FUNC_SQL})::numeric
                    / NULLIF(SUM({_EQ_TOTAL_SQL}), 0) * 100, 1) AS pct
            {lateral}
            GROUP BY COALESCE(NULLIF(TRIM(elem->>'tipo'), ''), 'Sin tipo')
            HAVING SUM({_EQ_TOTAL_SQL}) > 0
            ORDER BY pct ASC NULLS LAST
        """, base_params)
        por_tipo = [
            {
                'tipo':      r['tipo'],
                'total':     int(r['sum_total'] or 0),
                'func':      int(r['sum_func']  or 0),
                'pct':       float(r['pct'])    if r['pct'] is not None else None,
                'estado':    _eq_estado(r['pct']),
            }
            for r in cur.fetchall()
        ]
        tipos_no_conf = sum(1 for t in por_tipo if t['pct'] is not None and t['pct'] < 70)

        # ── Tendencia mensual ──────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', c.fecha::timestamp), 'YYYY-MM')  AS periodo,
                TO_CHAR(DATE_TRUNC('month', c.fecha::timestamp), 'Mon YYYY') AS periodo_label,
                ROUND(SUM({_EQ_FUNC_SQL})::numeric
                    / NULLIF(SUM({_EQ_TOTAL_SQL}), 0) * 100, 1) AS pct,
                COUNT(DISTINCT c.id)                                          AS registros
            {lateral}
            GROUP BY DATE_TRUNC('month', c.fecha::timestamp)
            ORDER BY DATE_TRUNC('month', c.fecha::timestamp)
        """, base_params)
        tendencia = [
            {
                'periodo':       r['periodo'],
                'periodo_label': (r['periodo_label'] or '').strip(),
                'pct':           float(r['pct']) if r['pct'] is not None else None,
                'registros':     int(r['registros'] or 0),
            }
            for r in cur.fetchall()
        ]

        # ── Alertas ────────────────────────────────────────────────────────
        alertas = {'no_conf_tipos': tipos_no_conf, 'deterioro': False, 'detalle': ''}
        if len(tendencia) >= 2:
            last, prev = tendencia[-1], tendencia[-2]
            if last['pct'] is not None and prev['pct'] is not None:
                if last['pct'] < prev['pct']:
                    alertas['deterioro'] = True
                    alertas['detalle'] = (
                        f"De {prev['pct']}% ({prev['periodo_label']}) "
                        f"a {last['pct']}% ({last['periodo_label']})"
                    )

        return jsonify({
            'kpi': {
                'total_registros':  total_registros,
                'pct_general':      pct_general,
                'estado_general':   _eq_estado(pct_general),
                'total_equipos':    sum_total,
                'tipos_no_conf':    tipos_no_conf,
            },
            'distribucion': distribucion,
            'por_tipo':     por_tipo,
            'tendencia':    tendencia,
            'alertas':      alertas,
        })
    except Exception as e:
        app_logger.error(f"api_equipos_data error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/equipos/detalles')
@jwt_required()
def api_equipos_detalles():
    cliente = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None
    day     = int(request.args.get('day'))   if request.args.get('day')   else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        base_conds, base_params = _eq_conds(cliente, year, month, day)
        where  = _eq_where(base_conds)
        lateral = f"""
            FROM confiabilidad_equipos c,
                 LATERAL jsonb_array_elements(c.inventario) AS elem
            {where}
        """

        # ── Consolidated matrix (by tipo, over filter period) ─────────────
        cur.execute(f"""
            SELECT
                COALESCE(NULLIF(TRIM(elem->>'tipo'), ''), 'Sin tipo') AS tipo,
                SUM({_EQ_TOTAL_SQL})  AS sum_total,
                SUM({_EQ_FUNC_SQL})   AS sum_func,
                ROUND(SUM({_EQ_FUNC_SQL})::numeric
                    / NULLIF(SUM({_EQ_TOTAL_SQL}), 0) * 100, 1) AS pct
            {lateral}
            GROUP BY COALESCE(NULLIF(TRIM(elem->>'tipo'), ''), 'Sin tipo')
            HAVING SUM({_EQ_TOTAL_SQL}) > 0
            ORDER BY pct ASC NULLS LAST
        """, base_params)
        consolidado = [
            {
                'tipo':   r['tipo'],
                'total':  int(r['sum_total'] or 0),
                'func':   int(r['sum_func']  or 0),
                'pct':    float(r['pct']) if r['pct'] is not None else None,
                'estado': _eq_estado(r['pct']),
            }
            for r in cur.fetchall()
        ]

        # ── Individual inspection records ──────────────────────────────────
        where_rec = _eq_where(base_conds)
        cur.execute(f"""
            SELECT
                c.id,
                COALESCE(NULLIF(TRIM(c.cliente_instalacion),''),'—') AS cliente,
                COALESCE(NULLIF(TRIM(c.sitio),''),'—')               AS sitio,
                c.fecha,
                COALESCE(NULLIF(TRIM(c.tecnico_mantenimiento),''),'—') AS tecnico,
                ARRAY_AGG(DISTINCT COALESCE(NULLIF(TRIM(elem->>'tipo'), ''), 'Sin tipo')) AS tipos,
                ROUND(SUM({_EQ_FUNC_SQL})::numeric
                    / NULLIF(SUM({_EQ_TOTAL_SQL}), 0) * 100, 1) AS pct_general
            {lateral}
            GROUP BY c.id, c.cliente_instalacion, c.sitio, c.fecha, c.tecnico_mantenimiento
            HAVING SUM({_EQ_TOTAL_SQL}) > 0
            ORDER BY c.fecha DESC NULLS LAST, c.id DESC
            LIMIT 100
        """, base_params)
        registros = [
            {
                'id':          r['id'],
                'cliente':     r['cliente'],
                'sitio':       r['sitio'],
                'fecha':       r['fecha'].strftime('%d/%m/%Y') if r['fecha'] else '—',
                'tecnico':     r['tecnico'],
                'tipos':       list(r['tipos'] or []),
                'pct_general': float(r['pct_general']) if r['pct_general'] is not None else None,
                'estado':      _eq_estado(r['pct_general']),
            }
            for r in cur.fetchall()
        ]

        return jsonify({'consolidado': consolidado, 'registros': registros})
    except Exception as e:
        app_logger.error(f"api_equipos_detalles error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/debug/thisweek')
@jwt_required()
def debug_thisweek():
    """Debug endpoint to see what's happening with this week's data"""
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Database connection failed"}), 500

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Get the current date and week boundaries
        boundary_query = """
            SELECT 
                CURRENT_DATE as current_date,
                DATE_TRUNC('week', CURRENT_DATE) as week_start,
                DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '1 week' - INTERVAL '1 day' as week_end
        """
        
        cur.execute(boundary_query)
        boundaries = cur.fetchone()
        
        # Get all incidents this week with full details
        incidents_query = f"""
            SELECT 
                ri.id_reporte_incidente,
                {INCIDENT_DATE_EXPR} as fecha_incidente,
                {INCIDENT_TIME_EXPR} as hora_incidente,
                NULL::integer as id_tipo_incidencia,
                {INCIDENT_TYPE_EXPR} as tipo_incidencia,
                p.nombre as propiedad_nombre,
                ri.descripcion_incidente
            FROM reportes_incidentes ri
            LEFT JOIN propiedades p ON ri.id_propiedad = p.id_propiedad
            WHERE {INCIDENT_DATE_EXPR} >= DATE_TRUNC('week', CURRENT_DATE)
              AND {INCIDENT_DATE_EXPR} < DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '1 week'
            ORDER BY {INCIDENT_ORDER_EXPR};
        """
        
        cur.execute(incidents_query)
        incidents = cur.fetchall()
        
        result = {
            "boundaries": {
                "current_date": boundaries['current_date'].isoformat(),
                "week_start": boundaries['week_start'].isoformat(),
                "week_end": boundaries['week_end'].isoformat()
            },
            "incidents_count": len(incidents),
            "incidents": []
        }
        
        for incident in incidents:
            result["incidents"].append({
                "id": incident['id_reporte_incidente'],
                "date": incident['fecha_incidente'].isoformat() if incident['fecha_incidente'] else None,
                "time": str(incident['hora_incidente']) if incident['hora_incidente'] else None,
                "type_id": incident['id_tipo_incidencia'],
                "type_name": incident['tipo_incidencia'],
                "property": incident['propiedad_nombre'],
                "description": incident['descripcion_incidente'][:100] if incident['descripcion_incidente'] else None
            })
        
        return jsonify(result)
        
    except Exception as e:
        app_logger.error(f"Error in debug endpoint: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@dashboard_bp.route('/api/stats')
@jwt_required()
def api_stats():
    """API endpoint to get consistent statistics"""
    property_id = request.args.get('property_id', type=int)
    
    try:
        total_count = get_total_count(property_id)
        this_month_count = get_this_month_count(property_id)
        this_week_count = get_this_week_count(property_id)
        
        # Calculate average from monthly data
        monthly_data = get_incidents_by_month(property_id)
        avg_per_month = 0
        if monthly_data:
            avg_per_month = round(sum(item['count'] for item in monthly_data) / len(monthly_data))
        
        return jsonify({
            'total': total_count,
            'thisMonth': this_month_count,
            'thisWeek': this_week_count,
            'averagePerMonth': avg_per_month
        })
        
    except Exception as e:
        app_logger.error(f"Error in api_stats: {e}", exc_info=True)
        return jsonify({
            'total': 0,
            'thisMonth': 0,
            'thisWeek': 0,
            'averagePerMonth': 0
        }), 500

@dashboard_bp.route('/api/report/<int:report_id>')
@jwt_required()
def api_report_details(report_id):
    """API endpoint to get detailed information for a specific report"""
    try:
        report = get_report_details(report_id)
        
        if not report:
            return jsonify({'error': 'Report not found'}), 404
        
        return jsonify({
            'report': report,
            'success': True
        })
        
    except Exception as e:
        app_logger.error(f"Error fetching report details for ID {report_id}: {e}", exc_info=True)
        return jsonify({
            'error': 'Error al obtener los detalles del reporte',
            'details': str(e)
        }), 500

@dashboard_bp.route('/api/incidents/period/<int:period_index>')
@jwt_required()
def api_incidents_for_period(period_index):
    """API endpoint for specific 7-day period incident types"""
    property_id = request.args.get('property_id', type=int)
    
    # Get the weekly data to find the specific period
    weekly_data = get_incidents_by_week_with_types(property_id)
    
    if period_index >= len(weekly_data) or period_index < 0:
        return jsonify({'error': 'Invalid period index'}), 400
    
    selected_period = weekly_data[period_index]
    
    # Get incident types for this specific period
    start_date = selected_period['date_range']['start']
    end_date = selected_period['date_range']['end']
    incident_types = get_incident_types_for_period(start_date, end_date, property_id)
    
    return jsonify({
        'incident_types': incident_types,
        'period_info': selected_period,
        'period_index': period_index
    })

@dashboard_bp.route('/dashboard')
@jwt_required()
def dashboard_redirect():
    """Redirect /dashboard to root for compatibility"""
    return redirect('/')

@dashboard_bp.route('/api/properties')
@jwt_required()
def api_properties():
    """API endpoint to get all available properties"""
    properties = get_properties()
    return jsonify(properties)

@dashboard_bp.route('/api/reports/<stat_type>')
@jwt_required()
def api_reports_for_stat(stat_type):
    """API endpoint to get detailed reports for a specific statistic"""
    property_id = request.args.get('property_id', type=int)
    limit = request.args.get('limit', 100, type=int)
    
    # Validate stat_type
    valid_stat_types = ['total', 'thisMonth', 'thisWeek', 'incidentTypes', 'incidentTypesMonthly', 'incidentTypesYearly']
    if stat_type not in valid_stat_types:
        return jsonify({'error': 'Invalid stat type'}), 400
    
    reports = get_reports_for_stat(stat_type, property_id, limit)
    return jsonify({
        'reports': reports,
        'count': len(reports),
        'stat_type': stat_type,
        'property_id': property_id
    })

@dashboard_bp.route('/api/reports/incident-type/<incident_type>')
@jwt_required()
def api_reports_for_incident_type(incident_type):
    """API endpoint to get detailed reports for a specific incident type"""
    property_id = request.args.get('property_id', type=int)
    stat_type = request.args.get('stat_type', 'weekly')  # weekly, monthly, yearly
    limit = request.args.get('limit', 100, type=int)
    
    # Validate incident_type
    valid_incident_types = ['Hurto', 'Olvido', 'Recuperacion', 'Robo']
    if incident_type not in valid_incident_types:
        return jsonify({'error': 'Invalid incident type'}), 400
    
    # Validate stat_type
    valid_stat_types = ['weekly', 'monthly', 'yearly']
    if stat_type not in valid_stat_types:
        return jsonify({'error': 'Invalid stat type'}), 400
    
    reports = get_reports_for_incident_type(incident_type, stat_type, property_id, limit)
    return jsonify({
        'reports': reports,
        'count': len(reports),
        'incident_type': incident_type,
        'stat_type': stat_type,
        'property_id': property_id
    })

@dashboard_bp.route('/api/incidents/weekly')
@jwt_required()
def api_incidents_weekly():
    """API endpoint for weekly incident data"""
    property_id = request.args.get('property_id', type=int)
    data = get_incidents_by_week(property_id)
    return jsonify(data)

@dashboard_bp.route('/api/incidents/monthly')
@jwt_required()
def api_incidents_monthly():
    """API endpoint for monthly incident data"""
    property_id = request.args.get('property_id', type=int)
    data = get_incidents_by_month(property_id)
    return jsonify(data)

@dashboard_bp.route('/api/incidents/yearly')
@jwt_required()
def api_incidents_yearly():
    """API endpoint for yearly incident data"""
    property_id = request.args.get('property_id', type=int)
    data = get_incidents_by_year(property_id)
    return jsonify(data)

@dashboard_bp.route('/api/incidents/types')
@jwt_required()
def api_incident_types():
    """API endpoint for incident types data (current calendar week)"""
    property_id = request.args.get('property_id', type=int)
    data = get_incident_types_stats(property_id)
    return jsonify(data)

@dashboard_bp.route('/api/incidents/types/monthly')
@jwt_required()
def api_incident_types_monthly():
    """API endpoint for monthly incident types data (last 30 days)"""
    property_id = request.args.get('property_id', type=int)
    data = get_incident_types_monthly(property_id)
    return jsonify(data)

@dashboard_bp.route('/api/incidents/types/yearly')
@jwt_required()
def api_incident_types_yearly():
    """API endpoint for yearly incident types data (last 365 days)"""
    property_id = request.args.get('property_id', type=int)
    data = get_incident_types_yearly(property_id)
    return jsonify(data)

@dashboard_bp.route('/api/incidents/weekly-with-kpi')
@jwt_required()
def api_incidents_weekly_with_kpi():
    """API endpoint for weekly incident data with KPI indicators"""
    property_id = request.args.get('property_id', type=int)
    data = get_incidents_by_week_with_types(property_id)
    return jsonify(data)

@dashboard_bp.route('/api/incidents/monthly-with-types')
@jwt_required()
def api_incidents_monthly_with_types():
    """API endpoint for monthly incident data with type breakdown"""
    property_id = request.args.get('property_id', type=int)
    data = get_incidents_by_month_with_types(property_id)
    return jsonify(data)

@dashboard_bp.route('/api/incidents/yearly-with-types')
@jwt_required()
def api_incidents_yearly_with_types():
    """API endpoint for yearly incident data with type breakdown"""
    property_id = request.args.get('property_id', type=int)
    data = get_incidents_by_year_with_types(property_id)
    return jsonify(data)

@dashboard_bp.route('/api/reports/period-range')
@jwt_required()
def api_reports_for_period_range():
    """API endpoint to get all reports for a specific date range"""
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    property_id = request.args.get('property_id', type=int)
    limit = request.args.get('limit', 100, type=int)
    
    if not start_date or not end_date:
        return jsonify({'error': 'start_date and end_date are required'}), 400
    
    # Use the existing function to get reports for date range
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"reports": []}), 500

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        where_conditions = [
            f"{INCIDENT_DATE_EXPR} >= %s",
            f"{INCIDENT_DATE_EXPR} <= %s"
        ]
        params = [start_date, end_date]
        
        if property_id:
            where_conditions.append("ri.id_propiedad = %s")
            params.append(property_id)
        
        query = f"""
            SELECT
                {_build_incident_select()}
            FROM reportes_incidentes ri
            LEFT JOIN propiedades p ON ri.id_propiedad = p.id_propiedad
            WHERE {' AND '.join(where_conditions)}
            ORDER BY {INCIDENT_ORDER_EXPR}
            LIMIT %s
        """
        
        params.append(limit)
        cur.execute(query, params)
        rows = cur.fetchall()
        
        reports = []
        for row in rows:
            reports.append(_serialize_incident_report_row(row))
        
        return jsonify({
            'reports': reports,
            'count': len(reports),
            'start_date': start_date,
            'end_date': end_date,
            'property_id': property_id
        })
        
    except Exception as e:
        app_logger.error(f"Error in api_reports_for_period_range: {e}", exc_info=True)
        return jsonify({"reports": []}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@dashboard_bp.route('/bases_de_datos/')
@jwt_required()
@admin_required
def dashboard_bases_de_datos():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    app_logger.info(f"Admin user {user_email} accessing bases_de_datos")
    return render_template("bases_de_datos.html",
                           current_user=user_email,
                           user_name=user_name,
                           is_admin=is_admin)


@dashboard_bp.route('/api/bases_de_datos/clientes')
@jwt_required()
def api_bases_de_datos_clientes():
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'clientes': []})
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT cliente
            FROM supervision_puesto
            WHERE cliente IS NOT NULL AND cliente <> ''
            ORDER BY cliente
        """)
        return jsonify({'clientes': [r[0] for r in cur.fetchall()]})
    except Exception as e:
        app_logger.error(f"api_bases_de_datos_clientes error: {e}", exc_info=True)
        return jsonify({'clientes': []})
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/bases_de_datos/armas')
@jwt_required()
def api_bases_de_datos_armas():
    cliente = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'rows': [], 'total': 0}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        conds, params = [], []
        conds.append("LOWER(TRIM(COALESCE(porta_arma,''))) = 'si'")
        if cliente:
            conds.append("cliente = %s")
            params.append(cliente)
        if year and month:
            conds.append("fecha_hora::TEXT LIKE %s")
            params.append(f"{year}-{month:02d}%")
        elif year:
            conds.append("fecha_hora::TEXT LIKE %s")
            params.append(f"{year}%")
        where = "WHERE " + " AND ".join(conds)

        # Check which optional columns exist to avoid errors on older DB schemas
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'supervision_puesto'
        """)
        existing_cols = {row['column_name'] for row in cur.fetchall()}

        tipo_arma_sel    = "tipo_arma" if 'tipo_arma' in existing_cols else "NULL::TEXT AS tipo_arma"
        fvp_sel          = "fecha_vencimiento_permiso_porte" if 'fecha_vencimiento_permiso_porte' in existing_cols else "NULL::DATE AS fecha_vencimiento_permiso_porte"
        fumtto_sel       = "fecha_ultimo_mtto_arma" if 'fecha_ultimo_mtto_arma' in existing_cols else "NULL::DATE AS fecha_ultimo_mtto_arma"

        cur.execute(f"""
            SELECT
                numero_empleado,
                nombre_guardia,
                documento_guardia,
                cliente,
                supervisor,
                COALESCE(NULLIF(TRIM(serie_arma), ''), '—') AS serie_arma,
                {tipo_arma_sel},
                {fvp_sel},
                {fumtto_sel},
                cantidad_municion,
                fecha_hora
            FROM supervision_puesto
            {where}
            ORDER BY fecha_hora DESC
            LIMIT 500
        """, params)
        rows = []
        for r in cur.fetchall():
            def _fmt_date(val):
                if val is None:
                    return '—'
                if hasattr(val, 'strftime'):
                    return val.strftime('%d/%m/%Y')
                return str(val)[:10] if val else '—'

            rows.append({
                'numero_empleado':               r['numero_empleado']  or '—',
                'nombre_guardia':                r['nombre_guardia']    or '—',
                'documento_guardia':             r['documento_guardia'] or '—',
                'cliente':                       r['cliente']           or '—',
                'supervisor':                    r['supervisor']        or '—',
                'serie_arma':                    r['serie_arma'],
                'tipo_arma':                     r['tipo_arma']         or '—',
                'fecha_vencimiento_matricula':   _fmt_date(r['fecha_vencimiento_permiso_porte']),
                'fecha_ultimo_mantenimiento':    _fmt_date(r['fecha_ultimo_mtto_arma']),
                'cantidad_municion':             str(r['cantidad_municion']) if r['cantidad_municion'] is not None else '—',
                'fecha_hora':                    r['fecha_hora'].strftime('%Y-%m-%d %H:%M') if r['fecha_hora'] else '—',
            })
        return jsonify({'rows': rows, 'total': len(rows)})
    except Exception as e:
        app_logger.error(f"api_bases_de_datos_armas error: {e}", exc_info=True)
        return jsonify({'rows': [], 'total': 0}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@dashboard_bp.route('/api/bases_de_datos/radios')
@jwt_required()
def api_bases_de_datos_radios():
    cliente = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'rows': [], 'total': 0}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        conds, params = [], []
        conds.append("TRIM(COALESCE(equipamiento_completo, '')) <> ''")
        if cliente:
            conds.append("cliente = %s")
            params.append(cliente)
        _gestion_add_multi_date_filter(conds, params, "fecha_hora::TEXT", year, month, None)
        where = "WHERE " + " AND ".join(conds)

        # Check which optional columns exist
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'supervision_puesto'
        """)
        existing_cols = {row['column_name'] for row in cur.fetchall()}

        serial_sel   = "radio_asignado_serial"    if 'radio_asignado_serial'    in existing_cols else "NULL::TEXT AS radio_asignado_serial"
        marca_sel    = "marca_radio"               if 'marca_radio'               in existing_cols else "NULL::TEXT AS marca_radio"
        tipo_sel     = "tipo_radio"                if 'tipo_radio'                in existing_cols else "NULL::TEXT AS tipo_radio"
        fumtto_sel   = "fecha_ultimo_mtto_radio"   if 'fecha_ultimo_mtto_radio'   in existing_cols else "NULL::DATE AS fecha_ultimo_mtto_radio"

        cur.execute(f"""
            SELECT
                numero_empleado,
                nombre_guardia,
                documento_guardia,
                cliente,
                {serial_sel},
                {marca_sel},
                {tipo_sel},
                {fumtto_sel},
                equipamiento_completo,
                supervisor,
                fecha_hora
            FROM supervision_puesto
            {where}
            ORDER BY fecha_hora DESC
            LIMIT 500
        """, params)
        rows = []
        for r in cur.fetchall():
            def _fmt_date(val):
                if val is None:
                    return '—'
                if hasattr(val, 'strftime'):
                    return val.strftime('%d/%m/%Y')
                return str(val)[:10] if val else '—'

            estado = str(r['equipamiento_completo']).strip() if r['equipamiento_completo'] is not None else '—'
            rows.append({
                'numero_empleado':        r['numero_empleado']  or '—',
                'nombre_guardia':         r['nombre_guardia']    or '—',
                'documento_guardia':      r['documento_guardia'] or '—',
                'cliente':                r['cliente']           or '—',
                'serial_radio':           r['radio_asignado_serial'] or '—',
                'marca_radio':            r['marca_radio']       or '—',
                'tipo_radio':             r['tipo_radio']        or '—',
                'fecha_ultimo_mantenimiento': _fmt_date(r['fecha_ultimo_mtto_radio']),
                'equipamiento':           estado,
                'supervisor':             r['supervisor']        or '—',
                'fecha_hora':             r['fecha_hora'].strftime('%Y-%m-%d %H:%M') if r['fecha_hora'] else '—',
            })
        return jsonify({'rows': rows, 'total': len(rows)})
    except Exception as e:
        app_logger.error(f"api_bases_de_datos_radios error: {e}", exc_info=True)
        return jsonify({'rows': [], 'total': 0}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


_SCORE_COLS = ['asistencia_puntualidad', 'presentacion_uniforme', 'estado_limpieza_puesto',
               'equipamiento_completo', 'estado_bitacora']

def _bd_score_expr():
    parts = [
        f"COALESCE(CASE WHEN TRIM({c}::TEXT) ~ '^[0-9]+(\\.[0-9]+)?$'"
        f" THEN TRIM({c}::TEXT)::NUMERIC ELSE NULL END, 0)"
        for c in _SCORE_COLS
    ]
    return " + ".join(parts)


@dashboard_bp.route('/api/bases_de_datos/personal')
@jwt_required()
def api_bases_de_datos_personal():
    cliente = request.args.get('cliente') or None
    year = request.args.get('year') or None
    month = request.args.get('month') or None

    conn = cur = cur2 = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'rows': [], 'total': 0}), 500
        cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur2 = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        sup_conds, params = [], []
        if cliente:
            sup_conds.append("cliente = %s")
            params.append(cliente)
        if year and month:
            sup_conds.append("fecha_hora::TEXT LIKE %s")
            params.append(f"{year}-{month:02d}%")
        elif year:
            sup_conds.append("fecha_hora::TEXT LIKE %s")
            params.append(f"{year}%")
        base_where = ("WHERE " + " AND ".join(sup_conds)) if sup_conds else ""

        score_sql = _bd_score_expr()

        # One row per distinct employee, most recent supervision, with avg score
        cur.execute(f"""
            SELECT
                emp_key,
                nombre_guardia,
                documento_guardia,
                numero_empleado,
                cliente,
                ultima_supervision,
                ROUND(avg_score::NUMERIC, 1) AS avg_score
            FROM (
                SELECT
                    COALESCE(NULLIF(TRIM(documento_guardia),''), NULLIF(TRIM(nombre_guardia),''), 'sin_id') AS emp_key,
                    nombre_guardia,
                    COALESCE(NULLIF(TRIM(documento_guardia),''), '—') AS documento_guardia,
                    COALESCE(NULLIF(TRIM(numero_empleado),''),  '—') AS numero_empleado,
                    cliente,
                    MAX(creado_en) AS ultima_supervision,
                    AVG({score_sql})                                  AS avg_score,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(NULLIF(TRIM(documento_guardia),''), NULLIF(TRIM(nombre_guardia),''), 'sin_id')
                        ORDER BY MAX(creado_en) DESC
                    ) AS rn
                FROM supervision_puesto
                {base_where}
                GROUP BY
                    COALESCE(NULLIF(TRIM(documento_guardia),''), NULLIF(TRIM(nombre_guardia),''), 'sin_id'),
                    nombre_guardia, documento_guardia, numero_empleado, cliente
            ) sub
            WHERE rn = 1
            ORDER BY nombre_guardia
        """, params)
        employees = cur.fetchall()

        rows = []
        for emp in employees:
            doc    = emp['documento_guardia'] if emp['documento_guardia'] != '—' else None
            nombre = (emp['nombre_guardia'] or '').strip()
            trainings = []
            novedades_count = 0

            # Novedades disciplinarias count
            if doc or nombre:
                disc_conds, disc_params = [], []
                if doc:
                    disc_conds.append("TRIM(COALESCE(empleado_numero::TEXT,'')) = %s")
                    disc_params.append(doc)
                if nombre:
                    disc_conds.append("TRIM(empleado_nombre) ILIKE %s")
                    disc_params.append(f"%{nombre}%")
                cur2.execute(
                    f"SELECT COUNT(*) AS cnt FROM informe_novedades_disciplinario WHERE {' OR '.join(disc_conds)}",
                    disc_params
                )
                novedades_count = int((cur2.fetchone() or {}).get('cnt', 0) or 0)

            if doc or nombre:
                match_parts, train_params = [], []
                if doc:
                    match_parts.append(
                        "EXISTS (SELECT 1 FROM json_array_elements(lista_asistencia::json) e "
                        "WHERE e->>'documento' = %s)"
                    )
                    train_params.append(doc)
                if nombre:
                    match_parts.append(
                        "EXISTS (SELECT 1 FROM json_array_elements(lista_asistencia::json) e "
                        "WHERE e->>'nombre' ILIKE %s)"
                    )
                    train_params.append(f"%{nombre}%")

                match_sql = " OR ".join(match_parts)
                cur2.execute(f"""
                    SELECT nombre_capacitacion, objetivo_capacitacion,
                           nivel_comprension, fecha_hora, nombre_responsable
                    FROM registro_de_capacitaciones
                    WHERE lista_asistencia IS NOT NULL
                      AND lista_asistencia NOT IN ('null', '[]', '')
                      AND ({match_sql})
                    ORDER BY fecha_hora DESC
                    LIMIT 20
                """, train_params)
                for t in cur2.fetchall():
                    trainings.append({
                        'capacitacion': t['nombre_capacitacion']  or '—',
                        'objetivo':     t['objetivo_capacitacion'] or '—',
                        'nivel':        t['nivel_comprension']     or '—',
                        'fecha':        t['fecha_hora'].strftime('%Y-%m-%d') if t['fecha_hora'] else '—',
                        'responsable':  t['nombre_responsable']    or '—',
                    })

            avg_score = float(emp['avg_score']) if emp['avg_score'] is not None else None
            rows.append({
                'numero_empleado':      emp['numero_empleado'],
                'nombre_guardia':       emp['nombre_guardia']     or '—',
                'documento_guardia':    emp['documento_guardia'],
                'cliente':              emp['cliente']             or '—',
                'ultima_supervision':   emp['ultima_supervision'].strftime('%Y-%m-%d') if emp['ultima_supervision'] else '—',
                'avg_score':            avg_score,
                'nivel_desempeno':      _sup_score_label(avg_score),
                'novedades_disciplina': novedades_count,
                'total_capacitaciones': len(trainings),
                'capacitaciones':       trainings,
            })

        return jsonify({'rows': rows, 'total': len(rows)})
    except Exception as e:
        app_logger.error(f"api_bases_de_datos_personal error: {e}", exc_info=True)
        return jsonify({'rows': [], 'total': 0}), 500
    finally:
        if cur:  cur.close()
        if cur2: cur2.close()
        if conn: conn.close()


@dashboard_bp.route('/logout')
def logout():
    try:
        app_logger.info("User logout requested")
        response = redirect('/')
        unset_jwt_cookies(response)
        app_logger.info("JWT cookies cleared, redirecting to login service")
        return response
    except Exception as e:
        app_logger.error(f"Error during logout: {e}", exc_info=True)
        return redirect('/')

# Dashboard routes initialized
