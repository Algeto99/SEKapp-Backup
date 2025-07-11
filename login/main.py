import os
import smtplib
import re
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
        app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(minutes=15)
        app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)
        app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', '.tzolkintech.com')

        app.config['SMTP_SERVER'] = os.environ.get('SMTP_SERVER', 'mail.tzolkintech.com')
        app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', 587))
        app.config['EMAIL_USERNAME'] = os.environ.get('EMAIL_USERNAME')
        app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL', 'no-reply@tzolkintech.com')
        app.config['GCP_PROJECT_ID'] = os.environ.get('GCP_PROJECT', os.environ.get('GOOGLE_CLOUD_PROJECT'))
        app.config['EMAIL_PASSWORD_SECRET_NAME'] = os.environ.get('EMAIL_PASSWORD_SECRET', 'admin-email-pass')
        app.config['JWT_SECRET_MANAGER_NAME'] = 'jwt-secret-key'

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

# --- Email Functions ---
def get_email_password():
    password = os.environ.get('EMAIL_PASSWORD')
    if password:
        app_logger.info("Using email password from environment variable.")
        return password
    try:
        return get_secret_value(app.config['EMAIL_PASSWORD_SECRET_NAME'],
                                app.config['GCP_PROJECT_ID'])
    except Exception as e:
        app_logger.warning(f"Could not retrieve email password: {e}")
        return None

def send_email(to_email, subject, body, is_html=False):
    email_username = app.config.get('EMAIL_USERNAME')
    email_password = get_email_password()
    smtp_server = app.config.get('SMTP_SERVER')
    smtp_port = app.config.get('SMTP_PORT')
    if not all([email_username, email_password, smtp_server, smtp_port]):
        app_logger.warning("Email configuration incomplete - skipping email send.")
        return False
    app_logger.info(f"Attempting to send email to {to_email}.")
    msg = MIMEMultipart()
    msg['From'] = email_username
    msg['To'] = to_email
    msg['Subject'] = subject

    if is_html:
        msg.attach(MIMEText(body, 'html'))
    else:
        msg.attach(MIMEText(body, 'plain'))
    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(email_username, email_password)
            server.sendmail(email_username, to_email, msg.as_string())
        app_logger.info(f"Email sent successfully to {to_email}.")
        return True
    except Exception as e:
        app_logger.error(f"Email send error to {to_email}: {e}", exc_info=True)
        return False

def send_registration_notification(user_email, user_name, phone_number):
    admin_email = app.config.get('ADMIN_EMAIL')
    if not admin_email:
        app_logger.warning("ADMIN_EMAIL not configured - skipping admin notification.")
        return True

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
    user_result = send_email(user_email, f"Confirmación de Registro - {user_name}", html_body, is_html=True)
    return admin_result and user_result

def send_welcome_email(user_email, user_name):
    subject = "¡Bienvenido a SMT SecApp!"
    login_url = app.config.get('LOGIN_SERVICE_URL', url_for('login', _external=True))
    if not login_url.startswith('http'):
        login_url = url_for('login', _external=True)
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

# --- JWT Error Handling ---
@jwt.unauthorized_loader
def handle_unauthorized_loader(callback):
    app_logger.info(f"Unauthorized access attempt to {request.path}.")
    flash('Su sesión ha caducado o no ha iniciado sesión. Por favor, inicie sesión de nuevo.', 'danger')
    return redirect(url_for('login'))

@jwt.invalid_token_loader
def handle_invalid_token_loader(callback):
    app_logger.info(f"Invalid token access attempt to {request.path}.")
    flash('Su sesión es inválida. Por favor, inicie sesión de nuevo.', 'danger')
    return redirect(url_for('login'))

@jwt.expired_token_loader
def handle_expired_token_loader(jwt_header, jwt_payload):
    app_logger.info(f"Expired token access attempt for {jwt_payload.get('sub')} to {request.path}.")
    flash('Su sesión ha caducado. Por favor, inicie sesión de nuevo.', 'danger')
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
            # REVERTED: Removed 'is_admin' from the SELECT query based on user's request
            cur.execute("SELECT id, email, password_hash, name FROM users WHERE email = %s", (username,))
            user = cur.fetchone()
            cur.close()

            # Added comprehensive logging around password check
            app_logger.info(f"Attempting login for email: {username}")
            if user:
                app_logger.info(f"User found in DB: {user.get('email')}, Hashed Pass (snippet): {user.get('password_hash', '')[:10]}...")
                # WARNING: DO NOT LOG PLAIN TEXT PASSWORDS IN PRODUCTION
                # app_logger.info(f"User entered password (DEBUG ONLY): {password}")

                is_password_correct = bcrypt.check_password_hash(user.get('password_hash'), password)
                app_logger.info(f"Bcrypt password check result: {is_password_correct}")

                if is_password_correct:
                    # MODIFIED: Updated additional_claims for consistency and Landing Service needs
                    # is_admin is now retrieved safely with .get() since it's not selected
                    additional_claims = {
                        "user_id": user['id'],
                        "user_name": user['name'], # Using 'name' from DB for 'user_name' claim
                        "email": user['email'],
                        "is_admin": user.get('is_admin', False) # Safely get 'is_admin', defaulting to False
                    }

                    access_token = create_access_token(
                        identity=user['email'],
                        additional_claims=additional_claims
                    )
                    refresh_token = create_refresh_token(
                        identity=user['email'],
                        additional_claims=additional_claims # Also add claims to refresh token for consistency
                    )

                    landing_url = app.config.get('LANDING_SERVICE_URL', '/')

                    # Redirect directly to the landing URL without appending the token.
                    # The tokens are already set in HTTP-only cookies in the response.
                    response = redirect(landing_url)

                    set_access_cookies(response, access_token)
                    set_refresh_cookies(response, refresh_token)

                    flash('Inicio de sesión exitoso.', 'success')
                    app_logger.info(f"User {username} logged in successfully, redirecting to {landing_url}.")
                    return response
                else:
                    app_logger.warning(f"Login failed for {username}: Incorrect password.")
                    flash('Usuario o contraseña incorrectos.', 'danger')
                    return render_template('login.html', username=username)
            else:
                app_logger.warning(f"Login failed: User {username} not found in DB.")
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
@csrf.exempt  # Exempt to support original register.html without CSRF token
def register():
    email = request.form.get('email', '')
    name = request.form.get('name', '')
    phone_number = request.form.get('phone_number', '')

    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not all([email, name, password, confirm_password]):
            flash('Todos los campos obligatorios son requeridos.', 'warning')
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
                cur.execute("SELECT id FROM authorized_emails WHERE email = %s AND is_active = TRUE", (email,))
                authorized_email_entry = cur.fetchone()
                if not authorized_email_entry:
                    flash('No estás autorizado para registrarte. Contacta al administrador.', 'danger')
                    return render_template('register.html', email=email, name=name,
                                           phone_number=phone_number)
            except psycopg2.Error as e:
                if "does not exist" in str(e).lower():
                    app_logger.warning("authorized_emails table does not exist - skipping authorization check.")
                    conn.rollback()
                else:
                    app_logger.error(f"Database error during authorization check: {e}", exc_info=True)
                    raise e

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

            email_issues = []
            if not send_registration_notification(email, name, phone_number):
                email_issues.append("notification")
            if not send_welcome_email(email, name):
                email_issues.append("welcome email")

            if email_issues:
                flash(f'Registro exitoso! Nota: Algunos emails no se pudieron enviar.', 'warning')
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
        'cors_configured': 'CORS configured' if app.config.get('LANDING_SERVICE_URL') else 'CORS not configured'
    })

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
        'expires_at': jwt_data.get('exp')
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

# Step 1: Add debug logging to both services

# LOGIN SERVICE (main.py) - Add this after setup_jwt_secret()
def debug_jwt_secret():
    secret = app.config.get('JWT_SECRET_KEY', 'NOT SET')
    app_logger.info(f"LOGIN SERVICE JWT Secret length: {len(secret)}")
    app_logger.info(f"LOGIN SERVICE JWT Secret hash: {hash(secret)}")
    app_logger.info(f"LOGIN SERVICE JWT Secret first 10 chars: {secret[:10]}...")
    
# Call this in your with app.app_context() block in main.py
debug_jwt_secret()

# LANDING SERVICE (app.py) - Add this after setup_jwt_secret()
def debug_jwt_secret():
    secret = app.config.get('JWT_SECRET_KEY', 'NOT SET')
    app_logger.info(f"LANDING SERVICE JWT Secret length: {len(secret)}")
    app_logger.info(f"LANDING SERVICE JWT Secret hash: {hash(secret)}")
    app_logger.info(f"LANDING SERVICE JWT Secret first 10 chars: {secret[:10]}...")

# Call this in your with app.app_context() block in app.py
debug_jwt_secret()

# Step 2: Enhanced JWT secret setup for both services
def setup_jwt_secret_enhanced():
    """Enhanced JWT secret setup with detailed logging"""
    app_logger.info("=== JWT SECRET SETUP START ===")
    
    # Method 1: Environment variable
    jwt_secret_key = os.environ.get('JWT_SECRET_KEY')
    if jwt_secret_key:
        app.config['JWT_SECRET_KEY'] = jwt_secret_key
        app_logger.info(f"✓ Using JWT_SECRET_KEY from environment variable (length: {len(jwt_secret_key)})")
        return
    
    # Method 2: Secret Manager
    secret_name = app.config.get('JWT_SECRET_MANAGER_NAME', 'jwt-secret-key')
    project_id = app.config.get('GCP_PROJECT_ID')
    
    app_logger.info(f"Attempting to retrieve secret: {secret_name} from project: {project_id}")
    
    if not project_id:
        app_logger.critical("❌ GCP_PROJECT_ID not set and JWT_SECRET_KEY not provided")
        sys.exit(1)
    
    try:
        retrieved_secret = get_secret_value(secret_name, project_id)
        app.config['JWT_SECRET_KEY'] = retrieved_secret
        app_logger.info(f"✓ JWT_SECRET_KEY from Secret Manager (length: {len(retrieved_secret)})")
        app_logger.info(f"✓ Secret name: {secret_name}, Project: {project_id}")
        app_logger.info("=== JWT SECRET SETUP COMPLETE ===")
    except Exception as e:
        app_logger.critical(f"❌ Failed to retrieve JWT_SECRET_KEY: {e}")
        sys.exit(1)

# Step 3: Create a test endpoint to verify JWT secrets match
@app.route('/debug/jwt-secret-info')
def debug_jwt_secret_info():
    """Debug endpoint to check JWT secret configuration"""
    if is_production:
        return "Debug endpoint disabled in production", 403
    
    secret = app.config.get('JWT_SECRET_KEY', 'NOT SET')
    return jsonify({
        'service': 'LOGIN_SERVICE',  # Change this to 'LANDING_SERVICE' in app.py
        'secret_length': len(secret),
        'secret_hash': hash(secret),
        'secret_preview': secret[:10] + '...' if len(secret) > 10 else secret,
        'secret_source': 'env_var' if os.environ.get('JWT_SECRET_KEY') else 'secret_manager',
        'secret_manager_name': app.config.get('JWT_SECRET_MANAGER_NAME'),
        'project_id': app.config.get('GCP_PROJECT_ID')
    })

# Step 4: Enhanced token creation logging in LOGIN SERVICE
def create_token_with_debug(user_email, additional_claims):
    """Create token with debug logging"""
    app_logger.info(f"=== TOKEN CREATION START ===")
    app_logger.info(f"Creating token for user: {user_email}")
    app_logger.info(f"Additional claims: {additional_claims}")
    
    secret = app.config.get('JWT_SECRET_KEY')
    app_logger.info(f"Using secret (hash): {hash(secret)}")
    
    access_token = create_access_token(
        identity=user_email,
        additional_claims=additional_claims
    )
    
    app_logger.info(f"Created token preview: {access_token[:50]}...")
    app_logger.info(f"=== TOKEN CREATION COMPLETE ===")
    return access_token

# Step 5: Enhanced token verification logging in LANDING SERVICE
@app.route('/debug/token-verify')
@jwt_required()
def debug_token_verify():
    """Debug token verification"""
    if is_production:
        return "Debug endpoint disabled in production", 403
    
    try:
        identity = get_jwt_identity()
        claims = get_jwt()
        secret = app.config.get('JWT_SECRET_KEY')
        
        return jsonify({
            'verification_status': 'SUCCESS',
            'identity': identity,
            'claims': claims,
            'secret_hash': hash(secret),
            'service': 'LANDING_SERVICE'
        })
    except Exception as e:
        return jsonify({
            'verification_status': 'FAILED',
            'error': str(e),
            'service': 'LANDING_SERVICE'
        })

# Step 6: Temporary hardcoded secret for testing
# Add this to BOTH services for immediate testing
def set_temporary_shared_secret():
    """TEMPORARY: Set the same hardcoded secret for both services"""
    TEMP_SECRET = "shared-secret-for-testing-12345"
    app.config['JWT_SECRET_KEY'] = TEMP_SECRET
    app_logger.warning(f"⚠️ USING TEMPORARY HARDCODED SECRET: {TEMP_SECRET}")
    app_logger.warning("⚠️ REMOVE THIS BEFORE PRODUCTION DEPLOYMENT")

# Step 7: Environment variable verification
def verify_environment():
    """Verify critical environment variables"""
    required_vars = ['GCP_PROJECT_ID', 'JWT_SECRET_MANAGER_NAME']
    optional_vars = ['JWT_SECRET_KEY']
    
    app_logger.info("=== ENVIRONMENT VERIFICATION ===")
    
    for var in required_vars:
        value = os.environ.get(var)
        if value:
            app_logger.info(f"✓ {var}: {value}")
        else:
            app_logger.error(f"❌ {var}: NOT SET")
    
    for var in optional_vars:
        value = os.environ.get(var)
        if value:
            app_logger.info(f"✓ {var}: SET (length: {len(value)})")
        else:
            app_logger.info(f"○ {var}: NOT SET (will use Secret Manager)")
    
    app_logger.info("=== ENVIRONMENT VERIFICATION COMPLETE ===")

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
# --- Server-to-Server Communication Helper ---
def call_landing_service(endpoint, method='GET', data=None, access_token=None):
    """
    Make authenticated requests to the landing service
    """
    landing_url = current_app.config.get('LANDING_SERVICE_URL')
    if not landing_url:
        app_logger.error("LANDING_SERVICE_URL not configured.")
        return None

    if not endpoint.startswith('/'):
        endpoint = '/' + endpoint
    full_url = landing_url.rstrip('/') + endpoint

    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'LoginService/1.0'
    }

    if access_token:
        headers['Authorization'] = f'Bearer {access_token}'

    try:
        response = requests.request(method.upper(), full_url, headers=headers, json=data, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        app_logger.error(f"Failed to call landing service at {full_url}: {e}")
        return None
