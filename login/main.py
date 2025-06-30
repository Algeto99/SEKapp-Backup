import os
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_bcrypt import Bcrypt
from flask_jwt_extended import (
    create_access_token, create_refresh_token, unset_jwt_cookies, JWTManager,
    set_access_cookies, set_refresh_cookies,
    jwt_required, get_jwt_identity
)
import psycopg2
from datetime import timedelta
from psycopg2 import extras

app = Flask(__name__)

# --- Flask App Configuration ---
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY')

app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'your-super-secret-jwt-key')
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)
app.config['JWT_TOKEN_LOCATION'] = ['cookies'] # ONLY look for tokens in cookies for auth
app.config['JWT_COOKIE_SECURE'] = True # Only send cookies over HTTPS (CRUCIAL for Cloud Run)
app.config['JWT_COOKIE_SAMESITE'] = 'Lax' # Helps with CSRF protection

# --- Set JWT_COOKIE_DOMAIN for cross-subdomain cookie sharing ---
# This is fundamental. It tells the browser to send these cookies to *.run.app
# so all your Cloud Run services can receive them.
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', ".run.app")

if not app.config.get('SECRET_KEY'):
    raise RuntimeError("FLASK_SECRET_KEY environment variable is not set. Flask sessions require a secret key.")
if not app.config.get('JWT_SECRET_KEY'):
    app.logger.warning("JWT_SECRET_KEY environment variable is not set. JWT operations might fail.")

# Initialize Flask extensions
jwt = JWTManager(app)
bcrypt = Bcrypt(app)

# --- Database Connection (PostgreSQL) ---
def get_db_connection():
    try:
        conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}")
        flash('Error de conexión a la base de datos.', 'danger')
        return None

# --- JWT Callbacks for Error Handling and Redirection ---
# These handlers will redirect to the login page on token issues across ALL services.
@jwt.unauthorized_loader
@jwt.invalid_token_loader
@jwt.expired_token_loader
def token_error_response(callback):
    flash('Su sesión ha caducado o es inválida. Por favor, inicie sesión de nuevo.', 'danger')
    login_url = os.environ.get('LOGIN_SERVICE_URL', '/')
    return redirect(login_url + '/login') # Ensure it redirects to the login form

# --- CORS Headers (Simplified) ---
# For cookie-based authentication with redirects, CORS headers are often less complex
# on the login route itself, as the browser handles the redirect.
# They are more critical for AJAX calls between services if those are implemented.
@app.after_request
def add_cors_headers(response):
    # This ensures that if the login service itself is accessed directly (e.g. for /login HTML)
    # or if any other AJAX calls are made to it (e.g. a future /refresh endpoint),
    # it can respond correctly.
    # For microservices, consider explicit origins or using Flask-CORS.
    response.headers['Access-Control-Allow-Origin'] = os.environ.get('LANDING_SERVICE_URL', '*') # Allow the Landing Service to access if needed (e.g., for direct image links)
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Credentials'] = 'true' # Essential if cookies are involved in CORS
    return response

# --- Routes ---

@app.route('/')
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Form submission is traditional, so use request.form
        username = request.form.get('username')
        password = request.form.get('password')

        conn = get_db_connection()
        if not conn:
            flash('Error de conexión a la base de datos.', 'danger')
            return render_template('login.html')

        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)
            cur.execute("SELECT id, username, password_hash FROM users WHERE username = %s", (username,))
            user = cur.fetchone()
            cur.close()

            if user and bcrypt.check_password_hash(user['password_hash'], password):
                access_token = create_access_token(identity=user['username'])
                refresh_token = create_refresh_token(identity=user['username'])

                landing_service_url = os.environ.get('LANDING_SERVICE_URL')
                if not landing_service_url:
                    app.logger.error("LANDING_SERVICE_URL environment variable is not set!")
                    flash('Error de configuración del servicio de aterrizaje.', 'danger')
                    return render_template('login.html')

                # --- CRITICAL: Create a redirect response and set HttpOnly cookies on it ---
                response = redirect(landing_service_url)
                set_access_cookies(response, access_token)
                set_refresh_cookies(response, refresh_token)

                flash('¡Inicio de sesión exitoso!', 'success') # Flash message for the redirect
                return response # This redirects the browser and sends the cookies
            else:
                flash('Usuario o contraseña incorrectos.', 'danger')
                return render_template('login.html')
        except Exception as e:
            print(f"Login error: {e}")
            app.logger.error(f"Login error: {e}")
            flash('Ocurrió un error durante el inicio de sesión.', 'danger')
            return render_template('login.html')
        finally:
            if conn:
                conn.close()

    # For GET requests to /login, render the template
    return render_template('login.html',
                           landing_service_url=os.environ.get('LANDING_SERVICE_URL', '/'),
                           login_service_url=os.environ.get('LOGIN_SERVICE_URL', '/'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not username or not password or not confirm_password:
            flash('Por favor, rellene todos los campos.', 'warning')
            return render_template('register.html')

        if password != confirm_password:
            flash('Las contraseñas no coinciden.', 'danger')
            return render_template('register.html', username=username)

        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')

        conn = get_db_connection()
        if not conn:
            return render_template('register.html')

        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id", (username, hashed_password))
            user_id = cur.fetchone()[0]
            conn.commit()
            cur.close()
            flash('¡Registro exitoso! Ahora puede iniciar sesión.', 'success')
            return redirect(os.environ.get('LOGIN_SERVICE_URL', '/login'))
        except psycopg2.errors.UniqueViolation:
            flash('Ese nombre de usuario ya está registrado. Por favor, elija otro.', 'danger')
            conn.rollback()
        except Exception as e:
            print(f"Registration error: {e}")
            app.logger.error(f"Registration error: {e}")
            flash('Ocurrió un error durante el registro.', 'danger')
        finally:
            if conn:
                conn.close()

    return render_template('register.html')

@app.route('/logout')
def logout():
    response = redirect(os.environ.get('LOGIN_SERVICE_URL', '/login')) # Redirect to login page after logout
    unset_jwt_cookies(response) # Clears all JWT cookies
    flash('Has cerrado sesión.', 'info')
    return response

# Placeholder routes for local testing of protected content, not part of microservice structure
@app.route('/dashboard_placeholder')
@jwt_required()
def dashboard_placeholder():
    current_user_identity = get_jwt_identity()
    return f"""
    <h1>Bienvenido a tu dashboard (Login Service View), {current_user_identity}!</h1>
    <p>This is a placeholder page within the login service. You should generally redirect to the actual dashboard service.</p>
    <p><a href="{os.environ.get('DASHBOARD_SERVICE_URL', '/')}" style="color: blue;">Go to Actual Dashboard</a></p>
    <p><a href="{os.environ.get('FORMS_SERVICE_URL', '/')}" style="color: blue;">Go to Actual Forms</a></p>
    <p><a href="{os.environ.get('LOGIN_SERVICE_URL', '/')}/logout" style="color: red;">Cerrar Sesión</a></p>
    """

@app.route('/forms_placeholder')
@jwt_required()
def forms_placeholder():
    current_user_identity = get_jwt_identity()
    return f"""
    <h1>Bienvenido al formulario (Login Service View), {current_user_identity}!</h1>
    <p>This is a placeholder page within the login service. You should generally redirect to the actual forms service.</p>
    <p><a href="{os.environ.get('FORMS_SERVICE_URL', '/')}" style="color: blue;">Go to Actual Forms</a></p>
    <p><a href="{os.environ.get('DASHBOARD_SERVICE_URL', '/')}" style="color: blue;">Go to Actual Dashboard</a></p>
    <p><a href="{os.environ.get('LOGIN_SERVICE_URL', '/')}/logout" style="color: red;">Cerrar Sesión</a></p>
    """

if __name__ == '__main__':
    # Local Development Environment Variables
    if 'FLASK_SECRET_KEY' not in os.environ:
        os.environ['FLASK_SECRET_KEY'] = 'a_very_secret_key_for_local_dev'
        print("WARNING: FLASK_SECRET_KEY not set. Using a default for local development. Set a strong key in production!")
    if 'JWT_SECRET_KEY' not in os.environ:
        os.environ['JWT_SECRET_KEY'] = 'dev-secret-key-for-local-testing'
        print("WARNING: JWT_SECRET_KEY not set. Using a default for local development. Set a strong key in production!")
    if 'DATABASE_URL' not in os.environ:
        os.environ['DATABASE_URL'] = 'postgresql://tz-dev-secapp-user:Tzolkin1!@localhost:5432/tz-dev-secapp-database'
        print("WARNING: DATABASE_URL not set. Using a default for local development. Update for your local DB!")

    # IMPORTANT: Ensure these URLs are correct for local testing
    if 'DASHBOARD_SERVICE_URL' not in os.environ:
        os.environ['DASHBOARD_SERVICE_URL'] = 'http://localhost:5002' # Assuming 5002 for dashboard
    if 'FORMS_SERVICE_URL' not in os.environ:
        os.environ['FORMS_SERVICE_URL'] = 'http://localhost:8081' # Forms service runs on 8081
    if 'LANDING_SERVICE_URL' not in os.environ:
        os.environ['LANDING_SERVICE_URL'] = 'http://localhost:5000' # Assuming 5000 for landing
    if 'LOGIN_SERVICE_URL' not in os.environ:
        os.environ['LOGIN_SERVICE_URL'] = 'http://localhost:8080' # Login service runs on 8080
    if 'JWT_COOKIE_DOMAIN' not in os.environ:
        os.environ['JWT_COOKIE_DOMAIN'] = ".run.app" # For Cloud Run deployments

    app.run(debug=True, host='0.0.0.0', port=os.environ.get('PORT', 8080))