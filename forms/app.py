import os
import logging
import traceback
from flask import Flask, render_template, request, redirect, flash, jsonify, url_for, send_from_directory
from flask_jwt_extended import JWTManager, get_jwt_identity, jwt_required, unset_jwt_cookies, get_jwt
from google.cloud import storage, secretmanager
from werkzeug.utils import secure_filename
from datetime import timedelta, datetime
import psycopg2
import psycopg2.extras
import uuid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
import ssl
import socket
from google.api_core.exceptions import NotFound

# --- Logger Setup ---
logging.basicConfig(level=logging.INFO)
app_logger = logging.getLogger('app')

# --- Flask App Setup ---
app = Flask(__name__)
GCS_BUCKET_NAME = 'smt-uploads'

def configure_app(app):
    is_production = os.getenv("K_SERVICE") is not None

    app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'forms-flask-secret-key')
    app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'jwt-secret-key')
    app.config['BASE_URL'] = os.environ.get('BASE_URL', '/')
    
    # External URLs for user navigation
    app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com')
    app.config['LANDING_SERVICE_URL'] = os.environ.get('LANDING_SERVICE_URL', 'https://landing.secapp.tzolkintech.com')
    app.config['DASHBOARD_SERVICE_URL'] = os.environ.get('DASHBOARD_SERVICE_URL', 'https://dashboard.secapp.tzolkintech.com')
    app.config['VIEWER_SERVICE_URL'] = os.environ.get('VIEWER_SERVICE_URL', 'https://viewer.secapp.tzolkintech.com')

    # NEW: Internal URLs for cost-free service communication
    app.config['INTERNAL_LOGIN_SERVICE_URL'] = os.environ.get('INTERNAL_LOGIN_SERVICE_URL', 'https://login-24309643178.us-central1.run.app')
    app.config['INTERNAL_LANDING_SERVICE_URL'] = os.environ.get('INTERNAL_LANDING_SERVICE_URL', 'https://landing-24309643178.us-central1.run.app')
    app.config['INTERNAL_DASHBOARD_SERVICE_URL'] = os.environ.get('INTERNAL_DASHBOARD_SERVICE_URL', 'https://dashboard-24309643178.us-central1.run.app')
    app.config['INTERNAL_VIEWER_SERVICE_URL'] = os.environ.get('INTERNAL_VIEWER_SERVICE_URL', 'https://viewer-24309643178.us-central1.run.app')

    # JWT settings (UNCHANGED - maintains cookie sharing)
    app.config['JWT_TOKEN_LOCATION'] = ['cookies']
    app.config['JWT_ACCESS_COOKIE_PATH'] = '/'
    app.config['JWT_COOKIE_SECURE'] = is_production
    app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
    app.config['JWT_ACCESS_COOKIE_NAME'] = 'access_token_cookie'
    app.config['JWT_COOKIE_CSRF_PROTECT'] = False
    app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', None)

    # Database and email config (UNCHANGED)
    app.config['DB_HOST'] = os.environ.get('DB_HOST')
    app.config['DB_NAME'] = os.environ.get('DB_NAME')
    app.config['DB_USER'] = os.environ.get('DB_USER')
    app.config['DB_PASSWORD'] = os.environ.get('DB_PASSWORD')
    app.config['DB_PORT'] = os.environ.get('DB_PORT', '5432')

    app.config['SMTP_SERVER'] = os.environ.get('SMTP_SERVER', 'tzolkintech.com')
    app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', 587))
    app.config['SMTP_USE_TLS'] = os.environ.get('SMTP_USE_TLS', 'true').lower() == 'true'
    app.config['EMAIL_USERNAME'] = os.environ.get('EMAIL_USERNAME', 'no-reply@tzolkintech.com')
    app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL', 'rcanton@tzolkintech.com')
    app.config['GCP_PROJECT_ID'] = os.environ.get('GCP_PROJECT', os.environ.get('GOOGLE_CLOUD_PROJECT'))
    app.config['EMAIL_PASSWORD_SECRET_NAME'] = os.environ.get('EMAIL_PASSWORD_SECRET', 'admin-email-pass')
    app.config['CC_EMAIL'] = os.environ.get('CC_EMAIL', 'alvaro.montalvo@gmail.com')

    app_logger.info(f"Forms service configured - Production: {is_production}")

configure_app(app)

jwt = JWTManager(app)
app_logger.info("JWT configured successfully")

# --- JWT Error Handlers for Automatic Redirect ---
@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_payload):
    user_email = jwt_payload.get('sub', 'unknown')
    app_logger.info(f"JWT token expired for user {user_email}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.invalid_token_loader
def invalid_token_callback(error_string):
    app_logger.info(f"Invalid JWT token encountered: {error_string}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.unauthorized_loader
def unauthorized_callback(error_string):
    app_logger.info(f"No JWT token found: {error_string}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.revoked_token_loader
def revoked_token_callback(jwt_header, jwt_payload):
    user_email = jwt_payload.get('sub', 'unknown')
    app_logger.info(f"Revoked JWT token for user {user_email}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.needs_fresh_token_loader
def needs_fresh_token_callback(jwt_header, jwt_payload):
    user_email = jwt_payload.get('sub', 'unknown')
    app_logger.info(f"Fresh token required for user {user_email}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

import urllib.parse as urlparse

def get_db_connection():
    db_url = os.getenv('DATABASE_URL')
    if not db_url:
        app_logger.critical("DATABASE_URL environment variable not set. Exiting.")
        raise Exception("DATABASE_URL environment variable not set")

    urlparse.uses_netloc.append('postgres')
    parsed_url = urlparse.urlparse(db_url)
    query = dict(urlparse.parse_qsl(parsed_url.query))

    try:
        conn = psycopg2.connect(
            dbname=parsed_url.path[1:],
            user=parsed_url.username,
            password=parsed_url.password,
            host=query.get('host', parsed_url.hostname),
            port=query.get('port', parsed_url.port or '5432')
        )
        app_logger.debug("Database connection successful.")
        return conn
    except Exception as e:
        app_logger.error(f"Database connection error: {e}", exc_info=True)
        raise

def get_user_details(user_email):
    """Helper function to get user details from JWT claims and database"""
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
        
        # Optionally fetch from database as fallback
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT name FROM users WHERE email = %s", (user_email,))
            result = cur.fetchone()
            if result and result[0]:
                user_name = result[0]
            cur.close()
        except Exception as e:
            app_logger.warning(f"Could not fetch user from database: {e}")
        finally:
            if conn:
                conn.close()
        
        return {
            'name': user_name,
            'is_admin': is_admin,
            'email': user_email
        }
    except Exception as e:
        app_logger.error(f"Error getting user details: {e}", exc_info=True)
        return None
    
def upload_file_to_gcs(file, bucket_name):
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    unique_filename = f"{uuid.uuid4()}_{secure_filename(file.filename)}"
    blob = bucket.blob(unique_filename)
    blob.upload_from_file(file, content_type=file.content_type)
    return f"https://storage.googleapis.com/{bucket.name}/{blob.name}"

# --- Secret Manager Helper ---
def get_secret_value(secret_name):
    project_id = app.config.get('GCP_PROJECT_ID')
    if not project_id:
        app_logger.error(f"Cannot retrieve secret '{secret_name}': GCP_PROJECT_ID is not set.")
        raise ValueError(f"GCP_PROJECT_ID is required to access Secret Manager for '{secret_name}'.")

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": name})
        secret_value = response.payload.data.decode("UTF-8")
        app_logger.info(f"Successfully retrieved secret: {secret_name}")
        return secret_value
    except NotFound:
        app_logger.error(f"Secret '{secret_name}' not found in project '{project_id}'.")
        raise ValueError(f"Secret '{secret_name}' not found.")
    except Exception as e:
        app_logger.error(f"Error accessing secret '{secret_name}': {e}", exc_info=True)
        raise RuntimeError(f"Failed to retrieve secret '{secret_name}'.") from e

# --- Email Functions ---
def get_email_password():
    password = os.environ.get('EMAIL_PASSWORD')
    if password:
        app_logger.info("Using email password from environment variable.")
        return password
    
    project_id = app.config.get('GCP_PROJECT_ID')
    secret_name = app.config.get('EMAIL_PASSWORD_SECRET_NAME')
    
    if not project_id:
        app_logger.error("GCP_PROJECT_ID is not configured. Cannot retrieve email password from Secret Manager.")
        return None
    if not secret_name:
        app_logger.error("EMAIL_PASSWORD_SECRET_NAME is not configured. Cannot retrieve email password from Secret Manager.")
        return None

    try:
        with app.app_context():
            secret_value = get_secret_value(secret_name)
        app_logger.info("Successfully retrieved email password from Secret Manager.")
        return secret_value
    except Exception as e:
        app_logger.warning(f"Could not retrieve email password from Secret Manager: {e}", exc_info=True)
        return None

def send_email(to_emails, subject, body, is_html=False, cc_emails=None):
    email_username = app.config.get('EMAIL_USERNAME')
    smtp_server = app.config.get('SMTP_SERVER')
    smtp_port = app.config.get('SMTP_PORT')
    
    email_password = get_email_password()

    if not all([email_username, email_password, smtp_server, smtp_port]):
        app_logger.error(f"Email configuration incomplete. Missing: "
                         f"sender_email={bool(email_username)}, "
                         f"password={bool(email_password)}, "
                         f"smtp_server={bool(smtp_server)}, "
                         f"smtp_port={bool(smtp_port)}. Skipping email send.")
        return False
    
    if isinstance(to_emails, str):
        to_emails = [to_emails]

    if isinstance(cc_emails, str):
        cc_emails = [cc_emails]
    elif cc_emails is None:
        cc_emails = []

    recipients = to_emails + cc_emails
    
    app_logger.info(f"Attempting to send email to {', '.join(to_emails)} (CC: {', '.join(cc_emails) if cc_emails else 'None'}) via {smtp_server}:{smtp_port} from {email_username}.")
    try:
        msg = MIMEMultipart()
        msg['From'] = email_username
        msg['To'] = ", ".join(to_emails)
        if cc_emails:
            msg['Cc'] = ", ".join(cc_emails)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html' if is_html else 'plain'))

        server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
        server.starttls()
        server.login(email_username, email_password)
        server.send_message(msg)
        server.quit()
        app_logger.info(f"Email sent successfully to {', '.join(to_emails)}.")
        return True

    except smtplib.SMTPAuthenticationError:
        app_logger.error(f"SMTP Authentication Error: Check sender email/password for {email_username}.", exc_info=True)
        return False
    except smtplib.SMTPServerDisconnected:
        app_logger.error(f"SMTP Server Disconnected: Server {smtp_server}:{smtp_port} disconnected unexpectedly.", exc_info=True)
        return False
    except socket.timeout:
        app_logger.error(f"SMTP Connection Timeout: Could not connect to {smtp_server}:{smtp_port}. Check network connectivity and firewall rules.", exc_info=True)
        return False
    except Exception as e:
        app_logger.error(f"An unexpected error occurred while sending email to {', '.join(to_emails)}: {e}", exc_info=True)
        return False
def send_report_notification(user_email, user_name, fields):
    subject = f"Nuevo Reporte de Incidencia - {fields.get('fecha_incidente')}"

    # Get propiedad name from database
    propiedad_name = "No especificado"
    if fields.get('propiedad'):
        try:
            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                cur.execute("SELECT nombre FROM propiedades WHERE id_propiedad = %s", (fields.get('propiedad'),))
                result = cur.fetchone()
                if result:
                    propiedad_name = result[0]
                cur.close()
                conn.close()
        except Exception as e:
            app_logger.error(f"Error getting propiedad name for email: {e}")

    # Generate HTML for attachments
    attachments_html = ""
    image_urls_string = fields.get('imagenes_pdfs')
    if image_urls_string:
        urls = image_urls_string.strip().split('\n')
        attachments_html += "<div style='display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px;'>"
        for url in urls:
            url = url.strip()
            if url:
                lower_url = url.lower()
                filename = os.path.basename(url)
                if lower_url.endswith(('.jpeg', '.jpg', '.png', '.gif', '.webp')):
                    attachments_html += f"""
                        <div style='margin-bottom: 10px; text-align: center;'>
                            <a href="{url}" target="_blank" style="text-decoration: none;">
                                <img src="{url}" alt="Imagen del reporte" style="max-width: 200px; height: auto; border-radius: 4px; border: 1px solid #ccc;">
                            </a>
                            <p style="font-size: 0.8em; color: #555; margin-top: 5px;">{filename}</p>
                        </div>
                    """
                elif lower_url.endswith('.pdf'):
                    attachments_html += f'<div style="margin-bottom: 10px;"><p style="margin: 0;">PDF: <a href="{url}" target="_blank" style="color: #2563eb; text-decoration: none;">{filename}</a></p></div>'
                else:
                    attachments_html += f'<div style="margin-bottom: 10px;"><p style="margin: 0;">Archivo: <a href="{url}" target="_blank" style="color: #2563eb; text-decoration: none;">{filename}</a></p></div>'
        attachments_html += "</div>"
    else:
        attachments_html = "Ninguno"

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #2563eb;">Nuevo Reporte de Incidencia - SMT SecApp</h2>
    <div style="background-color: #f8fafc; padding: 20px; border-radius: 8px; margin: 20px 0;">
        <p><strong>Nombre del Reportante:</strong> {user_name}</p>
        <p><strong>Email:</strong> {user_email}</p>
        <p><strong>Propiedad:</strong> {propiedad_name}</p>
        <p><strong>Fecha del Incidente:</strong> {fields.get('fecha_incidente')}</p>
        <p><strong>Hora del Incidente:</strong> {fields.get('hora_incidente')}</p>
        <p><strong>Descripción:</strong> {fields.get('descripcion_incidente')}</p>
        <p><strong>Ubicación:</strong> {fields.get('direccion')}</p>
        <p><strong>Valor Aproximado:</strong> {fields.get('valor_aproximado') or 'No especificado'}</p>
        <p><strong>Archivos Adjuntos:</strong></p>
        {attachments_html}
    </div>
    </div>
    </body>
    </html>
    """

    admin_email = app.config.get('ADMIN_EMAIL', 'no-reply@tzolkintech.com')
    cc_email = app.config.get('CC_EMAIL', 'alvaro.montalvo@gmail.com')

    admin_send_success = send_email(to_emails=admin_email, subject=subject, body=html_body, is_html=True, cc_emails=cc_email)
    
    user_send_success = send_email(to_emails=user_email, subject="Confirmación de Reporte - SMT SecApp", body=html_body, is_html=True)

    if not admin_send_success:
        app_logger.error("Failed to send report notification to admin.")
    if not user_send_success:
        app_logger.error("Failed to send report confirmation to user.")
    
    return admin_send_success and user_send_success

def create_tables_if_not_exists():
    """
    Creates necessary tables including 'reportes_incidentes', 'control_accesos' and lookup tables.
    """
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        app_logger.info("Checking and creating necessary tables...")

        # Create reportes_incidentes table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reportes_incidentes (
                id_reporte_incidente SERIAL PRIMARY KEY,
                id_tipo_incidencia INT NOT NULL,
                id_tipo_cliente INT NOT NULL,
                id_lugar_incidente INT NOT NULL,
                id_propiedad INT,
                descripcion_zona_comun TEXT,
                fecha_incidente DATE NOT NULL,
                hora_incidente TIME NOT NULL,
                descripcion_incidente TEXT NOT NULL,
                valor_aproximado VARCHAR(255),
                pertenencias_sustraidas TEXT,
                nombre_persona VARCHAR(255),
                telefono_persona VARCHAR(20),
                numero_identidad_persona VARCHAR(50),
                numero_local VARCHAR(50),
                direccion VARCHAR(255),
                imagenes_pdfs TEXT,
                id_supervisor INT,
                creado_en TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                user_email VARCHAR(255) NOT NULL,
                firma_usuario TEXT,
                FOREIGN KEY (id_tipo_incidencia) REFERENCES tipo_incidencia(id_tipo_incidencia),
                FOREIGN KEY (id_tipo_cliente) REFERENCES tipo_cliente(id_tipo_cliente),
                FOREIGN KEY (id_lugar_incidente) REFERENCES lugar_incidente(id_lugar_incidente),
                FOREIGN KEY (id_propiedad) REFERENCES propiedades(id_propiedad),
                FOREIGN KEY (id_supervisor) REFERENCES supervisor(id_supervisor)
            );
        """)
        conn.commit()
        app_logger.info("Table 'reportes_incidentes' checked/created.")

        # Create control_accesos table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS control_accesos (
                id_control_acceso SERIAL PRIMARY KEY,
                fecha DATE,
                hora TIME,
                sitio VARCHAR(255),
                punto_de_acceso VARCHAR(255),
                usuario_visitante VARCHAR(255),
                id_usuario_documento VARCHAR(255),
                rol_del_usuario VARCHAR(255),
                id_acceso VARCHAR(255),
                accion VARCHAR(255),
                motivo_de_ingreso TEXT,
                autorizacion VARCHAR(3),
                brecha_detectada VARCHAR(255),
                evidencia VARCHAR(255),
                responsable_del_control VARCHAR(255),
                observaciones TEXT,
                brecha_por_personas VARCHAR(500),
                brecha_por_procedimiento VARCHAR(500),
                brecha_por_tecnologia_equipos VARCHAR(500),
                brecha_por_seguridad_fisica VARCHAR(500),
                accion_inmediata_tomada TEXT,
                accion_correctiva_recomendada TEXT,
                responsable_asignado VARCHAR(255),
                fecha_limite_de_cierre DATE,
                estado VARCHAR(255),
                submitted_by_email VARCHAR(255),
                creado_en TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                firma_usuario TEXT
            );
        """)
        conn.commit()
        app_logger.info("Table 'control_accesos' checked/created.")

        # Create mantenimiento_seguridad_fisica table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mantenimiento_seguridad_fisica (
                id_mantenimiento SERIAL PRIMARY KEY,
                fecha DATE,
                hora TIME,
                sitio VARCHAR(255),
                equipo VARCHAR(255),
                id_equipo_serial VARCHAR(255),
                tecnico_responsable VARCHAR(255),
                tipo_servicio VARCHAR(255),
                actividad_realizada TEXT,
                resultado VARCHAR(255),
                downtime_horas NUMERIC(5, 2),
                repuestos_usados VARCHAR(3),
                tipo_alerta_generada VARCHAR(255),
                observaciones TEXT,
                descripcion_alerta_critica TEXT,
                accion_inmediata_critica TEXT,
                accion_correctiva_recomendada TEXT,
                responsable_asignado_critica VARCHAR(255),
                fecha_limite_cierre_critica DATE,
                estado_critica VARCHAR(255),
                submitted_by_email VARCHAR(255),
                creado_en TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                firma_usuario TEXT
            );
        """)
        conn.commit()
        app_logger.info("Table 'mantenimiento_seguridad_fisica' checked/created.")

        # Create satisfaccion_cliente table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS satisfaccion_cliente (
                id_encuesta SERIAL PRIMARY KEY,
                empresa_sitio VARCHAR(255),
                fecha_encuesta DATE,
                sitio_local VARCHAR(255),
                cliente_encargado VARCHAR(255),
                cargo_encuestado VARCHAR(255),
                puntuacion_presencia_personal INTEGER,
                puntuacion_tiempo_respuesta INTEGER,
                puntuacion_funcionamiento_sistemas INTEGER,
                puntuacion_seguridad_parqueaderos INTEGER,
                puntuacion_seguridad_areas_comunes INTEGER,
                puntuacion_comunicacion_informacion INTEGER,
                puntuacion_confianza_general INTEGER,
                riesgo_detectado TEXT,
                novedades_reportadas TEXT,
                calificacion_global_nps INTEGER,
                recomendaria_servicio VARCHAR(3),
                observaciones_cliente TEXT,
                submitted_by_email VARCHAR(255),
                creado_en TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                firma_usuario TEXT
            );
        """)
        conn.commit()
        app_logger.info("Table 'satisfaccion_cliente' checked/created.")

        # Create supervision_puesto table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS supervision_puesto (
                id_supervision SERIAL PRIMARY KEY,
                fecha_hora TIMESTAMP,
                turno VARCHAR(255),
                supervisor VARCHAR(255),
                ruta VARCHAR(255),
                placa_vehiculo VARCHAR(255),
                km_inicial INTEGER,
                km_final INTEGER,
                cliente VARCHAR(255),
                direccion VARCHAR(255),
                horario_servicio VARCHAR(255),
                tipo_servicio VARCHAR(255),
                nombre_guardia VARCHAR(255),
                documento_guardia VARCHAR(255),
                fecha_inicio_servicio_guardia DATE,
                serie_arma VARCHAR(255),
                cantidad_municion INTEGER,
                constancia_induccion VARCHAR(255),
                conoce_consignas VARCHAR(255),
                horario_claro VARCHAR(255),
                asistencia_puntualidad VARCHAR(255),
                presentacion_uniforme VARCHAR(255),
                estado_limpieza_puesto VARCHAR(255),
                equipamiento_completo VARCHAR(255),
                cumplimiento_ordenes VARCHAR(255),
                estado_bitacora VARCHAR(255),
                observaciones_novedades TEXT,
                submitted_by_email VARCHAR(255),
                creado_en TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                firma_supervisor TEXT,
                rol_aplicador VARCHAR(255),
                firma_guardia TEXT
            );
        """)
        conn.commit()
        app_logger.info("Table 'supervision_puesto' checked/created.")

        # Create informe_novedades_disciplinario table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS informe_novedades_disciplinario (
                id_informe SERIAL PRIMARY KEY,
                realizado_por_nombre VARCHAR(255),
                realizado_por_cargo VARCHAR(255),
                fecha DATE,
                hora TIME,
                dirigido_a VARCHAR(255),
                empleado_nombre VARCHAR(255),
                empleado_documento VARCHAR(255),
                empleado_cargo VARCHAR(255),
                cliente VARCHAR(255),
                puesto VARCHAR(255),
                tipo_novedad TEXT,
                sitio_ocurrencia TEXT,
                descripcion_novedad TEXT,
                otras_personas_involucradas TEXT,
                anexos TEXT,
                submitted_by_email VARCHAR(255),
                creado_en TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                firma_realizado_por TEXT,
                firma_recibido_revisado_por TEXT
            );
        """)
        conn.commit()
        app_logger.info("Table 'informe_novedades_disciplinario' checked/created.")
        
        # Create log_de_patrullas table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS log_de_patrullas (
                id_patrulla SERIAL PRIMARY KEY,
                id_guardia_nombre_guardia VARCHAR(255),
                sitio_ubicacion VARCHAR(255),
                id_patrulla_consecutivo VARCHAR(255),
                fecha DATE,
                hora_inicio TIME,
                hora_fin TIME,
                detalles_incidente TEXT,
                riesgo_detectado VARCHAR(255),
                nivel_riesgo VARCHAR(255),
                estado_patrulla VARCHAR(255),
                contexto_observaciones TEXT,
                firma_guardia TEXT,
                firma_supervisor TEXT,
                submitted_by_email VARCHAR(255),
                creado_en TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        app_logger.info("Table 'log_de_patrullas' checked/created.")

        # --- UPDATED: registro_de_capacitaciones table ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS registro_de_capacitaciones (
                id_capacitacion SERIAL PRIMARY KEY,
                cliente_instalacion VARCHAR(255),
                puesto_area_especifica VARCHAR(255),
                fecha_hora TIMESTAMP,
                rol_aplicador VARCHAR(255),
                turno VARCHAR(50),
                nombre_responsable VARCHAR(255),
                firma_responsable TEXT,
                nombre_capacitacion VARCHAR(255),
                objetivo_capacitacion TEXT,
                observaciones_retroalimentacion TEXT,
                lista_asistencia TEXT,
                practica_simulacro_realizado VARCHAR(50),
                nivel_comprension VARCHAR(100),
                recomendaciones TEXT,
                submitted_by_email VARCHAR(255),
                creado_en TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        app_logger.info("Table 'registro_de_capacitaciones' recreated with new structure.")


        # --- UPDATED: registro_y_acta_de_visita table ---
        cur.execute("""
            CREATE TABLE IF NOT EXISTS registro_y_acta_de_visita (
                id_visita SERIAL PRIMARY KEY,
                cliente_instalacion VARCHAR(255),
                puesto_area_especifica VARCHAR(255),
                fecha_hora TIMESTAMP,
                rol_aplicador VARCHAR(255),
                turno VARCHAR(50),
                visita_realizada_por VARCHAR(255),
                firma_visitante TEXT,
                motivo_visita TEXT,
                objetivo_reunion TEXT,
                actividades_realizadas TEXT,
                satisfaccion_cliente VARCHAR(50),
                comentarios_satisfaccion TEXT,
                compromisos_adquiridos TEXT,
                compromisos_responsable VARCHAR(255),
                compromisos_fecha_limite DATE,
                observaciones TEXT,
                persona_atendio VARCHAR(255),
                cargo_atendio VARCHAR(255),
                telefono_contacto VARCHAR(50),
                firma_participante_cliente TEXT,
                submitted_by_email VARCHAR(255),
                creado_en TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        app_logger.info("Table 'registro_y_acta_de_visita' checked/created with new structure.")


        # Create orden_mantenimiento table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS orden_mantenimiento (
                id_orden SERIAL PRIMARY KEY,
                cliente_instalacion VARCHAR(255),
                puesto_area VARCHAR(255),
                fecha_hora TIMESTAMP,
                rol_aplicador VARCHAR(255),
                turno VARCHAR(50),
                equipo VARCHAR(255),
                id_equipo_serial VARCHAR(255),
                nombre_tecnico VARCHAR(255),
                firma_tecnico TEXT,
                tipo_servicio VARCHAR(255),
                actividad_realizada TEXT,
                resultado_servicio TEXT,
                downtime_horas NUMERIC(5, 2),
                repuestos_usados BOOLEAN,
                tipo_alerta_generada VARCHAR(255),
                observaciones TEXT,
                tipo_servicio_clasificacion VARCHAR(255),
                resultado_clasificacion VARCHAR(255),
                tipo_alerta_clasificacion VARCHAR(255),
                descripcion_alerta TEXT,
                accion_inmediata TEXT,
                accion_correctiva_recomendada TEXT,
                responsable_asignado VARCHAR(255),
                fecha_limite_cierre DATE,
                estado VARCHAR(50),
                supervisor_seguridad VARCHAR(255),
                firma_supervisor_seguridad TEXT,
                submitted_by_email VARCHAR(255) NOT NULL,
                creado_en TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        app_logger.info("Table 'orden_mantenimiento' checked/created.")

        # Create planilla_de_rondas table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS planilla_de_rondas (
                id SERIAL PRIMARY KEY,
                cliente_instalacion VARCHAR(255),
                puesto_area_especifica VARCHAR(255),
                fecha_hora TIMESTAMP,
                rol_aplicador VARCHAR(255),
                turno VARCHAR(50),
                nombre_responsable VARCHAR(255),
                firma_responsable TEXT,
                punto_de_control TEXT,
                hora_programada TIME,
                hora_verificacion TIME,
                estado_punto VARCHAR(255),
                cumplimiento VARCHAR(50),
                novedades_relevantes TEXT,
                accion_inmediata TEXT,
                requerimiento_pendiente TEXT,
                firma_entrega_ronda TEXT,
                firma_recepcion_supervisor TEXT,
                submitted_by_email VARCHAR(255) NOT NULL,
                creado_en TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        app_logger.info("Table 'planilla_de_rondas' checked/created.")

    except Exception as e:
        app_logger.error(f"Error during database table creation: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

@app.route('/health')
def health():
    return "OK", 200

# FIXED: The root route now redirects to the selection page with proper logging
@app.route('/')
@jwt_required()
def root_redirect():
    app_logger.info("Root route accessed, redirecting to /select")
    return redirect('/select')

# ROUTE for the form selection page
@app.route('/select')
@jwt_required()
def select_form():
    user_email = get_jwt_identity()
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
    except Exception as e:
        app_logger.warning(f"Could not get JWT claims for {user_email}: {e}")
        user_name = user_email.split('@')[0]
        is_admin = False

    return render_template(
        'select_form.html',
        name=user_name,
        is_admin=is_admin,
        login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'),
        landing_service_url=app.config.get('LANDING_SERVICE_URL', '/'),
        dashboard_service_url=app.config.get('DASHBOARD_SERVICE_URL', '/'),
        viewer_service_url=app.config.get('VIEWER_SERVICE_URL', '/')
    )

# Debug route to check available routes
@app.route('/debug-routes')
def debug_routes():
    routes = []
    for rule in app.url_map.iter_rules():
        routes.append(f"{rule.rule} -> {rule.endpoint} ({', '.join(rule.methods)})")
    return "<br>".join(sorted(routes))

# The Reporte de Incidencia form
@app.route('/reporte_incidencia')
@jwt_required()
def reporte_incidencia():
    user_email = get_jwt_identity()
    
    # Get admin status from JWT claims
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
        app_logger.info(f"User {user_email} accessing forms (admin: {is_admin})")
    except Exception as e:
        app_logger.warning(f"Could not get JWT claims for {user_email}: {e}")
        user_name = user_email.split('@')[0]
        is_admin = False

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # Get user name from database as fallback
        cur.execute("SELECT name FROM users WHERE email = %s", (user_email,))
        result = cur.fetchone()
        if result and result[0]:
            user_name = result[0]

        # Load dropdown options
        cur.execute("SELECT id_tipo_incidencia AS id, nombre FROM tipo_incidencia ORDER BY nombre ASC")
        tipo_incidencia = cur.fetchall()

        cur.execute("SELECT id_tipo_cliente AS id, nombre FROM tipo_cliente ORDER BY nombre ASC")
        tipo_cliente = cur.fetchall()

        cur.execute("SELECT id_lugar_incidente AS id, nombre FROM lugar_incidente ORDER BY nombre ASC")
        lugar_incidente = cur.fetchall()

        cur.execute("SELECT id_supervisor AS id, nombre FROM supervisor ORDER BY nombre ASC")
        supervisor = cur.fetchall()

        # Load propiedades
        cur.execute("SELECT id_propiedad AS id, nombre FROM propiedades WHERE activa = TRUE ORDER BY nombre ASC")
        propiedades = cur.fetchall()
        app_logger.info(f"Loaded {len(propiedades)} propiedades for form")

        cur.close()
        return render_template(
            'form.html',
            tipo_incidencia=tipo_incidencia,
            tipo_cliente=tipo_cliente,
            lugar_incidente=lugar_incidente,
            supervisor=supervisor,
            propiedades=propiedades,
            name=user_name,
            is_admin=is_admin,
            login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'),
            landing_service_url=app.config.get('LANDING_SERVICE_URL', '/'),
            dashboard_service_url=app.config.get('DASHBOARD_SERVICE_URL', '/'),
            viewer_service_url=app.config.get('VIEWER_SERVICE_URL', '/')
        )
    except Exception as e:
        app_logger.error(f"Error rendering index page for {user_email}: {e}", exc_info=True)
        return render_template('error.html',
                               message="Error al cargar el formulario. Por favor, intente de nuevo más tarde.",
                               login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'))
    finally:
        if conn:
            conn.close()

# ROUTE for Planilla de Rondas form
@app.route('/planilla_de_rondas')
@jwt_required()
def planilla_de_rondas_form():
    user_email = get_jwt_identity()
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
        app_logger.info(f"User {user_email} accessing planilla de rondas form (admin: {is_admin})")
    except Exception as e:
        app_logger.warning(f"Could not get JWT claims for {user_email}: {e}")
        user_name = user_email.split('@')[0]
        is_admin = False

    return render_template(
        'planilla_de_rondas.html',
        name=user_name,
        is_admin=is_admin,
        login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'),
        landing_service_url=app.config.get('LANDING_SERVICE_URL', '/'),
        dashboard_service_url=app.config.get('DASHBOARD_SERVICE_URL', '/'),
        viewer_service_url=app.config.get('VIEWER_SERVICE_URL', '/')
    )

# ROUTE for the Mantenimiento de Seguridad Física form
@app.route('/mantenimiento_seguridad_fisica')
@jwt_required()
def mantenimiento_seguridad_fisica_form():
    user_email = get_jwt_identity()
    
    # Get admin status from JWT claims
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
        app_logger.info(f"User {user_email} accessing mantenimiento seguridad fisica form (admin: {is_admin})")
    except Exception as e:
        app_logger.warning(f"Could not get JWT claims for {user_email}: {e}")
        user_name = user_email.split('@')[0]
        is_admin = False

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # Get user name from database as fallback
        cur.execute("SELECT name FROM users WHERE email = %s", (user_email,))
        result = cur.fetchone()
        if result and result[0]:
            user_name = result[0]

        cur.close()
        
        return render_template(
            'mantenimiento_seguridad_fisica.html',
            name=user_name,
            is_admin=is_admin,
            login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'),
            landing_service_url=app.config.get('LANDING_SERVICE_URL', '/'),
            dashboard_service_url=app.config.get('DASHBOARD_SERVICE_URL', '/'),
            viewer_service_url=app.config.get('VIEWER_SERVICE_URL', '/')
        )
    except Exception as e:
        app_logger.error(f"Error rendering mantenimiento seguridad fisica page for {user_email}: {e}", exc_info=True)
        return render_template('error.html',
                               message="Error al cargar el formulario. Por favor, intente de nuevo más tarde.",
                               login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'))
    finally:
        if conn:
            conn.close()

# ROUTE for submitting the Mantenimiento de Seguridad Física form
@app.route('/submit_mantenimiento_seguridad_fisica', methods=['POST'])
@jwt_required()
def submit_mantenimiento_seguridad_fisica():
    user_email = get_jwt_identity()
    conn = None
    try:
        # Get all form fields from the request
        form_data = {
            'fecha': request.form.get('fecha'),
            'hora': request.form.get('hora'),
            'sitio': request.form.get('sitio'),
            'equipo': request.form.get('equipo'),
            'id_equipo_serial': request.form.get('id_equipo_serial'),
            'tecnico_responsable': request.form.get('tecnico_responsable'),
            'tipo_servicio': request.form.get('tipo_servicio'),
            'actividad_realizada': request.form.get('actividad_realizada'),
            'resultado': request.form.get('resultado'),
            'downtime_horas': request.form.get('downtime_horas'),
            'repuestos_usados': request.form.get('repuestos_usados'),
            'tipo_alerta_generada': request.form.get('tipo_alerta_generada'),
            'observaciones': request.form.get('observaciones'),
            'descripcion_alerta_critica': request.form.get('descripcion_alerta_critica'),
            'accion_inmediata_critica': request.form.get('accion_inmediata_critica'),
            'accion_correctiva_recomendada': request.form.get('accion_correctiva_recomendada'),
            'responsable_asignado_critica': request.form.get('responsable_asignado_critica'),
            'fecha_limite_cierre_critica': request.form.get('fecha_limite_cierre_critica'),
            'estado_critica': request.form.get('estado_critica'),
            'firma_usuario': request.form.get('firma_usuario'),
            'submitted_by_email': user_email
        }
        
        conn = get_db_connection()
        cur = conn.cursor()

        # Construct the SQL INSERT statement
        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO mantenimiento_seguridad_fisica ({columns}) VALUES ({placeholders})"
        
        # Execute the query
        cur.execute(sql, list(form_data.values()))
        
        conn.commit()
        cur.close()

        flash('Reporte de Mantenimiento de Seguridad Física enviado exitosamente!', 'success')
        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting mantenimiento seguridad fisica report: {e}", exc_info=True)
        flash('Hubo un error al enviar el reporte de mantenimiento de seguridad física.', 'danger')
        return redirect(url_for('mantenimiento_seguridad_fisica_form'))
    finally:
        if conn:
            conn.close()

# ROUTE for the Medicion de Experiencia del Cliente form
@app.route('/medicion_experiencia_cliente')
@jwt_required()
def medicion_experiencia_cliente_form():
    user_email = get_jwt_identity()
    
    # Get admin status from JWT claims
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
        app_logger.info(f"User {user_email} accessing medicion_experiencia_cliente form (admin: {is_admin})")
    except Exception as e:
        app_logger.warning(f"Could not get JWT claims for {user_email}: {e}")
        user_name = user_email.split('@')[0]
        is_admin = False

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # Get user name from database as fallback
        cur.execute("SELECT name FROM users WHERE email = %s", (user_email,))
        result = cur.fetchone()
        if result and result[0]:
            user_name = result[0]

        cur.close()
        
        return render_template(
            'medicion_experiencia_cliente.html',
            name=user_name,
            is_admin=is_admin,
            login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'),
            landing_service_url=app.config.get('LANDING_SERVICE_URL', '/'),
            dashboard_service_url=app.config.get('DASHBOARD_SERVICE_URL', '/'),
            viewer_service_url=app.config.get('VIEWER_SERVICE_URL', '/')
        )
    except Exception as e:
        app_logger.error(f"Error rendering medicion_experiencia_cliente page for {user_email}: {e}", exc_info=True)
        return render_template('error.html',
                               message="Error al cargar el formulario. Por favor, intente de nuevo más tarde.",
                               login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'))
    finally:
        if conn:
            conn.close()

# ROUTE for submitting the Medicion de Experiencia del Cliente form
@app.route('/submit_medicion_experiencia_cliente', methods=['POST'])
@jwt_required()
def submit_medicion_experiencia_cliente():
    user_email = get_jwt_identity()
    conn = None
    try:
        # Get all form fields from the request
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area': request.form.get('puesto_area'),
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma_responsable'),
            'puntuacion_presencia_personal': request.form.get('puntuacion_presencia_personal'),
            'puntuacion_tiempo_respuesta': request.form.get('puntuacion_tiempo_respuesta'),
            'puntuacion_funcionamiento_sistemas': request.form.get('puntuacion_funcionamiento_sistemas'),
            'puntuacion_seguridad_parqueaderos': request.form.get('puntuacion_seguridad_parqueaderos'),
            'puntuacion_seguridad_areas_comunes': request.form.get('puntuacion_seguridad_areas_comunes'),
            'puntuacion_comunicacion_informacion': request.form.get('puntuacion_comunicacion_informacion'),
            'puntuacion_confianza_general': request.form.get('puntuacion_confianza_general'),
            'riesgo_detectado': request.form.get('riesgo_detectado'),
            'novedades_reportadas': request.form.get('novedades_reportadas'),
            'calificacion_global_nps': request.form.get('calificacion_global_nps'),
            'recomendaria_servicio': request.form.get('recomendaria_servicio'),
            'observaciones_cliente': request.form.get('observaciones_cliente'),
            'encuestado': request.form.get('encuestado'),
            'firma_encuestado': request.form.get('firma_encuestado'),
            'submitted_by_email': user_email
        }
        
        conn = get_db_connection()
        cur = conn.cursor()

        # Construct the SQL INSERT statement
        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO medicion_experiencia_cliente ({columns}) VALUES ({placeholders})"
        
        # Execute the query
        cur.execute(sql, list(form_data.values()))
        
        conn.commit()
        cur.close()

        flash('Medición de Experiencia del Cliente enviada exitosamente!', 'success')
        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting medicion_experiencia_cliente report: {e}", exc_info=True)
        flash('Hubo un error al enviar la encuesta de medición de experiencia del cliente.', 'danger')
        return redirect(url_for('medicion_experiencia_cliente_form'))
    finally:
        if conn:
            conn.close()

# ROUTE for the Supervisión de Puesto form
@app.route('/supervision_puesto')
@jwt_required()
def supervision_puesto_form():
    user_email = get_jwt_identity()
    
    # Get admin status from JWT claims
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
        app_logger.info(f"User {user_email} accessing supervision puesto form (admin: {is_admin})")
    except Exception as e:
        app_logger.warning(f"Could not get JWT claims for {user_email}: {e}")
        user_name = user_email.split('@')[0]
        is_admin = False

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # Get user name from database as fallback
        cur.execute("SELECT name FROM users WHERE email = %s", (user_email,))
        result = cur.fetchone()
        if result and result[0]:
            user_name = result[0]

        cur.close()
        
        return render_template(
            'supervision_puesto.html',
            name=user_name,
            is_admin=is_admin,
            login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'),
            landing_service_url=app.config.get('LANDING_SERVICE_URL', '/'),
            dashboard_service_url=app.config.get('DASHBOARD_SERVICE_URL', '/'),
            viewer_service_url=app.config.get('VIEWER_SERVICE_URL', '/')
        )
    except Exception as e:
        app_logger.error(f"Error rendering supervision puesto page for {user_email}: {e}", exc_info=True)
        return render_template('error.html',
                               message="Error al cargar el formulario. Por favor, intente de nuevo más tarde.",
                               login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'))
    finally:
        if conn:
            conn.close()

# ROUTE for submitting the Supervisión de Puesto form
@app.route('/submit_supervision_puesto', methods=['POST'])
@jwt_required()
def submit_supervision_puesto():
    user_email = get_jwt_identity()
    conn = None
    try:
        # Get all form fields from the request, including new ones
        form_data = {
            'fecha_hora': request.form.get('fecha_hora'), # UPDATED
            'turno': request.form.get('turno'),
            'supervisor': request.form.get('supervisor'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'ruta': request.form.get('ruta'),
            'placa_vehiculo': request.form.get('placa_vehiculo'),
            'km_inicial': request.form.get('km_inicial'),
            'km_final': request.form.get('km_final'),
            'cliente': request.form.get('cliente'),
            'direccion': request.form.get('direccion'),
            'horario_servicio': request.form.get('horario_servicio'),
            'tipo_servicio': request.form.get('tipo_servicio'),
            'nombre_guardia': request.form.get('nombre_guardia'),
            'documento_guardia': request.form.get('documento_guardia'),
            'fecha_inicio_servicio_guardia': request.form.get('fecha_inicio_servicio_guardia'),
            'serie_arma': request.form.get('serie_arma'),
            'cantidad_municion': request.form.get('cantidad_municion'),
            'constancia_induccion': request.form.get('constancia_induccion'),
            'conoce_consignas': request.form.get('conoce_consignas'),
            'horario_claro': request.form.get('horario_claro'),
            'asistencia_puntualidad': request.form.get('asistencia_puntualidad'),
            'presentacion_uniforme': request.form.get('presentacion_uniforme'),
            'estado_limpieza_puesto': request.form.get('estado_limpieza_puesto'),
            'equipamiento_completo': request.form.get('equipamiento_completo'),
            'cumplimiento_ordenes': request.form.get('cumplimiento_ordenes'),
            'estado_bitacora': request.form.get('estado_bitacora'),
            'observaciones_novedades': request.form.get('observaciones_novedades'),
            'firma_supervisor': request.form.get('firma_supervisor'),
            'firma_guardia': request.form.get('firma_guardia'),
            'submitted_by_email': user_email
        }
        
        conn = get_db_connection()
        cur = conn.cursor()

        # Construct the SQL INSERT statement with the updated columns
        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO supervision_puesto ({columns}) VALUES ({placeholders})"
        
        # Execute the query
        cur.execute(sql, list(form_data.values()))
        
        conn.commit()
        cur.close()

        flash('Hoja de Supervisión de Puesto enviada exitosamente!', 'success')
        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting supervision puesto report: {e}", exc_info=True)
        flash('Hubo un error al enviar la hoja de supervisión.', 'danger')
        return redirect(url_for('supervision_puesto_form'))
    finally:
        if conn:
            conn.close()


# ROUTE for the Informe de Novedades y Disciplinario form
@app.route('/informe_novedades_disciplinario')
@jwt_required()
def informe_novedades_disciplinario_form():
    user_email = get_jwt_identity()
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
        app_logger.info(f"User {user_email} accessing informe novedades y disciplinario form (admin: {is_admin})")
    except Exception as e:
        app_logger.warning(f"Could not get JWT claims for {user_email}: {e}")
        user_name = user_email.split('@')[0]
        is_admin = False

    return render_template(
        'informe_novedades_disciplinario.html',
        name=user_name,
        is_admin=is_admin,
        login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'),
        landing_service_url=app.config.get('LANDING_SERVICE_URL', '/'),
        dashboard_service_url=app.config.get('DASHBOARD_SERVICE_URL', '/'),
        viewer_service_url=app.config.get('VIEWER_SERVICE_URL', '/')
    )

# ROUTE for submitting the Informe de Novedades y Disciplinario form
@app.route('/submit_informe_novedades_disciplinario', methods=['POST'])
@jwt_required()
def submit_informe_novedades_disciplinario():
    user_email = get_jwt_identity()
    conn = None
    try:
        form_data = {
            'realizado_por_nombre': request.form.get('realizado_por_nombre'),
            'realizado_por_cargo': request.form.get('realizado_por_cargo'),
            'fecha': request.form.get('fecha'),
            'hora': request.form.get('hora'),
            'dirigido_a': request.form.get('dirigido_a'),
            'empleado_nombre': request.form.get('empleado_nombre'),
            'empleado_documento': request.form.get('empleado_documento'),
            'empleado_cargo': request.form.get('empleado_cargo'),
            'cliente': request.form.get('cliente'),
            'puesto': request.form.get('puesto'),
            'tipo_novedad': request.form.get('tipo_novedad'),
            'sitio_ocurrencia': request.form.get('sitio_ocurrencia'),
            'descripcion_novedad': request.form.get('descripcion_novedad'),
            'otras_personas_involucradas': request.form.get('otras_personas_involucradas'),
            'anexos': request.form.get('anexos'),
            'firma_realizado_por': request.form.get('firma_realizado_por'),
            'firma_recibido_revisado_por': request.form.get('firma_recibido_revisado_por'),
            'submitted_by_email': user_email
        }
        
        conn = get_db_connection()
        cur = conn.cursor()

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO informe_novedades_disciplinario ({columns}) VALUES ({placeholders})"
        
        cur.execute(sql, list(form_data.values()))
        
        conn.commit()
        cur.close()

        flash('Informe de Novedades y Disciplinario enviado exitosamente!', 'success')
        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting informe de novedades y disciplinario: {e}", exc_info=True)
        flash('Hubo un error al enviar el informe de novedades y disciplinario.', 'danger')
        return redirect(url_for('informe_novedades_disciplinario_form'))
    finally:
        if conn:
            conn.close()

# --- START: REPORTE UNICO DE INCIDENTE (DEFINITIVE FIX) ---

@app.route('/reporte_incidente', methods=['GET'])
@jwt_required()
def reporte_incidente_form():
    """
    FIXED: This function now correctly fetches user details from the JWT
    and passes all necessary data to the template, resolving the redirect loop
    and rendering the navigation bar correctly.
    """
    try:
        user_email = get_jwt_identity()
        
        # Get JWT claims
        try:
            claims = get_jwt()
            user_name = claims.get('name', user_email.split('@')[0])
            is_admin = claims.get('is_admin', False)
        except Exception as e:
            app_logger.warning(f"Could not get JWT claims for {user_email}: {e}")
            user_name = user_email.split('@')[0]
            is_admin = False
        
        # Optionally get user name from database as fallback
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT name FROM users WHERE email = %s", (user_email,))
            result = cur.fetchone()
            if result and result[0]:
                user_name = result[0]
            cur.close()
        except Exception as e:
            app_logger.warning(f"Could not fetch user from database: {e}")
        finally:
            if conn:
                conn.close()

        app_logger.info(f"User {user_email} accessing reporte_incidente form")
        
        # This now correctly passes all the data the template needs
        return render_template(
            'reporte_incidente.html',
            name=user_name,
            is_admin=is_admin,
            login_service_url=app.config.get('LOGIN_SERVICE_URL'),
            landing_service_url=app.config.get('LANDING_SERVICE_URL'),
            dashboard_service_url=app.config.get('DASHBOARD_SERVICE_URL'),
            viewer_service_url=app.config.get('VIEWER_SERVICE_URL')
        )
    except Exception as e:
        app_logger.error(f"Error in reporte_incidente_form: {e}", exc_info=True)
        flash('Ocurrió un error inesperado al cargar el formulario de incidente.', 'danger')
        return redirect(url_for('select_form'))

@app.route('/submit_incident_report', methods=['POST'])
@jwt_required()
def submit_incident_report():
    """Handles the submission of the new incident report form."""
    user_email = get_jwt_identity()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area_especifica': request.form.get('puesto_area_especifica'),
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma'), # Matches name="firma" in HTML
            'categoria': request.form.get('categoria'),
            'tipo_incidente': request.form.get('tipo_incidente'),
            'descripcion_incidente': request.form.get('descripcion'), # Target DB column
            'nivel_severidad': request.form.get('nivel_severidad'),
            'impacto': request.form.get('impacto'),
            'tiempo_resolucion_min': request.form.get('tiempo_resolucion_min'),
            'responsable_asignado': request.form.get('responsable_asignado'),
            'estado': request.form.get('estado'),
            'accion_inmediata': request.form.get('accion_inmediata'),
            'accion_correctiva_preventiva': request.form.get('accion_correctiva_preventiva'),
            'responsable_seguimiento': request.form.get('responsable_seguimiento'),
            'fecha_limite_cierre': request.form.get('fecha_limite_cierre'),
            'user_email': user_email
        }

        form_data = {k: v for k, v in form_data.items() if v is not None}
        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO reportes_incidentes ({columns}) VALUES ({placeholders})"
        
        cur.execute(sql, list(form_data.values()))
        conn.commit()
    except Exception as e:
        if conn: conn.rollback()
        app_logger.error(f"Error submitting incident report: {e}", exc_info=True)
        flash('Hubo un error al enviar el reporte de incidente.', 'danger')
        return redirect(url_for('reporte_incidente_form'))
    finally:
        if conn:
            cur.close()
            conn.close()

    flash('Reporte de incidente enviado exitosamente!', 'success')
    return redirect(url_for('success'))

# --- END: REPORTE UNICO DE INCIDENTE ---

# ROUTE for the Log de Patrullas form
@app.route('/log_de_patrullas')
@jwt_required()
def log_de_patrullas_form():
    user_email = get_jwt_identity()
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
        app_logger.info(f"User {user_email} accessing log de patrullas form (admin: {is_admin})")
    except Exception as e:
        app_logger.warning(f"Could not get JWT claims for {user_email}: {e}")
        user_name = user_email.split('@')[0]
        is_admin = False

    return render_template(
        'log_de_patrullas.html',
        name=user_name,
        is_admin=is_admin,
        login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'),
        landing_service_url=app.config.get('LANDING_SERVICE_URL', '/'),
        dashboard_service_url=app.config.get('DASHBOARD_SERVICE_URL', '/'),
        viewer_service_url=app.config.get('VIEWER_SERVICE_URL', '/')
    )

# ROUTE for submitting the Log de Patrullas form
@app.route('/submit_log_de_patrullas', methods=['POST'])
@jwt_required()
def submit_log_de_patrullas():
    user_email = get_jwt_identity()
    conn = None
    try:
        form_data = {
            'id_guardia_nombre_guardia': request.form.get('id_guardia_nombre_guardia'),
            'sitio_ubicacion': request.form.get('sitio_ubicacion'),
            'id_patrulla_consecutivo': request.form.get('id_patrulla_consecutivo'),
            'fecha': request.form.get('fecha'),
            'hora_inicio': request.form.get('hora_inicio'),
            'hora_fin': request.form.get('hora_fin'),
            'detalles_incidente': request.form.get('detalles_incidente'),
            'riesgo_detectado': request.form.get('riesgo_detectado'),
            'nivel_riesgo': request.form.get('nivel_riesgo'),
            'estado_patrulla': request.form.get('estado_patrulla'),
            'contexto_observaciones': request.form.get('contexto_observaciones'),
            'firma_guardia': request.form.get('firma_guardia'),
            'firma_supervisor': request.form.get('firma_supervisor'),
            'submitted_by_email': user_email
        }
        
        conn = get_db_connection()
        cur = conn.cursor()

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO log_de_patrullas ({columns}) VALUES ({placeholders})"
        
        cur.execute(sql, list(form_data.values()))
        
        conn.commit()
        cur.close()

        flash('Log de Patrulla enviado exitosamente!', 'success')
        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting log de patrulla: {e}", exc_info=True)
        flash('Hubo un error al enviar el log de patrulla.', 'danger')
        return redirect(url_for('log_de_patrullas_form'))
    finally:
        if conn:
            conn.close()

# ROUTE for the Registro de Capacitaciones form
@app.route('/registro_de_capacitaciones')
@jwt_required()
def registro_de_capacitaciones_form():
    user_email = get_jwt_identity()
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
        app_logger.info(f"User {user_email} accessing registro de capacitaciones form (admin: {is_admin})")
    except Exception as e:
        app_logger.warning(f"Could not get JWT claims for {user_email}: {e}")
        user_name = user_email.split('@')[0]
        is_admin = False

    return render_template(
        'registro_de_capacitaciones.html',
        name=user_name,
        is_admin=is_admin,
        login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'),
        landing_service_url=app.config.get('LANDING_SERVICE_URL', '/'),
        dashboard_service_url=app.config.get('DASHBOARD_SERVICE_URL', '/'),
        viewer_service_url=app.config.get('VIEWER_SERVICE_URL', '/')
    )

# --- UPDATED: ROUTE for submitting the Registro de Capacitaciones form ---
@app.route('/submit_registro_de_capacitaciones', methods=['POST'])
@jwt_required()
def submit_registro_de_capacitaciones():
    user_email = get_jwt_identity()
    conn = None
    try:
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area_especifica': request.form.get('puesto_area_especifica'),
            'fecha_hora': request.form.get('fecha_hora') or None,
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma_responsable'),
            'nombre_capacitacion': request.form.get('nombre_capacitacion'),
            'objetivo_capacitacion': request.form.get('objetivo_capacitacion'),
            'observaciones_retroalimentacion': request.form.get('observaciones_retroalimentacion'),
            'lista_asistencia': request.form.get('lista_asistencia'),
            'practica_simulacro_realizado': request.form.get('practica_simulacro_realizado'),
            'nivel_comprension': request.form.get('nivel_comprension'),
            'recomendaciones': request.form.get('recomendaciones'),
            'submitted_by_email': user_email
        }
        
        conn = get_db_connection()
        cur = conn.cursor()

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO registro_de_capacitaciones ({columns}) VALUES ({placeholders})"
        
        cur.execute(sql, list(form_data.values()))
        
        conn.commit()
        cur.close()

        flash('Registro de Capacitación enviado exitosamente!', 'success')
        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting registro de capacitacion: {e}", exc_info=True)
        flash('Hubo un error al enviar el registro de capacitación.', 'danger')
        return redirect(url_for('registro_de_capacitaciones_form'))
    finally:
        if conn:
            conn.close()

# ROUTE for the Registro y Acta de Visita form
@app.route('/registro_y_acta_de_visita')
@jwt_required()
def registro_y_acta_de_visita_form():
    user_email = get_jwt_identity()
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
        app_logger.info(f"User {user_email} accessing registro y acta de visita form (admin: {is_admin})")
    except Exception as e:
        app_logger.warning(f"Could not get JWT claims for {user_email}: {e}")
        user_name = user_email.split('@')[0]
        is_admin = False

    return render_template(
        'registro_y_acta_de_visita.html',
        name=user_name,
        is_admin=is_admin,
        login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'),
        landing_service_url=app.config.get('LANDING_SERVICE_URL', '/'),
        dashboard_service_url=app.config.get('DASHBOARD_SERVICE_URL', '/'),
        viewer_service_url=app.config.get('VIEWER_SERVICE_URL', '/')
    )

# --- UPDATED: ROUTE for submitting the Registro y Acta de Visita form ---
@app.route('/submit_registro_y_acta_de_visita', methods=['POST'])
@jwt_required()
def submit_registro_y_acta_de_visita():
    user_email = get_jwt_identity()
    conn = None
    try:
        # Get all form fields from the updated form
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area_especifica': request.form.get('puesto_area_especifica'),
            'fecha_hora': request.form.get('fecha_hora') or None,
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'visita_realizada_por': request.form.get('visita_realizada_por'),
            'firma_visitante': request.form.get('firma_visitante'),
            'motivo_visita': request.form.get('motivo_visita'),
            'objetivo_reunion': request.form.get('objetivo_reunion'),
            'actividades_realizadas': request.form.get('actividades_realizadas'),
            'satisfaccion_cliente': request.form.get('satisfaccion_cliente'),
            'comentarios_satisfaccion': request.form.get('comentarios_satisfaccion'),
            'compromisos_adquiridos': request.form.get('compromisos_adquiridos'),
            'compromisos_responsable': request.form.get('compromisos_responsable'),
            'compromisos_fecha_limite': request.form.get('compromisos_fecha_limite') or None,
            'observaciones': request.form.get('observaciones'),
            'persona_atendio': request.form.get('persona_atendio'),
            'cargo_atendio': request.form.get('cargo_atendio'),
            'telefono_contacto': request.form.get('telefono_contacto'),
            'firma_participante_cliente': request.form.get('firma_participante_cliente'),
            'submitted_by_email': user_email
        }
        
        conn = get_db_connection()
        cur = conn.cursor()

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO registro_y_acta_de_visita ({columns}) VALUES ({placeholders})"
        
        cur.execute(sql, list(form_data.values()))
        
        conn.commit()
        cur.close()

        flash('Acta de Visita enviada exitosamente!', 'success')
        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting registro y acta de visita: {e}", exc_info=True)
        flash('Hubo un error al enviar el acta de visita.', 'danger')
        return redirect(url_for('registro_y_acta_de_visita_form'))
    finally:
        if conn:
            conn.close()

# Routes for Planilla Vehicular

@app.route('/planilla_vehicular')
@jwt_required()
def planilla_vehicular_form():
    user_email = get_jwt_identity()
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
        app_logger.info(f"User {user_email} accessing planilla vehicular form (admin: {is_admin})")
    except Exception as e:
        app_logger.warning(f"Could not get JWT claims for {user_email}: {e}")
        user_name = user_email.split('@')[0]
        is_admin = False

    return render_template(
        'planilla_vehicular.html',
        name=user_name,
        is_admin=is_admin,
        login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'),
        landing_service_url=app.config.get('LANDING_SERVICE_URL', '/'),
        dashboard_service_url=app.config.get('DASHBOARD_SERVICE_URL', '/'),
        viewer_service_url=app.config.get('VIEWER_SERVICE_URL', '/')
    )

@app.route('/submit_planilla_vehicular', methods=['POST'])
@jwt_required()
def submit_planilla_vehicular():
    user_email = get_jwt_identity()
    conn = None
    try:
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area_especifica': request.form.get('puesto_area_especifica'),
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma_responsable'),
            'placa_vehiculo': request.form.get('placa_vehiculo'),
            'kilometraje_entrega': request.form.get('kilometraje_entrega'),
            'kilometraje_salida': request.form.get('kilometraje_salida'),
            
            # Inspection items
            'estado_rines': request.form.get('estado_rines'),
            'juego_senales_carretera': request.form.get('juego_senales_carretera'),
            'gato_hidraulico': request.form.get('gato_hidraulico'),
            'palanca_gato': request.form.get('palanca_gato'),
            'estado_asientos': request.form.get('estado_asientos'),
            'estado_tapetes_alfombras': request.form.get('estado_tapetes_alfombras'),
            'limpieza_carroceria': request.form.get('limpieza_carroceria'),
            'luces_delanteras': request.form.get('luces_delanteras'),
            'luces_direccionales': request.form.get('luces_direccionales'),
            'luces_traseras': request.form.get('luces_traseras'),
            'parabrisas_delantero': request.form.get('parabrisas_delantero'),
            'parabrisas_trasero': request.form.get('parabrisas_trasero'),
            'defensa_delantera': request.form.get('defensa_delantera'),
            'defensa_trasera': request.form.get('defensa_trasera'),
            'puertas_vidrios': request.form.get('puertas_vidrios'),
            'tapa_radiador': request.form.get('tapa_radiador'),
            'tapa_aceite_motor': request.form.get('tapa_aceite_motor'),
            'bateria_tapa': request.form.get('bateria_tapa'),
            'espejo_retrovisor_interno': request.form.get('espejo_retrovisor_interno'),
            'espejos_retrovisores_externos': request.form.get('espejos_retrovisores_externos'),
            'limpia_brisas': request.form.get('limpia_brisas'),
            'antena_radio': request.form.get('antena_radio'),
            'radio_funciona': request.form.get('radio_funciona'),
            'llanta_repuesto': request.form.get('llanta_repuesto'),
            'aire_acondicionado': request.form.get('aire_acondicionado'),
            
            # Car damage diagram
            'diagrama_danos': request.form.get('diagrama_danos'),
            
            # Summary
            'novedades_criticas': request.form.get('novedades_criticas'),
            'accion_inmediata': request.form.get('accion_inmediata'),
            'firma_entrega': request.form.get('firma_entrega'),
            'firma_recibe': request.form.get('firma_recibe'),
            'oficial_operaciones_nombre': request.form.get('oficial_operaciones_nombre'),
            'oficial_operaciones_firma': request.form.get('oficial_operaciones_firma'),
            
            'submitted_by_email': user_email
        }
        
        conn = get_db_connection()
        cur = conn.cursor()

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO planilla_vehicular ({columns}) VALUES ({placeholders})"
        
        cur.execute(sql, list(form_data.values()))
        
        conn.commit()
        cur.close()

        flash('Planilla de Chequeo Vehicular enviada exitosamente!', 'success')
        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting planilla vehicular: {e}", exc_info=True)
        flash('Hubo un error al enviar la planilla vehicular.', 'danger')
        return redirect(url_for('planilla_vehicular_form'))
    finally:
        if conn:
            conn.close()

@app.route('/submit_report', methods=['POST'])
@jwt_required()
def submit_report():
    user_email = get_jwt_identity()
    conn = None
    try:
        app_logger.info("Starting submit_report function.")

        # Get form data including propiedad field
        tipo_incidencia = request.form.get('tipo_incidencia')
        tipo_cliente = request.form.get('tipo_cliente')
        lugar_incidente = request.form.get('lugar_incidente')
        propiedad = request.form.get('propiedad')
        descripcion_zona_comun = request.form.get('descripcion_zona_comun')
        fecha_incidente = request.form.get('fecha_incidente')
        hora_incidente = request.form.get('hora_incidente')
        descripcion_incidente = request.form.get('descripcion_incidente')
        valor_aproximado = request.form.get('valor_aproximado')
        pertenencias_sustraidas = request.form.get('pertenencias_sustraidas')
        nombre_persona = request.form.get('nombre_persona')
        telefono_persona = request.form.get('telefono_persona')
        numero_identidad_persona = request.form.get('numero_identidad_persona')
        numero_local = request.form.get('numero_local')
        direccion = request.form.get('direccion')
        supervisor = request.form.get('supervisor')
        firma_usuario = request.form.get('firma_usuario')

        # Validate required fields (including propiedad field)
        if not all([tipo_incidencia, tipo_cliente, lugar_incidente, propiedad, fecha_incidente,
                   hora_incidente, descripcion_incidente, nombre_persona, supervisor]):
            app_logger.warning("Missing required fields in form submission.")
            return redirect(url_for('error', message='Por favor, complete todos los campos obligatorios incluyendo la Propiedad.'))

        app_logger.info(f"Form data received: tipo_incidencia={tipo_incidencia}, tipo_cliente={tipo_cliente}, lugar_incidente={lugar_incidente}, propiedad={propiedad}")

        # Handle file uploads
        imagenes_pdfs = None
        uploaded_files = request.files.getlist('imagenes_pdfs')
        app_logger.info(f"Received {len(uploaded_files)} files from form for upload to GCS.")
        uploaded_urls = []
        for file in uploaded_files:
            if file and file.filename:
                app_logger.info(f"Attempting to upload file: {file.filename}")
                try:
                    public_url = upload_file_to_gcs(file, GCS_BUCKET_NAME)
                    app_logger.info(f"Uploaded to: {public_url}")
                    uploaded_urls.append(public_url)
                except Exception as e:
                    app_logger.error(f"Error uploading {file.filename}: {str(e)}", exc_info=True)
                    return redirect(url_for('error', message=f'Error al subir archivo {file.filename}: {str(e)}'))
        imagenes_pdfs = "\n".join(uploaded_urls) if uploaded_urls else None
        app_logger.info(f"GCS upload complete. URLs: {imagenes_pdfs}")

        app_logger.info("Attempting to get database connection.")
        conn = get_db_connection()
        cur = conn.cursor()
        app_logger.info("Database connection obtained.")

        # Get user name
        app_logger.info(f"Fetching user name for email: {user_email}")
        cur.execute("SELECT name FROM users WHERE email = %s", (user_email,))
        user_name_row = cur.fetchone()
        user_name = user_name_row[0] if user_name_row else user_email
        app_logger.info(f"User name: {user_name}")

        # Insert report with propiedad field
        app_logger.info("Preparing to insert report into database.")
        cur.execute(
            """
            INSERT INTO reportes_incidentes (
                id_tipo_incidencia, id_tipo_cliente, id_lugar_incidente, id_propiedad,
                descripcion_zona_comun, fecha_incidente, hora_incidente,
                descripcion_incidente, valor_aproximado, pertenencias_sustraidas,
                nombre_persona, telefono_persona, numero_identidad_persona,
                numero_local, direccion, imagenes_pdfs, id_supervisor, user_email, firma_usuario
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tipo_incidencia, tipo_cliente, lugar_incidente, propiedad,
                descripcion_zona_comun, fecha_incidente, hora_incidente,
                descripcion_incidente, valor_aproximado, pertenencias_sustraidas,
                nombre_persona, telefono_persona, numero_identidad_persona,
                numero_local, direccion, imagenes_pdfs, supervisor, user_email, firma_usuario
            )
        )
        app_logger.info("Executing database commit.")
        conn.commit()
        cur.close()
        app_logger.info("Database commit complete and cursor closed.")

        # Prepare report fields for email (including propiedad)
        report_fields = {
            'fecha_incidente': fecha_incidente,
            'hora_incidente': hora_incidente,
            'descripcion_incidente': descripcion_incidente,
            'direccion': direccion,
            'valor_aproximado': valor_aproximado,
            'imagenes_pdfs': imagenes_pdfs,
            'propiedad': propiedad
        }
        
        app_logger.info("Attempting to send report notification email.")
        email_success = send_report_notification(user_email, user_name, report_fields)
        app_logger.info("Report notification email process initiated.")

        if email_success:
            success_message = 'Reporte de incidencia enviado exitosamente. Se ha enviado una confirmación por email.'
        else:
            success_message = 'Reporte de incidencia enviado exitosamente. Nota: No se pudo enviar la confirmación por email.'

        app_logger.info("Redirecting to success page after successful report submission.")
        return redirect(url_for('success', message=success_message))

    except psycopg2.errors.UndefinedTable as e:
        app_logger.error(f"Database table not found. Error: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return redirect(url_for('error', message='Error de base de datos: Tabla no encontrada. Contacte al administrador.'))
    except psycopg2.Error as e:
        app_logger.error(f"Database error submitting report: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return redirect(url_for('error', message='Error de base de datos. Por favor, intente de nuevo más tarde.'))
    except Exception as e:
        app_logger.error(f"Error submitting report: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return redirect(url_for('error', message=f'Error inesperado: {str(e)}'))
    finally:
        if conn:
            conn.close()
            app_logger.info("Database connection closed in finally block.")

# NEW: ROUTE for Orden de Mantenimiento form
@app.route('/orden_mantenimiento')
@jwt_required()
def orden_mantenimiento_form():
    user_email = get_jwt_identity()
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
        app_logger.info(f"User {user_email} accessing orden de mantenimiento form (admin: {is_admin})")
    except Exception as e:
        app_logger.warning(f"Could not get JWT claims for {user_email}: {e}")
        user_name = user_email.split('@')[0]
        is_admin = False

    return render_template(
        'orden_mantenimiento.html',
        name=user_name,
        is_admin=is_admin,
        login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'),
        landing_service_url=app.config.get('LANDING_SERVICE_URL', '/'),
        dashboard_service_url=app.config.get('DASHBOARD_SERVICE_URL', '/'),
        viewer_service_url=app.config.get('VIEWER_SERVICE_URL', '/')
    )

# Replace the existing submit_orden_mantenimiento function in app.py with this updated version:

@app.route('/submit_orden_mantenimiento', methods=['POST'])
@jwt_required()
def submit_orden_mantenimiento():
    user_email = get_jwt_identity()
    conn = None
    try:
        # Convert repuestos_usados from string to boolean
        repuestos_value = request.form.get('repuestos_usados')
        if repuestos_value == 'true':
            repuestos_usados = True
        elif repuestos_value == 'false':
            repuestos_usados = False
        else:
            repuestos_usados = None

        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area': request.form.get('puesto_area'),
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'equipo': request.form.get('equipo'),
            'id_equipo_serial': request.form.get('id_equipo_serial'),
            'nombre_tecnico': request.form.get('nombre_tecnico'),
            'firma_tecnico': request.form.get('firma_tecnico'),
            
            # Section 2: Detalle del Servicio
            'tipo_servicio': request.form.get('tipo_servicio'),
            'actividad_realizada': request.form.get('actividad_realizada'),
            'resultado_servicio': request.form.get('resultado_servicio'),
            'downtime_horas': request.form.get('downtime_horas') or None,
            'repuestos_usados': repuestos_usados,
            'tipo_alerta_generada': request.form.get('tipo_alerta_generada'),
            'observaciones': request.form.get('observaciones'),
            
            # Section 4: Seguimiento de Alertas Críticas
            'descripcion_alerta': request.form.get('descripcion_alerta'),
            'accion_inmediata': request.form.get('accion_inmediata'),
            'accion_correctiva_recomendada': request.form.get('accion_correctiva_recomendada'),
            'responsable_asignado': request.form.get('responsable_asignado'),
            'fecha_limite_cierre': request.form.get('fecha_limite_cierre') or None,
            'estado': request.form.get('estado'),

            # Section 5: Firmas
            'supervisor_seguridad': request.form.get('supervisor_seguridad'),
            'firma_supervisor_seguridad': request.form.get('firma_supervisor_seguridad'),

            'submitted_by_email': user_email
        }

        conn = get_db_connection()
        cur = conn.cursor()

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO orden_mantenimiento ({columns}) VALUES ({placeholders})"
        
        cur.execute(sql, list(form_data.values()))
        
        conn.commit()
        cur.close()

        flash('Orden de Mantenimiento enviada exitosamente!', 'success')
        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting orden de mantenimiento: {e}", exc_info=True)
        flash('Hubo un error al enviar la orden de mantenimiento.', 'danger')
        return redirect(url_for('orden_mantenimiento_form'))
    finally:
        if conn:
            conn.close()

# ROUTE for the Control de Accesos form
@app.route('/control_accesos')
@jwt_required()
def control_accesos_form():
    user_email = get_jwt_identity()
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
        app_logger.info(f"User {user_email} accessing control de accesos form (admin: {is_admin})")
    except Exception as e:
        app_logger.warning(f"Could not get JWT claims for {user_email}: {e}")
        user_name = user_email.split('@')[0]
        is_admin = False

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT name FROM users WHERE email = %s", (user_email,))
        result = cur.fetchone()
        if result and result[0]:
            user_name = result[0]
        cur.close()
    except Exception as e:
        app_logger.warning(f"Could not fetch user from database: {e}")
    finally:
        if conn:
            conn.close()

    return render_template(
        'control_accesos.html',
        name=user_name,
        is_admin=is_admin,
        login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'),
        landing_service_url=app.config.get('LANDING_SERVICE_URL', '/'),
        dashboard_service_url=app.config.get('DASHBOARD_SERVICE_URL', '/'),
        viewer_service_url=app.config.get('VIEWER_SERVICE_URL', '/')
    )

# ROUTE for submitting the Control de Accesos form
@app.route('/submit_control_accesos', methods=['POST'])
@jwt_required()
def submit_control_accesos():
    user_email = get_jwt_identity()
    conn = None
    try:
        # Get all form fields from the request including NEW vehicle and material fields
        form_data = {
            # Section 1: General Data
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area_especifica': request.form.get('puesto_area_especifica'),
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma_responsable'),
            
            # Section 2: Person Information (NEW FIELDS)
            'nombre_persona': request.form.get('nombre_persona'),
            'documento_identidad': request.form.get('documento_identidad'),
            'empresa_visitante': request.form.get('empresa_visitante'),
            'rol_del_usuario': request.form.get('rol_del_usuario'),
            
            # Section 3: Access Control
            'accion': request.form.get('accion'),
            'motivo_de_ingreso': request.form.get('motivo_de_ingreso'),
            'autorizacion': request.form.get('autorizacion'),
            
            # Section 4: Vehicle Control (NEW FIELDS)
            'vehiculo_placa': request.form.get('vehiculo_placa'),
            'vehiculo_marca': request.form.get('vehiculo_marca'),
            'vehiculo_modelo': request.form.get('vehiculo_modelo'),
            'vehiculo_color': request.form.get('vehiculo_color'),
            
            # Section 5: Material Control (NEW FIELDS)
            'materiales_ingresados': request.form.get('materiales_ingresados'),
            'materiales_salida': request.form.get('materiales_salida'),
            
            # Section 6: Breach Detection
            'brecha_detectada': request.form.get('brecha_detectada'),
            'evidencia': request.form.get('evidencia'),
            'responsable_del_control': request.form.get('responsable_del_control'),
            'observaciones': request.form.get('observaciones'),
            
            # Section 7: Breach Follow-up (conditional)
            'brecha_por_personas': request.form.get('brecha_por_personas'),
            'brecha_por_procedimiento': request.form.get('brecha_por_procedimiento'),
            'brecha_por_tecnologia_equipos': request.form.get('brecha_por_tecnologia_equipos'),
            'brecha_por_seguridad_fisica': request.form.get('brecha_por_seguridad_fisica'),
            'accion_inmediata_tomada': request.form.get('accion_inmediata_tomada'),
            'accion_correctiva_recomendada': request.form.get('accion_correctiva_recomendada'),
            'responsable_asignado': request.form.get('responsable_asignado'),
            'fecha_limite_de_cierre': request.form.get('fecha_limite_de_cierre'),
            'estado': request.form.get('estado'),
            
            # Section 8: Closure Signature
            'nombre_cierre': request.form.get('nombre_cierre'),
            'firma_cierre': request.form.get('firma_cierre'),
            
            'submitted_by_email': user_email
        }
        
        conn = get_db_connection()
        cur = conn.cursor()

        # Construct the SQL INSERT statement
        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO control_accesos ({columns}) VALUES ({placeholders})"
        
        # Execute the query
        cur.execute(sql, list(form_data.values()))
        
        conn.commit()
        cur.close()

        flash('Reporte de Control de Accesos enviado exitosamente!', 'success')
        app_logger.info(f"Control de Accesos report submitted successfully by {user_email}")
        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting control de accesos report: {e}", exc_info=True)
        flash('Hubo un error al enviar el reporte de control de accesos.', 'danger')
        return redirect(url_for('control_accesos_form'))
    finally:
        if conn:
            conn.close()

@app.route('/offline.html')
def offline():
    """Serve offline page for PWA"""
    return render_template('offline.html')

@app.route('/sw.js')
def service_worker():
    """Serve service worker with proper headers"""
    response = send_from_directory('.', 'sw.js')
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    # Don't cache the service worker itself
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/install')
def install_instructions():
    """Serve PWA installation instructions"""
    return render_template('install_prompt.html')

# Modify your existing manifest route to include more PWA features
@app.route('/manifest.json')
def manifest():
    """Serve PWA manifest with enhanced offline capabilities"""
    return jsonify({
        "name": "SMT SecApp - Reportes de Incidencias",
        "short_name": "SMT SecApp",
        "description": "Aplicación para reportar incidencias de seguridad en propiedades comerciales",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#1a202c",
        "theme_color": "#2563eb",
        "orientation": "portrait",
        "scope": "/",
        "lang": "es",
        "icons": [
            {
                "src": "https://storage.googleapis.com/smt-misc/SMT-logo.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable"
            },
            {
                "src": "https://storage.googleapis.com/smt-misc/SMT-logo.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable"
            }
        ],
        "shortcuts": [
            {
                "name": "Nuevo Reporte",
                "short_name": "Reporte",
                "description": "Crear un nuevo reporte de incidencia",
                "url": "/",
                "icons": [{"src": "https://storage.googleapis.com/smt-misc/SMT-logo.png", "sizes": "96x96"}]
            }
        ],
        "categories": ["business", "productivity"],
        "prefer_related_applications": False
    })

@app.route('/api/my_reports', methods=['GET'])
@jwt_required()
def get_my_reports():
    """Get user's submitted reports from the last month"""
    user_email = get_jwt_identity()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Get reports from the last month for this user
        cur.execute("""
            SELECT r.*, 
                   ti.nombre as tipo_incidencia_nombre,
                   tc.nombre as tipo_cliente_nombre,
                   li.nombre as lugar_incidente_nombre,
                   p.nombre as propiedad_nombre,
                   s.nombre as supervisor_nombre
            FROM reportes_incidentes r
            LEFT JOIN tipo_incidencia ti ON r.id_tipo_incidencia = ti.id_tipo_incidencia
            LEFT JOIN tipo_cliente tc ON r.id_tipo_cliente = tc.id_tipo_cliente
            LEFT JOIN lugar_incidente li ON r.id_lugar_incidente = li.id_lugar_incidente
            LEFT JOIN propiedades p ON r.id_propiedad = p.id_propiedad
            LEFT JOIN supervisor s ON r.id_supervisor = s.id_supervisor
            WHERE r.user_email = %s 
            AND r.creado_en >= CURRENT_DATE - INTERVAL '30 days'
            ORDER BY r.creado_en DESC
            LIMIT 50
        """, (user_email,))
        
        reports = cur.fetchall()
        cur.close()
        
        # Convert to list of dictionaries
        reports_list = []
        for report in reports:
            report_dict = dict(report)
            # Convert datetime objects to strings for JSON serialization
            if report_dict.get('creado_en'):
                report_dict['creado_en'] = report_dict['creado_en'].isoformat()
            if report_dict.get('fecha_incidente'):
                report_dict['fecha_incidente'] = report_dict['fecha_incidente'].isoformat()
            if report_dict.get('hora_incidente'):
                report_dict['hora_incidente'] = str(report_dict['hora_incidente'])
            reports_list.append(report_dict)
        
        app_logger.info(f"Retrieved {len(reports_list)} reports for user {user_email}")
        return jsonify(reports_list)
        
    except Exception as e:
        app_logger.error(f"Error retrieving reports for {user_email}: {e}", exc_info=True)
        return jsonify({'error': 'Error retrieving reports'}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/my_reports/<int:report_id>', methods=['GET'])
@jwt_required()
def get_my_report_details(report_id):
    """Get detailed information for a specific report (only if it belongs to the user)"""
    user_email = get_jwt_identity()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Get specific report with all details, ensuring it belongs to the user
        cur.execute("""
            SELECT r.*, 
                   ti.nombre as tipo_incidencia_nombre,
                   tc.nombre as tipo_cliente_nombre,
                   li.nombre as lugar_incidente_nombre,
                   p.nombre as propiedad_nombre,
                   s.nombre as supervisor_nombre
            FROM reportes_incidentes r
            LEFT JOIN tipo_incidencia ti ON r.id_tipo_incidencia = ti.id_tipo_incidencia
            LEFT JOIN tipo_cliente tc ON r.id_tipo_cliente = tc.id_tipo_cliente
            LEFT JOIN lugar_incidente li ON r.id_lugar_incidente = li.id_lugar_incidente
            LEFT JOIN propiedades p ON r.id_propiedad = p.id_propiedad
            LEFT JOIN supervisor s ON r.id_supervisor = s.id_supervisor
            WHERE r.id_reporte_incidente = %s AND r.user_email = %s
        """, (report_id, user_email))
        
        report = cur.fetchone()
        cur.close()
        
        if not report:
            app_logger.warning(f"Report {report_id} not found or doesn't belong to user {user_email}")
            return jsonify({'error': 'Report not found'}), 404
        
        # Convert to dictionary and handle datetime serialization
        report_dict = dict(report)
        if report_dict.get('creado_en'):
            report_dict['creado_en'] = report_dict['creado_en'].isoformat()
        if report_dict.get('fecha_incidente'):
            report_dict['fecha_incidente'] = report_dict['fecha_incidente'].isoformat()
        if report_dict.get('hora_incidente'):
            report_dict['hora_incidente'] = str(report_dict['hora_incidente'])
        
        app_logger.info(f"Retrieved detailed report {report_id} for user {user_email}")
        return jsonify(report_dict)
        
    except Exception as e:
        app_logger.error(f"Error retrieving report {report_id} for {user_email}: {e}", exc_info=True)
        return jsonify({'error': 'Error retrieving report details'}), 500
    finally:
        if conn:
            conn.close()

# Add error handler for when app is accessed offline
@app.errorhandler(503)
def service_unavailable(error):
    return render_template('offline.html'), 503

@app.route('/logout')
def logout():
    response = redirect(app.config.get('LOGIN_SERVICE_URL', '/'))
    unset_jwt_cookies(response)
    flash("You have been logged out.", "info")
    return response

@app.route('/success')
@jwt_required()
def success():
    message = request.args.get('message', 'Reporte de incidencia enviado exitosamente!')
    return render_template('success.html',
                           message=message,
                           login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'))

@app.route('/error')
def error():
    message = request.args.get('message', 'Ha ocurrido un error inesperado.')
    return render_template('error.html',
                           message=message,
                           login_service_url=app.config.get('LOGIN_SERVICE_URL', '/'))

# ROUTE for submitting the Planilla de Rondas form
@app.route('/submit_planilla_de_rondas', methods=['POST'])
@jwt_required()
def submit_planilla_de_rondas():
    user_email = get_jwt_identity()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Get all form fields from the request
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area_especifica': request.form.get('puesto_area_especifica'),
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma_responsable'),
            'punto_de_control': request.form.get('punto_de_control'),
            'hora_programada': request.form.get('hora_programada') or None,
            'hora_verificacion': request.form.get('hora_verificacion') or None,
            'estado_punto': request.form.get('estado_punto'),
            'cumplimiento': request.form.get('cumplimiento'),
            'novedades_relevantes': request.form.get('novedades_relevantes'),
            'accion_inmediata': request.form.get('accion_inmediata'),
            'requerimiento_pendiente': request.form.get('requerimiento_pendiente'),
            'firma_entrega_ronda': request.form.get('firma_entrega_ronda'),
            'firma_recepcion_supervisor': request.form.get('firma_recepcion_supervisor'),
            'submitted_by_email': user_email
        }
        
        # Construct the SQL INSERT statement
        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO planilla_de_rondas ({columns}) VALUES ({placeholders})"
        
        # Execute the query
        cur.execute(sql, list(form_data.values()))
        
        conn.commit()
        cur.close()

        flash('Planilla de Rondas y Patrullaje enviada exitosamente!', 'success')
        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting planilla de rondas report: {e}", exc_info=True)
        flash('Hubo un error al enviar la planilla de rondas.', 'danger')
        return redirect(url_for('planilla_de_rondas_form'))
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    app_logger.info("Starting Flask app in local development mode.")
    with app.app_context():
        create_tables_if_not_exists()
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))