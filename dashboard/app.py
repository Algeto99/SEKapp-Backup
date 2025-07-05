# Secapp/dashboards/app.py
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, redirect, url_for, flash, request, Response, jsonify
import psycopg2
import psycopg2.extras
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity
from datetime import datetime, timedelta
from google.cloud import secretmanager
import traceback
import io
import csv
import logging

logging.basicConfig(level=logging.INFO)
app_logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Flask App Configuration ---
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_dashboard_service')

# --- JWT Configuration (MUST match login and forms services) ---
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'your-super-secret-jwt-key')
app.config['JWT_TOKEN_LOCATION'] = ['cookies']
app.config['JWT_COOKIE_SECURE'] = True # Set to True in production over HTTPS
app.config['JWT_COOKIE_SAMESITE'] = 'Lax'

# CRITICAL for cross-service cookie sharing:
# For Cloud Run default URLs (e.g., service-name.run.app), this should be '.run.app'.
# For custom domains (e.g., dashboard.yourdomain.com), this should be '.yourdomain.com'.
# For local testing, 'localhost' is appropriate.
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', 'localhost') # Changed default to 'localhost'

# --- Email Config ---
app.config['SMTP_SERVER'] = os.environ.get('SMTP_SERVER', 'mail.tzolkintech.com')
app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', 587))
app.config['EMAIL_USERNAME'] = os.environ.get('EMAIL_USERNAME', 'no-reply@tzolkintech.com')
app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL', 'rcanton@tzolkintech.com')
app.config['PROJECT_ID'] = os.environ.get('GCP_PROJECT_ID', 'tz-dev-secapp')
app.config['SECRET_NAME'] = os.environ.get('EMAIL_PASSWORD_SECRET', 'admin-email-pass')

jwt = JWTManager(app)

# --- Secret Manager Functions ---
def get_secret_value(secret_name, project_id=None):
    try:
        if not project_id:
            project_id = app.config.get('PROJECT_ID')
        
        if not project_id:
            app_logger.error("PROJECT_ID not found in environment variables for Secret Manager access.")
            return None
        
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        secret_value = response.payload.data.decode("UTF-8")
        app_logger.info(f"Successfully retrieved secret: {secret_name}")
        return secret_value
        
    except Exception as e:
        app_logger.error(f"Error retrieving secret {secret_name}: {e}", exc_info=True)
        return None

def get_email_password():
    password = os.environ.get('EMAIL_PASSWORD')
    if password:
        app_logger.info("Using email password from environment variable.")
        return password
    
    app_logger.info("Attempting to retrieve email password from Secret Manager.")
    return get_secret_value(app.config['SECRET_NAME'])

# --- Email Functions ---
def send_email(to_email, subject, body, is_html=False):
    try:
        email_username = app.config.get('EMAIL_USERNAME')
        email_password = get_email_password()
        smtp_server = app.config.get('SMTP_SERVER')
        smtp_port = app.config.get('SMTP_PORT')
        
        app_logger.info(f"Email config check - Username: {email_username}, Server: {smtp_server}, Port: {smtp_port}")
        
        if not all([email_username, email_password]):
            app_logger.warning(f"Email configuration incomplete. Username: {email_username}, Password: {'Set' if email_password else 'Not Set'}")
            return False

        app_logger.info(f"Attempting to send email to {to_email} with subject: {subject}")

        msg = MIMEMultipart()
        msg['From'] = email_username
        msg['To'] = to_email
        msg['Subject'] = subject

        if is_html:
            msg.attach(MIMEText(body, 'html'))
        else:
            msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(email_username, email_password)
        
        text = msg.as_string()
        server.sendmail(email_username, to_email, text)
        server.quit()
        
        app_logger.info(f"Email sent successfully to {to_email}")
        return True
        
    except smtplib.SMTPAuthenticationError as e:
        app_logger.error(f"SMTP Authentication Error: {e}. Possible causes: Incorrect password, 2FA, or app password issues.", exc_info=True)
        return False
    except smtplib.SMTPException as e:
        app_logger.error(f"SMTP Error: {e}", exc_info=True)
        return False
    except Exception as e:
        app_logger.error(f"General error sending email: {e}", exc_info=True)
        return False


# --- Database Connection (PostgreSQL) ---
def get_db_connection():
    conn = None
    try:
        db_url = os.environ.get('DATABASE_URL')
        if not db_url:
            app_logger.error("DATABASE_URL environment variable not set.")
            flash('Error de configuración de la base de datos para el dashboard.', 'error')
            return None

        conn = psycopg2.connect(db_url)
        app_logger.info("Dashboard database connection successful.")
        return conn
    except Exception as e:
        app_logger.error(f"Error connecting to dashboard database: {e}", exc_info=True)
        flash('Error de conexión a la base de datos para el dashboard.', 'error')
        return None

# --- JWT Callbacks for Error Handling and Redirection ---
@jwt.unauthorized_loader
def unauthorized_response(callback):
    login_url = os.environ.get('LOGIN_SERVICE_URL', '/')
    if not login_url.endswith('/login'):
        login_url = f"{login_url.rstrip('/')}/login"
    flash('Por favor, inicie sesión para acceder a esta página.', 'warning')
    app_logger.warning(f"Unauthorized access attempt. Redirecting to {login_url}")
    return redirect(login_url)

@jwt.invalid_token_loader
def invalid_token_response(callback):
    login_url = os.environ.get('LOGIN_SERVICE_URL', '/')
    if not login_url.endswith('/login'):
        login_url = f"{login_url.rstrip('/')}/login"
    flash('Token de sesión inválido. Por favor, inicie sesión de nuevo.', 'danger')
    app_logger.warning(f"Invalid token. Redirecting to {login_url}")
    return redirect(login_url)

@jwt.expired_token_loader
def expired_token_response(callback):
    login_url = os.environ.get('LOGIN_SERVICE_URL', '/')
    if not login_url.endswith('/login'):
        login_url = f"{login_url.rstrip('/')}/login"
    flash('Su sesión ha expirado. Por favor, inicie sesión de nuevo.', 'warning')
    app_logger.warning(f"Expired token. Redirecting to {login_url}")
    return redirect(login_url)

# --- Routes ---

@app.route('/')
@jwt_required()
def index():
    return redirect(url_for('show_dashboard'))

@app.route('/dashboard', methods=['GET'])
@jwt_required()
def show_dashboard():
    current_user_identity = get_jwt_identity()
    app_logger.info(f"User {current_user_identity} accessing dashboard.")

    conn = None
    submissions = []
    try:
        conn = get_db_connection()
        if conn is None:
            return render_template('dashboard.html',
                                   submissions=[],
                                   username=current_user_identity,
                                   login_service_url=os.environ.get('LOGIN_SERVICE_URL', '#'),
                                   forms_service_url=os.environ.get('FORMS_SERVICE_URL', '#'),
                                   current_datetime=datetime.now())
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT
                ri.id_reporte_incidente AS id_reporte,
                ti.nombre AS tipo_incidencia,
                tc.nombre AS tipo_cliente,
                li.nombre AS lugar_incidente,
                ri.fecha_incidente,
                ri.hora_incidente,
                ri.descripcion_incidente,
                ri.nombre_persona,
                ri.telefono_persona,
                ri.numero_identidad_persona,
                ri.valor_aproximado,
                ri.pertenencias_sustraidas,
                s.nombre AS supervisor_nombre,
                ri.creado_en
            FROM
                reportes_incidentes ri
            JOIN
                tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            JOIN
                tipo_cliente tc ON ri.id_tipo_cliente = tc.id_tipo_cliente
            JOIN
                lugar_incidente li ON ri.id_lugar_incidente = li.id_lugar_incidente
            JOIN
                supervisor s ON ri.id_supervisor = s.id_supervisor
            WHERE
                ri.user_email = %s
            ORDER BY
                ri.creado_en DESC;
        """, (current_user_identity,))
        
        submissions = cur.fetchall()
        cur.close()
        app_logger.info(f"Fetched {len(submissions)} reports for user {current_user_identity}.")

    except psycopg2.Error as e:
        app_logger.error(f"Database error fetching dashboard data for {current_user_identity}: {e}", exc_info=True)
        flash(f"Error al cargar datos del dashboard: {e}", 'error')
    except Exception as e:
        app_logger.error(f"An unexpected error occurred while fetching dashboard data for {current_user_identity}: {e}", exc_info=True)
        flash(f"Ocurrió un error inesperado al cargar el dashboard: {e}", 'error')
    finally:
        if conn:
            conn.close()

    return render_template('dashboard.html',
                           submissions=submissions,
                           username=current_user_identity,
                           login_service_url=os.environ.get('LOGIN_SERVICE_URL', '#'),
                           forms_service_url=os.environ.get('FORMS_SERVICE_URL', '#'),
                           current_datetime=datetime.now())


@app.route('/export_csv', methods=['GET'])
@jwt_required()
def export_csv():
    current_user_identity = get_jwt_identity()
    app_logger.info(f"User {current_user_identity} requesting CSV export.")

    conn = None
    try:
        conn = get_db_connection()
        if conn is None:
            return "Error de conexión a la base de datos.", 500

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT
                ri.id_reporte_incidente AS id_reporte,
                ti.nombre AS tipo_incidencia,
                tc.nombre AS tipo_cliente,
                li.nombre AS lugar_incidente,
                ri.fecha_incidente,
                ri.hora_incidente,
                ri.descripcion_incidente,
                ri.descripcion_zona_comun,
                ri.valor_aproximado,
                ri.pertenencias_sustraidas,
                ri.nombre_persona,
                ri.telefono_persona,
                ri.numero_identidad_persona,
                ri.numero_local,
                ri.direccion,
                ri.imagenes_pdfs,
                s.nombre AS supervisor_nombre,
                ri.creado_en
            FROM
                reportes_incidentes ri
            JOIN
                tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            JOIN
                tipo_cliente tc ON ri.id_tipo_cliente = tc.id_tipo_cliente
            JOIN
                lugar_incidente li ON ri.id_lugar_incidente = li.id_lugar_incidente
            JOIN
                supervisor s ON ri.id_supervisor = s.id_supervisor
            WHERE
                ri.user_email = %s
            ORDER BY
                ri.creado_en DESC;
        """, (current_user_identity,))
        
        reports = cur.fetchall()
        cur.close()

        if not reports:
            return "No hay reportes para exportar para este usuario.", 404

        si = io.StringIO()
        cw = csv.writer(si)

        headers = reports[0].keys() if reports else []
        cw.writerow(headers)

        for row in reports:
            cw.writerow([row[col] for col in headers])

        output = si.getvalue()
        
        response = Response(output, mimetype="text/csv")
        response.headers["Content-Disposition"] = f"attachment; filename=reportes_incidentes_{current_user_identity}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return response

    except psycopg2.Error as e:
        app_logger.error(f"Database error during CSV export for {current_user_identity}: {e}", exc_info=True)
        return "Error al exportar datos a CSV (DB Error).", 500
    except Exception as e:
        app_logger.error(f"An unexpected error occurred during CSV export for {current_user_identity}: {e}", exc_info=True)
        return "Ocurrió un error inesperado al exportar a CSV.", 500
    finally:
        if conn:
            conn.close()


@app.route('/email_reports', methods=['POST'])
@jwt_required()
def email_reports():
    current_user_identity = get_jwt_identity()
    recipient_email = request.json.get('recipient_email', current_user_identity)
    app_logger.info(f"User {current_user_identity} requesting email of reports to {recipient_email}.")

    conn = None
    try:
        conn = get_db_connection()
        if conn is None:
            return jsonify({'success': False, 'message': 'Error de conexión a la base de datos.'}), 500

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT
                ri.id_reporte_incidente AS id_reporte,
                ti.nombre AS tipo_incidencia,
                li.nombre AS lugar_incidente,
                ri.fecha_incidente,
                ri.hora_incidente,
                ri.descripcion_incidente,
                s.nombre AS supervisor_nombre,
                ri.creado_en
            FROM
                reportes_incidentes ri
            JOIN
                tipo_incidencia ti ON ri.id_tipo_incidencia = ti.id_tipo_incidencia
            JOIN
                lugar_incidente li ON ri.id_lugar_incidente = li.id_lugar_incidente
            JOIN
                supervisor s ON ri.id_supervisor = s.id_supervisor
            WHERE
                ri.user_email = %s
            ORDER BY
                ri.creado_en DESC;
        """, (current_user_identity,))
        
        reports = cur.fetchall()
        cur.close()

        if not reports:
            return jsonify({'success': False, 'message': 'No hay reportes para enviar por correo para este usuario.'}), 404

        email_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 800px; margin: 0 auto; padding: 20px; border: 1px solid #eee; border-radius: 8px;">
                <h2 style="color: #2563eb; text-align: center;">Reportes de Incidentes para {current_user_identity}</h2>
                <p>Adjunto encontrará un resumen de sus reportes de incidentes:</p>
                <table style="width: 100%; border-collapse: collapse; margin-top: 20px;">
                    <thead>
                        <tr style="background-color: #f2f2f2;">
                            <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">ID</th>
                            <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">Tipo</th>
                            <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">Lugar</th>
                            <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">Fecha</th>
                            <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">Hora</th>
                            <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">Descripción</th>
                            <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">Supervisor</th>
                            <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">Creado En</th>
                        </tr>
                    </thead>
                    <tbody>
        """
        for report in reports:
            email_body += f"""
                        <tr>
                            <td style="padding: 10px; border: 1px solid #ddd;">{report['id_reporte']}</td>
                            <td style="padding: 10px; border: 1px solid #ddd;">{report['tipo_incidencia']}</td>
                            <td style="padding: 10px; border: 1px solid #ddd;">{report['lugar_incidente']}</td>
                            <td style="padding: 10px; border: 1px solid #ddd;">{report['fecha_incidente']}</td>
                            <td style="padding: 10px; border: 1px solid #ddd;">{report['hora_incidente']}</td>
                            <td style="padding: 10px; border: 1px solid #ddd;">{report['descripcion_incidente'][:50]}...</td>
                            <td style="padding: 10px; border: 1px solid #ddd;">{report['supervisor_nombre']}</td>
                            <td style="padding: 10px; border: 1px solid #ddd;">{report['creado_en'].strftime('%Y-%m-%d %H:%M:%S') if report['creado_en'] else 'N/A'}</td>
                        </tr>
            """
        email_body += """
                    </tbody>
                </table>
                <p style="margin-top: 20px; font-size: 12px; color: #777;">
                    Este es un correo electrónico generado automáticamente. Por favor, no responda a este mensaje.
                </p>
            </div>
        </body>
        </html>
        """

        subject = f"Sus Reportes de Incidentes - SecApp ({datetime.now().strftime('%Y-%m-%d')})"
        email_sent = send_email(recipient_email, subject, email_body, is_html=True)

        if email_sent:
            return jsonify({'success': True, 'message': 'Reportes enviados por correo exitosamente.'}), 200
        else:
            return jsonify({'success': False, 'message': 'Fallo al enviar los reportes por correo.'}), 500

    except psycopg2.Error as e:
        app_logger.error(f"Database error during email report generation for {current_user_identity}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': 'Error al generar los reportes para el correo (DB Error).'}), 500
    except Exception as e:
        app_logger.error(f"An unexpected error occurred during email report generation for {current_user_identity}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': 'Ocurrió un error inesperado al enviar los reportes por correo.'}), 500
    finally:
        if conn:
            conn.close()


# --- Health Check Route ---
@app.route('/health')
def health_check():
    health_status = {
        'status': 'healthy',
        'service': 'dashboard-service',
        'timestamp': datetime.now().isoformat()
    }
    status_code = 200
    try:
        conn = get_db_connection()
        if conn:
            health_status['database'] = 'connected'
            conn.close()
        else:
            health_status['database'] = 'disconnected'
            health_status['status'] = 'unhealthy'
            status_code = 503
    except Exception as e:
        health_status['database'] = f'error: {str(e)}'
        health_status['status'] = 'unhealthy'
        status_code = 503
    
    app_logger.info(f"Health check status: {health_status['status']}")
    return health_status, status_code

# Add a startup check route
@app.route('/startup')
def startup_check():
    app_logger.info("Startup check requested.")
    return {
        'status': 'ready',
        'service': 'dashboard-service',
        'port': os.environ.get('PORT', '8080'),
        'timestamp': datetime.now().isoformat()
    }, 200


# --- Run App (for local development only) ---
if __name__ == '__main__':
    os.environ.setdefault('FLASK_SECRET_KEY', 'dev_flask_secret_key_for_dashboard')
    os.environ.setdefault('JWT_SECRET_KEY', 'dev-secret-key-for-local-testing')
    os.environ.setdefault('JWT_COOKIE_DOMAIN', 'localhost') # Explicitly set for local testing
    os.environ.setdefault('DATABASE_URL', 'postgresql://user:pass@localhost:5432/your_local_database')
    os.environ.setdefault('LOGIN_SERVICE_URL', 'http://localhost:8080')
    os.environ.setdefault('FORMS_SERVICE_URL', 'http://localhost:8081')
    os.environ.setdefault('EMAIL_USERNAME', 'no-reply@tzolkintech.com')
    os.environ.setdefault('ADMIN_EMAIL', 'rcanton@tzolkintech.com')
    os.environ.setdefault('GCP_PROJECT_ID', 'tz-dev-secapp')
    os.environ.setdefault('EMAIL_PASSWORD_SECRET', 'admin-email-pass')

    port = int(os.environ.get('PORT', 8082))
    debug_mode = os.environ.get('FLASK_ENV') == 'development'

    app_logger.info(f"Starting Flask app locally on port {port}")
    app_logger.info(f"Debug mode: {debug_mode}")
    app_logger.info(f"JWT Cookie Domain: {app.config['JWT_COOKIE_DOMAIN']}")
    app_logger.info(f"Login Service URL: {app.config['LOGIN_SERVICE_URL']}")
    app_logger.info(f"Email Username: {app.config['EMAIL_USERNAME']}")
    app_logger.info(f"SMTP Server: {app.config['SMTP_SERVER']}")
    app_logger.info(f"Secret Name: {app.config['SECRET_NAME']}")

    try:
        app.run(
            debug=debug_mode,
            host='0.0.0.0',
            port=port,
            threaded=True,
            use_reloader=False
        )
    except Exception as e:
        app_logger.error(f"Error starting Flask app: {e}", exc_info=True)
        raise
