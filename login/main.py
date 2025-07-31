import os
import smtplib
import socket
import ssl # Keep this import, smtplib might implicitly use it
import re
import secrets
import hashlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, current_app
from flask_bcrypt import Bcrypt
from flask_jwt_extended import (
    create_access_token, create_refresh_token, unset_jwt_cookies,
    set_access_cookies, set_refresh_cookies, jwt_required,
    get_jwt_identity, JWTManager, get_jwt
)
import psycopg2
from psycopg2 import extras
from datetime import timedelta, datetime, timezone
from google.cloud import secretmanager
from google.api_core.exceptions import NotFound
import traceback
import logging
import sys
import requests
from flask_cors import CORS
from flask_wtf.csrf import CSRFProtect
import google.auth.transport.requests
import google.oauth2.id_token

# --- Configure Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
app_logger = logging.getLogger(__name__)

# --- Initialize Flask App ---
app = Flask(__name__)
is_production = os.environ.get('K_SERVICE') is not None

# Set SECRET_KEY directly after app initialization
# This ensures it's available when CSRFProtect initializes and when templates are rendered.
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY')
if not app.config['SECRET_KEY']:
    app_logger.critical("FLASK_SECRET_KEY environment variable NOT SET. Exiting.")
    sys.exit(1)

# Initialize CSRFProtect with the app
csrf = CSRFProtect(app)

# --- CloudRunServiceClient ---
class CloudRunServiceClient:
    def __init__(self, service_url):
        self.service_url = service_url.rstrip('/')
        self.request_adapter = google.auth.transport.requests.Request()
        app_logger.info(f"CloudRunServiceClient initialized for URL: {self.service_url}")

    def _get_id_token(self):
        try:
            return google.oauth2.id_token.fetch_id_token(self.request_adapter, self.service_url)
        except Exception as e:
            app_logger.error(f"Failed to fetch ID token for {self.service_url}: {e}", exc_info=True)
            raise

    def call_service(self, endpoint, method='GET', data=None):
        url = f"{self.service_url}{endpoint}"
        try:
            id_token = self._get_id_token()
        except Exception as e:
            app_logger.error(f"Skipping call to {url} due to ID token fetching failure: {e}")
            return None

        headers = {
            'Authorization': f'Bearer {id_token}',
            'Content-Type': 'application/json',
            'User-Agent': 'LoginService/1.0'
        }
        app_logger.info(f"Making {method} request to {url}")
        try:
            if method.upper() == 'GET':
                response = requests.get(url, headers=headers, timeout=10)
            elif method.upper() == 'POST':
                response = requests.post(url, headers=headers, json=data, timeout=10)
            else:
                app_logger.error(f"Unsupported HTTP method: {method} for {url}")
                return None
            response.raise_for_status()
            app_logger.info(f"Successfully called {url}, status: {response.status_code}")
            return response
        except requests.exceptions.RequestException as e:
            app_logger.error(f"Error calling service at {url}: {e}", exc_info=True)
            if e.response is not None:
                app_logger.error(f"Response body from {url}: {e.response.text}")
            return None

landing_service_client = None

def verify_landing_service_connection():
    global landing_service_client
    if not landing_service_client:
        app_logger.error("Landing service client not initialized during verification.")
        return False
    try:
        app_logger.info("Verifying landing service connection...")
        response = landing_service_client.call_service('/health', method='GET')
        is_healthy = response is not None and response.status_code == 200
        if is_healthy:
            app_logger.info("Landing service connection successful.")
        else:
            app_logger.warning(f"Landing service connection failed. Status: {response.status_code if response else 'No Response'}")
        return is_healthy
    except Exception as e:
        app_logger.error(f"Failed to verify landing service connection: {e}", exc_info=True)
        return False

# --- Configuration Helper ---
def configure_app():
    try:
        app.config['LANDING_SERVICE_URL'] = os.environ.get('LANDING_SERVICE_URL')
        app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL')
        app.config['FORMS_SERVICE_URL'] = os.environ.get('FORMS_SERVICE_URL')
        app.config['DASHBOARD_SERVICE_URL'] = os.environ.get('DASHBOARD_SERVICE_URL')

        app.config['JWT_TOKEN_LOCATION'] = ['cookies', 'headers']
        app.config['JWT_COOKIE_HTTPONLY'] = True
        app.config['JWT_COOKIE_SECURE'] = is_production
        app.config['JWT_COOKIE_SAMESITE'] = 'None' if is_production else 'Lax'
        app.config['JWT_COOKIE_DOMAIN'] = '.secapp.tzolkintech.com'  # Share cookie across subdomains
        app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(minutes=15)
        app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)

        app.config['SMTP_SERVER'] = os.environ.get('SMTP_SERVER', 'tzolkintech.com')
        app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', 587))
        app.config['SENDER_EMAIL'] = os.environ.get('SENDER_EMAIL', 'no-reply@tzolkintech.com')
        app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL', 'rcanton@tzolkintech.com')
        
        # Ensure GCP_PROJECT_ID is loaded correctly
        app.config['GCP_PROJECT_ID'] = os.environ.get('GCP_PROJECT', os.environ.get('GOOGLE_CLOUD_PROJECT'))
        if not app.config['GCP_PROJECT_ID']:
            app_logger.warning("GCP_PROJECT_ID is not set. Secret Manager access may fail.")

        app.config['EMAIL_PASSWORD_SECRET_NAME'] = os.environ.get('EMAIL_PASSWORD_SECRET', 'admin-email-pass')
        app.config['JWT_SECRET_MANAGER_NAME'] = 'jwt-secret-key'

        # Password reset token expiry (1 hour)
        app.config['PASSWORD_RESET_TOKEN_EXPIRES'] = timedelta(hours=1)

        app_logger.info(f"App configured. Production: {is_production}")
    except Exception as e:
        app_logger.critical(f"Critical error during app configuration: {e}", exc_info=True)
        sys.exit(1)

# --- CORS Configuration ---
def setup_cors():
    landing_service_url = app.config.get('LANDING_SERVICE_URL')
    allowed_origins = [landing_service_url] if landing_service_url else []
    if not is_production:
        allowed_origins.extend([
            "http://localhost:5001", "http://localhost:3000", "http://localhost:8081",
            "http://127.0.0.1:5001", "http://127.0.0.1:3000", "http://127.0.0.1:8081"
        ])
    
    CORS(app, 
         supports_credentials=True, 
         origins=allowed_origins,
         allow_headers=['Content-Type', 'Authorization', 'X-Requested-With'],
         methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
         expose_headers=['Set-Cookie'])
    app_logger.info(f"CORS configured with origins: {allowed_origins}")

# --- Extensions (Initialized globally) ---
bcrypt = Bcrypt(app)
jwt = JWTManager() # Initialized without `app` here, will be explicitly initialized later

# --- Secret Manager Helper ---
def get_secret_value(secret_name, project_id):
    if not project_id:
        app_logger.error(f"Cannot retrieve secret '{secret_name}': PROJECT_ID is not set.")
        raise ValueError(f"PROJECT_ID is required to access Secret Manager for '{secret_name}'.")
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

# --- JWT Secret Setup ---
def setup_jwt_secret():
    jwt_secret_key = os.environ.get('JWT_SECRET_KEY')
    if jwt_secret_key:
        current_app.config['JWT_SECRET_KEY'] = jwt_secret_key
        app_logger.info("Using JWT_SECRET_KEY from environment variable.")
        return
    try:
        current_app.config['JWT_SECRET_KEY'] = get_secret_value(
            current_app.config['JWT_SECRET_MANAGER_NAME'],
            current_app.config['GCP_PROJECT_ID']
        )
        app_logger.info("JWT_SECRET_KEY configured from Secret Manager.")
    except Exception as e:
        app_logger.critical(f"FATAL: Failed to retrieve JWT_SECRET_KEY: {e}. Exiting.")
        sys.exit(1)

# --- Database Connection ---
def get_db_connection():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        app_logger.critical("DATABASE_URL environment variable NOT SET. Exiting.")
        sys.exit(1)
    try:
        conn = psycopg2.connect(db_url)
        app_logger.debug("Database connection successful.")
        return conn
    except Exception as e:
        app_logger.error(f"Database connection error: {e}", exc_info=True)
        return None

# --- Password Reset Token Functions ---
def generate_reset_token():
    """Generate a secure random token for password reset"""
    return secrets.token_urlsafe(32)

def hash_token(token):
    """Hash token for secure storage"""
    return hashlib.sha256(token.encode()).hexdigest()

def create_password_reset_token(email):
    """Create and store password reset token"""
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        # Generate token
        token = generate_reset_token()
        token_hash = hash_token(token)
        expires_at = datetime.now(timezone.utc) + app.config['PASSWORD_RESET_TOKEN_EXPIRES']
        
        cur = conn.cursor()
        
        # Delete any existing tokens for this email
        cur.execute("DELETE FROM password_reset_tokens WHERE email = %s", (email,))
        
        # Insert new token
        cur.execute(
            "INSERT INTO password_reset_tokens (email, token_hash, expires_at) VALUES (%s, %s, %s)",
            (email, token_hash, expires_at)
        )
        
        conn.commit()
        cur.close()
        app_logger.info(f"Password reset token created for {email}")
        return token
        
    except Exception as e:
        conn.rollback()
        app_logger.error(f"Error creating password reset token for {email}: {e}", exc_info=True)
        return None
    finally:
        if conn:
            conn.close()

def verify_reset_token(token):
    """Verify password reset token and return email if valid"""
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        token_hash = hash_token(token)
        cur = conn.cursor(cursor_factory=extras.DictCursor)
        
        cur.execute(
            "SELECT email, expires_at FROM password_reset_tokens WHERE token_hash = %s",
            (token_hash,)
        )
        
        result = cur.fetchone()
        cur.close()
        
        if not result:
            app_logger.info("Invalid password reset token used")
            return None
        
        if datetime.now(timezone.utc) > result['expires_at'].replace(tzinfo=timezone.utc):
            app_logger.info(f"Expired password reset token used for {result['email']}")
            # Clean up expired token
            cur = conn.cursor()
            cur.execute("DELETE FROM password_reset_tokens WHERE token_hash = %s", (token_hash,))
            conn.commit()
            cur.close()
            return None
        
        app_logger.info(f"Valid password reset token verified for {result['email']}")
        return result['email']
        
    except Exception as e:
        app_logger.error(f"Error verifying password reset token: {e}", exc_info=True)
        return None
    finally:
        if conn:
            conn.close()

def delete_reset_token(token):
    """Delete used password reset token"""
    conn = get_db_connection()
    if not conn:
        return
    
    try:
        token_hash = hash_token(token)
        cur = conn.cursor()
        cur.execute("DELETE FROM password_reset_tokens WHERE token_hash = %s", (token_hash,))
        conn.commit()
        cur.close()
        app_logger.info("Password reset token deleted after use")
        
    except Exception as e:
        app_logger.error(f"Error deleting password reset token: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()

# --- Email Functions ---
def get_email_password():
    """
    Retrieves the email password, prioritizing environment variable, then Secret Manager.
    Logs errors if retrieval fails.
    """
    password = os.environ.get('EMAIL_PASSWORD')
    if password:
        app_logger.info("Using email password from environment variable.")
        # --- TEMPORARY DEBUGGING LINE ---
        app_logger.info(f"DEBUG: Email password from env: {password[:2]}****{password[-2:]}") # Mask most of it
        # --- END TEMPORARY DEBUGGING LINE ---
        return password
    
    # If not in env, try Secret Manager
    project_id = app.config.get('GCP_PROJECT_ID')
    secret_name = app.config.get('EMAIL_PASSWORD_SECRET_NAME')
    
    if not project_id:
        app_logger.error("GCP_PROJECT_ID is not configured. Cannot retrieve email password from Secret Manager.")
        return None
    if not secret_name:
        app_logger.error("EMAIL_PASSWORD_SECRET_NAME is not configured. Cannot retrieve email password from Secret Manager.")
        return None

    try:
        # Use app.app_context() to ensure app.config is available when called from outside request context
        with app.app_context(): 
            secret_value = get_secret_value(secret_name, project_id)
        app_logger.info("Successfully retrieved email password from Secret Manager.")
        # --- TEMPORARY DEBUGGING LINE ---
        if secret_value:
            app_logger.info(f"DEBUG: Email password from Secret Manager: {secret_value[:2]}****{secret_value[-2:]}") # Mask most of it
        # --- END TEMPORARY DEBUGGING LINE ---
        return secret_value
    except Exception as e:
        app_logger.warning(f"Could not retrieve email password from Secret Manager: {e}", exc_info=True)
        return None

def send_email(to_email, subject, body, is_html=False):
    """
    Sends an email via SMTP.
    Includes robust error handling and logs.
    """
    email_username = app.config.get('SENDER_EMAIL')
    smtp_server = app.config.get('SMTP_SERVER')
    smtp_port = app.config.get('SMTP_PORT')
    
    # Get password directly before sending the email to ensure it's available
    email_password = get_email_password() 

    if not all([email_username, email_password, smtp_server, smtp_port]):
        app_logger.error(f"Email configuration incomplete. Missing: "
                         f"sender_email={bool(email_username)}, "
                         f"password={bool(email_password)}, "
                         f"smtp_server={bool(smtp_server)}, "
                         f"smtp_port={bool(smtp_port)}. Skipping email send to {to_email}.")
        # Use flash to notify the user if this is a web request
        if request:
            flash("Error en la configuración de envío de email. Contacte al administrador.", "danger")
        return False
    
    app_logger.info(f"Attempting to send email to {to_email} via {smtp_server}:{smtp_port} from {email_username}.")
    try:
        msg = MIMEMultipart()
        msg['From'] = email_username
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html' if is_html else 'plain'))

        # --- RESTORED PART (NO EXPLICIT SSL CONTEXT) ---
        server = smtplib.SMTP(smtp_server, smtp_port, timeout=10) 
        server.starttls()
        # --- END RESTORED PART ---

        server.login(email_username, email_password)
        server.send_message(msg)
        server.quit()
        app_logger.info(f"Email sent successfully to {to_email}.") # Use app_logger here
        return True

    except smtplib.SMTPAuthenticationError:
        app_logger.error(f"SMTP Authentication Error: Check sender email/password for {email_username}.", exc_info=True)
        if request:
            flash("Error de autenticación de email. Contacte al administrador.", "danger")
        return False
    except smtplib.SMTPServerDisconnected:
        app_logger.error(f"SMTP Server Disconnected: Server {smtp_server}:{smtp_port} disconnected unexpectedly.", exc_info=True)
        if request:
            flash("El servidor de email no está disponible. Intente de nuevo más tarde.", "danger")
        return False
    except socket.timeout:
        app_logger.error(f"SMTP Connection Timeout: Could not connect to {smtp_server}:{smtp_port}.", exc_info=True)
        if request:
            flash("Tiempo de espera agotado al conectar con el servidor de email.", "danger")
        return False
    except Exception as e:
        app_logger.error(f"An unexpected error occurred while sending email to {to_email}: {e}", exc_info=True)
        if request:
            flash(f"Error al enviar email: {e}", "danger")
        return False

def send_password_reset_email(email, reset_token):
    """Send password reset email with reset link"""
    reset_url = url_for('reset_password', token=reset_token, _external=True)
    
    subject = "Restablecer Contraseña - SMT SecApp"
    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #2563eb; text-align: center;">Restablecer Contraseña</h2>
        <div style="background-color: #f8fafc; padding: 20px; border-radius: 8px; margin: 20px 0;">
            <p>Hola,</p>
            <p>Recibimos una solicitud para restablecer la contraseña de tu cuenta en SMT SecApp.</p>
            <p>Si realizaste esta solicitud, haz clic en el siguiente enlace para restablecer tu contraseña:</p>
        </div>
        <div style="text-align: center; margin: 30px 0;">
            <a href="{reset_url}"
               style="background-color: #2563eb; color: white; padding: 12px 30px;
                      text-decoration: none; border-radius: 6px; font-weight: bold;
                      display: inline-block;">
                Restablecer Contraseña
            </a>
        </div>
        <div style="background-color: #fef2f2; padding: 15px; border-radius: 6px; border-left: 4px solid #ef4444;">
            <p style="margin: 0; color: #991b1b; font-size: 14px;">
                <strong>Importante:</strong> Este enlace expirará en 1 hora por seguridad.
                Si no solicitaste este cambio, puedes ignorar este correo electrónico.
            </p>
        </div>
        <p style="margin-top: 20px; font-size: 12px; color: #666;">
            Si tienes problemas al hacer clic en el enlace, copia y pega la siguiente URL en tu navegador:<br>
            <span style="word-break: break-all;">{reset_url}</span>
        </p>
    </div>
    </body>
    </html>
    """
    
    return send_email(email, subject, html_body, is_html=True)

def send_registration_notification(user_email, user_name, phone_number):
    admin_email = app.config.get('ADMIN_EMAIL')
    if not admin_email:
        app_logger.warning("ADMIN_EMAIL not configured - skipping admin notification.")
        # Even if admin email is not configured, we should still try to send user welcome email
        return send_welcome_email(user_email, user_name)

    subject = f"Nuevo Usuario Registrado - {user_name}"
    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #2563eb;">Nuevo Usuario Registrado - SMT SecApp</h2>
    <div style="background-color: #f8fafc; padding: 20px; border-radius: 8px; margin: 20px 0;">
    <p><strong>Nombre:</strong> {user_name}</p>
    <p><strong>Email:</strong> {user_email}</p>
    <p><strong>Teléfono:</strong> {phone_number or 'No proporcionado'}</p>
    <p><strong>Fecha de Registro:</strong> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')}</p>
    </div>
    </div>
    </body>
    </html>
    """
    admin_result = send_email(admin_email, subject, html_body, is_html=True)
    user_result = send_welcome_email(user_email, user_name) # Call welcome email separately
    return admin_result and user_result

def send_welcome_email(user_email, user_name):
    subject = "¡Bienvenido a SMT SecApp!"
    # Ensure login_url is always absolute for external email links
    login_url = app.config.get('LOGIN_SERVICE_URL') 
    if not login_url:
        # Fallback to current app's login URL if LOGIN_SERVICE_URL isn't explicitly set
        with app.app_context(): # Ensure we are in an app context for url_for
            login_url = url_for('login', _external=True)
        app_logger.warning(f"LOGIN_SERVICE_URL not set, falling back to {login_url} for welcome email.")

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #2563eb; text-align: center;">¡Bienvenido a SMT SecApp!</h2>
    <div style="background-color: #f8fafc; padding: 20px; border-radius: 8px; margin: 20px 0;">
    <p>Hola <strong>{user_name}</strong>,</p>
    <p>¡Tu cuenta ha sido creada exitosamente!</p>
    </div>
    <div style="text-align: center; margin: 30px 0;">
    <a href="{login_url}"
       style="background-color: #2563eb; color: white; padding: 12px 30px;
              text-decoration: none; border-radius: 6px; font-weight: bold;">
        Iniciar Sesión
    </a>
    </div>
    </div>
    </body>
    </html>
    """
    return send_email(user_email, subject, html_body, is_html=True)

# --- JWT Error Handlers for Automatic Redirect ---
# Note: Login service typically doesn't need these since it's the login destination,
# but including for completeness in case JWT is used for admin functions

@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_payload):
    """
    Called when an access token has expired.
    For the login service, redirect to login page (self).
    """
    user_email = jwt_payload.get('sub', 'unknown')
    app_logger.info(f"JWT token expired for user {user_email}. Redirecting to login.")
    flash('Su sesión ha caducado. Por favor, inicie sesión de nuevo.', 'danger')
    return redirect(url_for('login'))

@jwt.invalid_token_loader
def invalid_token_callback(error_string):
    """
    Called when an invalid token is encountered.
    For the login service, redirect to login page (self).
    """
    app_logger.info(f"Invalid JWT token encountered: {error_string}. Redirecting to login.")
    flash('Su sesión es inválida. Por favor, inicie sesión de nuevo.', 'danger')
    return redirect(url_for('login'))

@jwt.unauthorized_loader
def unauthorized_callback(error_string):
    """
    Called when no JWT token is present in the request.
    For the login service, redirect to login page (self).
    """
    app_logger.info(f"No JWT token found: {error_string}. Redirecting to login.")
    flash('Su sesión ha caducado o no ha iniciado sesión. Por favor, inicie sesión de nuevo.', 'danger')
    return redirect(url_for('login'))

@jwt.revoked_token_loader
def revoked_token_callback(jwt_header, jwt_payload):
    """
    Called when a revoked token is encountered.
    For the login service, redirect to login page (self).
    """
    user_email = jwt_payload.get('sub', 'unknown')
    app_logger.info(f"Revoked JWT token for user {user_email}. Redirecting to login.")
    flash('Su sesión ha sido revocada. Por favor, inicie sesión de nuevo.', 'danger')
    return redirect(url_for('login'))

@jwt.needs_fresh_token_loader
def needs_fresh_token_callback(jwt_header, jwt_payload):
    """
    Called when a fresh token is required but not provided.
    For the login service, redirect to login page (self).
    """
    user_email = jwt_payload.get('sub', 'unknown')
    app_logger.info(f"Fresh token required for user {user_email}. Redirecting to login.")
    flash('Se requiere una sesión fresca. Por favor, inicie sesión de nuevo.', 'warning')
    return redirect(url_for('login'))

# --- JWT Token Refreshing ---
@app.after_request
def refresh_expiring_jwts(response):
    try:
        jwt_data = get_jwt()
        exp_timestamp = jwt_data["exp"]
        identity = jwt_data["sub"]
        now = datetime.now(timezone.utc)
        refresh_window_seconds = 5 * 60
        target_timestamp = datetime.timestamp(now + timedelta(seconds=refresh_window_seconds))

        if exp_timestamp < target_timestamp:
            app_logger.info(f"Access token for {identity} is about to expire. Refreshing automatically.")
            new_access_token = create_access_token(identity=identity)
            set_access_cookies(response, new_access_token)
            app_logger.info(f"New access token set for {identity}.")
        return response
    except (RuntimeError, KeyError):
        app_logger.debug("No valid JWT or no 'exp' field for automatic refresh.")
        return response
    except Exception as e:
        app_logger.error(f"Unexpected error during JWT refresh: {e}", exc_info=True)
        return response

# --- Input Validation ---
def validate_email(email):
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return bool(re.match(pattern, email))

def validate_password(password):
    return (len(password) >= 8 and
            re.search(r'[A-Z]', password) and
            re.search(r'[a-z]', password) and
            re.search(r'\d', password))

def sanitize_input(value):
    return ''.join(c for c in value if c.isalnum() or c in '@.-_ ')[:255]

# --- Routes ---
@app.route('/')
@app.route('/login', methods=['GET', 'POST'])
@csrf.exempt  # Exempt to support original login.html without CSRF token
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if not username or not password:
            flash('Email y contraseña son requeridos.', 'warning')
            return render_template('login.html', username=username)

        if not validate_email(username):
            flash('Correo electrónico inválido.', 'warning')
            return render_template('login.html', username=username)

        conn = get_db_connection()
        if not conn:
            flash('Error de conexión a la base de datos.', 'danger')
            return render_template('login.html', username=username)

        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)
            cur.execute("SELECT id, email, password_hash, name FROM users WHERE email = %s", (username,))
            user = cur.fetchone()
            cur.close()

            if user and bcrypt.check_password_hash(user['password_hash'], password):
                # Always query the database for the latest name
                user_name = user['name'] if user['name'] else user['email']
                additional_claims = {
                    "user_id": user['id'],
                    "name": user_name,
                    "email": user['email']
                }
                access_token = create_access_token(
                    identity=user['email'],
                    additional_claims=additional_claims
                )
                refresh_token = create_refresh_token(identity=user['email'])

                app_logger.info(f"Generated access_token: {access_token}")
                app_logger.info(f"Generated refresh_token: {refresh_token}")

                if landing_service_client and not verify_landing_service_connection():
                    app_logger.error("Landing service is not accessible.")
                    flash('Servicio de destino no disponible temporalmente.', 'danger')
                    return render_template('login.html', username=username)

                landing_url = app.config.get('LANDING_SERVICE_URL', '/')

                response = redirect(landing_url)
                set_access_cookies(response, access_token)
                set_refresh_cookies(response, refresh_token)

                # Log the cookie value that will be set
                cookie_value = response.headers.get('Set-Cookie', None)
                app_logger.info(f"Set-Cookie header after login: {cookie_value}")

                flash('Bienvenido Cliente.', 'success')
                app_logger.info(f"User {username} logged in successfully, redirecting to {landing_url}.")
                return response
            else:
                flash('Usuario o contraseña incorrectos.', 'danger')
                return render_template('login.html', username=username)

        except Exception as e:
            app_logger.error(f"Login error for {username}: {e}", exc_info=True)
            flash('Error durante el inicio de sesión.', 'danger')
            return render_template('login.html', username=username)
        finally:
            if conn:
                conn.close()
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email', '')
        name = request.form.get('name', '')
        phone_number = request.form.get('phone_number', '')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not all([email, name, password, confirm_password]): # phone_number is optional
            flash('Los campos de Email, Nombre y Contraseña son requeridos.', 'warning')
            return render_template('register.html', email=email, name=name,
                                    phone_number=phone_number)

        if not validate_email(email):
            flash('Correo electrónico inválido.', 'warning')
            return render_template('register.html', email=email, name=name,
                                    phone_number=phone_number)

        if not validate_password(password):
            flash('La contraseña debe tener al menos 8 caracteres, con mayúsculas, minúsculas y números.', 'warning')
            return render_template('register.html', email=email, name=name,
                                    phone_number=phone_number)

        if password != confirm_password:
            flash('Las contraseñas no coinciden.', 'danger')
            return render_template('register.html', email=email, name=name,
                                    phone_number=phone_number)

        name = sanitize_input(name)
        phone_number = sanitize_input(phone_number) if phone_number else None

        conn = get_db_connection()
        if not conn:
            flash('Error de conexión a la base de datos.', 'danger')
            return render_template('register.html', email=email, name=name,
                                    phone_number=phone_number)

        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)
            try:
                # Check for authorized email if table exists
                cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'authorized_emails'")
                if cur.fetchone(): # Table exists
                    cur.execute("SELECT id FROM authorized_emails WHERE email = %s AND is_active = TRUE", (email,))
                    authorized_email_entry = cur.fetchone()
                    if not authorized_email_entry:
                        flash('No estás autorizado para registrarte. Contacta al administrador.', 'danger')
                        return render_template('register.html', email=email, name=name,
                                                phone_number=phone_number)
                else:
                    app_logger.warning("authorized_emails table does not exist - skipping authorization check for registration.")
            except psycopg2.Error as e:
                app_logger.error(f"Database error during authorization table check: {e}", exc_info=True)
                # Don't fail registration just for table check failure if it's not critical
                # However, if the intent is strict authorization, you might want to re-raise or handle differently.
                conn.rollback() # Rollback any potential partial operations
                flash('Error de base de datos al verificar autorización.', 'danger')
                return render_template('register.html', email=email, name=name, phone_number=phone_number)


            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            existing_user = cur.fetchone()
            if existing_user:
                flash('Este correo electrónico ya está registrado.', 'danger')
                return render_template('register.html', email=email, name=name,
                                        phone_number=phone_number)

            hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
            cur.execute(
                "INSERT INTO users (username, email, name, phone_number, password_hash) "
                "VALUES (%s, %s, %s, %s, %s)",
                (email, email, name, phone_number, hashed_password)
            )
            conn.commit()
            cur.close()

            # Separate flags for email sending results
            admin_email_sent = True
            welcome_email_sent = True

            # Send admin notification email
            admin_email = app.config.get('ADMIN_EMAIL')
            if admin_email:
                admin_subject = f"Nuevo Usuario Registrado - {name}"
                admin_html_body = f"""
                <html>
                <body style="font-family: Arial, sans-serif; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #2563eb;">Nuevo Usuario Registrado - SMT SecApp</h2>
                <div style="background-color: #f8fafc; padding: 20px; border-radius: 8px; margin: 20px 0;">
                <p><strong>Nombre:</strong> {name}</p>
                <p><strong>Email:</strong> {email}</p>
                <p><strong>Teléfono:</strong> {phone_number or 'No proporcionado'}</p>
                <p><strong>Fecha de Registro:</strong> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')}</p>
                </div>
                </div>
                </body>
                </html>
                """
                admin_email_sent = send_email(admin_email, admin_subject, admin_html_body, is_html=True)
                if not admin_email_sent:
                    app_logger.error("Failed to send admin registration notification email.")
            else:
                app_logger.warning("ADMIN_EMAIL not configured, skipping admin registration notification.")

            # Send welcome email to the user
            welcome_email_sent = send_welcome_email(email, name)
            if not welcome_email_sent:
                app_logger.error("Failed to send welcome email to registered user.")

            if not admin_email_sent or not welcome_email_sent:
                flash('Registro exitoso! Nota: Hubo problemas al enviar algunas notificaciones por correo electrónico.', 'warning')
            else:
                flash('Registro exitoso! Ahora puedes iniciar sesión.', 'success')

            return redirect(url_for('login'))

        except Exception as e:
            conn.rollback()
            app_logger.error(f"Registration error: {e}", exc_info=True)
            flash('Error durante el registro.', 'danger')
            return render_template('register.html', email=email, name=name,
                                phone_number=phone_number)
        finally:
            if conn:
                conn.close()
    return render_template('register.html')

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        
        if not email:
            flash('Por favor, ingresa tu correo electrónico.', 'warning')
            return render_template('forgot_password.html', email=email)
        
        if not validate_email(email):
            flash('Correo electrónico inválido.', 'warning')
            return render_template('forgot_password.html', email=email)
        
        conn = get_db_connection()
        if not conn:
            flash('Error de conexión a la base de datos.', 'danger')
            return render_template('forgot_password.html', email=email)
        
        try:
            # Check if user exists
            cur = conn.cursor(cursor_factory=extras.DictCursor)
            cur.execute("SELECT id, email, name FROM users WHERE email = %s", (email,))
            user = cur.fetchone()
            cur.close()
            
            if user:
                # Generate and send reset token
                reset_token = create_password_reset_token(email)
                if reset_token:
                    if send_password_reset_email(email, reset_token):
                        flash('Se ha enviado un enlace de restablecimiento de contraseña a tu correo electrónico.', 'success')
                        app_logger.info(f"Password reset email sent to {email}")
                    else:
                        flash('Error al enviar el correo electrónico. Intenta de nuevo más tarde.', 'danger')
                        app_logger.error(f"Failed to send password reset email to {email}")
                else:
                    flash('Error al generar el token de restablecimiento. Intenta de nuevo más tarde.', 'danger')
                    app_logger.error(f"Failed to create password reset token for {email}")
            else:
                # Don't reveal if email exists or not for security
                flash('Si el correo electrónico está registrado, recibirás un enlace de restablecimiento.', 'info')
                app_logger.info(f"Password reset requested for non-existent email: {email}")
            
            return redirect(url_for('login'))
            
        except Exception as e:
            app_logger.error(f"Error in forgot password for {email}: {e}", exc_info=True)
            flash('Error durante el proceso. Intenta de nuevo más tarde.', 'danger')
            return render_template('forgot_password.html', email=email)
        finally:
            if conn:
                conn.close()
    
    return render_template('forgot_password.html')

@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    # Verify token
    email = verify_reset_token(token)
    if not email:
        flash('El enlace de restablecimiento es inválido o ha expirado.', 'danger')
        return redirect(url_for('forgot_password'))
    
    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if not password or not confirm_password:
            flash('Ambos campos de contraseña son requeridos.', 'warning')
            return render_template('reset_password.html', token=token, email=email)
        
        if not validate_password(password):
            flash('La contraseña debe tener al menos 8 caracteres, con mayúsculas, minúsculas y números.', 'warning')
            return render_template('reset_password.html', token=token, email=email)
        
        if password != confirm_password:
            flash('Las contraseñas no coinciden.', 'danger')
            return render_template('reset_password.html', token=token, email=email)
        
        conn = get_db_connection()
        if not conn:
            flash('Error de conexión a la base de datos.', 'danger')
            return render_template('reset_password.html', token=token, email=email)
        
        try:
            # Update password
            hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE email = %s",
                (hashed_password, email)
            )
            conn.commit()
            cur.close()
            
            # Delete the used token
            delete_reset_token(token)
            
            flash('Tu contraseña ha sido actualizada exitosamente. Ahora puedes iniciar sesión.', 'success')
            app_logger.info(f"Password successfully reset for {email}")
            return redirect(url_for('login'))
            
        except Exception as e:
            conn.rollback()
            app_logger.error(f"Error resetting password for {email}: {e}", exc_info=True)
            flash('Error al actualizar la contraseña. Intenta de nuevo.', 'danger')
            return render_template('reset_password.html', token=token, email=email)
        finally:
            if conn:
                conn.close()
    
    return render_template('reset_password.html', token=token, email=email)

@app.route('/change-password', methods=['GET', 'POST'])
def change_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        
        if not all([email, current_password, new_password, confirm_password]):
            flash('Todos los campos son requeridos.', 'warning')
            return render_template('change_password.html', email=email)
        
        if not validate_email(email):
            flash('Correo electrónico inválido.', 'warning')
            return render_template('change_password.html', email=email)
        
        if not validate_password(new_password):
            flash('La nueva contraseña debe tener al menos 8 caracteres, con mayúsculas, minúsculas y números.', 'warning')
            return render_template('change_password.html', email=email)
        
        if new_password != confirm_password:
            flash('Las contraseñas nuevas no coinciden.', 'danger')
            return render_template('change_password.html', email=email)
        
        conn = get_db_connection()
        if not conn:
            flash('Error de conexión a la base de datos.', 'danger')
            return render_template('change_password.html', email=email)
        
        try:
            # Verify user exists and current password is correct
            cur = conn.cursor(cursor_factory=extras.DictCursor)
            cur.execute("SELECT id, password_hash, name FROM users WHERE email = %s", (email,))
            user = cur.fetchone()
            
            if not user:
                flash('Usuario no encontrado.', 'danger')
                return render_template('change_password.html', email=email)
            
            if not bcrypt.check_password_hash(user['password_hash'], current_password):
                flash('La contraseña actual es incorrecta.', 'danger')
                return render_template('change_password.html', email=email)
            
            # Update to new password
            hashed_new_password = bcrypt.generate_password_hash(new_password).decode('utf-8')
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE email = %s",
                (hashed_new_password, email)
            )
            conn.commit()
            cur.close()
            
            flash('Tu contraseña ha sido cambiada exitosamente. Ahora puedes iniciar sesión.', 'success')
            app_logger.info(f"Password successfully changed for {email}")
            return redirect(url_for('login'))
            
        except Exception as e:
            conn.rollback()
            app_logger.error(f"Error changing password for {email}: {e}", exc_info=True)
            flash('Error al cambiar la contraseña. Intenta de nuevo.', 'danger')
            return render_template('change_password.html', email=email)
        finally:
            if conn:
                conn.close()
    
    return render_template('change_password.html')

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    response = redirect(url_for('login'))
    unset_jwt_cookies(response)
    flash('Sesión cerrada.', 'info')
    app_logger.info("User logged out.")
    return response

@app.route('/get_user_info', methods=['GET'])
@jwt_required()
def get_user_info():
    app_logger.info("GET /get_user_info called")
    
    current_user_email = get_jwt_identity()
    if not current_user_email:
        app_logger.warning("JWT is valid but no identity found.")
        return jsonify({"error": "Unauthorized"}), 401

    try:
        jwt_claims = get_jwt()
        if 'name' in jwt_claims and 'email' in jwt_claims:
            app_logger.info(f"User info retrieved from JWT claims for {current_user_email}")
            return jsonify({
                "email": jwt_claims['email'],
                "name": jwt_claims['name'],
                "user_id": jwt_claims.get('user_id')
            }), 200
    except Exception as e:
        app_logger.debug(f"Could not get user info from JWT claims: {e}")

    conn = get_db_connection()
    if not conn:
        app_logger.error("Database connection failed in get_user_info")
        return jsonify({"error": "Database connection failed"}), 500

    try:
        cur = conn.cursor(cursor_factory=extras.DictCursor)
        cur.execute("SELECT id, email, name FROM users WHERE email = %s", (current_user_email,))
        user_data = cur.fetchone()
        cur.close()
        
        if user_data:
            app_logger.info(f"User info retrieved from database for {current_user_email}")
            return jsonify({
                "email": user_data['email'],
                "name": user_data.get('name', user_data['email']),
                "user_id": user_data['id']
            }), 200
        else:
            app_logger.warning(f"User data not found in DB for email: {current_user_email}")
            return jsonify({"error": "User not found"}), 404
            
    except Exception as e:
        app_logger.error(f"Error fetching user info for {current_user_email} from DB: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if conn:
            conn.close()

@app.route('/token/refresh', methods=['POST'])
@jwt_required(refresh=True)
def refresh_access():
    current_user = get_jwt_identity()
    app_logger.info(f"Attempting to refresh access token for {current_user}.")
    
    conn = get_db_connection()
    additional_claims = {}
    
    if conn:
        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)
            cur.execute("SELECT id, email, name FROM users WHERE email = %s", (current_user,))
            user_data = cur.fetchone()
            cur.close()
            
            if user_data:
                additional_claims = {
                    "user_id": user_data['id'],
                    "name": user_data['name'],
                    "email": user_data['email']
                }
        except Exception as e:
            app_logger.error(f"Error fetching user data for token refresh: {e}")
        finally:
            conn.close()
    
    new_access_token = create_access_token(
        identity=current_user,
        additional_claims=additional_claims
    )
    
    response = jsonify({
        "message": "Token refreshed successfully",
        "access_token": new_access_token
    })
    set_access_cookies(response, new_access_token)
    app_logger.info(f"Access token refreshed for {current_user}.")
    return response

@app.route('/health')
def health_check():
    health_status = {
        'status': 'healthy',
        'service': 'login-service',
        'timestamp': datetime.now(timezone.utc).isoformat()
    }
    status_code = 200

    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            conn.close()
            health_status['database'] = 'connected'
        else:
            health_status['database'] = 'disconnected'
            status_code = 500
    except Exception as e:
        health_status['database'] = f'error: {str(e)}'
        app_logger.error(f"Health check: Database error: {e}", exc_info=True)
        status_code = 500

    try:
        get_secret_value(app.config['JWT_SECRET_MANAGER_NAME'],
                         app.config['GCP_PROJECT_ID'])
        health_status['jwt_secret_manager'] = 'accessible'
    except Exception:
        health_status['jwt_secret_manager'] = 'unreachable or secret missing'
        status_code = 500

    return health_status, status_code

@app.route('/startup')
def startup_check():
    db_connected = False
    try:
        conn = get_db_connection()
        if conn:
            conn.close()
            db_connected = True
    except Exception:
        pass

    jwt_secret_ready = False
    try:
        get_secret_value(app.config['JWT_SECRET_MANAGER_NAME'],
                         app.config['GCP_PROJECT_ID'])
        jwt_secret_ready = True
    except Exception:
        pass

    status = 'ready' if db_connected and jwt_secret_ready else 'not_ready'
    status_code = 200 if db_connected and jwt_secret_ready else 503
    return {
        'status': status,
        'service': 'login-service',
        'database_status': 'connected' if db_connected else 'disconnected',
        'jwt_secret_status': 'ready' if jwt_secret_ready else 'not_ready',
        'timestamp': datetime.now(timezone.utc).isoformat()
    }, status_code

@app.errorhandler(404)
def not_found_error(error):
    app_logger.warning(f"404 Not Found: {request.path}")
    if request.path.startswith('/api/') or request.accept_mimetypes.accept_json:
        return jsonify({"error": "Not found"}), 404
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    app_logger.error(f"Internal server error: {error}", exc_info=True)
    if request.path.startswith('/api/') or request.accept_mimetypes.accept_json:
        return jsonify({"error": "Internal server error"}), 500
    return render_template('500.html'), 500

# --- Debug Routes (Restricted to Non-Production) ---
@app.route('/debug/config')
def debug_config():
    if is_production:
        return "Debug endpoint disabled in production", 403
    return jsonify({
        'is_production': is_production,
        'landing_service_url': app.config.get('LANDING_SERVICE_URL'),
        'jwt_cookie_secure': app.config.get('JWT_COOKIE_SECURE'),
        'jwt_cookie_samesite': app.config.get('JWT_COOKIE_SAMESITE'),
        'cors_configured': 'CORS configured' if app.config.get('LANDING_SERVICE_URL') else 'CORS not configured',
        'sender_email': app.config.get('SENDER_EMAIL'),
        'smtp_server': app.config.get('SMTP_SERVER'),
        'smtp_port': app.config.get('SMTP_PORT'),
        'admin_email': app.config.get('ADMIN_EMAIL'),
        'gcp_project_id': app.config.get('GCP_PROJECT_ID'),
        'email_password_secret_name': app.config.get('EMAIL_PASSWORD_SECRET_NAME'),
        'jwt_secret_manager_name': app.config.get('JWT_SECRET_MANAGER_NAME'),
        'flask_secret_key_set': bool(app.config.get('SECRET_KEY'))
    })

# Add this temporary route for debugging SMTP connectivity
@app.route('/debug/smtp_connectivity')
def debug_smtp_connectivity():
    if is_production:
        return "Debug endpoint disabled in production", 403

    smtp_server = app.config.get('SMTP_SERVER')
    smtp_port = app.config.get('SMTP_PORT')
    timeout_seconds = 5 # Shorter timeout for quick test

    if not smtp_server or not smtp_port:
        return jsonify({"status": "error", "message": "SMTP server or port not configured."}), 500

    try:
        app_logger.info(f"Attempting raw TCP connection to {smtp_server}:{smtp_port} with timeout {timeout_seconds}s...")
        sock = socket.create_connection((smtp_server, smtp_port), timeout=timeout_seconds)
        sock.close()
        app_logger.info(f"Successfully established TCP connection to {smtp_server}:{smtp_port}.")
        return jsonify({"status": "success", "message": f"Successfully connected to {smtp_server}:{smtp_port}."}), 200
    except socket.timeout:
        app_logger.error(f"TCP connection to {smtp_server}:{smtp_port} timed out after {timeout_seconds}s.")
        return jsonify({"status": "error", "message": f"Connection to {smtp_server}:{smtp_port} timed out."}), 500
    except ConnectionRefusedError:
        app_logger.error(f"TCP connection to {smtp_server}:{smtp_port} refused. Firewall/Service not running?")
        return jsonify({"status": "error", "message": f"Connection to {smtp_server}:{smtp_port} refused. Check firewall or if SMTP service is running."}), 500
    except Exception as e:
        app_logger.error(f"Error connecting to {smtp_server}:{smtp_port}: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"Unexpected error: {str(e)}."}), 500


@app.route('/debug/token-validate')
def debug_token_validate():
    if is_production:
        return "Debug endpoint disabled in production", 403
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        token = auth_header.split(' ')[1]
        return jsonify({
            'token_source': 'header',
            'token_present': True,
            'token_preview': token[:20] + '...' if len(token) > 20 else token
        })
    access_token_cookie = request.cookies.get('access_token_cookie')
    if access_token_cookie:
        return jsonify({
            'token_source': 'cookie',
            'token_present': True,
            'token_preview': access_token_cookie[:20] + '...' if len(access_token_cookie) > 20 else access_token_cookie
        })
    return jsonify({
        'token_source': 'none',
        'token_present': False,
        'cookies_present': list(request.cookies.keys()),
        'headers_present': dict(request.headers)
    })

@app.route('/debug/headers')
def debug_headers():
    if is_production:
        return "Debug endpoint disabled in production", 403
    headers_dict = dict(request.headers)
    return jsonify({
        'headers': headers_dict,
        'method': request.method,
        'origin': request.headers.get('Origin'),
        'user_agent': request.headers.get('User-Agent'),
        'authorization': request.headers.get('Authorization', 'Not present')[:50] + '...' if request.headers.get('Authorization') else 'Not present'
    })

@app.route('/debug/cors-test')
def debug_cors():
    if is_production:
        return "Debug endpoint disabled in production", 403
    response = jsonify({
        'message': 'CORS test successful',
        'origin': request.headers.get('Origin'),
        'timestamp': datetime.now(timezone.utc).isoformat()
    })
    response.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE', 'OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

@app.route('/debug/jwt-test')
@jwt_required()
def debug_jwt():
    if is_production:
        return "Debug endpoint disabled in production", 403
    current_user = get_jwt_identity()
    jwt_data = get_jwt()
    return jsonify({
        'current_user': current_user,
        'jwt_claims': jwt_data,
        'token_type': jwt_data.get('type'),
        'expires_at': datetime.fromtimestamp(jwt_data.get('exp'), tz=timezone.utc).isoformat() if jwt_data.get('exp') else None
    })

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        response = jsonify()
        origin = request.headers.get('Origin')
        allowed_origins = [app.config.get('LANDING_SERVICE_URL')] if app.config.get('LANDING_SERVICE_URL') else []
        if not is_production:
            allowed_origins.extend([
                "http://localhost:5001", "http://localhost:3000", "http://127.0.0.1:5001", "http://127.0.0.1:3000",
                "http://localhost:8081", "http://127.0.0.1:8081"
            ])
        if origin in allowed_origins:
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Access-Control-Allow-Credentials'] = 'true'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
            response.headers['Access-Control-Max-Age'] = '86400'
        return response

# --- Crucial Application Initialization (MOVED HERE) ---
# These ensure that Flask-JWT-Extended and other app configurations
# are set up when the module is imported by a WSGI server (like Gunicorn on Cloud Run),
# not just when main.py is run directly.
with app.app_context():
    configure_app()
    setup_cors()
    setup_jwt_secret() # Ensure JWT_SECRET_KEY is loaded before JWTManager initialization
    jwt.init_app(app) # Initialize JWTManager with the app after config is loaded

# Initialize CloudRunServiceClient for landing service globally after app config
landing_service_url = app.config.get('LANDING_SERVICE_URL')
if landing_service_url:
    landing_service_client = CloudRunServiceClient(landing_service_url)
    app_logger.info("CloudRunServiceClient initialized for landing service.")
else:
    app_logger.warning("LANDING_SERVICE_URL not set.")


# --- Main Application Entry Point (For local development or direct execution) ---
if __name__ == '__main__':
    # This block is primarily for running the app via 'python main.py' directly.
    # When deployed with Gunicorn/Cloud Run, the app is configured by the code above.
    port = int(os.environ.get('PORT', 8080))
    debug_mode = not is_production
    app_logger.info(f"Starting Flask app on port {port}, debug={debug_mode}, Production: {is_production}")
    app.run(host='0.0.0.0', port=port, debug=debug_mode, threaded=True)