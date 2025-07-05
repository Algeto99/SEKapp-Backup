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
            app.logger.error("DATABASE_URL environment variable not set.")
            raise ValueError("DATABASE_URL environment variable not set.")
        conn = psycopg2.connect(db_url)
        return conn
    except Exception as e:
        app.logger.error(f"DB connection error: {e}")
        flash('Error de conexión a la base de datos.', 'danger')
        return None

# --- JWT Error Handling ---
@jwt.unauthorized_loader
@jwt.invalid_token_loader
@jwt.expired_token_loader
def token_error_response(callback):
    flash('Su sesión ha caducado o es inválida. Por favor, inicie sesión de nuevo.', 'danger')
    return redirect(url_for('login'))

# --- CORS (optional for cookie mode) ---
@app.after_request
def add_cors_headers(response):
    # Ensure LANDING_SERVICE_URL is properly set in Cloud Run
    allowed_origin = os.environ.get('LANDING_SERVICE_URL', '*')
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
            # Since username is now email, we search by email (which is the username)
            cur.execute("SELECT id, email, password_hash FROM users WHERE email = %s", (username,))
            user = cur.fetchone()
            cur.close()

            if user and bcrypt.check_password_hash(user['password_hash'], password):
                access_token = create_access_token(identity=user['email'])
                refresh_token = create_refresh_token(identity=user['email'])

                response = redirect(os.environ.get('LANDING_SERVICE_URL', '/'))
                set_access_cookies(response, access_token)
                set_refresh_cookies(response, refresh_token)
                flash('Inicio de sesión exitoso.', 'success')
                return response
            else:
                flash('Usuario o contraseña incorrectos.', 'danger')
                return render_template('login.html', username=username)
        except Exception as e:
            app.logger.error(f"Login error: {e}")
            flash('Error durante el inicio de sesión.', 'danger')
            return render_template('login.html', username=username)
        finally:
            if conn:
                conn.close()

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    # Capture form data if it's a POST request, to pre-fill form on error
    email = request.form.get('email', '')
    name = request.form.get('name', '')
    phone_number = request.form.get('phone_number', '')

    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        # Basic validation for required fields (removed username from validation)
        if not all([email, name, password, confirm_password]):
            flash('Todos los campos obligatorios son requeridos.', 'warning')
            return render_template('register.html', email=email, name=name, phone_number=phone_number)

        # Password confirmation check
        if password != confirm_password:
            flash('Las contraseñas no coinciden.', 'danger')
            return render_template('register.html', email=email, name=name, phone_number=phone_number)

        conn = get_db_connection()
        if not conn:
            return render_template('register.html', email=email, name=name, phone_number=phone_number)

        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)

            # --- 1. Check if email is authorized ---
            cur.execute("SELECT id FROM authorized_emails WHERE email = %s AND is_active = TRUE", (email,))
            authorized_email_entry = cur.fetchone()

            if not authorized_email_entry:
                flash('No estás autorizado para registrarte. Por favor, contacta a tu administrador.', 'danger')
                app.logger.warning(f"Registration attempt by unauthorized email: {email}")
                return render_template('register.html', email=email, name=name, phone_number=phone_number)

            # --- 2. Check if email already exists in users table ---
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            existing_user_email = cur.fetchone()
            if existing_user_email:
                flash('Este correo electrónico ya está registrado. Por favor, inicia sesión.', 'danger')
                return render_template('register.html', email=email, name=name, phone_number=phone_number)

            # --- 3. Hash the password ---
            hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')

            # --- 4. Insert new user into the database (removed username field) ---
            # Since username = email, we store email in both username and email fields for compatibility
            cur.execute(
                "INSERT INTO users (username, email, name, phone_number, password_hash) VALUES (%s, %s, %s, %s, %s)",
                (email, email, name, phone_number if phone_number else None, hashed_password)
            )
            conn.commit()
            cur.close()

            # --- 5. Send Email Notifications ---
            app.logger.info(f"Starting email notifications for user: {email}")

            email_issues = []

            # Send notification to both admin and user
            app.logger.info("Sending registration notification to admin and user...")
            notification_sent = send_registration_notification(email, name, phone_number)
            if notification_sent:
                app.logger.info(f"Registration notification sent successfully for user: {email}")
            else:
                app.logger.error(f"Failed to send registration notification for user: {email}")
                email_issues.append("registration notification")

            # Send welcome email to user
            app.logger.info("Sending welcome email to user...")
            welcome_sent = send_welcome_email(email, name)
            if welcome_sent:
                app.logger.info(f"Welcome email sent successfully to user: {email}")
            else:
                app.logger.error(f"Failed to send welcome email to user: {email}")
                email_issues.append("welcome email")

            # Provide feedback about email status
            if email_issues:
                flash(f'¡Registro exitoso! Nota: No se pudieron enviar algunos emails ({", ".join(email_issues)}). Contacta al administrador si es necesario.', 'warning')
            else:
                flash('¡Registro exitoso! Se han enviado emails de confirmación. Ahora puedes iniciar sesión.', 'success')

            app.logger.info(f"User {email} registered successfully.")
            return redirect(url_for('login'))

        except psycopg2.errors.UniqueViolation as e:
            # This catch handles unique violations for username or email
            conn.rollback()
            if "users_username_key" in str(e) or "users_email_key" in str(e):
                flash('Este correo electrónico ya está registrado. Por favor, inicia sesión.', 'danger')
            else:
                flash('Error de registro: un valor duplicado ya existe.', 'danger')
            app.logger.error(f"Unique violation during registration: {e}")
            return render_template('register.html', email=email, name=name, phone_number=phone_number)

        except Exception as e:
            conn.rollback()
            app.logger.error(f"Error during registration: {e}")
            flash('Ocurrió un error inesperado durante el registro. Por favor, inténtalo de nuevo.', 'danger')
            return render_template('register.html', email=email, name=name, phone_number=phone_number)
        finally:
            if conn:
                conn.close()

    # For GET request, just render the empty form
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
    """Health check endpoint for Cloud Run"""
    health_status = {
        'status': 'healthy',
        'service': 'login-service',
        'timestamp': datetime.now().isoformat()
    }

    # Optional: Add database connectivity check
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
    """Startup check endpoint"""
    return {
        'status': 'ready',
        'service': 'login-service',
        'port': os.environ.get('PORT', '8080'),
        'timestamp': datetime.now().isoformat()
    }, 200

# --- Test Route for Email ---
@app.route('/test-email')
def test_email():
    """Test route to check email configuration"""
    email_password = get_email_password()
    if not email_password:
        return "Email password not configured or accessible from Secret Manager."

    # Test sending email to admin
    test_result = send_email(
        "roberto.j.canton@gmail.com", # Sending test email to your Gmail
        "Test Email - SMT SecApp (via Gmail)",
        "This is a test email to verify Gmail configuration with Secret Manager is working.",
        is_html=False
    )

    if test_result:
        return "Test email sent successfully! Check roberto.j.canton@gmail.com"
    else:
        return "Test email failed. Check logs for details."

# --- Debug Route ---
@app.route('/debug-email')
def debug_email():
    """Debug route to check email configuration and test sending"""
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

    # Test email sending
    test_result = None
    if debug_info['email_username'] and debug_info['email_password_set']:
        test_result = send_email(
            "roberto.j.canton@gmail.com", # Sending debug email to your Gmail
            "Debug Test Email - SMT SecApp (via Gmail)",
            "This is a test email from the debug route to verify Secret Manager integration is working.",
            is_html=False
        )

    return {
        'config': debug_info,
        'test_email_sent': test_result,
        'message': 'Check your application logs for detailed SMTP debug output'
    }

# --- Placeholder Routes ---
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

# --- Run App ---
if __name__ == '__main__':
    # Get port from environment variable or default to 8080
    port = int(os.environ.get('PORT', 8080))

    # Set debug mode based on environment
    debug_mode = os.environ.get('FLASK_ENV') == 'development'

    print(f"Starting Flask app on port {port}")
    print(f"Debug mode: {debug_mode}")
    print(f"Database URL configured: {'Yes' if os.environ.get('DATABASE_URL') else 'No'}")
    print(f"Project ID: {os.environ.get('GOOGLE_CLOUD_PROJECT', 'Not Set')}")
    print(f"Landing Service URL: {os.environ.get('LANDING_SERVICE_URL', 'Not Set')}")
    print(f"Login Service URL: {os.environ.get('LOGIN_SERVICE_URL', 'Not Set')}")

    # Test database connection on startup
    try:
        conn = get_db_connection()
        if conn:
            print("Database connection test: SUCCESS")
            conn.close()
        else:
            print("Database connection test: FAILED")
    except Exception as e:
        print(f"Database connection test error: {e}")

    # Test Secret Manager access
    try:
        email_password = get_email_password()
        if email_password:
            print("Secret Manager access: SUCCESS")
        else:
            print("Secret Manager access: FAILED (password not retrieved)")
    except Exception as e:
        print(f"Secret Manager access error: {e}")

    try:
        # Use production-ready settings
        app.run(
            debug=debug_mode,
            host='0.0.0.0',
            port=port,
            threaded=True,
            use_reloader=False # Important: disable reloader in production
        )
    except Exception as e:
        print(f"Error starting Flask app: {e}")
        traceback.print_exc()
        raise

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
