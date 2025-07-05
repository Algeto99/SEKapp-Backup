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

logging.basicConfig(level=logging.INFO)
app_logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Flask Config ---
# FLASK_SECRET_KEY MUST be set as an environment variable in production
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY')
if not app.config['SECRET_KEY']:
    app_logger.error("FATAL ERROR: FLASK_SECRET_KEY environment variable not set. Application cannot start securely.")
    raise ValueError("FLASK_SECRET_KEY environment variable not set.")
app_logger.info("FLASK_SECRET_KEY successfully loaded.")


# Dynamic JWT_COOKIE_SECURE based on environment (Cloud Run uses K_SERVICE)
is_production = os.environ.get('K_SERVICE') is not None # K_SERVICE is set in Cloud Run
app.config['JWT_COOKIE_SECURE'] = is_production
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)
app.config['JWT_TOKEN_LOCATION'] = ['cookies']
app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
# JWT_COOKIE_DOMAIN MUST be set to '.run.app' in production or your custom domain
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', '.run.app') # Sensible default for Cloud Run
app_logger.info(f"JWT_COOKIE_SECURE: {app.config['JWT_COOKIE_SECURE']} (is_production: {is_production})")
app_logger.info(f"JWT_COOKIE_DOMAIN: {app.config['JWT_COOKIE_DOMAIN']}")


# --- Email Config ---
app.config['SMTP_SERVER'] = os.environ.get('SMTP_SERVER')
app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', 587))
app.config['EMAIL_USERNAME'] = os.environ.get('EMAIL_USERNAME')
app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL')
app.config['PROJECT_ID'] = os.environ.get('GCP_PROJECT_ID') # MUST be set in production
app.config['SECRET_NAME'] = os.environ.get('EMAIL_PASSWORD_SECRET', 'admin-email-pass')

# Ensure critical email configs are set, especially in production
if is_production and not all([app.config['SMTP_SERVER'], app.config['EMAIL_USERNAME'], app.config['ADMIN_EMAIL'], app.config['PROJECT_ID']]):
    missing_email_configs = [k for k, v in {
        'SMTP_SERVER': app.config['SMTP_SERVER'],
        'EMAIL_USERNAME': app.config['EMAIL_USERNAME'],
        'ADMIN_EMAIL': app.config['ADMIN_EMAIL'],
        'GCP_PROJECT_ID': app.config['PROJECT_ID']
    }.items() if not v]
    app_logger.error(f"FATAL ERROR: Incomplete Email or GCP PROJECT_ID configuration for production. Missing: {', '.join(missing_email_configs)}. App cannot start.")
    raise ValueError(f"Incomplete Email or GCP PROJECT_ID configuration in production. Missing: {', '.join(missing_email_configs)}.")
app_logger.info("Email configuration checked.")


# --- Extensions ---
bcrypt = Bcrypt(app)
# JWTManager will be initialized AFTER JWT_SECRET_KEY is set

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
        app_logger.error(f"Login service: Error retrieving secret {secret_name} from Secret Manager: {e}", exc_info=True)
        return None

def get_email_password():
    """Get email password from environment or Secret Manager"""
    password = os.environ.get('EMAIL_PASSWORD') # Try environment variable first
    if password:
        app_logger.info("Login service: Using email password from environment variable.")
        return password
    
    app_logger.info("Login service: Attempting to retrieve email password from Secret Manager.")
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
    app_logger.info("Login service: Attempting to retrieve JWT_SECRET_KEY from Secret Manager ('jwt-secret-key').")
    # 'jwt-secret-key' is the name of your secret in Secret Manager
    return get_secret_value('jwt-secret-key', app.config.get('PROJECT_ID'))

# Set JWT Secret Key from Secret Manager or environment
jwt_secret = get_jwt_secret()
if not jwt_secret:
    app_logger.error("FATAL ERROR: JWT_SECRET_KEY not found for Login service (neither from environment nor Secret Manager). Application cannot start securely.")
    # In production, this should cause a hard failure if the secret is missing
    raise ValueError("JWT_SECRET_KEY not set in production for Login service!")
else:
    app.config['JWT_SECRET_KEY'] = jwt_secret
app_logger.info("JWT_SECRET_KEY successfully loaded.")


jwt = JWTManager(app) # Initialize JWTManager after the secret is set

# --- Email Functions ---
def send_email(to_email, subject, body, is_html=False):
    """Send email notification"""
    try:
        email_username = app.config.get('EMAIL_USERNAME')
        email_password = get_email_password()
        smtp_server = app.config.get('SMTP_SERVER')
        smtp_port = app.config.get('SMTP_PORT')
        
        app_logger.info(f"Email config check - Username: {email_username}, Server: {smtp_server}, Port: {smtp_port}")
        
        if not all([email_username, email_password, smtp_server, smtp_port]):
            app_logger.warning(f"Email configuration incomplete. Username: {email_username}, Password: {'Set' if email_password else 'Not Set'}, SMTP Server/Port: {smtp_server}:{smtp_port}. Skipping email send.")
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
        app_logger.error(f"SMTP Authentication Error: {e}. Possible causes: Incorrect password, 2FA, or app password issues. Ensure service account has Secret Manager access or EMAIL_PASSWORD env var is correct.", exc_info=True)
        return False
    except smtplib.SMTPException as e:
        app_logger.error(f"SMTP Error: {e}", exc_info=True)
        return False
    except Exception as e:
        app_logger.error(f"General error sending email: {e}", exc_info=True)
        return False

def send_registration_notification(user_email, user_name, phone_number):
    """Send notification email to both admin and the new user"""
    admin_email = app.config['ADMIN_EMAIL']

    email_password = get_email_password()
    if not email_password:
        app_logger.warning("Email password not available. Skipping notification.")
        return False

    subject = f"Nuevo Usuario Registrado - {user_name}"

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

    admin_result = send_email(admin_email, subject, html_body, is_html=True)
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
        app_logger.info("Database connection successful within request context.")
        return conn
    except Exception as e:
        app_logger.error(f"DB connection error: {e}", exc_info=True)
        flash