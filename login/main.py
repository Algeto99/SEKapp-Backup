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

# Configure logging for Cloud Run
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
app_logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Configuration with better error handling ---
def configure_app():
    """Configure the Flask app with proper error handling"""
    try:
        # --- Flask Config ---
        app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY')
        if not app.config['SECRET_KEY']:
            app_logger.error("FLASK_SECRET_KEY environment variable not set")
            # Generate a temporary key for development, but log the issue
            import secrets
            app.config['SECRET_KEY'] = secrets.token_hex(32)
            app_logger.warning("Using temporary SECRET_KEY - set FLASK_SECRET_KEY in production")
        
        # Dynamic environment detection
        global is_production
        is_production = os.environ.get('K_SERVICE') is not None
        
        # JWT Configuration
        app.config['JWT_COOKIE_SECURE'] = is_production
        app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
        app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)
        app.config['JWT_TOKEN_LOCATION'] = ['cookies']
        app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
        app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN')
        
        # --- Email Config ---
        app.config['SMTP_SERVER'] = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
        app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', 587))
        app.config['EMAIL_USERNAME'] = os.environ.get('EMAIL_USERNAME')
        app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL')
        app.config['PROJECT_ID'] = os.environ.get('GCP_PROJECT_ID')
        app.config['SECRET_NAME'] = os.environ.get('EMAIL_PASSWORD_SECRET', 'admin-email-pass')
        
        app_logger.info(f"App configured - Production: {is_production}")
        return True
        
    except Exception as e:
        app_logger.error(f"Configuration error: {e}", exc_info=True)
        return False

# Configure the app
if not configure_app():
    app_logger.error("Failed to configure app")

# --- Extensions ---
bcrypt = Bcrypt(app)

# --- Secret Manager Functions ---
def get_secret_value(secret_name, project_id=None):
    """Retrieve secret value from GCP Secret Manager"""
    try:
        if not project_id:
            project_id = app.config.get('PROJECT_ID')

        if not project_id:
            app_logger.warning("PROJECT_ID not found - Secret Manager access unavailable")
            return None

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        secret_value = response.payload.data.decode("UTF-8")
        app_logger.info(f"Successfully retrieved secret: {secret_name}")
        return secret_value

    except Exception as e:
        app_logger.warning(f"Could not retrieve secret {secret_name}: {e}")
        return None

def get_email_password():
    """Get email password from environment or Secret Manager"""
    password = os.environ.get('EMAIL_PASSWORD')
    if password:
        app_logger.info("Using email password from environment variable")
        return password
    
    app_logger.info("Attempting to retrieve email password from Secret Manager")
    return get_secret_value(app.config['SECRET_NAME'])

def get_jwt_secret():
    """Get JWT secret key from environment or Secret Manager."""
    secret_key = os.environ.get('JWT_SECRET_KEY')
    if secret_key:
        app_logger.info("Using JWT_SECRET_KEY from environment variable")
        return secret_key

    app_logger.info("Attempting to retrieve JWT_SECRET_KEY from Secret Manager")
    return get_secret_value('jwt-secret-key', app.config.get('PROJECT_ID'))

# Set JWT Secret Key with fallback
def setup_jwt():
    """Setup JWT with proper error handling"""
    try:
        jwt_secret = get_jwt_secret()
        if not jwt_secret:
            app_logger.warning("JWT_SECRET_KEY not found - generating temporary key")
            import secrets
            jwt_secret = secrets.token_hex(32)
            app_logger.warning("Using temporary JWT key - set JWT_SECRET_KEY in production")
        
        app.config['JWT_SECRET_KEY'] = jwt_secret
        app_logger.info("JWT_SECRET_KEY configured successfully")
        return True
    except Exception as e:
        app_logger.error(f"JWT setup error: {e}", exc_info=True)
        return False

# Setup JWT
if setup_jwt():
    jwt = JWTManager(app)
else:
    app_logger.error("Failed to setup JWT")

# --- Database Connection ---
def get_db_connection():
    """Get database connection with better error handling"""
    try:
        db_url = os.environ.get('DATABASE_URL')
        if not db_url:
            app_logger.warning("DATABASE_URL not set - database features unavailable")
            return None
            
        conn = psycopg2.connect(db_url)
        app_logger.debug("Database connection successful")
        return conn
    except Exception as e:
        app_logger.error(f"Database connection error: {e}")
        return None

# --- Email Functions ---
def send_email(to_email, subject, body, is_html=False):
    """Send email notification with better error handling"""
    try:
        email_username = app.config.get('EMAIL_USERNAME')
        email_password = get_email_password()
        smtp_server = app.config.get('SMTP_SERVER')
        smtp_port = app.config.get('SMTP_PORT')
        
        if not all([email_username, email_password, smtp_server, smtp_port]):
            app_logger.warning("Email configuration incomplete - skipping email send")
            return False

        app_logger.info(f"Sending email to {to_email}")

        msg = MIMEMultipart()
        msg['From'] = email_username
        msg['To'] = to_email
        msg['Subject'] = subject

        if is_html:
            msg.attach(MIMEText(body, 'html'))
        else:
            msg.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(email_username, email_password)
            server.sendmail(email_username, to_email, msg.as_string())
        
        app_logger.info(f"Email sent successfully to {to_email}")
        return True
        
    except Exception as e:
        app_logger.error(f"Email send error: {e}")
        return False

def send_registration_notification(user_email, user_name, phone_number):
    """Send notification email to admin and user"""
    admin_email = app.config.get('ADMIN_EMAIL')
    if not admin_email:
        app_logger.warning("ADMIN_EMAIL not configured - skipping admin notification")
        return True

    subject = f"Nuevo Usuario Registrado - {user_name}"
    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #2563eb;">Nuevo Usuario Registrado - SMT SecApp</h2>
    <div style="background-color: #f8fafc; padding: 20px; border-radius: 8px; margin: 20px 0;">
    <h3 style="color: #1e40af;">Detalles del Usuario:</h3>
    <p><strong>Nombre:</strong> {user_name}</p>
    <p><strong>Email:</strong> {user_email}</p>
    <p><strong>Teléfono:</strong> {phone_number or 'No proporcionado'}</p>
    <p><strong>Fecha de Registro:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    </div>
    </div>
    </body>
    </html>
    """

    admin_result = send_email(admin_email, subject, html_body, is_html=True)
    user_result = send_email(user_email, f"Confirmación de Registro - {user_name}", html_body, is_html=True)

    return admin_result and user_result

def send_welcome_email(user_email, user_name):
    """Send welcome email to newly registered user"""
    subject = "¡Bienvenido a SMT SecApp!"
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
    <a href="{os.environ.get('LOGIN_SERVICE_URL', '#')}"
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
    flash('Su sesión ha caducado. Por favor, inicie sesión de nuevo.', 'danger')
    return redirect(url_for('login'))

@jwt.invalid_token_loader
def handle_invalid_token_loader(callback):
    flash('Su sesión es inválida. Por favor, inicie sesión de nuevo.', 'danger')
    return redirect(url_for('login'))

@jwt.expired_token_loader
def handle_expired_token_loader(jwt_header, jwt_payload):
    flash('Su sesión ha caducado. Por favor, inicie sesión de nuevo.', 'danger')
    return redirect(url_for('login'))

# --- CORS ---
@app.after_request
def add_cors_headers(response):
    allowed_origin = os.environ.get('LANDING_SERVICE_URL')
    if allowed_origin:
        response.headers['Access-Control-Allow-Origin'] = allowed_origin
        response.headers['Access-Control-Allow-Credentials'] = 'true'
    
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

# --- Routes ---
@app.route('/')
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if not username or not password:
            flash('Email y contraseña son requeridos.', 'warning')
            return render_template('login.html', username=username)

        conn = get_db_connection()
        if not conn:
            flash('Error de conexión a la base de datos.', 'danger')
            return render_template('login.html', username=username)

        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)
            cur.execute("SELECT id, email, password_hash FROM users WHERE email = %s", (username,))
            user = cur.fetchone()
            cur.close()

            if user and bcrypt.check_password_hash(user['password_hash'], password):
                access_token = create_access_token(identity=user['email'])
                refresh_token = create_refresh_token(identity=user['email'])

                landing_url = os.environ.get('LANDING_SERVICE_URL', '/')
                response = redirect(landing_url)
                set_access_cookies(response, access_token)
                set_refresh_cookies(response, refresh_token)
                flash('Inicio de sesión exitoso.', 'success')
                app_logger.info(f"User {username} logged in successfully")
                return response
            else:
                flash('Usuario o contraseña incorrectos.', 'danger')
                return render_template('login.html', username=username)
        except Exception as e:
            app_logger.error(f"Login error: {e}")
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
            flash('Error de conexión a la base de datos.', 'danger')
            return render_template('register.html', email=email, name=name, phone_number=phone_number)

        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)

            # Check if email is authorized (gracefully handle if table doesn't exist)
            try:
                cur.execute("SELECT id FROM authorized_emails WHERE email = %s AND is_active = TRUE", (email,))
                authorized_email_entry = cur.fetchone()
                if not authorized_email_entry:
                    flash('No estás autorizado para registrarte. Por favor, contacta a tu administrador.', 'danger')
                    return render_template('register.html', email=email, name=name, phone_number=phone_number)
            except psycopg2.Error as e:
                if "does not exist" in str(e).lower():
                    app_logger.warning("authorized_emails table does not exist - skipping authorization check")
                    conn.rollback()
                else:
                    raise e

            # Check if user already exists
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            existing_user = cur.fetchone()
            if existing_user:
                flash('Este correo electrónico ya está registrado.', 'danger')
                return render_template('register.html', email=email, name=name, phone_number=phone_number)

            # Create user
            hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
            cur.execute(
                "INSERT INTO users (username, email, name, phone_number, password_hash) VALUES (%s, %s, %s, %s, %s)",
                (email, email, name, phone_number if phone_number else None, hashed_password)
            )
            conn.commit()
            cur.close()

            # Send notifications
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
            app_logger.error(f"Registration error: {e}")
            flash('Error durante el registro.', 'danger')
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

# --- Health Check Routes ---
@app.route('/health')
def health_check():
    """Health check endpoint for Cloud Run"""
    health_status = {
        'status': 'healthy',
        'service': 'login-service',
        'timestamp': datetime.now().isoformat()
    }

    # Test database connection
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
    except Exception as e:
        health_status['database'] = f'error: {str(e)}'

    return health_status, 200

@app.route('/startup')
def startup_check():
    """Startup check endpoint"""
    return {
        'status': 'ready',
        'service': 'login-service',
        'timestamp': datetime.now().isoformat()
    }, 200

# --- Error Handlers ---
@app.errorhandler(404)
def not_found_error(error):
    return redirect(url_for('login'))

@app.errorhandler(500)
def internal_error(error):
    app_logger.error(f"Internal server error: {error}")
    flash('Error interno del servidor.', 'danger')
    return redirect(url_for('login'))

# --- Debug Routes (for development only) ---
@app.route('/test-email')
def test_email():
    """Test email configuration"""
    if is_production:
        return "Test endpoint disabled in production", 403
        
    admin_email = app.config.get('ADMIN_EMAIL')
    if not admin_email:
        return "ADMIN_EMAIL not configured", 400
        
    result = send_email(admin_email, "Test Email", "Test message")
    return f"Email test result: {result}"

# --- Main Application Entry Point ---
if __name__ == '__main__':
    try:
        port = int(os.environ.get('PORT', 8080))
        debug_mode = not is_production
        
        app_logger.info(f"Starting Flask app on port {port}, debug={debug_mode}")
        
        # For Cloud Run, we need to listen on all interfaces
        app.run(host='0.0.0.0', port=port, debug=debug_mode, threaded=True)
        
    except Exception as e:
        app_logger.critical(f"Failed to start Flask application: {e}", exc_info=True)
        raise