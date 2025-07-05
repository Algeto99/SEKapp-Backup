import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_bcrypt import Bcrypt
from flask_jwt_extended import (
    create_access_token, create_refresh_token, unset_jwt_cookies,
    set_access_cookies, set_refresh_cookies, jwt_required,
    get_jwt_identity, JWTManager
)
import psycopg2
from psycopg2 import extras
from datetime import timedelta, datetime
from google.cloud import secretmanager
import traceback
import logging

loggin.basicConfig(level=logging.INFO)
app_logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Flask Config ---
# FLASK_SECRET_KEY MUST be set as an environment variable in production
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'jwt-secret-key')
if not app.config['SECRET_KEY']:
    app_logger.error("FATAL: FLASK_SECRET_KEY environment variable not set. App cannot start securely.")
    raise ValueError("FLASK_SECRET_KEY environment variable not set.")

# Dynamic JWT_COOKIE_SECURE based on environment (Cloud Run uses K_SERVICE)
is_production = os.environ.get('K_SERVICE') is not None # K_SERVICE is set in Cloud Run
app.config['JWT_COOKIE_SECURE'] = is_production
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)
app.config['JWT_TOKEN_LOCATION'] = ['cookies']
app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
# JWT_COOKIE_DOMAIN MUST be set to '.run.app' in production
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', '.run.app')

# --- Email Config ---
app.config['SMTP_SERVER'] = os.environ.get('SMTP_SERVER', 'mail.tzolkintech.com')
app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', 587))
app.config['EMAIL_USERNAME'] = os.environment.get('EMAIL_USERNAME', 'no-reply@tzolkintech.com')
app.config['ADMIN_EMAIL'] = os.environment.get(ADMIN_EMAIL, 'rcanton@tzolkintech.com')
app.config['PROJECT_ID'] = os.environment.get('PROJECT_ID', 'tz-dev-secapp')
app.config['SECRET_NAME'] = os.environment.get('EMAIL_PASSWORD_SECRET', 'admin-email-pass')

# Ensure critical email configs are set
if not all([app.config['SMTP_SERVER'], app.config['EMAIL_USERNAME'], app.config['ADMIN_EMAIL'], app.config['PROJECT_ID']]):
    app_logger.error("FATAL: Incomplete Email or GCP PROJECT_ID configuration. Check environment variables.")
    # In production, you might raise an error here to prevent startup
    if is_production:
        raise ValueError("Email or GCP PROJECT_ID configuration missing for production.")

# --- Extensions ---
bcrypt = Bcrypt(app)

# --- Secret Manager Functions ---
def get_secret_value(secret_name, project_id=None):
    """Retrieve secret value from GCP Secret Manager"""
    try:
        if not project_id:
            project_id = app.config.get('PROJECT_ID')

        if not project_id:
            app_logger.error("PROJECT_ID not found in environment variables or app config for Secret Manager access.")
            return None

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        secret_value = response.payload.data.decode("UTF-8")
        app_logger.info(f"Login service: Successfully retrieved secret: {secret_name}")
        return secret_value

    except Exception as e:
        app_logger.error(f"Login service: Error retrieving secret {secret_name}: {e}", exc_info=True)
        return None

def get_email_password():
    """Get email password from Secret Manager"""
    # First try environment variable (for mounted secrets)
    password = os.environ.get('EMAIL_PASSWORD')
    if password:
        app.logger.info("Using email password from environment variable")
        return password

    # If not found, try Secret Manager API
    app.logger.info("Attempting to retrieve email password from Secret Manager")
    return get_secret_value(app.config['SECRET_NAME'])

# NEW: Function to get JWT Secret
def get_jwt_secret():
    """Get JWT secret key from environment or Secret Manager."""
    # First, try environment variable (e.g., if mounted as a secret directly)
    secret_key = os.environ.get('JWT_SECRET_KEY')
    if secret_key:
        app_logger.info("Login service: Using JWT_SECRET_KEY from environment variable.")
        return secret_key

    # If not found, try Secret Manager API
    app_logger.info("Login service: Attempting to retrieve JWT_SECRET_KEY from Secret Manager.")
    # 'jwt-secret-key' is the name of your secret in Secret Manager
    return get_secret_value('jwt-secret-key', app.config.get('PROJECT_ID'))


# Set JWT Secret Key from Secret Manager or environment
jwt_secret = get_jwt_secret()
if not jwt_secret:
    app_logger.error("FATAL: JWT_SECRET_KEY not found for Login service. App cannot start securely.")
    # In production, this should cause a hard failure if the secret is missing
    raise ValueError("JWT_SECRET_KEY not set in production for Login service!")
else:
    app.config['JWT_SECRET_KEY'] = jwt_secret

jwt = JWTManager(app) # Initialize JWTManager after the secret is set

# --- Email Functions ---
def send_email(to_email, subject, body, is_html=False):
    """Send email notification"""
    try:
        # Get email configuration
        email_username = app.config.get('EMAIL_USERNAME')
        email_password = get_email_password()
        smtp_server = app.config.get('SMTP_SERVER')
        smtp_port = app.config.get('SMTP_PORT')

        app.logger.info(f"Email config check - Username: {email_username}, Server: {smtp_server}, Port: {smtp_port}")

        if not all([email_username, email_password]):
            app.logger.warning(f"Email configuration incomplete. Username: {email_username}, Password: {'Set' if email_password else 'Not Set'}")
            return False

        app.logger.info(f"Attempting to send email to {to_email} with subject: {subject}")

        msg = MIMEMultipart()
        msg['From'] = email_username # This sets the 'From' header to your Gmail address
        msg['To'] = to_email
        msg['Subject'] = subject

        if is_html:
            msg.attach(MIMEText(body, 'html'))
        else:
            msg.attach(MIMEText(body, 'plain'))

        # Create SMTP session with detailed logging
        app.logger.info(f"Connecting to SMTP server: {smtp_server}:{smtp_port}")
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.set_debuglevel(1) # Enable SMTP debugging

        app.logger.info("Starting TLS...")
        server.starttls() # Enable TLS encryption

        app.logger.info("Attempting login...")
        server.login(email_username, email_password)

        # Send email
        app.logger.info("Sending email...")
        text = msg.as_string()
        server.sendmail(email_username, to_email, text)
        server.quit()

        app.logger.info(f"Email sent successfully to {to_email}")
        return True

    except smtplib.SMTPAuthenticationError as e:
        app.logger.error(f"SMTP Authentication Error: {e}")
        app.logger.error("Possible causes: Incorrect Gmail App Password, 2-Step Verification not set up, or Less Secure App Access not enabled (if 2SV is off).")
        return False
    except smtplib.SMTPException as e:
        app.logger.error(f"SMTP Error: {e}")
        return False
    except Exception as e:
        app.logger.error(f"General error sending email: {e}")
        traceback.print_exc()
        return False

def send_registration_notification(user_email, user_name, phone_number):
    """Send notification email to both admin and the new user"""
    admin_email = app.config['ADMIN_EMAIL']

    email_password = get_email_password()
    if not email_password:
        app.logger.warning("Email password not available. Skipping notification.")
        return False

    # Email subject
    subject = f"Nuevo Usuario Registrado - {user_name}"

    # Create HTML email body
    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #2563eb; border-bottom: 2px solid #2563eb; padding-bottom: 10px;">
    Nuevo Usuario Registrado - SMT SecApp
    </h2>

    <div style="background-color: #f8fafc; padding: 20px; border-radius: 8px; margin: 20px 0;">
    <h3 style="color: #1e40af; margin-top: 0;">Detalles del Usuario:</h3>
    <p><strong>Nombre:</strong> {user_name}</p>
    <p><strong>Email:</strong> {user_email}</p>
    <p><strong>Teléfono:</strong> {phone_number or 'No proporcionado'}</p>
    <p><strong>Fecha de Registro:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    </div>

    <div style="background-color: #dbeafe; padding: 15px; border-radius: 8px; border-left: 4px solid #2563eb;">
    <p style="margin: 0;"><strong>Nota:</strong> Este usuario se ha registrado exitosamente en el sistema SMT SecApp.</p>
    </div>

    <div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #e5e7eb;">
    <p style="color: #6b7280; font-size: 14px;">
    Este es un mensaje automático del sistema de registro de SMT SecApp.
    </p>
    </div>
    </div>
    </body>
    </html>
    """

    # Send to admin
    app.logger.info(f"Sending registration notification to admin: {admin_email}")
    admin_result = send_email(admin_email, subject, html_body, is_html=True)

    # Send copy to the user as confirmation
    app.logger.info(f"Sending registration confirmation to user: {user_email}")
    user_result = send_email(user_email, f"Confirmación de Registro - {user_name}", html_body, is_html=True)

    return admin_result and user_result

def send_welcome_email(user_email, user_name):
    """Send welcome email to the newly registered user"""
    subject = "¡Bienvenido a SMT SecApp!"

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
    <div style="text-align: center; margin-bottom: 30px;">
    <img src="https://storage.googleapis.com/smt-misc/SMT-logo.png" alt="SMT Logo" style="width: 80px; opacity: 0.9;">
    </div>

    <h2 style="color: #2563eb; text-align: center;">¡Bienvenido a SMT SecApp!</h2>

    <div style="background-color: #f8fafc; padding: 20px; border-radius: 8px; margin: 20px 0;">
    <p>Hola <strong>{user_name}</strong>,</p>
    <p>¡Tu cuenta ha sido creada exitosamente! Ahora puedes acceder a todas las funcionalidades de SMT SecApp.</p>
    </div>

    <div style="background-color: #dbeafe; padding: 15px; border-radius: 8px; margin: 20px 0;">
    <h3 style="color: #1e40af; margin-top: 0;">Próximos Pasos:</h3>
    <ul style="margin: 10px 0;">
    <li>Inicia sesión con tu email: <strong>{user_email}</strong></li>
    <li>Explora las funcionalidades del sistema</li>
    <li>Contacta al administrador si tienes alguna pregunta</li>
    </ul>
    </div>

    <div style="text-align: center; margin: 30px 0;">
    <a href="{os.environ.get('LOGIN_SERVICE_URL', '#')}"
    style="background-color: #2563eb; color: white; padding: 12px 30px;
    text-decoration: none; border-radius: 6px; font-weight: bold;">
    Iniciar Sesión
    </a>
    </div>

    <div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #e5e7eb;">
    <p style="color: #6b7280; font-size: 14px; text-align: center;">
    Gracias por unirte a SMT SecApp.<br>
    Para soporte, contacta a rcanton@tzolkintech.com
    </p>
    </div>
    </div>
    </body>
    </html>
    """

    return send_email(user_email, subject, html_body, is_html=True)

# --- DB Connection ---
def get_db_connection():
    try:
        db_url = os.environ.get('DATABASE_URL')
        if not db_url:
            app_logger.error("DATABASE_URL environment variable not set.")
            flash('Error de configuración de la base de datos.', 'danger')
            return None
        conn = psycopg2.connect(db_url)
        return conn
    except Exception as e:
        app_logger.error(f"DB connection error: {e}", exc_info=True)
        flash('Error de conexión a la base de datos.', 'danger')
        return None

# --- JWT Error Handling ---
@jwt.unauthorized_loader
@jwt.invalid_token_loader
@jwt.expired_token_loader
def token_error_response(callback):
    flash('Su sesión ha caducado o es inválida. Por favor, inicie sesión de nuevo.', 'danger')
    # LOGIN_SERVICE_URL MUST be set in the environment
    login_url = os.environ.get('LOGIN_SERVICE_URL', '/')
    if not login_url.endswith('/login'):
        login_url = f"{login_url.rstrip('/')}/login"
    return redirect(login_url)

# --- CORS ---
@app.after_request
def add_cors_headers(response):
    # LANDING_SERVICE_URL MUST be set in the environment
    allowed_origin = os.environ.get('LANDING_SERVICE_URL', '*') # Use '*' as fallback only if you understand the risks
    response.headers['Access-Control-Allow-Origin'] = allowed_origin
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

# --- Routes ---
@app.route('/')
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username') # This is actually the email
        password = request.form.get('password')

        conn = get_db_connection()
        if not conn:
            return render_template('login.html', username=username)

        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)
            cur.execute("SELECT id, email, password_hash FROM users WHERE email = %s", (username,))
            user = cur.fetchone()
            cur.close()

            if user and bcrypt.check_password_hash(user['password_hash'], password):
                access_token = create_access_token(identity=user['email'])
                refresh_token = create_refresh_token(identity=user['email'])

                # LANDING_SERVICE_URL MUST be set in the environment
                response = redirect(os.environ.get('LANDING_SERVICE_URL', '/'))
                set_access_cookies(response, access_token)
                set_refresh_cookies(response, refresh_token)
                flash('Inicio de sesión exitoso.', 'success')
                return response
            else:
                flash('Usuario o contraseña incorrectos.', 'danger')
                return render_template('login.html', username=username)
        except Exception as e:
            app_logger.error(f"Login error: {e}", exc_info=True)
            flash('Error durante el inicio de sesión.', 'danger')
            return render_template('login.html', username=username)
        finally:
            if conn:
                conn.close()

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    email = request.form.get('email', '')
    name = request.form.get('name', '')
    phone_number = request.form.get('phone_number', '')

    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not all([email, name, password, confirm_password]):
            flash('Todos los campos obligatorios son requeridos.', 'warning')
            return render_template('register.html', email=email, name=name, phone_number=phone_number)

        if password != confirm_password:
            flash('Las contraseñas no coinciden.', 'danger')
            return render_template('register.html', email=email, name=name, phone_number=phone_number)

        conn = get_db_connection()
        if not conn:
            return render_template('register.html', email=email, name=name, phone_number=phone_number)

        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)

            cur.execute("SELECT id FROM authorized_emails WHERE email = %s AND is_active = TRUE", (email,))
            authorized_email_entry = cur.fetchone()

            if not authorized_email_entry:
                flash('No estás autorizado para registrarte. Por favor, contacta a tu administrador.', 'danger')
                app_logger.warning(f"Registration attempt by unauthorized email: {email}")
                return render_template('register.html', email=email, name=name, phone_number=phone_number)

            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            existing_user_email = cur.fetchone()
            if existing_user_email:
                flash('Este correo electrónico ya está registrado. Por favor, inicia sesión.', 'danger')
                return render_template('register.html', email=email, name=name, phone_number=phone_number)

            hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')

            cur.execute(
                "INSERT INTO users (username, email, name, phone_number, password_hash) VALUES (%s, %s, %s, %s, %s)",
                (email, email, name, phone_number if phone_number else None, hashed_password)
            )
            conn.commit()
            cur.close()

            app_logger.info(f"Starting email notifications for user: {email}")

            email_issues = []

            app_logger.info("Sending registration notification to admin and user...")
            notification_sent = send_registration_notification(email, name, phone_number)
            if notification_sent:
                app_logger.info(f"Registration notification sent successfully for user: {email}")
            else:
                app_logger.error(f"Failed to send registration notification for user: {email}")
                email_issues.append("registration notification")

            app_logger.info("Sending welcome email to user...")
            welcome_sent = send_welcome_email(email, name)
            if welcome_sent:
                app_logger.info(f"Welcome email sent successfully to user: {email}")
            else:
                app_logger.error(f"Failed to send welcome email to user: {email}")
                email_issues.append("welcome email")

            if email_issues:
                flash(f'¡Registro exitoso! Nota: No se pudieron enviar algunos emails ({", ".join(email_issues)}). Contacta al administrador si es necesario.', 'warning')
            else:
                flash('¡Registro exitoso! Se han enviado emails de confirmación. Ahora puedes iniciar sesión.', 'success')

            app_logger.info(f"User {email} registered successfully.")
            return redirect(url_for('login'))

        except psycopg2.errors.UniqueViolation as e:
            conn.rollback()
            if "users_username_key" in str(e) or "users_email_key" in str(e):
                flash('Este correo electrónico ya está registrado. Por favor, inicia sesión.', 'danger')
            else:
                flash('Error de registro: un valor duplicado ya existe.', 'danger')
            app_logger.error(f"Unique violation during registration: {e}", exc_info=True)
            return render_template('register.html', email=email, name=name, phone_number=phone_number)

        except Exception as e:
            conn.rollback()
            app_logger.error(f"Error during registration: {e}", exc_info=True)
            flash('Ocurrió un error inesperado durante el registro. Por favor, inténtalo de nuevo.', 'danger')
            return render_template('register.html', email=email, name=name, phone_number=phone_number)
        finally:
            if conn:
                conn.close()

    return render_template('register.html')


@app.route('/logout')
def logout():
    response = redirect(url_for('login'))
    unset_jwt_cookies(response)
    flash('Sesión cerrada.', 'info')
    return response

# --- Health Check Route ---
@app.route('/health')
def health_check():
    health_status = {
        'status': 'healthy',
        'service': 'login-service',
        'timestamp': datetime.now().isoformat()
    }

    try:
        conn = get_db_connection()
        if conn:
            health_status['database'] = 'connected'
            conn.close()
        else:
            health_status['database'] = 'disconnected'
            health_status['status'] = 'unhealthy'
    except Exception as e:
        health_status['database'] = f'error: {str(e)}'
        health_status['status'] = 'unhealthy'

    status_code = 200 if health_status['status'] == 'healthy' else 503
    return health_status, status_code

# Add a startup check route
@app.route('/startup')
def startup_check():
    app_logger.info("Startup check requested.")
    return {
        'status': 'ready',
        'service': 'login-service',
        'timestamp': datetime.now().isoformat()
    }, 200

# You would typically remove these test routes in production
@app.route('/test-email')
def test_email():
    email_password = get_email_password()
    if not email_password:
        return "Email password not configured or accessible from Secret Manager."

    test_result = send_email(
        app.config['ADMIN_EMAIL'], # Send test email to admin email configured
        "Test Email - SMT SecApp (Prod)",
        "This is a test email to verify production email configuration with Secret Manager is working.",
        is_html=False
    )

    if test_result:
        return f"Test email sent successfully! Check {app.config['ADMIN_EMAIL']}"
    else:
        return "Test email failed. Check logs for details."

@app.route('/debug-email')
def debug_email():
    email_password = get_email_password()

    debug_info = {
        'email_username': app.config.get('EMAIL_USERNAME'),
        'email_password_set': bool(email_password),
        'smtp_server': app.config.get('SMTP_SERVER'),
        'smtp_port': app.config.get('SMTP_PORT'),
        'admin_email': app.config.get('ADMIN_EMAIL'),
        'project_id': app.config.get('PROJECT_ID'),
        'secret_name': app.config.get('SECRET_NAME')
    }

    test_result = None
    if debug_info['email_username'] and debug_info['email_password_set']:
        test_result = send_email(
            app.config['ADMIN_EMAIL'],
            "Debug Test Email - SMT SecApp (Prod)",
            "This is a test email from the debug route to verify Secret Manager integration is working.",
            is_html=False
        )

    return {
        'config': debug_info,
        'test_email_sent': test_result,
        'message': 'Check your application logs for detailed SMTP debug output'
    }

# Placeholder Routes - these would be actual service calls in a real setup
# You might remove these from your login service as it only handles login
@app.route('/dashboard_placeholder')
@jwt_required()
def dashboard_placeholder():
    user = get_jwt_identity()
    return f"<h1>Dashboard: Bienvenido {user}</h1>"

@app.route('/forms_placeholder')
@jwt_required()
def forms_placeholder():
    user = get_jwt_identity()
    return f"<h1>Formulario: Bienvenido {user}</h1>"

# --- Main App Entry Point (for Cloud Run) ---
if __name__ == '__main__':
    # When deployed to Cloud Run, the PORT environment variable is automatically set.
    # We explicitly listen on 0.0.0.0 to bind to all available network interfaces.
    port = int(os.environ.get('PORT', 8080)) # Cloud Run sets PORT

    # No debug mode in production unless explicitly needed for advanced debugging.
    # FLASK_ENV is not typically 'development' in Cloud Run.
    # The 'is_production' flag already handles JWT_COOKIE_SECURE based on K_SERVICE.
    app_logger.info(f"Starting Flask app on port {port}")
    app.run(host='0.0.0.0', port=port, threaded=True)

2. app.py (Forms Service)
Python

import os
from flask import Flask, render_template, request, redirect, url_for, flash
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity
import logging
from google.cloud import secretmanager
import traceback

logging.basicConfig(level=logging.INFO)
app_logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Flask App Configuration ---
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY')
if not app.config['SECRET_KEY']:
    app_logger.error("FATAL: FLASK_SECRET_KEY environment variable not set for Forms service. App cannot start securely.")
    raise ValueError("FLASK_SECRET_KEY environment variable not set for Forms service.")

# JWT Configuration (MUST match login and dashboard services)
is_production = os.environ.get('K_SERVICE') is not None
app.config['JWT_COOKIE_SECURE'] = is_production
app.config['JWT_TOKEN_LOCATION'] = ['cookies']
app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', '.run.app')

app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)

app.config['PROJECT_ID'] = os.environ.get('GCP_PROJECT_ID') # MUST be set in production

# --- Secret Manager Functions ---
def get_secret_value(secret_name, project_id=None):
    """Retrieve secret value from GCP Secret Manager"""
    try:
        if not project_id:
            project_id = app.config.get('PROJECT_ID')

        if not project_id:
            app_logger.error("PROJECT_ID not found in environment variables or app config for Forms service Secret Manager access.")
            return None

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        secret_value = response.payload.data.decode("UTF-8")
        app_logger.info(f"Forms service: Successfully retrieved secret: {secret_name}")
        return secret_value

    except Exception as e:
        app_logger.error(f"Forms service: Error retrieving secret {secret_name}: {e}", exc_info=True)
        return None

# Function to get JWT Secret
def get_jwt_secret():
    """Get JWT secret key from environment or Secret Manager."""
    secret_key = os.environ.get('JWT_SECRET_KEY')
    if secret_key:
        app_logger.info("Forms service: Using JWT_SECRET_KEY from environment variable.")
        return secret_key

    app_logger.info("Forms service: Attempting to retrieve JWT_SECRET_KEY from Secret Manager.")
    return get_secret_value('jwt-secret-key', app.config.get('PROJECT_ID'))

# Set JWT Secret Key from Secret Manager or environment
jwt_secret = get_jwt_secret()
if not jwt_secret:
    app_logger.error("FATAL: JWT_SECRET_KEY not found for Forms service. App cannot start securely.")
    raise ValueError("JWT_SECRET_KEY not set in production for Forms service!")
else:
    app.config['JWT_SECRET_KEY'] = jwt_secret

jwt = JWTManager(app) # Initialize JWTManager after the secret is set

# Service URLs MUST be set as environment variables in production
app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL')
app.config['DASHBOARD_SERVICE_URL'] = os.environ.get('DASHBOARD_SERVICE_URL')
app.config['LANDING_SERVICE_URL'] = os.environ.get('LANDING_SERVICE_URL')

if not all([app.config['LOGIN_SERVICE_URL'], app.config['DASHBOARD_SERVICE_URL'], app.config['LANDING_SERVICE_URL']]):
    app_logger.error("FATAL: Service URLs (LOGIN_SERVICE_URL, DASHBOARD_SERVICE_URL, LANDING_SERVICE_URL) not fully set for Forms service. App cannot function correctly.")
    if is_production:
        raise ValueError("Missing service URLs in production for Forms service.")

# --- Database Connection ---
def get_db_connection():
    try:
        db_url = os.environ.get('DATABASE_URL')
        if not db_url:
            app_logger.error("DATABASE_URL environment variable not set for Forms service.")
            flash('Error de configuración de la base de datos.', 'error')
            return None
        conn = psycopg2.connect(db_url)
        app_logger.info("Forms service database connection successful.")
        return conn
    except Exception as e:
        app_logger.error(f"Forms service: Error connecting to DB: {e}", exc_info=True)
        flash('Error de conexión a la base de datos.', 'error')
        return None

# --- JWT Callbacks for Error Handling and Redirection ---
@jwt.unauthorized_loader
def unauthorized_response(callback):
    login_url = app.config['LOGIN_SERVICE_URL']
    if not login_url.endswith('/login'):
        login_url = f"{login_url.rstrip('/')}/login"
    flash('Por favor, inicie sesión para acceder a esta página.', 'warning')
    app_logger.warning(f"Unauthorized access attempt to forms service. Redirecting to {login_url}")
    return redirect(login_url)

@jwt.invalid_token_loader
def invalid_token_response(callback):
    login_url = app.config['LOGIN_SERVICE_URL']
    if not login_url.endswith('/login'):
        login_url = f"{login_url.rstrip('/')}/login"
    flash('Token de sesión inválido. Por favor, inicie sesión de nuevo.', 'danger')
    app_logger.warning(f"Invalid token for forms service. Redirecting to {login_url}")
    return redirect(login_url)

@jwt.expired_token_loader
def expired_token_response(callback):
    login_url = app.config['LOGIN_SERVICE_URL']
    if not login_url.endswith('/login'):
        login_url = f"{login_url.rstrip('/')}/login"
    flash('Su sesión ha expirado. Por favor, inicie sesión de nuevo.', 'warning')
    app_logger.warning(f"Expired token for forms service. Redirecting to {login_url}")
    return redirect(login_url)

# --- CORS Headers ---
@app.after_request
def add_cors_headers(response):
    allowed_origin = app.config['LANDING_SERVICE_URL']
    # If allowed_origin is None, you might want to raise an error or handle it.
    # For now, it will use 'None' as origin which will fail CORS.
    response.headers['Access-Control-Allow-Origin'] = allowed_origin if allowed_origin else '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

# --- Routes ---

@app.route('/')
@jwt_required()
def index():
    return redirect(url_for('show_report_form'))

@app.route('/report_form', methods=['GET'])
@jwt_required()
def show_report_form():
    current_user_identity = get_jwt_identity()
    app_logger.info(f"User {current_user_identity} accessing report form.")

    conn = get_db_connection()
    if conn is None:
        return redirect(app.config['LOGIN_SERVICE_URL'] + '/login')

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT id_tipo_incidencia AS id, nombre FROM tipo_incidencia ORDER BY nombre;")
        tipo_incidencia_data = cur.fetchall()
        cur.execute("SELECT id_tipo_cliente AS id, nombre FROM tipo_cliente ORDER BY nombre;")
        tipo_cliente_data = cur.fetchall()
        cur.execute("SELECT id_lugar_incidente AS id, nombre FROM lugar_incidente ORDER BY nombre;")
        lugar_incidente_data = cur.fetchall()
        cur.execute("SELECT id_supervisor AS id, nombre FROM supervisor ORDER BY nombre;")
        supervisor_data = cur.fetchall()
        cur.close()
        return render_template(
            'form.html',
            tipo_incidencia=tipo_incidencia_data,
            tipo_cliente=tipo_cliente_data,
            lugar_incidente=lugar_incidente_data,
            supervisor=supervisor_data,
            username=current_user_identity,
            login_service_url=app.config['LOGIN_SERVICE_URL'],
            dashboard_service_url=app.config['DASHBOARD_SERVICE_URL']
        )
    except psycopg2.Error as e:
        app_logger.error(f"Forms service: DB error loading form data: {e}", exc_info=True)
        flash("Error al cargar datos del formulario.", 'error')
        return redirect(app.config['LOGIN_SERVICE_URL'] + '/login')
    finally:
        if conn:
            conn.close()

@app.route('/submit_report', methods=['POST'])
@jwt_required()
def submit_report():
    current_user_email = get_jwt_identity()
    app_logger.info(f"User {current_user_email} submitting report.")

    data = request.form
    if not data:
        flash("No se recibió información del formulario.", 'error')
        return redirect(url_for('show_report_form'))

    required_fields = {
        'tipo_incidencia': data.get('tipo_incidencia'),
        'tipo_cliente': data.get('tipo_cliente'),
        'lugar_incidente': data.get('lugar_incidente'),
        'fecha_incidente': data.get('fecha_incidente'),
        'hora_incidente': data.get('hora_incidente'),
        'descripcion_incidente': data.get('descripcion_incidente'),
        'nombre_persona': data.get('nombre_persona'),
        'supervisor': data.get('supervisor')
    }

    for field, value in required_fields.items():
        if not value:
            flash(f"El campo '{field.replace('_', ' ').capitalize()}' es requerido.", 'error')
            return redirect(url_for('show_report_form'))

    conn = get_db_connection()
    if conn is None:
        flash("No se pudo conectar a la base de datos.", 'error')
        return redirect(url_for('show_report_form'))

    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO reportes_incidentes (
                id_tipo_incidencia, id_tipo_cliente, id_lugar_incidente,
                descripcion_zona_comun, fecha_incidente, hora_incidente,
                descripcion_incidente, valor_aproximado, pertenencias_sustraidas,
                nombre_persona, telefono_persona, numero_identidad_persona,
                numero_local, direccion, imagenes_pdfs, id_supervisor,
                user_email,
                creado_en
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            data.get('tipo_incidencia'), data.get('tipo_cliente'), data.get('lugar_incidente'),
            data.get('descripcion_zona_comun') or None,
            data.get('fecha_incidente'), data.get('hora_incidente'),
            data.get('descripcion_incidente'),
            float(data.get('valor_aproximado')) if data.get('valor_aproximado') else None,
            data.get('pertenencias_sustraidas') or None,
            data.get('nombre_persona'), data.get('telefono_persona') or None,
            data.get('numero_identidad_persona') or None, data.get('numero_local') or None,
            data.get('direccion') or None, data.get('imagenes_pdfs') or None,
            data.get('supervisor'),
            current_user_email,
            datetime.now()
        ))
        conn.commit()
        cur.close()
        flash("¡Reporte enviado exitosamente!", 'success')
        app_logger.info(f"Forms service: Report submitted successfully by {current_user_email}.")
        return redirect(url_for('show_report_form'))
    except Exception as e:
        conn.rollback()
        app_logger.error(f"Forms service: Error saving report for {current_user_email}: {e}", exc_info=True)
        flash("Error al guardar el reporte en la base de datos.", 'error')
        return redirect(url_for('show_report_form'))
    finally:
        if conn:
            conn.close()

# --- Health Check Route ---
@app.route('/health')
def health_check():
    health_status = {
        'status': 'healthy',
        'service': 'forms-service',
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
    
    app_logger.info(f"Forms service: Health check status: {health_status['status']}")
    return health_status, status_code

# Add a startup check route
@app.route('/startup')
def startup_check():
    app_logger.info("Forms service: Startup check requested.")
    return {
        'status': 'ready',
        'service': 'forms-service',
        'timestamp': datetime.now().isoformat()
    }, 200

# --- Main App Entry Point (for Cloud Run) ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080)) # Cloud Run sets PORT
    app_logger.info(f"Forms service: Starting Flask app on port {port}")
    app.run(host='0.0.0.0', port=port, threaded=True)

3. app.py (Dashboard Service)
Python

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
app.secret_key = os.environ.get('FLASK_SECRET_KEY')
if not app.secret_key:
    app_logger.error("FATAL: FLASK_SECRET_KEY environment variable not set for Dashboard service. App cannot start securely.")
    raise ValueError("FLASK_SECRET_KEY environment variable not set for Dashboard service.")


# JWT Configuration (MUST match login and forms services)
is_production = os.environ.get('K_SERVICE') is not None
app.config['JWT_COOKIE_SECURE'] = is_production
app.config['JWT_TOKEN_LOCATION'] = ['cookies']
app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', '.run.app') # Sensible default for Cloud Run

app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)

# --- Email Config ---
app.config['SMTP_SERVER'] = os.environ.get('SMTP_SERVER')
app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', 587))
app.config['EMAIL_USERNAME'] = os.environ.get('EMAIL_USERNAME')
app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL')
app.config['PROJECT_ID'] = os.environ.get('GCP_PROJECT_ID') # MUST be set in production
app.config['SECRET_NAME'] = os.environ.get('EMAIL_PASSWORD_SECRET', 'admin-email-pass')

# Ensure critical email configs are set
if not all([app.config['SMTP_SERVER'], app.config['EMAIL_USERNAME'], app.config['ADMIN_EMAIL'], app.config['PROJECT_ID']]):
    app_logger.error("FATAL: Incomplete Email or GCP PROJECT_ID configuration for Dashboard service. Check environment variables.")
    if is_production:
        raise ValueError("Email or GCP PROJECT_ID configuration missing for Dashboard service.")


# --- Secret Manager Functions ---
def get_secret_value(secret_name, project_id=None):
    try:
        if not project_id:
            project_id = app.config.get('PROJECT_ID')
        
        if not project_id:
            app_logger.error("PROJECT_ID not found in environment variables for Secret Manager access (Dashboard service).")
            return None
        
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        secret_value = response.payload.data.decode("UTF-8")
        app_logger.info(f"Dashboard service: Successfully retrieved secret: {secret_name}")
        return secret_value
        
    except Exception as e:
        app_logger.error(f"Dashboard service: Error retrieving secret {secret_name}: {e}", exc_info=True)
        return None

def get_email_password():
    password = os.environ.get('EMAIL_PASSWORD')
    if password:
        app_logger.info("Dashboard service: Using email password from environment variable.")
        return password
    
    app_logger.info("Dashboard service: Attempting to retrieve email password from Secret Manager.")
    return get_secret_value(app.config['SECRET_NAME'])

# Function to get JWT Secret
def get_jwt_secret():
    """Get JWT secret key from environment or Secret Manager."""
    secret_key = os.environ.get('JWT_SECRET_KEY')
    if secret_key:
        app_logger.info("Dashboard service: Using JWT_SECRET_KEY from environment variable.")
        return secret_key

    app_logger.info("Dashboard service: Attempting to retrieve JWT_SECRET_KEY from Secret Manager.")
    return get_secret_value('jwt-secret-key', app.config.get('PROJECT_ID'))

# Set JWT Secret Key from Secret Manager or environment
jwt_secret = get_jwt_secret()
if not jwt_secret:
    app_logger.error("FATAL: JWT_SECRET_KEY not found for Dashboard service. App cannot start securely.")
    raise ValueError("JWT_SECRET_KEY not set in production for Dashboard service!")
else:
    app.config['JWT_SECRET_KEY'] = jwt_secret

jwt = JWTManager(app) # Initialize JWTManager after the secret is set

# --- Email Functions ---
def send_email(to_email, subject, body, is_html=False):
    try:
        email_username = app.config.get('EMAIL_USERNAME')
        email_password = get_email_password()
        smtp_server = app.config.get('SMTP_SERVER')
        smtp_port = app.config.get('SMTP_PORT')
        
        app_logger.info(f"Email config check - Username: {email_username}, Server: {smtp_server}, Port: {smtp_port}")
        
        if not all([email_username, email_password, smtp_server, smtp_port]):
            app_logger.warning(f"Email configuration incomplete. Username: {email_username}, Password: {'Set' if email_password else 'Not Set'}, SMTP Server/Port: {smtp_server}:{smtp_port}")
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
            app_logger.error("DATABASE_URL environment variable not set for Dashboard service.")
            flash('Error de configuración de la base de datos para el dashboard.', 'error')
            return None

        conn = psycopg2.connect(db_url)
        app_logger.info("Dashboard service database connection successful.")
        return conn
    except Exception as e:
        app_logger.error(f"Dashboard service: Error connecting to dashboard database: {e}", exc_info=True)
        flash('Error de conexión a la base de datos para el dashboard.', 'error')
        return None

# --- JWT Callbacks for Error Handling and Redirection ---
@jwt.unauthorized_loader
def unauthorized_response(callback):
    # LOGIN_SERVICE_URL MUST be set in the environment
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
        app_logger.error(f"Dashboard service: Database error fetching dashboard data for {current_user_identity}: {e}", exc_info=True)
        flash(f"Error al cargar datos del dashboard: {e}", 'error')
    except Exception as e:
        app_logger.error(f"Dashboard service: An unexpected error occurred while fetching dashboard data for {current_user_identity}: {e}", exc_info=True)
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
        app_logger.error(f"Dashboard service: Database error during CSV export for {current_user_identity}: {e}", exc_info=True)
        return "Error al exportar datos a CSV (DB Error).", 500
    except Exception as e:
        app_logger.error(f"Dashboard service: An unexpected error occurred during CSV export for {current_user_identity}: {e}", exc_info=True)
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
        app_logger.error(f"Dashboard service: Database error during email report generation for {current_user_identity}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': 'Error al generar los reportes para el correo (DB Error).'}), 500
    except Exception as e:
        app_logger.error(f"Dashboard service: An unexpected error occurred during email report generation for {current_user_identity}: {e}", exc_info=True)
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
    
    app_logger.info(f"Dashboard service: Health check status: {health_status['status']}")
    return health_status, status_code

# Add a startup check route
@app.route('/startup')
def startup_check():
    app_logger.info("Dashboard service: Startup check requested.")
    return {
        'status': 'ready',
        'service': 'dashboard-service',
        'timestamp': datetime.now().isoformat()
    }, 200

# --- Main App Entry Point (for Cloud Run) ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080)) # Cloud Run sets PORT
    app_logger.info(f"Dashboard service: Starting Flask app on port {port}")
    app.run(host='0.0.0.0', port=port, threaded=True)
