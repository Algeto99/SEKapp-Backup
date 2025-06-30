# Secapp/forms/app.py
import os
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
import psycopg2
import psycopg2.extras # Needed for DictCursor
from datetime import datetime
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity # NEW IMPORTS

app = Flask(__name__)

# --- Flask App Configuration ---
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_forms_service')

# --- JWT Configuration (MUST match login service) ---
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'your-super-secret-jwt-key')
# IMPORTANT: Configure Flask-JWT-Extended to expect tokens in cookies
app.config['JWT_TOKEN_LOCATION'] = ['cookies'] # Changed back to primarily 'cookies'
app.config['JWT_COOKIE_SECURE'] = True # Only send cookies over HTTPS (CRUCIAL for Cloud Run)
app.config['JWT_COOKIE_SAMESITE'] = 'Lax' # Helps with CSRF protection

# --- RE-ENABLED JWT_COOKIE_DOMAIN for cross-subdomain cookie sharing ---
# This is CRITICAL for cookies to be sent from one .run.app subdomain to another.
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', ".run.app")

if not app.config.get('SECRET_KEY'):
    raise RuntimeError("FLASK_SECRET_KEY environment variable is not set. Flask sessions for Forms service require a secret key.")
if not app.config.get('JWT_SECRET_KEY'):
    app.logger.warning("JWT_SECRET_KEY environment variable is not set. JWT authentication might not work correctly in Forms service.")

# Initialize Flask extensions
jwt = JWTManager(app)

# --- Database Connection (PostgreSQL) ---
def get_db_connection():
    conn = None
    try:
        db_url = os.environ.get('DATABASE_URL')
        if not db_url:
            app.logger.error("DATABASE_URL environment variable not set.")
            # Flash messages won't show on API call, logs are better.
            # But for full page loads, a flash message is still useful before redirect.
            flash('Error de configuración de la base de datos.', 'error')
            return None

        conn = psycopg2.connect(db_url)
        app.logger.info("Database connection successful.")
        return conn
    except Exception as e:
        app.logger.error(f"Error connecting to database: {e}")
        flash('Error de conexión a la base de datos.', 'error') # Also flash for full page renders
        return None

# --- JWT Callbacks for Error Handling and Redirection ---
# These functions will now perform an HTTP redirect.
@jwt.unauthorized_loader
@jwt.invalid_token_loader
@jwt.expired_token_loader
def token_error_response(callback):
    # This will flash a message and redirect to the login page.
    flash('Su sesión ha caducada o es inválida. Por favor, inicie sesión de nuevo.', 'danger')
    login_url = os.environ.get('LOGIN_SERVICE_URL')
    if login_url:
        return redirect(login_url + '/login')
    else:
        # Fallback if LOGIN_SERVICE_URL is not set
        return "Login service URL not configured.", 500

# --- CORS Headers (Less critical for full page navigations with cookies) ---
@app.after_request
def add_cors_headers(response):
    # For full page navigations, the browser automatically sends cookies based on domain.
    # CORS is primarily for AJAX requests. However, it's good practice if any cross-origin
    # JavaScript fetches might occur.
    allowed_origin = os.environ.get('LANDING_SERVICE_URL', '*') # Use '*' for development, be specific in production
    response.headers['Access-Control-Allow-Origin'] = allowed_origin
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization' # Keep Authorization for safety
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Credentials'] = 'true' # Essential if cookies are involved in CORS
    return response

@app.route('/')
@jwt_required() # Protect this route
def index():
    # When hitting the root, redirect to the main form page
    return redirect(url_for('show_report_form'))


@app.route('/report_form', methods=['GET'])
@jwt_required() # Protect this route
def show_report_form():
    current_user_identity = get_jwt_identity()
    app.logger.info(f"User {current_user_identity} accessing forms page.")

    conn = get_db_connection()
    if conn is None:
        # If DB connection fails, flash message handled by get_db_connection,
        # and we should redirect to login as there's no page to render.
        # This will be caught by the general token_error_response if no token,
        # or result in a blank page if not caught. Let's redirect explicitly.
        flash('Fallo al conectar con la base de datos, por favor intente de nuevo.', 'danger')
        return redirect(os.environ.get('LOGIN_SERVICE_URL', '/') + '/login')


    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # Fetch incident types
        cur.execute("SELECT id_tipo_incidencia AS id, nombre FROM tipo_incidencia ORDER BY nombre;")
        tipo_incidencia_data = cur.fetchall()

        # Fetch client types
        cur.execute("SELECT id_tipo_cliente AS id, nombre FROM tipo_cliente ORDER BY nombre;")
        tipo_cliente_data = cur.fetchall()

        # Fetch incident locations
        cur.execute("SELECT id_lugar_incidente AS id, nombre FROM lugar_incidente ORDER BY nombre;")
        lugar_incidente_data = cur.fetchall()

        # Fetch supervisors
        cur.execute("SELECT id_supervisor AS id, nombre FROM supervisor ORDER BY nombre;")
        supervisor_data = cur.fetchall()

        cur.close()
        # Return the HTML template directly
        return render_template(
            'form.html',
            tipo_incidencia=tipo_incidencia_data,
            tipo_cliente=tipo_cliente_data,
            lugar_incidente=lugar_incidente_data,
            supervisor=supervisor_data,
            username=current_user_identity,
            login_service_url=os.environ.get('LOGIN_SERVICE_URL', '#'), # Pass URLs for navigation
            dashboard_service_url=os.environ.get('DASHBOARD_SERVICE_URL', '#')
        )
    except psycopg2.Error as e:
        app.logger.error(f"Database error fetching lookup data: {e}")
        flash(f"Error al cargar opciones del formulario: {e}", 'error')
        # If DB error, redirect to login as well, or a specific error page
        return redirect(os.environ.get('LOGIN_SERVICE_URL', '/') + '/login')
    finally:
        if conn:
            conn.close()

@app.route('/submit_report', methods=['POST'])
@jwt_required() # Protect this route
def submit_report():
    current_user_identity = get_jwt_identity()
    app.logger.info(f"User {current_user_identity} submitting report.")

    # --- IMPORTANT: Revert to request.form for traditional form submission ---
    # If your form.html submits traditionally (method="POST"), use request.form.
    # If it submits via JavaScript fetch with JSON, then use request.get_json().
    # Based on the original form.html, it's a traditional POST.
    data = request.form # Assuming form.html now uses traditional POST

    if not data:
        flash("Invalid request: No form data received.", 'error')
        return redirect(url_for('show_report_form'))

    # Retrieve data using .get() to avoid KeyError if field is missing
    id_tipo_incidencia = data.get('tipo_incidencia')
    id_tipo_cliente = data.get('tipo_cliente')
    id_lugar_incidente = data.get('lugar_incidente')
    descripcion_zona_comun = data.get('descripcion_zona_comun')
    fecha_incidente = data.get('fecha_incidente')
    hora_incidente = data.get('hora_incidente')
    descripcion_incidente = data.get('descripcion_incidente')
    valor_aproximado = data.get('valor_aproximado')
    pertenencias_sustraidas = data.get('pertenencias_sustraidas')
    nombre_persona = data.get('nombre_persona')
    telefono_persona = data.get('telefono_persona')
    numero_identidad_persona = data.get('numero_identidad_persona')
    numero_local = data.get('numero_local')
    direccion = data.get('direccion')
    imagenes_pdfs = data.get('imagenes_pdfs')
    id_supervisor = data.get('supervisor')

    # Basic validation
    required_fields = {
        'tipo_incidencia': id_tipo_incidencia,
        'tipo_cliente': id_tipo_cliente,
        'lugar_incidente': id_lugar_incidente,
        'fecha_incidente': fecha_incidente,
        'hora_incidente': hora_incidente,
        'descripcion_incidente': descripcion_incidente,
        'nombre_persona': nombre_persona,
        'supervisor': id_supervisor
    }

    for field_name, value in required_fields.items():
        if not value:
            flash(f"El campo '{field_name.replace('_', ' ').capitalize()}' es requerido.", 'error')
            return redirect(url_for('show_report_form')) # Redirect back with flash

    conn = None
    try:
        conn = get_db_connection()
        if conn is None:
            flash("Fallo al conectar con la base de datos.", 'error')
            return redirect(url_for('show_report_form'))

        cur = conn.cursor()

        # Handle potential None values for optional fields
        descripcion_zona_comun = descripcion_zona_comun if descripcion_zona_comun else None
        # Convert to float if not None, else None
        valor_aproximado = float(valor_aproximado) if valor_aproximado else None
        pertenencias_sustraidas = pertenencias_sustraidas if pertenencias_sustraidas else None
        telefono_persona = telefono_persona if telefono_persona else None
        numero_identidad_persona = numero_identidad_persona if numero_identidad_persona else None
        numero_local = numero_local if numero_local else None
        direccion = direccion if direccion else None
        imagenes_pdfs = imagenes_pdfs if imagenes_pdfs else None

        cur.execute(
            """
            INSERT INTO reportes_incidentes (
                id_tipo_incidencia, id_tipo_cliente, id_lugar_incidente,
                descripcion_zona_comun, fecha_incidente, hora_incidente,
                descripcion_incidente, valor_aproximado, pertenencias_sustraidas,
                nombre_persona, telefono_persona, numero_identidad_persona,
                numero_local, direccion, imagenes_pdfs, id_supervisor
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                id_tipo_incidencia, id_tipo_cliente, id_lugar_incidente,
                descripcion_zona_comun, fecha_incidente, hora_incidente,
                descripcion_incidente, valor_aproximado, pertenencias_sustraidas,
                nombre_persona, telefono_persona, numero_identidad_persona,
                numero_local, direccion, imagenes_pdfs, id_supervisor
            )
        )
        conn.commit()
        cur.close()
        flash('Reporte de incidencia enviado exitosamente!', 'success')
        return redirect(url_for('show_report_form')) # Redirect back to form on success
    except psycopg2.Error as e:
        app.logger.error(f"Database error: {e}")
        if conn:
            conn.rollback()
        flash(f"Ocurrió un error en la base de datos al enviar el reporte: {e}", 'error')
        return redirect(url_for('show_report_form'))
    except Exception as e:
        app.logger.error(f"An unexpected error occurred: {e}")
        flash(f"Ocurrió un error inesperado: {e}", 'error')
        return redirect(url_for('show_report_form'))
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    # --- Local Development Environment Variables ---
    if 'FLASK_SECRET_KEY' not in os.environ:
        os.environ['FLASK_SECRET_KEY'] = 'dev_flask_secret_key_for_forms'
        app.logger.warning("WARNING: FLASK_SECRET_KEY not set. Using a default for local development.")

    if 'JWT_SECRET_KEY' not in os.environ:
        os.environ['JWT_SECRET_KEY'] = 'dev-secret-key-for-local-testing'
        app.logger.warning("WARNING: JWT_SECRET_KEY not set. Using default for local forms service.")

    if 'DATABASE_URL' not in os.environ:
        os.environ['DATABASE_URL'] = 'postgresql://your_local_user:your_local_password@localhost:5432/your_local_database'
        app.logger.warning("WARNING: DATABASE_URL not set. Using a default for local development. Update for your local DB!")

    # IMPORTANT: Ensure these URLs are correct for local testing and deployment
    if 'LOGIN_SERVICE_URL' not in os.environ:
        os.environ['LOGIN_SERVICE_URL'] = 'http://localhost:8080' # Login service runs on 8080
        app.logger.warning("WARNING: LOGIN_SERVICE_URL not set for forms service.")

    if 'DASHBOARD_SERVICE_URL' not in os.environ:
        os.environ['DASHBOARD_SERVICE_URL'] = 'http://localhost:5002' # Assuming 5002 for dashboard
        app.logger.warning("WARNING: DASHBOARD_SERVICE_URL not set for forms service.")

    if 'LANDING_SERVICE_URL' not in os.environ: # Added for CORS
        os.environ['LANDING_SERVICE_URL'] = 'http://localhost:5000' # Assuming 5000 for landing

    # IMPORTANT: JWT_COOKIE_DOMAIN will default to ".run.app" if not explicitly set
    if 'JWT_COOKIE_DOMAIN' not in os.environ:
        os.environ['JWT_COOKIE_DOMAIN'] = ".run.app" # Or ".us-central1.run.app"
        app.logger.warning("WARNING: JWT_COOKIE_DOMAIN not set. Using default for forms service.")


    app.run(host='0.0.0.0', port=os.environ.get('PORT', 8081), debug=True)