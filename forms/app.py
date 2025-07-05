import os
from flask import Flask, render_template, request, redirect, url_for, flash
import psycopg2
import psycopg2.extras
from datetime import datetime
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity # NEW IMPORTS for JWT
import logging # For better logging

# Configure logging for better visibility in Cloud Run logs
logging.basicConfig(level=logging.INFO) # Set default logging level to INFO
app_logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Flask App Configuration ---
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_forms_service')

# --- JWT Configuration (MUST match login and dashboard services) ---
# IMPORTANT: This key MUST be identical to the JWT_SECRET_KEY in your login service.
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'your-super-secret-jwt-key')
app.config['JWT_TOKEN_LOCATION'] = ['cookies'] # JWTs will be stored in cookies
app.config['JWT_COOKIE_SECURE'] = True # Only send cookies over HTTPS in production
app.config['JWT_COOKIE_SAMESITE'] = 'Lax' # Helps with CSRF protection. Can be 'Strict' or 'None' (needs secure=True)

# CRITICAL for cross-service cookie sharing with custom domains (e.g., .yourdomain.com)
# Set this environment variable in Cloud Run if you are using custom domains.
# Example: export JWT_COOKIE_DOMAIN=".yourdomain.com"
# If NOT using custom domains (i.e., using *.a.run.app), set to None or remove
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', None)

app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL', 'http://localhost:8080')
app.config['DASHBOARD_SERVICE_URL'] = os.environ.get('DASHBOARD_SERVICE_URL', 'http://localhost:8082')
app.config['LANDING_SERVICE_URL'] = os.environ.get('LANDING_SERVICE_URL', 'http://localhost:8081')


# Initialize Flask extensions
jwt = JWTManager(app)

# --- Database Connection ---
def get_db_connection():
    try:
        db_url = os.environ.get('DATABASE_URL')
        if not db_url:
            app_logger.error("DATABASE_URL not set.")
            flash('Error de configuración de la base de datos.', 'error')
            return None
        conn = psycopg2.connect(db_url)
        app_logger.info("Forms service database connection successful.")
        return conn
    except Exception as e:
        app_logger.error(f"Error connecting to DB: {e}", exc_info=True)
        flash('Error de conexión a la base de datos.', 'error')
        return None

# --- JWT Callbacks for Error Handling and Redirection ---
@jwt.unauthorized_loader
def unauthorized_response(callback):
    login_url = app.config['LOGIN_SERVICE_URL']
    if not login_url.endswith('/login'):
        login_url = f"{login_url.rstrip('/')}/login"
    flash('Por favor, inicie sesión para acceder a esta página.', 'warning')
    app_logger.warning(f"Unauthorized access attempt to forms service. Redirecting to {login_url}")
    return redirect(login_url)

@jwt.invalid_token_loader
def invalid_token_response(callback):
    login_url = app.config['LOGIN_SERVICE_URL']
    if not login_url.endswith('/login'):
        login_url = f"{login_url.rstrip('/')}/login"
    flash('Token de sesión inválido. Por favor, inicie sesión de nuevo.', 'danger')
    app_logger.warning(f"Invalid token for forms service. Redirecting to {login_url}")
    return redirect(login_url)

@jwt.expired_token_loader
def expired_token_response(callback):
    login_url = app.config['LOGIN_SERVICE_URL']
    if not login_url.endswith('/login'):
        login_url = f"{login_url.rstrip('/')}/login"
    flash('Su sesión ha expirado. Por favor, inicie sesión de nuevo.', 'warning')
    app_logger.warning(f"Expired token for forms service. Redirecting to {login_url}")
    return redirect(login_url)


# --- CORS Headers (optional if using fetch/XHR) ---
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
@jwt_required() # Protect the root route
def index():
    return redirect(url_for('show_report_form'))

@app.route('/report_form', methods=['GET'])
@jwt_required() # Protect this route
def show_report_form():
    current_user_identity = get_jwt_identity() # Get the logged-in user's identity (email)
    app_logger.info(f"User {current_user_identity} accessing report form.")

    conn = get_db_connection()
    if conn is None:
        # Redirection handled by JWT error loaders if token is invalid/missing
        return redirect(app.config['LOGIN_SERVICE_URL'] + '/login')

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT id_tipo_incidencia AS id, nombre FROM tipo_incidencia ORDER BY nombre;")
        tipo_incidencia_data = cur.fetchall()
        cur.execute("SELECT id_tipo_cliente AS id, nombre FROM tipo_cliente ORDER BY nombre;")
        tipo_cliente_data = cur.fetchall()
        cur.execute("SELECT id_lugar_incidente AS id, nombre FROM lugar_incidente ORDER BY nombre;")
        lugar_incidente_data = cur.fetchall()
        cur.execute("SELECT id_supervisor AS id, nombre FROM supervisor ORDER BY nombre;")
        supervisor_data = cur.fetchall()
        cur.close()
        return render_template(
            'form.html',
            tipo_incidencia=tipo_incidencia_data,
            tipo_cliente=tipo_cliente_data,
            lugar_incidente=lugar_incidente_data,
            supervisor=supervisor_data,
            username=current_user_identity, # Pass username to template
            login_service_url=app.config['LOGIN_SERVICE_URL'],
            dashboard_service_url=app.config['DASHBOARD_SERVICE_URL']
        )
    except psycopg2.Error as e:
        app_logger.error(f"DB error loading form data: {e}", exc_info=True)
        flash("Error al cargar datos del formulario.", 'error')
        return redirect(app.config['LOGIN_SERVICE_URL'] + '/login')
    finally:
        if conn:
            conn.close()

@app.route('/submit_report', methods=['POST'])
@jwt_required() # Protect this route
def submit_report():
    current_user_email = get_jwt_identity() # Get the user's email from the JWT
    app_logger.info(f"User {current_user_email} submitting report.")

    data = request.form
    if not data:
        flash("No se recibió información del formulario.", 'error')
        return redirect(url_for('show_report_form'))

    required_fields = {
        'tipo_incidencia': data.get('tipo_incidencia'),
        'tipo_cliente': data.get('tipo_cliente'),
        'lugar_incidente': data.get('lugar_incidente'),
        'fecha_incidente': data.get('fecha_incidente'),
        'hora_incidente': data.get('hora_incidente'),
        'descripcion_incidente': data.get('descripcion_incidente'),
        'nombre_persona': data.get('nombre_persona'),
        'supervisor': data.get('supervisor')
    }

    for field, value in required_fields.items():
        if not value:
            flash(f"El campo '{field.replace('_', ' ').capitalize()}' es requerido.", 'error')
            return redirect(url_for('show_report_form'))

    conn = get_db_connection()
    if conn is None:
        flash("No se pudo conectar a la base de datos.", 'error')
        return redirect(url_for('show_report_form'))

    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO reportes_incidentes (
                id_tipo_incidencia, id_tipo_cliente, id_lugar_incidente,
                descripcion_zona_comun, fecha_incidente, hora_incidente,
                descripcion_incidente, valor_aproximado, pertenencias_sustraidas,
                nombre_persona, telefono_persona, numero_identidad_persona,
                numero_local, direccion, imagenes_pdfs, id_supervisor,
                user_email, -- NEW COLUMN
                creado_en   -- Use your schema's column name
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            data.get('tipo_incidencia'), data.get('tipo_cliente'), data.get('lugar_incidente'),
            data.get('descripcion_zona_comun') or None,
            data.get('fecha_incidente'), data.get('hora_incidente'),
            data.get('descripcion_incidente'),
            float(data.get('valor_aproximado')) if data.get('valor_aproximado') else None,
            data.get('pertenencias_sustraidas') or None,
            data.get('nombre_persona'), data.get('telefono_persona') or None,
            data.get('numero_identidad_persona') or None, data.get('numero_local') or None,
            data.get('direccion') or None, data.get('imagenes_pdfs') or None,
            data.get('supervisor'),
            current_user_email, # <--- Pass the current user's email
            datetime.now() # <--- Pass the current timestamp
        ))
        conn.commit()
        cur.close()
        flash("¡Reporte enviado exitosamente!", 'success')
        app_logger.info(f"Report submitted successfully by {current_user_email}.")
        return redirect(url_for('show_report_form'))
    except Exception as e:
        conn.rollback() # Rollback in case of error
        app_logger.error(f"Error saving report for {current_user_email}: {e}", exc_info=True)
        flash("Error al guardar el reporte en la base de datos.", 'error')
        return redirect(url_for('show_report_form'))
    finally:
        if conn:
            conn.close()

# --- Health Check Route ---
@app.route('/health')
def health_check():
    """Health check endpoint for Cloud Run"""
    health_status = {
        'status': 'healthy',
        'service': 'forms-service',
        'timestamp': datetime.now().isoformat()
    }
    status_code = 200
    # Optional: Add database connectivity check
    try:
        conn = get_db_connection()
        if conn:
            health_status['database'] = 'connected'
            conn.close()
        else:
            health_status['database'] = 'disconnected'
            health_status['status'] = 'unhealthy'
            status_code = 503
    except Exception as e:
        health_status['database'] = f'error: {str(e)}'
        health_status['status'] = 'unhealthy'
        status_code = 503
    
    app_logger.info(f"Health check status: {health_status['status']}")
    return health_status, status_code

# Add a startup check route
@app.route('/startup')
def startup_check():
    """Startup check endpoint for Cloud Run"""
    app_logger.info("Startup check requested.")
    return {
        'status': 'ready',
        'service': 'forms-service',
        'port': os.environ.get('PORT', '8080'),
        'timestamp': datetime.now().isoformat()
    }, 200

# --- Run App (for local development only) ---
if __name__ == '__main__':
    # These environment variables are typically set by Cloud Run or your local shell
    # but are provided here as fallbacks for direct script execution.
    os.environ.setdefault('FLASK_SECRET_KEY', 'dev_forms_secret')
    os.environ.setdefault('JWT_SECRET_KEY', 'dev-secret-key-for-local-testing') # Must match login service's local dev key
    os.environ.setdefault('JWT_COOKIE_DOMAIN', 'localhost') # For local testing
    os.environ.setdefault('DATABASE_URL', 'postgresql://user:pass@localhost/db')
    os.environ.setdefault('LOGIN_SERVICE_URL', 'http://localhost:8080')
    os.environ.setdefault('DASHBOARD_SERVICE_URL', 'http://localhost:8082')
    os.environ.setdefault('LANDING_SERVICE_URL', 'http://localhost:8081')

    port = int(os.environ.get('PORT', 8081))
    debug_mode = os.environ.get('FLASK_ENV') == 'development'

    app_logger.info(f"Starting Flask app locally on port {port}")
    app_logger.info(f"Debug mode: {debug_mode}")
    app_logger.info(f"JWT Cookie Domain: {app.config['JWT_COOKIE_DOMAIN']}")
    app_logger.info(f"Login Service URL: {app.config['LOGIN_SERVICE_URL']}")
    
    try:
        app.run(
            debug=debug_mode,
            host='0.0.0.0',
            port=port,
            threaded=True,
            use_reloader=False # Important: disable reloader in production
        )
    except Exception as e:
        app_logger.error(f"Error starting Flask app: {e}", exc_info=True)
        raise
