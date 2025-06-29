# Secapp/dashboards/app.py
import os
from flask import Flask, render_template, redirect, url_for, flash # Added flash
import psycopg2
import psycopg2.extras # Needed for DictCursor if you're using DictCursor for fetching
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity # NEW IMPORTS

app = Flask(__name__)

# --- Flask App Configuration ---
# Set a secret key for flash messages and Flask session.
# IMPORTANT: Use a strong, randomly generated key in production.
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_dashboard_service')

# --- JWT Configuration (MUST match login and forms services) ---
# IMPORTANT: This key MUST be identical to the JWT_SECRET_KEY in your login service.
# Set this environment variable in Cloud Run with a strong, random key!
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'your-super-secret-jwt-key')
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

# --- Database Connection (PostgreSQL) ---
def get_db_connection():
    """
    Establishes and returns a connection to the PostgreSQL database using DATABASE_URL.
    """
    conn = None
    try:
        # DATABASE_URL should be set as an environment variable in Cloud Run
        # e.g., postgresql://USER:PASSWORD@/DB_NAME?host=/cloudsql/PROJECT_ID:REGION:INSTANCE_NAME
        db_url = os.environ.get('DATABASE_URL')
        if not db_url:
            print("DATABASE_URL environment variable not set.")
            flash('Error de configuración de la base de datos para el dashboard.', 'error')
            return None

        conn = psycopg2.connect(db_url)
        print("Dashboard database connection successful.")
        return conn
    except Exception as e:
        print(f"Error connecting to dashboard database: {e}")
        flash('Error de conexión a la base de datos para el dashboard.', 'error')
        return None

# --- JWT Callbacks for Error Handling and Redirection ---
# These functions define what happens when a JWT is missing, invalid, or expired.
@jwt.unauthorized_loader
def unauthorized_response(callback):
    # IMPORTANT: Redirect to the external URL of your login service's login page
    login_url = os.environ.get('LOGIN_SERVICE_URL', '/') # Default to root if not set
    if not login_url.endswith('/login'):
        login_url = f"{login_url.rstrip('/')}/login" # Ensure it points to the login path
    flash('Por favor, inicie sesión para acceder a esta página.', 'warning')
    return redirect(login_url)

@jwt.invalid_token_loader
def invalid_token_response(callback):
    login_url = os.environ.get('LOGIN_SERVICE_URL', '/')
    if not login_url.endswith('/login'):
        login_url = f"{login_url.rstrip('/')}/login"
    flash('Token de sesión inválido. Por favor, inicie sesión de nuevo.', 'danger')
    return redirect(login_url)

@jwt.expired_token_loader
def expired_token_response(callback):
    # A more advanced setup would use refresh tokens here
    login_url = os.environ.get('LOGIN_SERVICE_URL', '/')
    if not login_url.endswith('/login'):
        login_url = f"{login_url.rstrip('/')}/login"
    flash('Su sesión ha expirado. Por favor, inicie sesión de nuevo.', 'warning')
    return redirect(login_url)


@app.route('/')
@jwt_required() # Protect this route
def index():
    return redirect(url_for('show_dashboard'))


@app.route('/dashboard', methods=['GET'])
@jwt_required() # Protect this route
def show_dashboard():
    """
    Renders the dashboard page, fetching all submitted data from the database.
    """
    current_user_identity = get_jwt_identity() # Get the logged-in user's identity
    print(f"User {current_user_identity} accessing dashboard.")

    conn = None
    submissions = []
    try:
        conn = get_db_connection()
        if conn is None:
            # Flash message already handled by get_db_connection
            return render_template('dashboard.html',
                                   submissions=[], # Pass empty list to prevent template errors
                                   username=current_user_identity,
                                   login_service_url=os.environ.get('LOGIN_SERVICE_URL', '#'),
                                   forms_service_url=os.environ.get('FORMS_SERVICE_URL', '#'))

        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor) # Using DictCursor for easier access in template
        # TODO: Update this query to fetch relevant dashboard data from 'reportes_incidentes'
        # Example: Fetching a summary or latest reports
        # For a real dashboard, you'd fetch aggregated data, not raw form submissions
        cur.execute("""
            SELECT
                ri.id_reporte,
                ti.nombre AS tipo_incidencia,
                tc.nombre AS tipo_cliente,
                li.nombre AS lugar_incidente,
                ri.fecha_incidente,
                ri.hora_incidente,
                ri.descripcion_incidente,
                s.nombre AS supervisor_nombre,
                ri.created_at -- Assuming you have a timestamp for when the report was created
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
            ORDER BY
                ri.created_at DESC
            LIMIT 20; -- Limit to recent reports for dashboard
        """)
        submissions = cur.fetchall()
        cur.close()
    except psycopg2.Error as e:
        print(f"Database error fetching dashboard data: {e}")
        flash(f"Error al cargar datos del dashboard: {e}", 'error')
        return render_template('dashboard.html',
                               submissions=[], # Pass empty list on error
                               username=current_user_identity,
                               login_service_url=os.environ.get('LOGIN_SERVICE_URL', '#'),
                               forms_service_url=os.environ.get('FORMS_SERVICE_URL', '#'))
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        flash(f"Ocurrió un error inesperado al cargar el dashboard: {e}", 'error')
        return render_template('dashboard.html',
                               submissions=[], # Pass empty list on error
                               username=current_user_identity,
                               login_service_url=os.environ.get('LOGIN_SERVICE_URL', '#'),
                               forms_service_url=os.environ.get('FORMS_SERVICE_URL', '#'))
    finally:
        if conn:
            conn.close()

    return render_template('dashboard.html',
                           submissions=submissions,
                           username=current_user_identity,
                           login_service_url=os.environ.get('LOGIN_SERVICE_URL', '#'),
                           forms_service_url=os.environ.get('FORMS_SERVICE_URL', '#'))

if __name__ == '__main__':
    # --- Local Development Environment Variables ---
    # IMPORTANT: These are for local testing only.
    # Cloud Run environment variables will be set during deployment.
    if 'FLASK_SECRET_KEY' not in os.environ:
        os.environ['FLASK_SECRET_KEY'] = 'dev_flask_secret_key_for_dashboard'
        print("WARNING: FLASK_SECRET_KEY not set. Using a default for local development.")

    if 'JWT_SECRET_KEY' not in os.environ:
        os.environ['JWT_SECRET_KEY'] = 'dev-secret-key-for-local-testing'
        print("WARNING: JWT_SECRET_KEY not set. Using default for local dashboard service.")

    if 'DATABASE_URL' not in os.environ:
        # Example for local PostgreSQL connection
        os.environ['DATABASE_URL'] = 'postgresql://your_local_user:your_local_password@localhost:5432/your_local_database'
        print("WARNING: DATABASE_URL not set. Using a default for local development. Update for your local DB!")

    if 'LOGIN_SERVICE_URL' not in os.environ:
        # Replace with your actual local login service URL
        os.environ['LOGIN_SERVICE_URL'] = 'http://localhost:8080'
        print("WARNING: LOGIN_SERVICE_URL not set for dashboard service.")

    if 'FORMS_SERVICE_URL' not in os.environ:
        # Replace with your actual local forms service URL
        os.environ['FORMS_SERVICE_URL'] = 'http://localhost:8081'
        print("WARNING: FORMS_SERVICE_URL not set for dashboard service.")

    app.run(host='0.0.0.0', port=os.environ.get('PORT', 8082), debug=True)