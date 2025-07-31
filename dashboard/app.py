import os
import sys
import logging
import re
from datetime import timedelta, datetime, timezone

from flask import Flask, render_template, request, jsonify, Response, flash, session, redirect, url_for
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity, get_jwt, unset_jwt_cookies
from flask_cors import CORS
from google.cloud import secretmanager
from google.api_core.exceptions import NotFound

import google.auth.transport.requests
import google.oauth2.id_token
import requests

import psycopg2
from psycopg2 import extras

# --- Configure Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
app_logger = logging.getLogger(__name__)

# --- Initialize Flask App ---
app = Flask(__name__)
is_production = os.environ.get('K_SERVICE') is not None
app_logger.info(f"Starting Dashboard Service in {'production' if is_production else 'development'} mode")

# --- Secret Manager Client ---
secret_manager_client = secretmanager.SecretManagerServiceClient()

# Helper to determine if a string is a full secret path
def is_full_secret_path(s, project_id):
    """
    Checks if a given string is a well-formed full Google Secret Manager path.
    This helps differentiate between a secret name and a full path.
    """
    if not s or not project_id:
        return False
    return s.startswith(f"projects/{project_id}/secrets/") and "/versions/" in s

# Function to get secret
def get_secret(project_id, secret_name_or_path):
    try:
        if is_full_secret_path(secret_name_or_path, project_id):
            name = secret_name_or_path
        else:
            name = f"projects/{project_id}/secrets/{secret_name_or_path}/versions/latest"
        response = secret_manager_client.access_secret_version(request={"name": name})
        app_logger.info(f"Successfully accessed secret: {secret_name_or_path}")
        return response.payload.data.decode("UTF-8")
    except NotFound:
        app_logger.error(f"Secret '{secret_name_or_path}' not found in project '{project_id}'.")
        return None
    except Exception as e:
        app_logger.error(f"Error accessing secret '{secret_name_or_path}': {e}", exc_info=True)
        return None

# Load JWT_SECRET_KEY from Secret Manager
project_id = os.environ.get('GCP_PROJECT_ID')
if not project_id:
    app_logger.error("GCP_PROJECT_ID environment variable not set.")
    project_id = "your-gcp-project-id"  # Placeholder for local testing if not set

jwt_secret = get_secret(project_id, 'jwt-secret-key')

if not jwt_secret:
    raise ValueError(f"Secret 'jwt-secret-key' not found or accessible from Secret Manager for project {project_id}. Ensure GOOGLE_APPLICATION_CREDENTIALS is set and has Secret Manager Accessor role.")

app.config['JWT_SECRET_KEY'] = jwt_secret
app.config['JWT_TOKEN_LOCATION'] = ['cookies']
app.config['JWT_COOKIE_SECURE'] = True if is_production else False
app.config['JWT_COOKIE_CSRF_PROTECT'] = False
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', '.tzolkintech.com')

# Service URLs configuration
app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com')

jwt = JWTManager(app)
CORS(app)

# --- JWT Error Handlers for Automatic Redirect ---
@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_payload):
    """
    Called when an access token has expired.
    Always redirect user to login service for both web and API requests.
    """
    user_email = jwt_payload.get('sub', 'unknown')
    app_logger.info(f"JWT token expired for user {user_email}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.invalid_token_loader
def invalid_token_callback(error_string):
    """
    Called when an invalid token is encountered.
    Always redirect user to login service for both web and API requests.
    """
    app_logger.info(f"Invalid JWT token encountered: {error_string}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.unauthorized_loader
def unauthorized_callback(error_string):
    """
    Called when no JWT token is present in the request.
    Always redirect user to login service for both web and API requests.
    """
    app_logger.info(f"No JWT token found: {error_string}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.revoked_token_loader
def revoked_token_callback(jwt_header, jwt_payload):
    """
    Called when a revoked token is encountered.
    Always redirect user to login service for both web and API requests.
    """
    user_email = jwt_payload.get('sub', 'unknown')
    app_logger.info(f"Revoked JWT token for user {user_email}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.needs_fresh_token_loader
def needs_fresh_token_callback(jwt_header, jwt_payload):
    """
    Called when a fresh token is required but not provided.
    Always redirect user to login service for both web and API requests.
    """
    user_email = jwt_payload.get('sub', 'unknown')
    app_logger.info(f"Fresh token required for user {user_email}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

# DB Config
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    app_logger.critical("DATABASE_URL environment variable is not set. Database connection will fail.")

# --- Database Helper Functions ---
def get_db_connection():
    """Establishes and returns a database connection."""
    conn = None
    if not DATABASE_URL:
        app_logger.error("Attempted to connect to DB, but DATABASE_URL is not set.")
        return None
    try:
        app_logger.info(f"Attempting to connect to database using DSN (first 20 chars): {DATABASE_URL[:20]}...")
        conn = psycopg2.connect(DATABASE_URL)
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

def get_incidents_by_week():
    """Get incident counts grouped by week"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incidents_by_week.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        query = """
            SELECT 
                DATE_TRUNC('week', fecha_incidente) as week_start,
                COUNT(*) as incident_count
            FROM reportes_incidentes 
            WHERE fecha_incidente IS NOT NULL
            GROUP BY DATE_TRUNC('week', fecha_incidente)
            ORDER BY week_start DESC
            LIMIT 12;
        """
        
        cur.execute(query)
        rows = cur.fetchall()
        
        data = []
        for row in rows:
            week_start = row['week_start']
            week_end = week_start + timedelta(days=6)
            data.append({
                'period': f"{week_start.strftime('%d/%m')} - {week_end.strftime('%d/%m/%Y')}",
                'count': row['incident_count']
            })
        
        return list(reversed(data))  # Reverse to show chronological order
        
    except Exception as e:
        app_logger.error(f"Error in get_incidents_by_week: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incidents_by_month():
    """Get incident counts grouped by month"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incidents_by_month.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        query = """
            SELECT 
                DATE_TRUNC('month', fecha_incidente) as month_start,
                COUNT(*) as incident_count
            FROM reportes_incidentes 
            WHERE fecha_incidente IS NOT NULL
            GROUP BY DATE_TRUNC('month', fecha_incidente)
            ORDER BY month_start DESC
            LIMIT 12;
        """
        
        cur.execute(query)
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
            
            data.append({
                'period': f"{month_name} {month_start.year}",
                'count': incident_count,
                'has_kpi_violation': incident_count >= monthly_kpi_threshold
            })
        
        return list(reversed(data))  # Reverse to show chronological order
        
    except Exception as e:
        app_logger.error(f"Error in get_incidents_by_month: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incidents_by_year():
    """Get incident counts grouped by year"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incidents_by_year.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        query = """
            SELECT 
                EXTRACT(YEAR FROM fecha_incidente) as year,
                COUNT(*) as incident_count
            FROM reportes_incidentes 
            WHERE fecha_incidente IS NOT NULL
            GROUP BY EXTRACT(YEAR FROM fecha_incidente)
            ORDER BY year;
        """
        
        cur.execute(query)
        rows = cur.fetchall()
        
        # KPI threshold for yearly: 52 weeks * 4 incidents = 208 incidents per year
        yearly_kpi_threshold = 208
        
        data = []
        for row in rows:
            incident_count = row['incident_count']
            data.append({
                'period': str(int(row['year'])),
                'count': incident_count,
                'has_kpi_violation': incident_count >= yearly_kpi_threshold
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

def get_incident_types_stats():
    """Get incident counts by type for the current week"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incident_types_stats.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Get current week's data
        query = """
            SELECT 
                ti.nombre as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            LEFT JOIN "tipo_incidencia" ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            WHERE ri.fecha_incidente >= DATE_TRUNC('week', CURRENT_DATE)
              AND ri.fecha_incidente < DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '1 week'
              AND ti.nombre IS NOT NULL
            GROUP BY ti.nombre
            ORDER BY ti.nombre;
        """
        
        cur.execute(query)
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
        for incident_type, count in incident_types.items():
            result.append({
                'type': incident_type,
                'count': count,
                'is_critical': count >= 4  # KPI threshold
            })
        
        return result
        
    except Exception as e:
        app_logger.error(f"Error in get_incident_types_stats: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incident_types_monthly():
    """Get incident counts by type for the current month"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incident_types_monthly.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Get current month's data
        query = """
            SELECT 
                ti.nombre as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            LEFT JOIN "tipo_incidencia" ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            WHERE ri.fecha_incidente >= DATE_TRUNC('month', CURRENT_DATE)
              AND ri.fecha_incidente < DATE_TRUNC('month', CURRENT_DATE) + INTERVAL '1 month'
              AND ti.nombre IS NOT NULL
            GROUP BY ti.nombre
            ORDER BY ti.nombre;
        """
        
        cur.execute(query)
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

def get_incident_types_yearly():
    """Get incident counts by type for the current year"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incident_types_yearly.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Get current year's data
        query = """
            SELECT 
                ti.nombre as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            LEFT JOIN "tipo_incidencia" ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            WHERE EXTRACT(YEAR FROM ri.fecha_incidente) = EXTRACT(YEAR FROM CURRENT_DATE)
              AND ti.nombre IS NOT NULL
            GROUP BY ti.nombre
            ORDER BY ti.nombre;
        """
        
        cur.execute(query)
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

def get_incidents_by_week_with_types():
    """Get incident counts grouped by week with type breakdown for KPI alerts"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incidents_by_week_with_types.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        query = """
            SELECT 
                DATE_TRUNC('week', ri.fecha_incidente) as week_start,
                ti.nombre as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            LEFT JOIN "tipo_incidencia" ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            WHERE ri.fecha_incidente IS NOT NULL
              AND ti.nombre IN ('Hurto', 'Olvido', 'Recuperacion', 'Robo')
            GROUP BY DATE_TRUNC('week', ri.fecha_incidente), ti.nombre
            ORDER BY week_start DESC, ti.nombre
            LIMIT 48; -- 12 weeks * 4 types max
        """
        
        cur.execute(query)
        rows = cur.fetchall()
        
        # KPI threshold for weekly: 4 incidents per week
        weekly_kpi_threshold = 4
        
        # Group by week and check for KPI violations
        weeks_data = {}
        for row in rows:
            week_start = row['week_start']
            incident_type = row['incident_type']
            count = row['incident_count']
            
            if week_start not in weeks_data:
                week_end = week_start + timedelta(days=6)
                weeks_data[week_start] = {
                    'period': f"{week_start.strftime('%d/%m')} - {week_end.strftime('%d/%m/%Y')}",
                    'total_count': 0,
                    'has_kpi_violation': False,
                    'types': {}
                }
            
            weeks_data[week_start]['types'][incident_type] = count
            weeks_data[week_start]['total_count'] += count
            
            # Check KPI violation (4 or more incidents total per week)
            if weeks_data[week_start]['total_count'] >= weekly_kpi_threshold:
                weeks_data[week_start]['has_kpi_violation'] = True
        
        # Convert to list and sort chronologically
        result = []
        for week_start in sorted(weeks_data.keys(), reverse=True)[:12]:  # Last 12 weeks
            result.append(weeks_data[week_start])
        
        return list(reversed(result))  # Reverse to show chronological order
        
    except Exception as e:
        app_logger.error(f"Error in get_incidents_by_week_with_types: {e}", exc_info=True)
        return []
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def get_incidents_by_month_with_types():
    """Get incident counts grouped by month with type breakdown"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incidents_by_month_with_types.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        query = """
            SELECT 
                DATE_TRUNC('month', ri.fecha_incidente) as month_start,
                ti.nombre as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            LEFT JOIN "tipo_incidencia" ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            WHERE ri.fecha_incidente IS NOT NULL
              AND ti.nombre IN ('Hurto', 'Olvido', 'Recuperacion', 'Robo')
            GROUP BY DATE_TRUNC('month', ri.fecha_incidente), ti.nombre
            ORDER BY month_start DESC, ti.nombre
            LIMIT 48; -- 12 months * 4 types max
        """
        
        cur.execute(query)
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

def get_incidents_by_year_with_types():
    """Get incident counts grouped by year with type breakdown"""
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in get_incidents_by_year_with_types.")
            return []

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        query = """
            SELECT 
                EXTRACT(YEAR FROM ri.fecha_incidente) as year,
                ti.nombre as incident_type,
                COUNT(*) as incident_count
            FROM reportes_incidentes ri
            LEFT JOIN "tipo_incidencia" ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            WHERE ri.fecha_incidente IS NOT NULL
              AND ti.nombre IN ('Hurto', 'Olvido', 'Recuperacion', 'Robo')
            GROUP BY EXTRACT(YEAR FROM ri.fecha_incidente), ti.nombre
            ORDER BY year DESC, ti.nombre;
        """
        
        cur.execute(query)
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

# --- Routes ---
@app.route('/')
@jwt_required()
def dashboard():
    user_email = get_jwt_identity()
    user_name = user_email.split('@')[0]  # Default fallback to email prefix

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            app_logger.info(f"Attempting to fetch user name for email: {user_email}")
            cur.execute('SELECT "name" FROM "users" WHERE email = %s', (user_email,))
            user_row = cur.fetchone()
            if user_row and user_row[0]:
                user_name = user_row[0]
                app_logger.info(f"User found in DB: {user_name}")
            else:
                app_logger.warning(f"User {user_email} not found in 'users' table or 'name' field is empty. Displaying email prefix as name: {user_name}")
        else:
            app_logger.error("No database connection available for fetching user name.")

    except psycopg2.Error as e:
        app_logger.error(f"PostgreSQL Error getting user name: {e}", exc_info=True)
    except Exception as e:
        app_logger.error(f"An unexpected error occurred getting user name: {e}", exc_info=True)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
            app_logger.info("Database connection closed after fetching user name.")

    app_logger.info(f"Rendering dashboard.html with user_name: {user_name}")
    return render_template("dashboard.html", current_user=user_email, user_name=user_name)

@app.route('/dashboard')
@jwt_required()
def dashboard_redirect():
    """Redirect /dashboard to root for compatibility"""
    return redirect('/')

@app.route('/api/incidents/weekly')
@jwt_required()
def api_incidents_weekly():
    """API endpoint for weekly incident data"""
    data = get_incidents_by_week()
    return jsonify(data)

@app.route('/api/incidents/monthly')
@jwt_required()
def api_incidents_monthly():
    """API endpoint for monthly incident data"""
    data = get_incidents_by_month()
    return jsonify(data)

@app.route('/api/incidents/yearly')
@jwt_required()
def api_incidents_yearly():
    """API endpoint for yearly incident data"""
    data = get_incidents_by_year()
    return jsonify(data)

@app.route('/api/incidents/types')
@jwt_required()
def api_incident_types():
    """API endpoint for incident types data (weekly)"""
    data = get_incident_types_stats()
    return jsonify(data)

@app.route('/api/incidents/types/monthly')
@jwt_required()
def api_incident_types_monthly():
    """API endpoint for monthly incident types data"""
    data = get_incident_types_monthly()
    return jsonify(data)

@app.route('/api/incidents/types/yearly')
@jwt_required()
def api_incident_types_yearly():
    """API endpoint for yearly incident types data"""
    data = get_incident_types_yearly()
    return jsonify(data)

@app.route('/api/incidents/weekly-with-kpi')
@jwt_required()
def api_incidents_weekly_with_kpi():
    """API endpoint for weekly incident data with KPI indicators"""
    data = get_incidents_by_week_with_types()
    return jsonify(data)

@app.route('/api/incidents/monthly-with-types')
@jwt_required()
def api_incidents_monthly_with_types():
    """API endpoint for monthly incident data with type breakdown"""
    data = get_incidents_by_month_with_types()
    return jsonify(data)

@app.route('/api/incidents/yearly-with-types')
@jwt_required()
def api_incidents_yearly_with_types():
    """API endpoint for yearly incident data with type breakdown"""
    data = get_incidents_by_year_with_types()
    return jsonify(data)

@app.route('/logout')
def logout():
    try:
        app_logger.info("User logout requested")
        response = redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))
        unset_jwt_cookies(response)
        app_logger.info("JWT cookies cleared, redirecting to login service")
        return response
    except Exception as e:
        app_logger.error(f"Error during logout: {e}", exc_info=True)
        return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app_logger.info(f"Starting Flask app on port {port}")
    app.run(debug=True, host='0.0.0.0', port=port)