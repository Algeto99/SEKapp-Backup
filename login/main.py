from flask import Flask, render_template, request, redirect, url_for, flash
from flask_bcrypt import Bcrypt
from flask_jwt_extended import create_access_token, create_refresh_token, set_access_cookies, set_refresh_cookies, unset_jwt_cookies, JWTManager, jwt_required, get_jwt_identity
import os
import psycopg2
from datetime import timedelta

app = Flask(__name__)

# --- Flask App Configuration ---
# IMPORTANT: Set this environment variable in Cloud Run with a strong, random key!
# Generate with: python -c "import os; print(os.urandom(32).hex())"
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'your-super-secret-jwt-key')
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)  # Access token validity
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30) # Refresh token validity
app.config['JWT_TOKEN_LOCATION'] = ['cookies'] # JWTs will be stored in cookies
app.config['JWT_COOKIE_SECURE'] = True # Only send cookies over HTTPS
app.config['JWT_COOKIE_SAMESITE'] = 'Lax' # Helps with CSRF protection. Can be 'Strict' or 'None' (needs secure=True)

# CRITICAL for cross-service cookie sharing with custom domains (e.g., .yourdomain.com)
# Set this environment variable in Cloud Run if you are using custom domains.
# Example: export JWT_COOKIE_DOMAIN=".yourdomain.com"
# If NOT using custom domains (i.e., using *.a.run.app), remove this or set to None
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', None)

# Initialize Flask extensions
jwt = JWTManager(app)
bcrypt = Bcrypt(app)

# --- Database Connection (PostgreSQL) ---
def get_db_connection():
    try:
        # DATABASE_URL should be set as an environment variable in Cloud Run
        # e.g., postgresql://USER:PASSWORD@/DB_NAME?host=/cloudsql/PROJECT_ID:REGION:INSTANCE_NAME
        conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}")
        # In production, you might log this error and show a generic message
        flash('Error de conexión a la base de datos.', 'danger')
        return None

# --- JWT Callbacks for Error Handling and Redirection ---
# These functions define what happens when a JWT is missing, invalid, or expired.
@jwt.unauthorized_loader
def unauthorized_response(callback):
    flash('Por favor, inicie sesión para acceder a esta página.', 'warning')
    return redirect(url_for('login'))

@jwt.invalid_token_loader
def invalid_token_response(callback):
    flash('Token de sesión inválido. Por favor, inicie sesión de nuevo.', 'danger')
    return redirect(url_for('login'))

@jwt.expired_token_loader
def expired_token_response(callback):
    # A more advanced setup would use refresh tokens here
    flash('Su sesión ha expirado. Por favor, inicie sesión de nuevo.', 'warning')
    return redirect(url_for('login'))

# --- Routes ---

@app.route('/')
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        conn = get_db_connection()
        if not conn:
            return render_template('login.html') # Render login template with generic error via flash

        try:
            cur = conn.cursor()
            cur.execute("SELECT id, username, password_hash FROM users WHERE username = %s", (username,))
            user = cur.fetchone()
            cur.close()
            conn.close()

            if user and bcrypt.check_password_hash(user[2], password): # user[2] is password_hash
                access_token = create_access_token(identity=user[1]) # Use username as JWT identity
                refresh_token = create_refresh_token(identity=user[1])

                # IMPORTANT: In a multi-service app, you'd redirect to the *actual* dashboard URL here.
                # This should be the external URL of your dashboards service.
                dashboard_url = os.environ.get('DASHBOARD_SERVICE_URL', '/dashboard_placeholder')
                response = redirect(dashboard_url)

                set_access_cookies(response, access_token)
                set_refresh_cookies(response, refresh_token)
                flash('¡Inicio de sesión exitoso!', 'success')
                return response
            else:
                flash('Usuario o contraseña incorrectos.', 'danger')
        except Exception as e:
            print(f"Login error: {e}")
            flash('Ocurrió un error durante el inicio de sesión.', 'danger')
        finally:
            if conn:
                conn.close()

    return render_template('login.html')

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
            conn.close()

            flash('¡Registro exitoso! Ahora puede iniciar sesión.', 'success')
            return redirect(url_for('login'))
        except psycopg2.errors.UniqueViolation:
            flash('Ese nombre de usuario ya está registrado. Por favor, elija otro.', 'danger')
            conn.rollback() # Rollback transaction on unique violation
        except Exception as e:
            print(f"Registration error: {e}")
            flash('Ocurrió un error durante el registro.', 'danger')
        finally:
            if conn:
                conn.close()

    return render_template('register.html')

@app.route('/logout')
def logout():
    # IMPORTANT: After unsetting cookies, redirect to a public page, e.g., login or landing.
    response = redirect(os.environ.get('LANDING_SERVICE_URL', url_for('login'))) # Redirect to landing or login
    unset_jwt_cookies(response)
    flash('Has cerrado sesión.', 'info')
    return response

# --- Placeholder Routes for Testing Redirection ---
# These are just to demonstrate the protected redirection.
# In production, these would be the actual external URLs of your dashboard and forms services.

@app.route('/dashboard_placeholder')
@jwt_required() # This route is protected
def dashboard_placeholder():
    current_user_identity = get_jwt_identity()
    return f"""
    <h1>Bienvenido a tu dashboard (Login Service View), {current_user_identity}!</h1>
    <p>This is a placeholder page within the login service to show successful login and redirection.</p>
    <p><a href="{os.environ.get('DASHBOARD_SERVICE_URL', '/')}" style="color: blue;">Go to Actual Dashboard</a></p>
    <p><a href="{os.environ.get('FORMS_SERVICE_URL', '/')}" style="color: blue;">Go to Actual Forms</a></p>
    <p><a href="{url_for('logout')}" style="color: red;">Cerrar Sesión</a></p>
    """

@app.route('/forms_placeholder')
@jwt_required() # This route is protected
def forms_placeholder():
    current_user_identity = get_jwt_identity()
    return f"""
    <h1>Bienvenido al formulario (Login Service View), {current_user_identity}!</h1>
    <p>This is a placeholder page within the login service to show successful login and redirection.</p>
    <p><a href="{os.environ.get('FORMS_SERVICE_URL', '/')}" style="color: blue;">Go to Actual Forms</a></p>
    <p><a href="{os.environ.get('DASHBOARD_SERVICE_URL', '/')}" style="color: blue;">Go to Actual Dashboard</a></p>
    <p><a href="{url_for('logout')}" style="color: red;">Cerrar Sesión</a></p>
    """

# --- Main Runner for Local Development ---
if __name__ == '__main__':
    # Set dummy environment variables for local testing
    if 'JWT_SECRET_KEY' not in os.environ:
        os.environ['JWT_SECRET_KEY'] = 'dev-secret-key-for-local-testing'
        print("WARNING: JWT_SECRET_KEY not set. Using a default for local development. Set a strong key in production!")

    if 'DATABASE_URL' not in os.environ:
        os.environ['DATABASE_URL'] = 'postgresql://your_local_user:your_local_password@localhost:5432/your_local_database'
        print("WARNING: DATABASE_URL not set. Using a default for local development. Update for your local DB!")

    if 'DASHBOARD_SERVICE_URL' not in os.environ:
        os.environ['DASHBOARD_SERVICE_URL'] = 'http://localhost:5002/' # Example local URL for dashboard service
    if 'FORMS_SERVICE_URL' not in os.environ:
        os.environ['FORMS_SERVICE_URL'] = 'http://localhost:5001/' # Example local URL for forms service
    if 'LANDING_SERVICE_URL' not in os.environ:
        os.environ['LANDING_SERVICE_URL'] = 'http://localhost:5000/' # Example local URL for landing service

    app.run(debug=True, host='0.0.0.0', port=os.environ.get('PORT', 8080))