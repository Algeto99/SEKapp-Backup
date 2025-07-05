import os
import smtplib
import traceback
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

app = Flask(__name__)

# --- Flask Config ---
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'dev-secret')
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'dev-jwt')
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)
app.config['JWT_TOKEN_LOCATION'] = ['cookies']
app.config['JWT_COOKIE_SECURE'] = True
app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', '.run.app')

# --- Email Config ---
app.config['SMTP_SERVER'] = os.environ.get('SMTP_SERVER', 'mail.tzolkintech.com')
app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', 587))
app.config['EMAIL_USERNAME'] = 'no-reply@tzolkintech.com'
app.config['ADMIN_EMAIL'] = 'rcanton@tzolkintech.com'
app.config['PROJECT_ID'] = 'tz-dev-secapp'
app.config['SECRET_NAME'] = 'admin-email-pass'

# --- Extensions ---
bcrypt = Bcrypt(app)
jwt = JWTManager(app)

# --- Secret Manager Functions ---
def get_secret_value(secret_name, project_id=None):
    """Retrieve secret value from GCP Secret Manager"""
    try:
        if not project_id:
            project_id = app.config.get('PROJECT_ID')

        if not project_id:
            app.logger.error("PROJECT_ID not found in environment variables")
            return None

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        secret_value = response.payload.data.decode("UTF-8")
        app.logger.info(f"Successfully retrieved secret: {secret_name}")
        return secret_value

    except Exception as e:
        app.logger.error(f"Error retrieving secret {secret_name}: {e}")
        return None

def get_email_password():
    """Get email password from Secret Manager or environment"""
    password = os.environ.get('EMAIL_PASSWORD')
    if password:
        app.logger.info("Using email password from environment variable")
        return password

    app.logger.info("Attempting to retrieve email password from Secret Manager")
    return get_secret_value(app.config['SECRET_NAME'])

# --- Email Functions ---
def send_email(to_email, subject, body, is_html=False):
    """Send email notification"""
    try:
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
        msg['From'] = email_username
        msg['To'] = to_email
        msg['Subject'] = subject

        if is_html:
            msg.attach(MIMEText(body, 'html'))
        else:
            msg.attach(MIMEText(body, 'plain'))

        app.logger.info(f"Connecting to SMTP server: {smtp_server}:{smtp_port}")
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.set_debuglevel(1)  # Enable SMTP debugging
        app.logger.info("Starting TLS...")
        server.starttls()
        app.logger.info("Attempting login...")
        server.login(email_username, email_password)
        app.logger.info("Sending email...")
        server.sendmail(email_username, to_email, msg.as_string())
        server.quit()

        app.logger.info(f"Email sent successfully to {to_email}")
        return True

    except smtplib.SMTPAuthenticationError as e:
        app.logger.error(f"SMTP Authentication Error: {e}")
        app.logger.error("Possible causes: Incorrect password, 2FA not set up, or app access not allowed.")
        return False
    except smtplib.SMTPException as e:
        app.logger.error(f"SMTP Error: {e}")
        return False
    except Exception as e:
        app.logger.error(f"General error sending email: {e}")
        traceback.print_exc()
        return False

def send_registration_notification(user_email, user_name, phone_number):
    """Send notification email to admin and new user"""
    admin_email = app.config['ADMIN_EMAIL']

    email_password = get_email_password()
    if not email_password:
        app.logger.warning("Email password not available. Skipping notification.")
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

    app.logger.info(f"Sending registration notification to admin: {admin_email}")
    admin_result = send_email(admin_email, subject, html_body, is_html=True)

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
            return None
        conn = psycopg2.connect(db_url)
        return conn
    except Exception as e:
        app.logger.error(f"DB connection error: {e}")
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
        username = request.form.get('username')  # This is actually the email
        password = request.form.get('password')

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

            cur.execute("SELECT id FROM authorized_emails WHERE email = %s AND is_active = TRUE", (email,))
            authorized_email_entry = cur.fetchone()

            if not authorized_email_entry:
                flash('No estás autorizado para registrarte. Por favor, contacta a tu administrador.', 'danger')
                app.logger.warning(f"Registration attempt by unauthorized email: {email}")
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

            app.logger.info(f"Starting email notifications for user: {email}")

            email_issues = []

            notification_sent = send_registration_notification(email, name, phone_number)
            if not notification_sent:
                app.logger.error(f"Failed to send registration notification for user: {email}")
                email_issues.append("registration notification")

            welcome_sent = send_welcome_email(email, name)
            if not welcome_sent:
                app.logger.error(f"Failed to send welcome email to user: {email}")
                email_issues.append("welcome email")

            if email_issues:
                flash(f'¡Registro exitoso! Nota: No se pudieron enviar algunos emails ({", ".join(email_issues)}). Contacta al administrador si es necesario.', 'warning')
            else:
                flash('¡Registro exitoso! Se han enviado emails de confirmación. Ahora puedes iniciar sesión.', 'success')

            app.logger.info(f"User {email} registered successfully.")
            return redirect(url_for('login'))

        except psycopg2.errors.UniqueViolation as e:
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

# --- Startup Check ---
@app.route('/startup')
def startup_check():
    return {
        'status': 'ready',
        'service': 'login-service',
        'port': os.environ.get('PORT', '8080'),
        'timestamp': datetime.now().isoformat()
    }, 200

# --- Run App ---
if __name__ == '__main__':
