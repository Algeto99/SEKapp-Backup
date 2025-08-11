import os
import sys
import logging
import re
from datetime import timedelta, datetime, timezone
from io import BytesIO

from flask import Flask, render_template, request, jsonify, Response, flash, session, redirect, url_for, send_file
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity, get_jwt, unset_jwt_cookies
from flask_cors import CORS
from google.cloud import secretmanager
from google.api_core.exceptions import NotFound

import google.auth.transport.requests
import google.oauth2.id_token
import requests

import psycopg2
from psycopg2 import extras

import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from functools import wraps
from flask_jwt_extended import get_jwt

# PDF generation imports
from weasyprint import HTML, CSS


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
app_logger.info(f"Starting Viewer Service in {'production' if is_production else 'development'} mode")

# --- Service URL Configuration ---
app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com')
app.config['LANDING_SERVICE_URL'] = os.environ.get('LANDING_SERVICE_URL', 'https://landing.secapp.tzolkintech.com')
app.config['FORMS_SERVICE_URL'] = os.environ.get('FORMS_SERVICE_URL', 'https://form1.secapp.tzolkintech.com')
app.config['DASHBOARD_SERVICE_URL'] = os.environ.get('DASHBOARD_SERVICE_URL', 'https://dashboard.secapp.tzolkintech.com')

app.config['INTERNAL_LOGIN_SERVICE_URL'] = os.environ.get('INTERNAL_LOGIN_SERVICE_URL', 'https://login-24309643178.us-central1.run.app')
app.config['INTERNAL_LANDING_SERVICE_URL'] = os.environ.get('INTERNAL_LANDING_SERVICE_URL', 'https://landing-24309643178.us-central1.run.app')
app.config['INTERNAL_FORMS_SERVICE_URL'] = os.environ.get('INTERNAL_FORMS_SERVICE_URL', 'https://forms-24309643178.us-central1.run.app')
app.config['INTERNAL_DASHBOARD_SERVICE_URL'] = os.environ.get('INTERNAL_DASHBOARD_SERVICE_URL', 'https://dashboard-24309643178.us-central1.run.app')

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
    project_id = "your-gcp-project-id" # Placeholder for local testing if not set

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


def fetch_reports(offset, limit, filters=None):
    conn = None
    cur = None
    reports = []
    total_count = 0
    try:
        conn = get_db_connection()
        if not conn:
            app_logger.error("Failed to get database connection in fetch_reports. Returning empty list.")
            return reports, total_count

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # Build WHERE clause based on filters
        where_conditions = []
        query_params = []
        
        if filters:
            # Report ID filter
            if filters.get('report_id'):
                try:
                    # Handle multiple report IDs separated by commas
                    report_ids = [int(id.strip()) for id in filters['report_id'].split(',') if id.strip().isdigit()]
                    if report_ids:
                        placeholders = ','.join(['%s'] * len(report_ids))
                        where_conditions.append(f"ri.id_reporte_incidente IN ({placeholders})")
                        query_params.extend(report_ids)
                except (ValueError, AttributeError):
                    pass
            
            # Submitted by filter (searches both name and email)
            if filters.get('submitted_by'):
                where_conditions.append("(LOWER(u.name) LIKE LOWER(%s) OR LOWER(ri.user_email) LIKE LOWER(%s))")
                search_term = f"%{filters['submitted_by']}%"
                query_params.extend([search_term, search_term])
            
            # Property filter (changed from location to property)
            if filters.get('property'):
                where_conditions.append("LOWER(p.nombre) LIKE LOWER(%s)")
                query_params.append(f"%{filters['property']}%")
            
            # Date range filters
            if filters.get('start_date'):
                where_conditions.append("ri.fecha_incidente >= %s")
                query_params.append(filters['start_date'])
            
            if filters.get('end_date'):
                where_conditions.append("ri.fecha_incidente <= %s")
                query_params.append(filters['end_date'])

        where_clause = ""
        if where_conditions:
            where_clause = "WHERE " + " AND ".join(where_conditions)

        # First, get the total count of reports with filters
        count_query = f"""
            SELECT COUNT(*)
            FROM reportes_incidentes ri
            LEFT JOIN "tipo_cliente" tc ON ri.id_tipo_cliente = tc.id_tipo_cliente
            LEFT JOIN "lugar_incidente" li ON ri.id_lugar_incidente = li.id_lugar_incidente
            LEFT JOIN "tipo_incidencia" ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            LEFT JOIN supervisor s ON ri.id_supervisor = s.id_supervisor
            LEFT JOIN users u ON ri.user_email = u.email
            LEFT JOIN propiedades p ON ri.id_propiedad = p.id_propiedad
            {where_clause}
        """
        
        app_logger.info("Executing COUNT(*) query for reports with filters.")
        cur.execute(count_query, query_params)
        total_count = cur.fetchone()[0]
        app_logger.info(f"Total reports found with filters: {total_count}")
        
        # Debug: Let's also check the sorting with a simple query
        debug_query = """
            SELECT id_reporte_incidente, creado_en, fecha_incidente 
            FROM reportes_incidentes 
            ORDER BY creado_en DESC NULLS LAST, id_reporte_incidente DESC
            LIMIT 5
        """
        cur.execute(debug_query)
        debug_rows = cur.fetchall()
        app_logger.info("DEBUG - Recent reports by creado_en:")
        for row in debug_rows:
            app_logger.info(f"  ID: {row[0]}, Created: {row[1]}, Incident Date: {row[2]}")

        # Then, get the paginated reports with user names and filters
        # ORDER BY: Sort by creation date (newest reports first - most recent submissions at the top)
        query = f"""
                SELECT
                    ri.id_reporte_incidente,
                    ri.user_email,
                    ri.creado_en,
                    ti.nombre AS titulo_incidencia,
                    ri.descripcion_incidente,
                    ri.valor_aproximado,
                    ri.pertenencias_sustraidas,
                    ri.nombre_persona,
                    ri.telefono_persona,
                    ri.numero_identidad_persona,
                    ri.numero_local,
                    ri.direccion,
                    ri.imagenes_pdfs,
                    s.nombre AS supervisor_name,
                    ri.fecha_incidente,
                    ri.hora_incidente,
                    tc.nombre AS id_tipo_cliente,
                    li.nombre AS id_lugar_incidente,
                    p.nombre AS propiedad_nombre,  -- Include propiedad
                    ri.descripcion_zona_comun,
                    u.name AS user_name
                FROM
                    reportes_incidentes ri
                LEFT JOIN
                    "tipo_cliente" tc ON ri.id_tipo_cliente = tc.id_tipo_cliente
                LEFT JOIN
                    "lugar_incidente" li ON ri.id_lugar_incidente = li.id_lugar_incidente
                LEFT JOIN
                    "tipo_incidencia" ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
                LEFT JOIN
                    supervisor s ON ri.id_supervisor = s.id_supervisor
                LEFT JOIN
                    users u ON ri.user_email = u.email
                LEFT JOIN
                    propiedades p ON ri.id_propiedad = p.id_propiedad  -- Include JOIN with propiedades
                {where_clause}
                ORDER BY
                    ri.creado_en DESC NULLS LAST,
                    ri.id_reporte_incidente DESC
                OFFSET %s LIMIT %s;
            """

        # Add pagination parameters
        final_params = query_params + [offset, limit]
        
        app_logger.info(f"Executing fetch_reports query with offset={offset}, limit={limit}, filters={filters}.")
        app_logger.info(f"Query ORDER BY: ri.creado_en DESC (newest submissions first)")
        cur.execute(query, final_params)
        rows = cur.fetchall()
        app_logger.info(f"Fetched {len(rows)} reports from the database.")
        
        # Debug: Log the first few report timestamps to verify sorting
        if rows:
            for i, row in enumerate(rows[:3]):  # Log first 3 reports
                app_logger.info(f"Report {i+1}: ID={row['id_reporte_incidente']}, creado_en={row['creado_en']}")

        for row_dict in rows:
            display_name = row_dict.get("user_name") or row_dict.get("user_email", "desconocido")
            
            forms_data = {
                "id": row_dict["id_reporte_incidente"],
                "title": f"Reporte #{row_dict['id_reporte_incidente']}",
                "submittedBy": display_name,
                "dateSubmitted": row_dict.get("creado_en").strftime("%Y-%m-%d %H:%M:%S") if row_dict.get("creado_en") else "N/A",
                "data": {
                    "Título de Incidencia": row_dict.get("titulo_incidencia"),
                    "Tipo de Cliente": row_dict.get("id_tipo_cliente"),
                    "Lugar del Incidente": row_dict.get("id_lugar_incidente"),
                    "Propiedad": row_dict.get("propiedad_nombre") or "No especificado",
                    "Zona Común": row_dict.get("descripcion_zona_comun"),
                    "Fecha del Incidente": str(row_dict.get("fecha_incidente")),
                    "Hora del Incidente": str(row_dict.get("hora_incidente")),
                    "Descripción del Incidente": str(row_dict.get("descripcion_incidente")),
                    "Valor Aproximado": str(row_dict.get("valor_aproximado")),
                    "Pertenencias Sustraídas": str(row_dict.get("pertenencias_sustraidas")),
                    "Nombre de la Persona": str(row_dict.get("nombre_persona")),
                    "Teléfono": str(row_dict.get("telefono_persona")),
                    "Número de Identidad": str(row_dict.get("numero_identidad_persona")),
                    "Número de Local": str(row_dict.get("numero_local")),
                    "Dirección": str(row_dict.get("direccion")),
                    "URLs de Imágenes o PDFs": str(row_dict.get("imagenes_pdfs")),
                    "Nombre del Supervisor": row_dict.get("supervisor_name") or "N/A"
                }
            }
            reports.append(forms_data)

    except psycopg2.Error as e:
        app_logger.error(f"PostgreSQL Error in fetch_reports: {e}", exc_info=True)
        reports = []
        total_count = 0
    except Exception as e:
        app_logger.error(f"An unexpected error occurred in fetch_reports: {e}", exc_info=True)
        reports = []
        total_count = 0
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
            app_logger.info("Database connection closed in fetch_reports.")
    return reports, total_count

def fetch_reports_by_ids(report_ids):
    conn = None
    cur = None
    reports = []
    if not report_ids:
        app_logger.info("fetch_reports_by_ids received an empty report_ids list.")
        return []
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
            SELECT
                ri.id_reporte_incidente,
                ri.user_email,
                ri.creado_en,
                ti.nombre AS titulo_incidencia,
                ri.descripcion_incidente,
                ri.valor_aproximado,
                ri.pertenencias_sustraidas,
                ri.nombre_persona,
                ri.telefono_persona,
                ri.numero_identidad_persona,
                ri.numero_local,
                ri.direccion,
                ri.imagenes_pdfs,
                s.nombre AS supervisor_name,
                ri.fecha_incidente,
                ri.hora_incidente,
                tc.nombre AS id_tipo_cliente,
                li.nombre AS id_lugar_incidente,
                p.nombre AS propiedad_nombre,  -- ADD THIS LINE
                ri.descripcion_zona_comun,
                u.name AS user_name -- Get the user's full name
            FROM
                reportes_incidentes ri
            LEFT JOIN
                "tipo_cliente" tc ON ri.id_tipo_cliente = tc.id_tipo_cliente
            LEFT JOIN
                "lugar_incidente" li ON ri.id_lugar_incidente = li.id_lugar_incidente
            LEFT JOIN
                "tipo_incidencia" ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            LEFT JOIN
                supervisor s ON ri.id_supervisor = s.id_supervisor
            LEFT JOIN
                users u ON ri.user_email = u.email -- Join with users table to get full name
            LEFT JOIN
                propiedades p ON ri.id_propiedad = p.id_propiedad  -- ADD THIS JOIN
            WHERE
                ri.id_reporte_incidente IN ({placeholders})
            ORDER BY
                ri.creado_en DESC NULLS LAST,
                ri.id_reporte_incidente DESC;
        """
        app_logger.info(f"Executing fetch_reports_by_ids query for IDs: {clean_ids}.")
        app_logger.info(f"Query ORDER BY: ri.creado_en DESC (newest submissions first)")
        cur.execute(query, clean_ids)
        rows = cur.fetchall()
        app_logger.info(f"Fetched {len(rows)} specific reports.")
        
        # Debug: Log the report timestamps to verify sorting
        if rows:
            for i, row in enumerate(rows):
                app_logger.info(f"Specific Report {i+1}: ID={row['id_reporte_incidente']}, creado_en={row['creado_en']}")

        for row_dict in rows:
            # Use the full name if available, otherwise fall back to email
            display_name = row_dict.get("user_name") or row_dict.get("user_email", "desconocido")
            
            reports_data = {
                "id": row_dict["id_reporte_incidente"],
                "title": f"Reporte #{row_dict['id_reporte_incidente']}",
                "submittedBy": display_name,
                "dateSubmitted": row_dict.get("creado_en").strftime("%Y-%m-%d %H:%M:%S") if row_dict.get("creado_en") else "N/A",
                "data": {
                    "Título de Incidencia": row_dict.get("titulo_incidencia"),
                    "Tipo de Cliente": row_dict.get("id_tipo_cliente"),
                    "Lugar del Incidente": row_dict.get("id_lugar_incidente"),
                    "Propiedad": row_dict.get("propiedad_nombre") or "No especificado",  # ADD THIS LINE
                    "Zona Común": row_dict.get("descripcion_zona_comun"),
                    "Fecha del Incidente": str(row_dict.get("fecha_incidente")),
                    "Hora del Incidente": str(row_dict.get("hora_incidente")),
                    "Descripción del Incidente": str(row_dict.get("descripcion_incidente")),
                    "Valor Aproximado": str(row_dict.get("valor_aproximado")),
                    "Pertenencias Sustraídas": str(row_dict.get("pertenencias_sustraidas")),
                    "Nombre de la Persona": str(row_dict.get("nombre_persona")),
                    "Teléfono": str(row_dict.get("telefono_persona")),
                    "Número de Identidad": str(row_dict.get("numero_identidad_persona")),
                    "Número de Local": str(row_dict.get("numero_local")),
                    "Dirección": str(row_dict.get("direccion")),
                    "URLs de Imágenes o PDFs": str(row_dict.get("imagenes_pdfs")),
                    "Nombre del Supervisor": row_dict.get("supervisor_name") or "N/A"
                }
            }
            reports.append(reports_data)

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
                    landing_url = app.config.get('LANDING_SERVICE_URL', 'https://landing.secapp.tzolkintech.com')
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
                login_url = app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com')
                return redirect(login_url)
    
    return decorated_function

# --- Routes ---
@app.route('/')
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
                         login_service_url=app.config.get('LOGIN_SERVICE_URL'),
                         landing_service_url=app.config.get('LANDING_SERVICE_URL'),
                         forms_service_url=app.config.get('FORMS_SERVICE_URL'),
                         dashboard_service_url=app.config.get('DASHBOARD_SERVICE_URL'))

@app.route('/api/reports', methods=['GET'])
@jwt_required()
def get_more_reports():
    offset = request.args.get('offset', type=int, default=0)
    limit = request.args.get('limit', type=int, default=10)

    if offset < 0:
        offset = 0
    if limit <= 0 or limit > 100:
        limit = 10

    # Extract filter parameters
    filters = {}
    if request.args.get('report_id'):
        filters['report_id'] = request.args.get('report_id')
    if request.args.get('submitted_by'):
        filters['submitted_by'] = request.args.get('submitted_by')
    if request.args.get('property'):  # CHANGED: from 'location' to 'property'
        filters['property'] = request.args.get('property')
    if request.args.get('start_date'):
        filters['start_date'] = request.args.get('start_date')
    if request.args.get('end_date'):
        filters['end_date'] = request.args.get('end_date')

    app_logger.info(f"API request with filters: {filters}")
    
    reports, total_count = fetch_reports(offset, limit, filters if filters else None)
    return jsonify({"reports": reports, "total_count": total_count})

# New route to handle fetching a single report by ID, assumed to be used by "Ver Detalles"
@app.route('/api/report/<int:report_id>', methods=['GET'])
@jwt_required()
def get_single_report(report_id):
    app_logger.info(f"Attempting to fetch single report with ID: {report_id} via GET /api/report/<id>")
    # fetch_reports_by_ids expects a list of IDs
    reports = fetch_reports_by_ids([report_id])
    
    if reports:
        app_logger.info(f"Successfully fetched report {report_id} for details.")
        return jsonify(reports[0]), 200
    else:
        app_logger.warning(f"Report with ID {report_id} not found for details.")
        return jsonify({"success": False, "message": f"Report with ID {report_id} not found."}), 404


@app.route('/api/email-reports', methods=['POST'])
@jwt_required()
def email_selected_reports_api():
    user_email = get_jwt_identity()
    data = request.get_json()
    report_ids = data.get('report_ids')
    recipient_email = data.get('recipient_email')

    if not report_ids or not isinstance(report_ids, list):
        return jsonify({"success": False, "message": "No report IDs provided or invalid format."}), 400
    if not recipient_email or not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", recipient_email):
        return jsonify({"success": False, "message": "Invalid recipient email address."}), 400

    app_logger.info(f"User {user_email} requested to email reports {report_ids} to {recipient_email}")

    reports_to_email = fetch_reports_by_ids(report_ids)

    if not reports_to_email:
        app_logger.warning(f"No reports found for the provided IDs during email request: {report_ids}")
        return jsonify({"success": False, "message": "No reports found for the provided IDs."}), 404

    subject = f"Reportes de Incidencias Seleccionados ({len(reports_to_email)} Reportes)"
    
    html_body_parts = [
        f"<html><body style='font-family: Arial, sans-serif; color: #333;'>",
        f"<div style='max-width: 600px; margin: 0 auto; padding: 20px;'>",
        f"<h2 style='color: #2563eb;'>Reportes de Incidencias Seleccionados - SMT SecApp</h2>",
        f"<p>Hola,</p>",
        f"<p>Adjuntos se encuentran los detalles de los reportes de incidencias seleccionados:</p>"
    ]

    for i, report in enumerate(reports_to_email):
        html_body_parts.append(f"<div style='background-color: #f8fafc; padding: 15px; border-radius: 8px; margin: 15px 0; border: 1px solid #e2e8f0;'>")
        html_body_parts.append(f"<h3 style='color: #4a5568; margin-top: 0;'>Reporte {i+1} (ID: {report['id']})</h3>")
        html_body_parts.append(f"<p><strong>Título:</strong> {report['title']}</p>")
        html_body_parts.append(f"<p><strong>Enviado por:</strong> {report['submittedBy']} el {report['dateSubmitted']}</p>")
        
        for key, value in report['data'].items():
            display_value = value if value and str(value).strip() != 'N/A' and str(value).strip() != 'None' else 'No especificado'
            # Modified section for handling URLs de Imágenes o PDFs
            if key == 'URLs de Imágenes o PDFs' and display_value != 'No especificado':
                urls = display_value.split('\n')
                html_body_parts.append(f"<p><strong>Archivos Adjuntos:</strong></p><div style='display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px;'>")
                for url in urls:
                    url = url.strip()
                    if url:
                        lower_url = url.lower()
                        if lower_url.endswith(('.jpeg', '.jpg', '.png', '.gif', '.webp')):
                            html_body_parts.append(f"""
                                <div style='margin-bottom: 10px;'>
                                    <a href="{url}" target="_blank" style="text-decoration: none;">
                                        <img src="{url}" alt="Imagen del reporte" style="max-width: 200px; height: auto; border-radius: 4px; border: 1px solid #ccc;">
                                    </a>
                                </div>
                            """)
                        elif lower_url.endswith('.pdf'):
                            html_body_parts.append(f"""
                                <div style='margin-bottom: 10px;'>
                                    <p style="margin: 0;">PDF: <a href="{url}" target="_blank" style="color: #2563eb; text-decoration: none;">{os.path.basename(url)}</a></p>
                                </div>
                            """)
                        else:
                            html_body_parts.append(f"""
                                <div style='margin-bottom: 10px;'>
                                    <p style="margin: 0;">Archivo: <a href="{url}" target="_blank" style="color: #2563eb; text-decoration: none;">{os.path.basename(url)}</a></p>
                                </div>
                            """)
                html_body_parts.append(f"</div>") # Close flex container
            else:
                html_body_parts.append(f"<p><strong>{key}:</strong> {display_value}</p>")
        
        html_body_parts.append(f"</div>")

    html_body_parts.append(f"<p style='margin-top: 20px;'>Generado por {user_email} desde SMT SecApp.</p>")
    html_body_parts.append(f"</div></body></html>")
    email_html_body = "\n".join(html_body_parts)

    success, message = send_reports_email(recipient_email, subject, email_html_body, is_html=True)

    if success:
        return jsonify({"success": True, "message": "Reportes enviados por correo electrónico exitosamente!"}), 200
    else:
        app_logger.error(f"Failed to send email: {message}")
        return jsonify({"success": False, "message": f"Error al enviar correo electrónico: {message}"}), 500


@app.route('/api/generate-pdf', methods=['POST'])
@jwt_required()
def generate_pdf():
    """Generate PDF for selected reports using WeasyPrint"""
    user_email = get_jwt_identity()
    data = request.get_json()
    report_ids = data.get('report_ids')

    if not report_ids or not isinstance(report_ids, list):
        return jsonify({"success": False, "message": "No report IDs provided or invalid format."}), 400

    app_logger.info(f"User {user_email} requested PDF generation for reports {report_ids}")

    try:
        # Fetch the reports
        reports_to_pdf = fetch_reports_by_ids(report_ids)

        if not reports_to_pdf:
            app_logger.warning(f"No reports found for the provided IDs during PDF request: {report_ids}")
            return jsonify({"success": False, "message": "No reports found for the provided IDs."}), 404

        # Generate HTML content for PDF
        html_content = generate_reports_html(reports_to_pdf)
        
        # Create PDF using WeasyPrint
        pdf_buffer = BytesIO()
        HTML(string=html_content).write_pdf(pdf_buffer)
        pdf_buffer.seek(0)

        # Create filename
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
                    content: "Página " counter(page) " - SMT SecApp";
                    font-size: 10px;
                    color: #666;
                }
            }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>Reportes de Incidencias - SMT SecApp</h1>
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
        # Fallback: just redirect without cookie clearing if there's an error
        return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app_logger.info(f"Starting Flask app on port {port}")
    app.run(debug=True, host='0.0.0.0', port=port)