import os
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify # Added jsonify
from flask_bcrypt import Bcrypt
from flask_jwt_extended import (
    create_access_token, create_refresh_token, unset_jwt_cookies, JWTManager, # Removed set_access_cookies, set_refresh_cookies
    jwt_required, get_jwt_identity
)
import psycopg2
from datetime import timedelta
from psycopg2 import extras # Needed for DictCursor

app = Flask(__name__)

# --- Flask App Configuration ---
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY')

app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'your-super-secret-jwt-key')
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)
# Configure Flask-JWT-Extended to expect tokens primarily in headers
# We can still keep cookies enabled for flexibility or for refresh tokens if desired,
# but we'll explicitly send access token via header from frontend.
app.config['JWT_TOKEN_LOCATION'] = ['headers', 'cookies'] # IMPORTANT: Now looks in headers first
app.config['JWT_COOKIE_SECURE'] = True # Only send cookies over HTTPS (still good practice for refresh tokens if used)
app.config['JWT_COOKIE_SAMESITE'] = 'Lax' # Helps with CSRF protection.

# Removed JWT_COOKIE_DOMAIN as we are not relying on it for access token now.
# If you decide to use HttpOnly cookies for refresh tokens only, you might
# re-add this and adjust refresh token handling.

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
# These now assume the frontend will handle redirection based on API response
@jwt.unauthorized_loader
@jwt.invalid_token_loader
@jwt.expired_token_loader
def token_error_response(callback):
    # For API authentication, we often return JSON errors for token issues.
    # The frontend is then responsible for redirecting to login.
    # For a direct browser navigation, it might still trigger a redirect on a full page load.
    return jsonify(message='Token missing, invalid, or expired. Please log in again.', redirect_to_login=True), 401

# --- CORS Headers (Crucial for JavaScript Fetch requests from different origins) ---
@app.after_request
def add_cors_headers(response):
    # Replace with the actual origin of your Landing service in production
    # Or use Flask-CORS extension for more robust CORS handling
    allowed_origin = os.environ.get('LANDING_SERVICE_URL', 'http://localhost:5000')
    response.headers['Access-Control-Allow-Origin'] = allowed_origin
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Credentials'] = 'true' # If you ever send cookies with CORS
    return response

# --- Routes ---

@app.route('/')
@app.route('/login', methods=['GET', 'POST'])
def login():
    # Frontend will handle redirection based on JSON response, so this render_template
    # is only for the initial GET request to display the login form.
    # The actual POST for login will return JSON.
    if request.method == 'POST':
        username = request.form.get('username') # Assuming form submission
        password = request.form.get('password')

        # If using JSON for login, switch to:
        # data = request.get_json()
        # username = data.get('username')
        # password = data.get('password')

        conn = get_db_connection()
        if not conn:
            return jsonify(message='Error de conexión a la base de datos.'), 500

        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)
            cur.execute("SELECT id, username, password_hash FROM users WHERE username = %s", (username,))
            user = cur.fetchone()
            cur.close()

            if user and bcrypt.check_password_hash(user['password_hash'], password):
                access_token = create_access_token(identity=user['username'])
                # Refresh token can still be set as HttpOnly cookie if preferred,
                # to keep it secure from XSS for long-lived sessions.
                # However, for simplicity here, we're returning it in JSON too.
                refresh_token = create_refresh_token(identity=user['username'])

                # Return tokens in JSON response
                return jsonify(
                    access_token=access_token,
                    refresh_token=refresh_token,
                    message='¡Inicio de sesión exitoso!',
                    landing_url=os.environ.get('LANDING_SERVICE_URL', '/') # Tell frontend where to go
                ), 200
            else:
                return jsonify(message='Usuario o contraseña incorrectos.', status='error'), 401
        except Exception as e:
            print(f"Login error: {e}")
            app.logger.error(f"Login error: {e}")
            return jsonify(message='Ocurrió un error durante el inicio de sesión.', status='error'), 500
        finally:
            if conn:
                conn.close()
    
    # For GET requests to /login, render the template
    return render_template('login.html',
                           landing_service_url=os.environ.get('LANDING_SERVICE_URL', '/'),
                           login_service_url=os.environ.get('LOGIN_SERVICE_URL', '/'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    # Registration can largely remain the same as it doesn't involve JWTs directly initially.
    # It might redirect to the login page as before.
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
            return redirect(os.environ.get('LOGIN_SERVICE_URL', '/login')) # Redirect to the login page directly
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
    # This endpoint is now more about clearing the client-side tokens.
    # It will also unset any remaining HttpOnly cookies if you chose to use them for refresh tokens.
    response = redirect(os.environ.get('LOGIN_SERVICE_URL', '/login'))
    unset_jwt_cookies(response) # Clears any HttpOnly JWT cookies
    flash('Has cerrado sesión.', 'info')
    return response

# --- Placeholder Routes for Testing Redirection ---
# These would ideally be removed in a true microservices setup,
# where the login service doesn't host protected content.
# They are kept for demonstration of jwt_required working here.
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


# --- Main Runner for Local Development ---
if __name__ == '__main__':
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

    app.run(debug=True, host='0.0.0.0', port=os.environ.get('PORT', 8080))