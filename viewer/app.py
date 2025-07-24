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

import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# NEW IMPORT for PDF generation
from weasyprint import HTML


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


def fetch_reports(offset, limit):
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

        # First, get the total count of reports
        app_logger.info("Executing COUNT(*) query for reports.")
        cur.execute("SELECT COUNT(*) FROM reportes_incidentes")
        total_count = cur.fetchone()[0]
        app_logger.info(f"Total reports found: {total_count}")

        # Then, get the paginated reports
        query = """
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
                s.nombre AS supervisor_name, -- Added supervisor name
                ri.fecha_incidente,
                ri.hora_incidente,
                tc.nombre AS id_tipo_cliente,
                li.nombre AS id_lugar_incidente,
                ri.descripcion_zona_comun
            FROM
                reportes_incidentes ri
            LEFT JOIN
                "tipo_cliente" tc ON ri.id_tipo_cliente = tc.id_tipo_cliente
            LEFT JOIN
                "lugar_incidente" li ON ri.id_lugar_incidente = li.id_lugar_incidente
            LEFT JOIN
                "tipo_incidencia" ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            LEFT JOIN
                supervisor s ON ri.id_supervisor = s.id_supervisor -- Join with supervisor table
            ORDER BY
                ri.fecha_incidente DESC, ri.hora_incidente DESC
            OFFSET %s LIMIT %s;
        """
        app_logger.info(f"Executing fetch_reports query with offset={offset}, limit={limit}.")
        cur.execute(query, (offset, limit))
        rows = cur.fetchall()
        app_logger.info(f"Fetched {len(rows)} reports from the database.")

        for row_dict in rows:
            forms_data = {
                "id": row_dict["id_reporte_incidente"],
                "title": f"Reporte #{row_dict['id_reporte_incidente']}",
                "submittedBy": row_dict.get("user_email", "desconocido"),
                "dateSubmitted": row_dict.get("creado_en").strftime("%Y-%m-%d %H:%M:%S") if row_dict.get("creado_en") else "N/A",
                "data": {
                    "Título de Incidencia": row_dict.get("titulo_incidencia"),
                    "Tipo de Cliente": row_dict.get("id_tipo_cliente"),
                    "Lugar del Incidente": row_dict.get("id_lugar_incidente"),
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
                    "Nombre del Supervisor": row_dict.get("supervisor_name") or "N/A" # Display supervisor name
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
                s.nombre AS supervisor_name, -- Added supervisor name
                ri.fecha_incidente,
                ri.hora_incidente,
                tc.nombre AS id_tipo_cliente,
                li.nombre AS id_lugar_incidente,
                ri.descripcion_zona_comun
            FROM
                reportes_incidentes ri
            LEFT JOIN
                "tipo_cliente" tc ON ri.id_tipo_cliente = tc.id_tipo_cliente
            LEFT JOIN
                "lugar_incidente" li ON ri.id_lugar_incidente = li.id_lugar_incidente
            LEFT JOIN
                "tipo_incidencia" ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            LEFT JOIN
                supervisor s ON ri.id_supervisor = s.id_supervisor -- Join with supervisor table
            WHERE
                ri.id_reporte_incidente IN ({placeholders})
            ORDER BY
                ri.fecha_incidente DESC, ri.hora_incidente DESC;
        """
        app_logger.info(f"Executing fetch_reports_by_ids query for IDs: {clean_ids}.")
        cur.execute(query, clean_ids)
        rows = cur.fetchall()
        app_logger.info(f"Fetched {len(rows)} specific reports.")

        for row_dict in rows:
            reports_data = {
                "id": row_dict["id_reporte_incidente"],
                "title": f"Reporte #{row_dict['id_reporte_incidente']}",
                "submittedBy": row_dict.get("user_email", "desconocido"),
                "dateSubmitted": row_dict.get("creado_en").strftime("%Y-%m-%d %H:%M:%S") if row_dict.get("creado_en") else "N/A",
                "data": {
                    "Título de Incidencia": row_dict.get("titulo_incidencia"),
                    "Tipo de Cliente": row_dict.get("id_tipo_cliente"),
                    "Lugar del Incidente": row_dict.get("id_lugar_incidente"),
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
                    "Nombre del Supervisor": row_dict.get("supervisor_name") or "N/A" # Display supervisor name
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

# --- Routes ---
@app.route('/')
@jwt_required()
def index():
    user_email = get_jwt_identity()
    user_name = user_email.split('@')[0] # Default fallback to email prefix

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            app_logger.info(f"Attempting to fetch user name for email: {user_email}")
            cur.execute('SELECT "name" FROM "users" WHERE email = %s', (user_email,))
            user_row = cur.fetchone()
            if user_row and user_row[0]: # Check if user_row exists and name is not empty
                user_name = user_row[0]
                app_logger.info(f"User found in DB: {user_name}")
            else:
                app_logger.warning(f"User {user_email} not found in 'users' table or 'name' field is empty. Displaying email prefix as name: {user_name}")
        else:
            app_logger.error("No database connection available for fetching user name.")

    except psycopg2.Error as e:
        app_logger.error(f"PostgreSQL Error getting user name: {e}", exc_info=True)
        # user_name already defaulted to email prefix, no change needed here.
    except Exception as e:
        app_logger.error(f"An unexpected error occurred getting user name: {e}", exc_info=True)
        # user_name already defaulted to email prefix, no change needed here.
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
            app_logger.info("Database connection closed after fetching user name.")

    app_logger.info(f"Rendering index.html with user_name: {user_name}") # New log line
    initial_reports, total_reports_count = fetch_reports(offset=0, limit=10)
    return render_template("index.html", current_user=user_email, forms=initial_reports, user_name=user_name, total_reports=total_reports_count)


@app.route('/api/reports', methods=['GET'])
@jwt_required()
def get_more_reports():
    offset = request.args.get('offset', type=int, default=0)
    limit = request.args.get('limit', type=int, default=10)

    if offset < 0:
        offset = 0
    if limit <= 0 or limit > 100:
        limit = 10

    reports, total_count = fetch_reports(offset, limit)
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
            display_value = value if value and str(value).strip() != 'N/A' else 'No especificado'
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


# NEW ROUTE: PDF Generation
@app.route('/api/generate-pdf', methods=['POST'])
@jwt_required()
def generate_pdf_api():
    user_email = get_jwt_identity()
    data = request.get_json()
    report_ids = data.get('report_ids')

    if not report_ids or not isinstance(report_ids, list):
        return jsonify({"success": False, "message": "No report IDs provided or invalid format."}), 400

    app_logger.info(f"User {user_email} requested to generate PDF for reports {report_ids}")

    reports_to_pdf = fetch_reports_by_ids(report_ids)

    if not reports_to_pdf:
        app_logger.warning(f"No reports found for the provided IDs during PDF generation request: {report_ids}")
        return jsonify({"success": False, "message": "No reports found for the provided IDs."}), 404

    # Build HTML content for the PDF
    html_content_parts = [
        "<!DOCTYPE html>",
        "<html lang='es'>",
        "<head>",
        "<meta charset='UTF-8'>",
        "<title>Reportes de Incidencias</title>",
        "<style>",
        "body { font-family: Arial, sans-serif; margin: 20px; color: #333; }",
        ".report-container { border: 1px solid #e2e8f0; border-radius: 8px; padding: 15px; margin-bottom: 20px; background-color: #f8fafc; page-break-inside: avoid; }",
        "h1 { color: #1e3a8a; text-align: center; margin-bottom: 30px; }",
        "h3 { color: #4a5568; margin-top: 0; border-bottom: 1px solid #cbd5e0; padding-bottom: 5px; margin-bottom: 15px; }",
        "p { margin: 5px 0; line-height: 1.5; }",
        "strong { color: #555; }",
        ".attachment-section { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }",
        ".attachment-item { margin-bottom: 10px; text-align: center; }",
        ".attachment-item img { max-width: 150px; height: auto; object-fit: contain; border-radius: 4px; border: 1px solid #ccc; }",
        ".attachment-item a { text-decoration: none; color: #2563eb; }",
        "@page { size: A4; margin: 2cm; }", # Define A4 size and margins for PDF
        "</style>",
        "</head>",
        "<body>",
        "<h1>Reportes de Incidencias Seleccionados</h1>"
    ]

    for i, report in enumerate(reports_to_pdf):
        html_content_parts.append(f"<div class='report-container'>")
        html_content_parts.append(f"<h3>Reporte {i+1} (ID: {report['id']})</h3>")
        html_content_parts.append(f"<p><strong>Título:</strong> {report['title']}</p>")
        html_content_parts.append(f"<p><strong>Enviado por:</strong> {report['submittedBy']} el {report['dateSubmitted']}</p>")
        
        for key, value in report['data'].items():
            display_value = value if value and str(value).strip() != 'N/A' and str(value).strip() != 'None' else 'No especificado'
            
            if key == 'URLs de Imágenes o PDFs' and display_value != 'No especificado':
                # Split URLs by newline, comma, or space, similar to frontend, and filter out empty strings
                urls = re.split(r'[\n, ]+', display_value)
                urls = [url.strip() for url in urls if url.strip()]

                if urls:
                    html_content_parts.append(f"<p><strong>Archivos Adjuntos:</strong></p><div class='attachment-section'>")
                    for url in urls:
                        lower_url = url.lower()
                        if lower_url.endswith(('.jpeg', '.jpg', '.png', '.gif', '.webp')):
                            html_content_parts.append(f"""
                                <div class='attachment-item'>
                                    <img src="{url}" alt="Imagen del reporte">
                                    <p style="font-size: 0.8em; color: #555; margin-top: 5px;">{os.path.basename(url)}</p>
                                </div>
                            """)
                        elif lower_url.endswith('.pdf'):
                            html_content_parts.append(f"""
                                <div class='attachment-item'>
                                    <p>PDF: <a href="{url}" target="_blank">{os.path.basename(url)}</a></p>
                                </div>
                            """)
                        else:
                            html_content_parts.append(f"""
                                <div class='attachment-item'>
                                    <p>Archivo: <a href="{url}" target="_blank">{os.path.basename(url)}</a></p>
                                </div>
                            """)
                    html_content_parts.append(f"</div>") # Close attachment-section
            else:
                # Replace newlines with <br> for multi-line text
                cleaned_value = str(display_value).replace('\n', '<br>')
                html_content_parts.append(f"<p><strong>{key}:</strong> {cleaned_value}</p>")
        
        html_content_parts.append(f"</div>") # Close report-container

    html_content_parts.append("</body></html>")
    full_html_content = "\n".join(html_content_parts)

    try:
        pdf_bytes = HTML(string=full_html_content).write_pdf()
        app_logger.info(f"Successfully generated PDF for {len(reports_to_pdf)} reports.")
        
        # Determine filename based on number of reports
        if len(reports_to_pdf) == 1:
            filename = f"reporte_{reports_to_pdf[0]['id']}.pdf"
        else:
            filename = f"reportes_seleccionados.pdf"

        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment;filename={filename}'}
        )
    except Exception as e:
        app_logger.error(f"Error generating PDF: {e}", exc_info=True)
        return jsonify({"success": False, "message": f"Error al generar el PDF: {e}"}), 500


@app.route('/logout')
def logout():
    response = redirect(os.environ.get('LOGIN_SERVICE_URL', '/'))
    unset_jwt_cookies(response)
    flash("Has cerrado sesión exitosamente.", "success")
    return response

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app_logger.info(f"Starting Flask app on port {port}")
    app.run(debug=True, host='0.0.0.0', port=port)