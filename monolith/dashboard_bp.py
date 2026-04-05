import os
import sys
import logging
import re
from datetime import timedelta, datetime, timezone
import calendar

from flask import Blueprint, current_app, Flask, render_template, request, jsonify, Response, flash, session, redirect, url_for
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
from functools import wraps

# --- Configure Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
app_logger = logging.getLogger(__name__)

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
            SELECT DISTINCT p.id_propiedad, p.nombre
            FROM propiedades p
            INNER JOIN reportes_incidentes ri ON p.id_propiedad = ri.id_propiedad
            WHERE p.activa = TRUE
            ORDER BY p.nombre;
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
        
        query = """
            SELECT 
                ri.id_reporte_incidente,
                ri.fecha_incidente,
                ri.hora_incidente,
                ri.descripcion_incidente,
                ri.nombre_persona,
                ri.user_email,
                ri.telefono_persona,
                ri.numero_identidad_persona,
                ri.numero_local,
                ri.direccion,
                ri.valor_aproximado,
                ri.creado_en,
                ri.pertenencias_sustraidas,
                ri.descripcion_zona_comun,
                ri.imagenes_pdfs,
                p.nombre as propiedad_nombre,
                p.direccion as propiedad_direccion,
                p.descripcion as propiedad_descripcion,
                ti.nombre as tipo_incidencia,
                tc.nombre as tipo_cliente,
                li.nombre as lugar_incidente,
                s.nombre as supervisor_name
            FROM reportes_incidentes ri
            LEFT JOIN propiedades p ON ri.id_propiedad = p.id_propiedad
            LEFT JOIN tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            LEFT JOIN tipo_cliente tc ON ri.id_tipo_cliente = tc.id_tipo_cliente
            LEFT JOIN lugar_incidente li ON ri.id_lugar_incidente = li.id_lugar_incidente
            LEFT JOIN supervisor s ON ri.id_supervisor = s.id_supervisor
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
        where_clause = """WHERE ri.fecha_incidente >= DATE_TRUNC('week', CURRENT_DATE)
              AND ri.fecha_incidente < DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '1 week'"""
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
        where_clause = """WHERE DATE_TRUNC('month', ri.fecha_incidente) = DATE_TRUNC('month', CURRENT_DATE)"""
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
        base_query = """
            SELECT 
                ri.id_reporte_incidente,
                ri.fecha_incidente,
                ri.hora_incidente,
                ri.descripcion_incidente,
                ri.nombre_persona,
                ri.user_email,
                ri.telefono_persona,
                ri.numero_identidad_persona,
                ri.numero_local,
                ri.direccion,
                p.nombre as propiedad_nombre,
                ti.nombre as tipo_incidencia,
                tc.nombre as tipo_cliente,
                li.nombre as lugar_incidente,
                s.nombre as supervisor_name,
                ri.valor_aproximado
            FROM reportes_incidentes ri
            LEFT JOIN propiedades p ON ri.id_propiedad = p.id_propiedad
            LEFT JOIN tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            LEFT JOIN tipo_cliente tc ON ri.id_tipo_cliente = tc.id_tipo_cliente
            LEFT JOIN lugar_incidente li ON ri.id_lugar_incidente = li.id_lugar_incidente
            LEFT JOIN supervisor s ON ri.id_supervisor = s.id_supervisor
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
                DATE_TRUNC('month', ri.fecha_incidente) = DATE_TRUNC('month', CURRENT_DATE)
            """)
        elif stat_type == 'thisWeek':
            # FIXED: Use calendar week consistently
            where_conditions.append("""
                ri.fecha_incidente >= DATE_TRUNC('week', CURRENT_DATE)
                AND ri.fecha_incidente < DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '1 week'
            """)
            # DEBUG: Add logging for thisWeek
            app_logger.info("ThisWeek date range: week start = DATE_TRUNC('week', CURRENT_DATE)")
        elif stat_type == 'incidentTypes':
            # FIXED: Use same calendar week calculation for consistency
            where_conditions.append("""
                ri.fecha_incidente >= DATE_TRUNC('week', CURRENT_DATE)
                AND ri.fecha_incidente < DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '1 week'
            """)
            # DEBUG: Add logging for incidentTypes
            app_logger.info("IncidentTypes date range: week start = DATE_TRUNC('week', CURRENT_DATE)")
        elif stat_type == 'incidentTypesMonthly':
            # Last 30 days for monthly incident types
            where_conditions.append("""
                ri.fecha_incidente >= CURRENT_DATE - INTERVAL '30 days'
                AND ri.fecha_incidente < CURRENT_DATE + INTERVAL '1 day'
            """)
        elif stat_type == 'incidentTypesYearly':
            # Last 365 days for yearly incident types
            where_conditions.append("""
                ri.fecha_incidente >= CURRENT_DATE - INTERVAL '365 days'
                AND ri.fecha_incidente < CURRENT_DATE + INTERVAL '1 day'
            """)
        
        # Build final query
        if where_conditions:
            query = base_query + " WHERE " + " AND ".join(where_conditions)
        else:
            query = base_query
            
        query += " ORDER BY ri.fecha_incidente DESC, ri.hora_incidente DESC"
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
            reports.append({
                'id_reporte': row['id_reporte_incidente'],
                'fecha_incidente': row['fecha_incidente'].strftime('%Y-%m-%d') if row['fecha_incidente'] else '',
                'hora_incidente': str(row['hora_incidente']) if row['hora_incidente'] else '',
                'descripcion_incidente': row['descripcion_incidente'] or '',
                'nombre_reportante': row['user_email'] or '',  # Using user_email as reportante
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
            })
        
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
        base_query = """
            SELECT 
                ri.id_reporte_incidente,
                ri.fecha_incidente,
                ri.hora_incidente,
                ri.descripcion_incidente,
                ri.nombre_persona,
                ri.user_email,
                ri.telefono_persona,
                ri.numero_identidad_persona,
                ri.numero_local,
                ri.direccion,
                p.nombre as propiedad_nombre,
                ti.nombre as tipo_incidencia,
                tc.nombre as tipo_cliente,
                li.nombre as lugar_incidente,
                s.nombre as supervisor_name,
                ri.valor_aproximado
            FROM reportes_incidentes ri
            LEFT JOIN propiedades p ON ri.id_propiedad = p.id_propiedad
            LEFT JOIN tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            LEFT JOIN tipo_cliente tc ON ri.id_tipo_cliente = tc.id_tipo_cliente
            LEFT JOIN lugar_incidente li ON ri.id_lugar_incidente = li.id_lugar_incidente
            LEFT JOIN supervisor s ON ri.id_supervisor = s.id_supervisor
        """
        
        where_conditions = ["ti.nombre = %s"]
        params = [incident_type]
        
        # Add property filter if specified
        if property_id:
            where_conditions.append("ri.id_propiedad = %s")
            params.append(property_id)
        
        # FIXED: Add date conditions based on stat type with consistent calculations
        if stat_type == 'weekly':
            # Use calendar week for consistency with "Esta Semana"
            where_conditions.append("""
                ri.fecha_incidente >= DATE_TRUNC('week', CURRENT_DATE)
                AND ri.fecha_incidente < DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '1 week'
            """)
        elif stat_type == 'monthly':
            # Last 30 days
            where_conditions.append("""
                ri.fecha_incidente >= CURRENT_DATE - INTERVAL '30 days'
                AND ri.fecha_incidente < CURRENT_DATE + INTERVAL '1 day'
            """)
        elif stat_type == 'yearly':
            # Last 365 days
            where_conditions.append("""
                ri.fecha_incidente >= CURRENT_DATE - INTERVAL '365 days'
                AND ri.fecha_incidente < CURRENT_DATE + INTERVAL '1 day'
            """)
        
        # Build final query
        query = base_query + " WHERE " + " AND ".join(where_conditions)
        query += " ORDER BY ri.fecha_incidente DESC, ri.hora_incidente DESC"
        query += f" LIMIT {limit}"
        
        app_logger.info(f"Executing query for incident_type {incident_type}, stat_type {stat_type}: {query}")
        app_logger.info(f"Query parameters: {params}")
        
        cur.execute(query, params)
        rows = cur.fetchall()
        
        app_logger.info(f"Found {len(rows)} reports for incident_type {incident_type}")
        
        reports = []
        for row in rows:
            reports.append({
                'id_reporte': row['id_reporte_incidente'],
                'fecha_incidente': row['fecha_incidente'].strftime('%Y-%m-%d') if row['fecha_incidente'] else '',
                'hora_incidente': str(row['hora_incidente']) if row['hora_incidente'] else '',
                'descripcion_incidente': row['descripcion_incidente'] or '',
                'nombre_reportante': row['user_email'] or '',  # Using user_email as reportante
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
            })
        
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
                    ti.nombre as incident_type,
                    COUNT(ri.id_reporte_incidente) as incident_count
                FROM seven_day_periods sdp
                LEFT JOIN reportes_incidentes ri ON (
                    ri.fecha_incidente >= sdp.period_start 
                    AND ri.fecha_incidente <= sdp.period_end
                    {property_filter}
                )
                LEFT JOIN tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
                WHERE ti.nombre IN ('Hurto', 'Olvido', 'Recuperacion', 'Robo') OR ti.nombre IS NULL
                GROUP BY sdp.period_start, sdp.period_end, ti.nombre
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
                ri.fecha_incidente >= sdp.period_start 
                AND ri.fecha_incidente <= sdp.period_end
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
        where_clause = "WHERE ri.fecha_incidente IS NOT NULL"
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            SELECT 
                DATE_TRUNC('month', ri.fecha_incidente) as month_start,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            {where_clause}
            GROUP BY DATE_TRUNC('month', ri.fecha_incidente)
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
        where_clause = "WHERE ri.fecha_incidente IS NOT NULL"
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            SELECT 
                EXTRACT(YEAR FROM ri.fecha_incidente) as year,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            {where_clause}
            GROUP BY EXTRACT(YEAR FROM ri.fecha_incidente)
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
        where_clause = """WHERE ri.fecha_incidente >= DATE_TRUNC('week', CURRENT_DATE)
              AND ri.fecha_incidente < DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '1 week'"""
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        # DEBUG: First let's see all incidents in this week
        debug_query = f"""
            SELECT 
                ri.id_reporte_incidente,
                ri.fecha_incidente,
                ti.nombre as tipo_incidencia,
                p.nombre as propiedad_nombre
            FROM reportes_incidentes ri
            LEFT JOIN tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            LEFT JOIN propiedades p ON ri.id_propiedad = p.id_propiedad
            {where_clause}
            ORDER BY ri.fecha_incidente DESC;
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
                COALESCE(ti.nombre, 'Sin Tipo') as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            LEFT JOIN tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            {where_clause}
            GROUP BY ti.nombre
            ORDER BY ti.nombre;
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
        where_clause = """WHERE ri.fecha_incidente >= CURRENT_DATE - INTERVAL '30 days'
              AND ri.fecha_incidente < CURRENT_DATE + INTERVAL '1 day'
              AND ti.nombre IS NOT NULL"""
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            SELECT 
                ti.nombre as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            LEFT JOIN tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            {where_clause}
            GROUP BY ti.nombre
            ORDER BY ti.nombre;
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
        where_clause = """WHERE ri.fecha_incidente >= CURRENT_DATE - INTERVAL '365 days'
              AND ri.fecha_incidente < CURRENT_DATE + INTERVAL '1 day'
              AND ti.nombre IS NOT NULL"""
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            SELECT 
                ti.nombre as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            LEFT JOIN tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            {where_clause}
            GROUP BY ti.nombre
            ORDER BY ti.nombre;
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
        where_clause = """WHERE ri.fecha_incidente IS NOT NULL
              AND ti.nombre IN ('Hurto', 'Olvido', 'Recuperacion', 'Robo')"""
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            SELECT 
                DATE_TRUNC('month', ri.fecha_incidente) as month_start,
                ti.nombre as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            LEFT JOIN tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            {where_clause}
            GROUP BY DATE_TRUNC('month', ri.fecha_incidente), ti.nombre
            ORDER BY month_start DESC, ti.nombre
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
        where_clause = """WHERE ri.fecha_incidente IS NOT NULL
              AND ti.nombre IN ('Hurto', 'Olvido', 'Recuperacion', 'Robo')"""
        params = []
        
        if property_id:
            where_clause += " AND ri.id_propiedad = %s"
            params.append(property_id)
        
        query = f"""
            SELECT 
                EXTRACT(YEAR FROM ri.fecha_incidente) as year,
                ti.nombre as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            LEFT JOIN tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            {where_clause}
            GROUP BY EXTRACT(YEAR FROM ri.fecha_incidente), ti.nombre
            ORDER BY year DESC, ti.nombre;
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
        where_clause = """WHERE ri.fecha_incidente >= %s
              AND ri.fecha_incidente <= %s"""
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
                    ti.nombre as incident_type,
                    COUNT(*) as incident_count
                FROM reportes_incidentes ri
                LEFT JOIN tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
                {where_clause}
                    AND ti.nombre IN ('Hurto', 'Olvido', 'Recuperacion', 'Robo')
                GROUP BY ti.nombre
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
@admin_required
def dashboard_incidentes():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    app_logger.info(f"Admin user {user_email} accessing incidentes dashboard")
    return render_template("dashboard.html",
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
    return _module_view('supervision')

@dashboard_bp.route('/cumplimiento/')
@jwt_required()
@admin_required
def dashboard_cumplimiento():
    return _module_view('cumplimiento')

@dashboard_bp.route('/capacitacion/')
@jwt_required()
@admin_required
def dashboard_capacitacion():
    return _module_view('capacitacion')

@dashboard_bp.route('/disciplina/')
@jwt_required()
@admin_required
def dashboard_disciplina():
    return _module_view('disciplina')

@dashboard_bp.route('/visitas/')
@jwt_required()
@admin_required
def dashboard_visitas():
    return _module_view('visitas')

@dashboard_bp.route('/vehiculos/')
@jwt_required()
@admin_required
def dashboard_vehiculos():
    return _module_view('vehiculos')

@dashboard_bp.route('/equipos/')
@jwt_required()
@admin_required
def dashboard_equipos():
    return _module_view('equipos')

@dashboard_bp.route('/gestion/')
@jwt_required()
@admin_required
def dashboard_gestion():
    return _module_view('gestion')


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
    if score is None: return '#6b7280'
    if score >= 34: return '#22c55e'
    if score >= 26: return '#eab308'
    if score >= 18: return '#f97316'
    return '#ef4444'

def _sat_label_global(score):
    if score is None: return 'Sin datos'
    if score >= 34: return 'Totalmente satisfecho'
    if score >= 26: return 'Con oportunidades de mejora'
    if score >= 18: return 'Satisfacción baja'
    return 'Insatisfecho'

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


def _sat_where(cliente, year, month, day):
    conds, params = [], []
    if cliente:
        conds.append("cliente_instalacion = %s")
        params.append(cliente)
    prefix = _sat_date_prefix(year, month, day)
    if prefix:
        conds.append("fecha_hora::TEXT LIKE %s")
        params.append(prefix + "%")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


def _sat_prev_where(cliente, year, month, day):
    """Build WHERE clause for the previous comparison period."""
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
    cliente = request.args.get('cliente') or None
    year    = int(request.args.get('year'))  if request.args.get('year')  else None
    month   = int(request.args.get('month')) if request.args.get('month') else None
    day     = int(request.args.get('day'))   if request.args.get('day')   else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        where,      params      = _sat_where(cliente, year, month, day)
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
                SUM(CASE WHEN {_safe_int('calificacion_global_nps')} >= 34 THEN 1 ELSE 0 END) AS dist_satisfecho,
                SUM(CASE WHEN {_safe_int('calificacion_global_nps')} >= 26 AND {_safe_int('calificacion_global_nps')} < 34 THEN 1 ELSE 0 END) AS dist_oportunidad,
                SUM(CASE WHEN {_safe_int('calificacion_global_nps')} >= 18 AND {_safe_int('calificacion_global_nps')} < 26 THEN 1 ELSE 0 END) AS dist_baja,
                SUM(CASE WHEN {_safe_int('calificacion_global_nps')} < 18 AND {_safe_int('calificacion_global_nps')} IS NOT NULL THEN 1 ELSE 0 END) AS dist_insatisfecho,
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
                'satisfecho':   int(row['dist_satisfecho'])   if row['dist_satisfecho']   else 0,
                'oportunidad':  int(row['dist_oportunidad'])  if row['dist_oportunidad']  else 0,
                'baja':         int(row['dist_baja'])         if row['dist_baja']         else 0,
                'insatisfecho': int(row['dist_insatisfecho']) if row['dist_insatisfecho'] else 0,
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
    cliente = request.args.get('cliente') or None
    year    = int(request.args.get('year'))  if request.args.get('year')  else None
    month   = int(request.args.get('month')) if request.args.get('month') else None
    day     = int(request.args.get('day'))   if request.args.get('day')   else None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB connection failed'}), 500
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        where, params = _sat_where(cliente, year, month, day)

        cur.execute(f"""
            SELECT 
                id_encuesta as id, 
                fecha_hora, 
                cliente_instalacion, 
                encuestado,
                recomendaria_servicio,
                calificacion_global_nps 
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
            
            detalles.append({
                'id': r['id'] if r['id'] else 0,
                'fecha_hora': fh_str,
                'cliente': r['cliente_instalacion'] if r['cliente_instalacion'] else '—',
                'encuestado': r['encuestado'] if 'encuestado' in r and r['encuestado'] else '—',
                'recomendaria': r['recomendaria_servicio'] if r['recomendaria_servicio'] else '—',
                'calificacion': calc_str
            })
            
        return jsonify({'detalles': detalles})
    except Exception as e:
        app_logger.error(f"api_satisfaccion_detalles error: {e}", exc_info=True)
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
        incidents_query = """
            SELECT 
                ri.id_reporte_incidente,
                ri.fecha_incidente,
                ri.hora_incidente,
                ri.id_tipo_incidencia,
                ti.nombre as tipo_incidencia,
                p.nombre as propiedad_nombre,
                ri.descripcion_incidente
            FROM reportes_incidentes ri
            LEFT JOIN tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            LEFT JOIN propiedades p ON ri.id_propiedad = p.id_propiedad
            WHERE ri.fecha_incidente >= DATE_TRUNC('week', CURRENT_DATE)
              AND ri.fecha_incidente < DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '1 week'
            ORDER BY ri.fecha_incidente DESC, ri.hora_incidente DESC;
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
            "ri.fecha_incidente >= %s",
            "ri.fecha_incidente <= %s"
        ]
        params = [start_date, end_date]
        
        if property_id:
            where_conditions.append("ri.id_propiedad = %s")
            params.append(property_id)
        
        query = f"""
            SELECT 
                ri.id_reporte_incidente,
                ri.fecha_incidente,
                ri.hora_incidente,
                ri.descripcion_incidente,
                ri.nombre_persona,
                ri.user_email,
                ri.telefono_persona,
                ri.numero_identidad_persona,
                ri.numero_local,
                ri.direccion,
                p.nombre as propiedad_nombre,
                ti.nombre as tipo_incidencia,
                tc.nombre as tipo_cliente,
                li.nombre as lugar_incidente,
                s.nombre as supervisor_name,
                ri.valor_aproximado
            FROM reportes_incidentes ri
            LEFT JOIN propiedades p ON ri.id_propiedad = p.id_propiedad
            LEFT JOIN tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            LEFT JOIN tipo_cliente tc ON ri.id_tipo_cliente = tc.id_tipo_cliente
            LEFT JOIN lugar_incidente li ON ri.id_lugar_incidente = li.id_lugar_incidente
            LEFT JOIN supervisor s ON ri.id_supervisor = s.id_supervisor
            WHERE {' AND '.join(where_conditions)}
            ORDER BY ri.fecha_incidente DESC, ri.hora_incidente DESC
            LIMIT %s
        """
        
        params.append(limit)
        cur.execute(query, params)
        rows = cur.fetchall()
        
        reports = []
        for row in rows:
            reports.append({
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
            })
        
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