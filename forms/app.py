# Secapp/forms/app.py
import os
from flask import Flask, render_template, request, redirect, url_for, flash
import psycopg2
import psycopg2.extras # Needed for DictCursor
from datetime import datetime
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity # NEW IMPORTS

app = Flask(__name__)

# --- Flask App Configuration ---
# Set a secret key for flash messages and Flask session.
# IMPORTANT: Use a strong, randomly generated key in production.
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_forms_service')

# --- JWT Configuration (MUST match login service) ---
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
    Retries connection for robustness.
    """
    conn = None
    try:
        # DATABASE_URL should be set as an environment variable in Cloud Run
        # e.g., postgresql://USER:PASSWORD@/DB_NAME?host=/cloudsql/PROJECT_ID:REGION:INSTANCE_NAME
        db_url = os.environ.get('DATABASE_URL')
        if not db_url:
            print("DATABASE_URL environment variable not set.")
            flash('Error de configuración de la base de datos.', 'error')
            return None

        conn = psycopg2.connect(db_url)
        print("Database connection successful.")
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}")
        flash('Error de conexión a la base de datos.', 'error')
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
    return redirect(url_for('show_report_form'))


@app.route('/report_form', methods=['GET'])
@jwt_required() # Protect this route
def show_report_form():
    """
    Renders the incident report form, populating dropdowns from the database.
    """
    current_user_identity = get_jwt_identity() # Get the logged-in user's identity
    print(f"User {current_user_identity} accessing forms page.")

    conn = get_db_connection()
    if conn is None:
        # Flash message already handled by get_db_connection
        # We can still pass empty lists to render_template to prevent errors
        return render_template('form.html',
                               tipo_incidencia=[],
                               tipo_cliente=[],
                               lugar_incidente=[],
                               supervisor=[],
                               username=current_user_identity, # Pass username to template
                               login_service_url=os.environ.get('LOGIN_SERVICE_URL', '#'),
                               dashboard_service_url=os.environ.get('DASHBOARD_SERVICE_URL', '#'))

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # Fetch incident types
        cur.execute("SELECT id_tipo_incidencia AS id, nombre FROM tipo_incidencia ORDER BY nombre;")
        tipo_incidencia_data = cur.fetchall() # Renamed to avoid confusion with template variable

        # Fetch client types
        cur.execute("SELECT id_tipo_cliente AS id, nombre FROM tipo_cliente ORDER BY nombre;")
        tipo_cliente_data = cur.fetchall() # Renamed

        # Fetch incident locations
        cur.execute("SELECT id_lugar_incidente AS id, nombre FROM lugar_incidente ORDER BY nombre;")
        lugar_incidente_data = cur.fetchall() # Renamed

        # Fetch supervisors
        cur.execute("SELECT id_supervisor AS id, nombre FROM supervisor ORDER BY nombre;")
        supervisor_data = cur.fetchall()

        cur.close()
        return render_template(
            'form.html',
            tipo_incidencia=tipo_incidencia_data, # Pass fetched data to template
            tipo_cliente=tipo_cliente_data,
            lugar_incidente=lugar_incidente_data,
            supervisor=supervisor_data,
            username=current_user_identity, # Pass username to template
            login_service_url=os.environ.get('LOGIN_SERVICE_URL', '#'), # Pass URLs for navigation
            dashboard_service_url=os.environ.get('DASHBOARD_SERVICE_URL', '#')
        )
    except psycopg2.Error as e:
        print(f"Database error fetching lookup data: {e}")
        flash(f"Error al cargar opciones del formulario: {e}", 'error')
        return render_template('form.html',
                               tipo_incidencia=[],
                               tipo_cliente=[],
                               lugar_incidente=[],
                               supervisor=[],
                               username=current_user_identity, # Still pass username on error
                               login_service_url=os.environ.get('LOGIN_SERVICE_URL', '#'),
                               dashboard_service_url=os.environ.get('DASHBOARD_SERVICE_URL', '#'))
    finally:
        if conn:
            conn.close()

@app.route('/submit_report', methods=['POST'])
@jwt_required() # Protect this route
def submit_report():
    """
    Handles the submission of the incident report form data.
    Inserts the data into the PostgreSQL database.
    """
    current_user_identity = get_jwt_identity() # Get the logged-in user's identity
    print(f"User {current_user_identity} submitting report.")

    # Extract form data
    id_tipo_incidencia = request.form.get('tipo_incidencia')
    id_tipo_cliente = request.form.get('tipo_cliente')
    id_lugar_incidente = request.form.get('lugar_incidente')
    descripcion_zona_comun = request.form.get('descripcion_zona_comun')
    fecha_incidente = request.form.get('fecha_incidente')
    hora_incidente = request.form.get('hora_incidente')
    descripcion_incidente = request.form.get('descripcion_incidente')
    valor_aproximado = request.form.get('valor_aproximado')
    pertenencias_sustraidas = request.form.get('pertenencias_sustraidas')
    nombre_persona = request.form.get('nombre_persona')
    telefono_persona = request.form.get('telefono_persona')
    numero_identidad_persona = request.form.get('numero_identidad_persona')
    numero_local = request.form.get('numero_local')
    direccion = request.form.get('direccion')
    imagenes_pdfs = request.form.get('imagenes_pdfs')
    id_supervisor = request.form.get('supervisor')

    # Basic validation (add more robust validation as needed)
    required_fields = {
        'tipo_incidencia': id_tipo_incidencia,
        'tipo_cliente': id_tipo_cliente,
        'lugar_incidente': id_lugar_incidente,
        'fecha_incidente': fecha_incidente,
        'hora_incidente': hora_incidente,
        'descripcion_incidente': descripcion_incidente,
        'nombre_persona': nombre_persona, # Consider if this is always required or conditional
        'supervisor': id_supervisor
    }

    for field_name, value in required_fields.items():
        if not value:
            flash(f"El campo '{field_name.replace('_', ' ').capitalize()}' es requerido.", 'error')
            # Redirect back, retaining the existing form data if possible (more advanced)
            return redirect(url_for('show_report_form'))

    conn = None
    try:
        conn = get_db_connection()
        if conn is None:
            flash("Fallo al conectar con la base de datos.", 'error')
            return redirect(url_for('show_report_form'))

        cur = conn.cursor()

        # Convert empty strings to None for optional fields that can be NULL in DB
        descripcion_zona_comun = descripcion_zona_comun if descripcion_zona_comun else None
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
        # Redirect to the form page with a success message, instead of a separate success.html
        return redirect(url_for('show_report_form'))
    except psycopg2.Error as e:
        print(f"Database error: {e}")
        if conn:
            conn.rollback() # Rollback in case of error
        flash(f"Ocurrió un error en la base de datos al enviar el reporte: {e}", 'error')
        return redirect(url_for('show_report_form'))
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        flash(f"Ocurrió un error inesperado: {e}", 'error')
        return redirect(url_for('show_report_form'))
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    # --- Local Development Environment Variables ---
    # IMPORTANT: These are for local testing only.
    # Cloud Run environment variables will be set during deployment.
    if 'FLASK_SECRET_KEY' not in os.environ:
        os.environ['FLASK_SECRET_KEY'] = 'dev_flask_secret_key_for_forms'
        print("WARNING: FLASK_SECRET_KEY not set. Using a default for local development.")

    if 'JWT_SECRET_KEY' not in os.environ:
        os.environ['JWT_SECRET_KEY'] = 'dev-secret-key-for-local-testing'
        print("WARNING: JWT_SECRET_KEY not set. Using default for local forms service.")

    if 'DATABASE_URL' not in os.environ:
        # Example for local PostgreSQL connection
        os.environ['DATABASE_URL'] = 'postgresql://your_local_user:your_local_password@localhost:5432/your_local_database'
        print("WARNING: DATABASE_URL not set. Using a default for local development. Update for your local DB!")

    if 'LOGIN_SERVICE_URL' not in os.environ:
        # Replace with your actual local login service URL
        os.environ['LOGIN_SERVICE_URL'] = 'http://localhost:8080'
        print("WARNING: LOGIN_SERVICE_URL not set for forms service.")

    if 'DASHBOARD_SERVICE_URL' not in os.environ:
        # Replace with your actual local dashboard service URL
        os.environ['DASHBOARD_SERVICE_URL'] = 'http://localhost:8082'
        print("WARNING: DASHBOARD_SERVICE_URL not set for forms service.")

    app.run(host='0.0.0.0', port=os.environ.get('PORT', 8081), debug=True)