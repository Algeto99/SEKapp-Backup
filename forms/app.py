import os
import logging
import traceback
from flask import Flask, render_template, request, redirect, flash, jsonify, url_for
from flask_jwt_extended import JWTManager, get_jwt_identity, jwt_required, unset_jwt_cookies
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
    app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com')
    app.config['LANDING_SERVICE_URL'] = os.environ.get('LANDING_SERVICE_URL', 'https://landing.secapp.tzolkintech.com')
    app.config['DASHBOARD_SERVICE_URL'] = os.environ.get('DASHBOARD_SERVICE_URL', 'https://dashboard.secapp.tzolkintech.com')
    app.config['VIEWER_SERVICE_URL'] = os.environ.get('VIEWER_SERVICE_URL', 'https://viewer.secapp.tzolkintech.com')

    app.config['JWT_TOKEN_LOCATION'] = ['cookies']
    app.config['JWT_ACCESS_COOKIE_PATH'] = '/'
    app.config['JWT_COOKIE_SECURE'] = is_production
    app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
    app.config['JWT_ACCESS_COOKIE_NAME'] = 'access_token_cookie'
    app.config['JWT_COOKIE_CSRF_PROTECT'] = False
    app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', None)

    # Database config (used by get_db_connection via DATABASE_URL)
    app.config['DB_HOST'] = os.environ.get('DB_HOST')
    app.config['DB_NAME'] = os.environ.get('DB_NAME')
    app.config['DB_USER'] = os.environ.get('DB_USER')
    app.config['DB_PASSWORD'] = os.environ.get('DB_PASSWORD')
    app.config['DB_PORT'] = os.environ.get('DB_PORT', '5432')

    # Email configuration
    app.config['SMTP_SERVER'] = os.environ.get('SMTP_SERVER', 'tzolkintech.com')
    app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', 587))
    app.config['SMTP_USE_TLS'] = os.environ.get('SMTP_USE_TLS', 'true').lower() == 'true'
    app.config['EMAIL_USERNAME'] = os.environ.get('EMAIL_USERNAME', 'no-reply@tzolkintech.com')
    app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL', 'rcanton@tzolkintech.com')
    app.config['GCP_PROJECT_ID'] = os.environ.get('GCP_PROJECT', os.environ.get('GOOGLE_CLOUD_PROJECT'))
    app.config['EMAIL_PASSWORD_SECRET_NAME'] = os.environ.get('EMAIL_PASSWORD_SECRET', 'admin-email-pass')
    app.config['CC_EMAIL'] = os.environ.get('CC_EMAIL', 'rcanton@tzolkintech.com')

    app_logger.info(f"Forms service configured - Production: {is_production}")

configure_app(app)

jwt = JWTManager(app)
app_logger.info("JWT configured successfully")

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

    # --- Generate HTML for attachments ---
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
    # --- End of attachment HTML generation ---

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #2563eb;">Nuevo Reporte de Incidencia - SMT SecApp</h2>
    <div style="background-color: #f8fafc; padding: 20px; border-radius: 8px; margin: 20px 0;">
        <p><strong>Nombre del Reportante:</strong> {user_name}</p>
        <p><strong>Email:</strong> {user_email}</p>
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
    cc_email = app.config.get('CC_EMAIL', None)

    admin_send_success = send_email(to_emails=admin_email, subject=subject, body=html_body, is_html=True, cc_emails=cc_email)
    
    user_send_success = send_email(to_emails=user_email, subject="Confirmación de Reporte - SMT SecApp", body=html_body, is_html=True)

    if not admin_send_success:
        app_logger.error("Failed to send report notification to admin.")
    if not user_send_success:
        app_logger.error("Failed to send report confirmation to user.")
    
    return admin_send_success and user_send_success

def create_tables_if_not_exists():
    """
    Creates necessary tables including 'reportes_incidentes' and lookup tables.
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
                FOREIGN KEY (id_tipo_incidencia) REFERENCES tipo_incidencia(id_tipo_incidencia),
                FOREIGN KEY (id_tipo_cliente) REFERENCES tipo_cliente(id_tipo_cliente),
                FOREIGN KEY (id_lugar_incidente) REFERENCES lugar_incidente(id_lugar_incidente),
                FOREIGN KEY (id_supervisor) REFERENCES supervisor(id_supervisor)
            );
        """)
        conn.commit() # Commit the changes to create tables
        app_logger.info("Table 'reportes_incidentes' checked/created.")

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

@app.route('/')
@jwt_required()
def index():
    user_email = get_jwt_identity()

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("SELECT name FROM users WHERE email = %s", (user_email,))
        result = cur.fetchone()
        name = result[0] if result else user_email

        # -- Load dropdown options --
        cur.execute("SELECT id_tipo_incidencia AS id, nombre FROM tipo_incidencia ORDER BY nombre ASC")
        tipo_incidencia = cur.fetchall()

        cur.execute("SELECT id_tipo_cliente AS id, nombre FROM tipo_cliente ORDER BY nombre ASC")
        tipo_cliente = cur.fetchall()

        cur.execute("SELECT id_lugar_incidente AS id, nombre FROM lugar_incidente ORDER BY nombre ASC")
        lugar_incidente = cur.fetchall()

        cur.execute("SELECT id_supervisor AS id, nombre FROM supervisor ORDER BY nombre ASC")
        supervisor = cur.fetchall()

        cur.close()
        return render_template(
            'form.html',
            tipo_incidencia=tipo_incidencia,
            tipo_cliente=tipo_cliente,
            lugar_incidente=lugar_incidente,
            supervisor=supervisor,
            name=name,
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

@app.route('/submit_report', methods=['POST'])
@jwt_required()
def submit_report():
    user_email = get_jwt_identity()
    conn = None
    try:
        app_logger.info("Starting submit_report function.")

        tipo_incidencia = request.form.get('tipo_incidencia')
        tipo_cliente = request.form.get('tipo_cliente')
        lugar_incidente = request.form.get('lugar_incidente')
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

        # Validate required fields
        if not all([tipo_incidencia, tipo_cliente, lugar_incidente, fecha_incidente, 
                   hora_incidente, descripcion_incidente, nombre_persona, supervisor]):
            app_logger.warning("Missing required fields in form submission.")
            return redirect(url_for('error', message='Por favor, complete todos los campos obligatorios.'))

        app_logger.info(f"Form data received: tipo_incidencia={tipo_incidencia}, tipo_cliente={tipo_cliente}, lugar_incidente={lugar_incidente}")

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

        app_logger.info(f"Fetching user name for email: {user_email}")
        cur.execute("SELECT name FROM users WHERE email = %s", (user_email,))
        user_name_row = cur.fetchone()
        user_name = user_name_row[0] if user_name_row else user_email
        app_logger.info(f"User name: {user_name}")

        app_logger.info("Preparing to insert report into database.")
        cur.execute(
            """
            INSERT INTO reportes_incidentes (
                id_tipo_incidencia, id_tipo_cliente, id_lugar_incidente,
                descripcion_zona_comun, fecha_incidente, hora_incidente,
                descripcion_incidente, valor_aproximado, pertenencias_sustraidas,
                nombre_persona, telefono_persona, numero_identidad_persona,
                numero_local, direccion, imagenes_pdfs, id_supervisor, user_email
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tipo_incidencia, tipo_cliente, lugar_incidente,
                descripcion_zona_comun, fecha_incidente, hora_incidente,
                descripcion_incidente, valor_aproximado, pertenencias_sustraidas,
                nombre_persona, telefono_persona, numero_identidad_persona,
                numero_local, direccion, imagenes_pdfs, supervisor, user_email
            )
        )
        app_logger.info("Executing database commit.")
        conn.commit()
        cur.close()
        app_logger.info("Database commit complete and cursor closed.")

        report_fields = {
            'fecha_incidente': fecha_incidente,
            'hora_incidente': hora_incidente,
            'descripcion_incidente': descripcion_incidente,
            'direccion': direccion,
            'valor_aproximado': valor_aproximado,
            'imagenes_pdfs': imagenes_pdfs
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
        app_logger.error(f"Database table 'reportes_incidentes' (or related lookup tables) not found. Please ensure your database schema is initialized. Error: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return redirect(url_for('error', message='Error de base de datos: La tabla de incidentes no existe. Contacte al administrador.'))
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

if __name__ == '__main__':
    app_logger.info("Starting Flask app in local development mode.")
    with app.app_context(): # Run this within an app context
        create_tables_if_not_exists()
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))