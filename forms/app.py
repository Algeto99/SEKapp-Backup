import os
import logging
from flask import Flask, render_template, request, redirect, flash, jsonify, url_for, send_from_directory
from flask_jwt_extended import JWTManager, get_jwt_identity, jwt_required, unset_jwt_cookies, get_jwt
from google.cloud import storage, secretmanager
from werkzeug.utils import secure_filename
from datetime import datetime
import psycopg2
import psycopg2.extras
import uuid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
import socket
from google.api_core.exceptions import NotFound
import urllib.parse as urlparse
import re
from concurrent.futures import ThreadPoolExecutor
import time

# --- Logger Setup ---
logging.basicConfig(level=logging.INFO)
app_logger = logging.getLogger('app')

# --- Flask App Setup ---
app = Flask(__name__)
GCS_BUCKET_NAME = 'smt-uploads' # Make sure this bucket exists and permissions are set

# Initialize GCS client lazily or globally if environment is ready
try:
    gcs_client = storage.Client()
    logging.info("Global GCS Client initialized successfully.")
except Exception as e:
    logging.warning(f"Failed to initialize global GCS Client: {e}")
    gcs_client = None

def configure_app(app):
    is_production = os.getenv("K_SERVICE") is not None

    app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'forms-flask-secret-key')
    app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'jwt-secret-key')
    app.config['BASE_URL'] = os.environ.get('BASE_URL', '/')

    app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com')
    app.config['LANDING_SERVICE_URL'] = os.environ.get('LANDING_SERVICE_URL', 'https://landing.secapp.tzolkintech.com')
    app.config['DASHBOARD_SERVICE_URL'] = os.environ.get('DASHBOARD_SERVICE_URL', 'https://dashboard.secapp.tzolkintech.com')
    app.config['VIEWER_SERVICE_URL'] = os.environ.get('VIEWER_SERVICE_URL', 'https://viewer.secapp.tzolkintech.com')

    app.config['INTERNAL_LOGIN_SERVICE_URL'] = os.environ.get('INTERNAL_LOGIN_SERVICE_URL', 'https://login-24309643178.us-central1.run.app')
    app.config['INTERNAL_LANDING_SERVICE_URL'] = os.environ.get('INTERNAL_LANDING_SERVICE_URL', 'https://landing-24309643178.us-central1.run.app')
    app.config['INTERNAL_DASHBOARD_SERVICE_URL'] = os.environ.get('INTERNAL_DASHBOARD_SERVICE_URL', 'https://dashboard-24309643178.us-central1.run.app')
    app.config['INTERNAL_VIEWER_SERVICE_URL'] = os.environ.get('INTERNAL_VIEWER_SERVICE_URL', 'https://viewer-24309643178.us-central1.run.app')

    app.config['JWT_TOKEN_LOCATION'] = ['cookies']
    app.config['JWT_ACCESS_COOKIE_PATH'] = '/'
    app.config['JWT_COOKIE_SECURE'] = is_production
    app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
    app.config['JWT_ACCESS_COOKIE_NAME'] = 'access_token_cookie'
    app.config['JWT_COOKIE_CSRF_PROTECT'] = False
    app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', None)

    app.config['SMTP_SERVER'] = os.environ.get('SMTP_SERVER', 'tzolkintech.com')
    app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', 587))
    app.config['SMTP_USE_TLS'] = os.environ.get('SMTP_USE_TLS', 'true').lower() == 'true'
    app.config['EMAIL_USERNAME'] = os.environ.get('EMAIL_USERNAME', 'no-reply@tzolkintech.com')
    app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL', 'rcanton@tzolkintech.com')
    app.config['GCP_PROJECT_ID'] = os.environ.get('GCP_PROJECT', os.environ.get('GOOGLE_CLOUD_PROJECT'))
    app.config['EMAIL_PASSWORD_SECRET_NAME'] = os.environ.get('EMAIL_PASSWORD_SECRET', 'admin-email-pass')
    app.config['CC_EMAIL'] = os.environ.get('CC_EMAIL', 'alvaro.montalvo@gmail.com')

    app_logger.info(f"Forms service configured - Production: {is_production}")

configure_app(app)
jwt = JWTManager(app)

# --- JWT Error Handlers ---
@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_payload):
    return redirect(app.config.get('LOGIN_SERVICE_URL'))

@jwt.invalid_token_loader
def invalid_token_callback(error_string):
    return redirect(app.config.get('LOGIN_SERVICE_URL'))

@jwt.unauthorized_loader
def unauthorized_callback(error_string):
    return redirect(app.config.get('LOGIN_SERVICE_URL'))

@jwt.revoked_token_loader
def revoked_token_callback(jwt_header, jwt_payload):
    return redirect(app.config.get('LOGIN_SERVICE_URL'))

@jwt.needs_fresh_token_loader
def needs_fresh_token_callback(jwt_header, jwt_payload):
    return redirect(app.config.get('LOGIN_SERVICE_URL'))

# --- Database Connection ---
def get_db_connection():
    db_url = os.getenv('DATABASE_URL')
    if not db_url:
        raise Exception("DATABASE_URL environment variable not set")

    urlparse.uses_netloc.append('postgres')
    parsed_url = urlparse.urlparse(db_url)
    query = dict(urlparse.parse_qsl(parsed_url.query))

    try:
        conn = psycopg2.connect(
            dbname=parsed_url.path[1:],
            user=parsed_url.username,
            password=parsed_url.password,
            host=query.get('host', parsed_url.hostname),
            port=query.get('port', parsed_url.port or '5432')
        )
        return conn
    except Exception as e:
        app_logger.error(f"Database connection error: {e}", exc_info=True)
        raise

# --- Helper Functions ---
def upload_file_to_gcs(file, bucket_name):
    """Uploads a file to Google Cloud Storage."""
    if not file or not file.filename:
        return None
    try:
        # Use global client if available, else fallback (though global should be preferred)
        global gcs_client
        client = gcs_client if gcs_client else storage.Client()
        
        bucket = client.bucket(bucket_name)
        unique_filename = f"{uuid.uuid4()}_{secure_filename(file.filename)}"
        blob = bucket.blob(unique_filename)
        # app_logger.info(f"Starting upload for file: {unique_filename} to bucket {bucket_name}")
        
        start_time = time.time()
        blob.upload_from_file(file, content_type=file.content_type)
        duration = time.time() - start_time
        
        app_logger.info(f"File {unique_filename} uploaded to {bucket_name} in {duration:.2f}s.")
        return f"https://storage.googleapis.com/{bucket.name}/{blob.name}"
    except Exception as e:
        app_logger.error(f"Error uploading file to GCS: {e}", exc_info=True)
        return None # Return None or raise an exception based on desired error handling

def get_secret_value(secret_name):
    project_id = app.config.get('GCP_PROJECT_ID')
    if not project_id:
        raise ValueError(f"GCP_PROJECT_ID required for '{secret_name}'.")

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except NotFound:
        raise ValueError(f"Secret '{secret_name}' not found.")
    except Exception as e:
        app_logger.error(f"Error accessing secret '{secret_name}': {e}", exc_info=True)
        raise

def get_email_password():
    password = os.environ.get('EMAIL_PASSWORD')
    if password:
        return password

    try:
        with app.app_context():
            return get_secret_value(app.config.get('EMAIL_PASSWORD_SECRET_NAME'))
    except Exception as e:
        app_logger.warning(f"Could not retrieve email password: {e}")
        return None

def send_email(to_emails, subject, body, is_html=False, cc_emails=None):
    # ... (send_email function remains the same) ...
    pass # Keep existing implementation

def get_service_urls():
    """Helper to get all service URLs for templates."""
    return {
        'login_service_url': app.config.get('LOGIN_SERVICE_URL'),
        'landing_service_url': app.config.get('LANDING_SERVICE_URL'),
        'dashboard_service_url': app.config.get('DASHBOARD_SERVICE_URL'),
        'viewer_service_url': app.config.get('VIEWER_SERVICE_URL')
    }

def get_user_info_from_jwt():
    """Helper to extract user info from JWT, handling both string and dict identities."""
    try:
        identity = get_jwt_identity()
        claims = get_jwt()
        
        if isinstance(identity, str):
            # Identity is email, look in claims for details
            user_name = claims.get('name', 'Usuario')
            is_admin = claims.get('is_admin', False)
        else:
            # Fallback for old tokens or dict identity
            user_name = identity.get('name', 'Usuario')
            is_admin = identity.get('is_admin', False)
            
        return user_name, is_admin
    except Exception as e:
        app_logger.warning(f"Could not parse JWT info: {e}")
        return "Usuario", False

# --- Health Check ---
@app.route('/health')
def health():
    return "OK", 200

# --- Root and Form Selection ---
@app.route('/')
@jwt_required()
def root_redirect():
    return redirect('/select')

@app.route('/select_form')
@app.route('/select')
@jwt_required()
def select_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'select_form.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

# --- REPORTE DE INCIDENTE ---
@app.route('/reporte_incidente', methods=['GET'])
@jwt_required()
def reporte_incidente_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'reporte_incidente.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_incident_report', methods=['POST'])
@jwt_required()
def submit_incident_report():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        foto_url = None
        if 'foto_evidencia' in request.files:
            file = request.files['foto_evidencia']
            foto_url = upload_file_to_gcs(file, GCS_BUCKET_NAME)

        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area_especifica': request.form.get('puesto_area_especifica'),
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma_responsable'),
            'categoria': request.form.get('categoria'),
            'tipo_incidente': request.form.get('tipo_incidente'),
            'descripcion_incidente': request.form.get('descripcion'),
            'nivel_severidad': request.form.get('nivel_severidad'),

            'impacto': ", ".join(request.form.getlist('impacto')),
            'descripcion_impacto': request.form.get('descripcion_impacto'),
            'foto_evidencia_url': foto_url,
            'user_email': user_email
        }

        form_data = {k: v for k, v in form_data.items() if v is not None and v != ''}

        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'reportes_incidentes'")
        table_columns = [row[0] for row in cur.fetchall()]

        valid_form_data = {k: v for k, v in form_data.items() if k in table_columns}

        columns = ', '.join(valid_form_data.keys())
        placeholders = ', '.join(['%s'] * len(valid_form_data))
        sql = f"INSERT INTO reportes_incidentes ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(valid_form_data.values()))
        conn.commit()
        cur.close()

        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting incident report: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- MANTENIMIENTO SEGURIDAD FISICA ---
@app.route('/mantenimiento_seguridad_fisica')
@jwt_required()
def mantenimiento_seguridad_fisica_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'mantenimiento_seguridad_fisica.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_mantenimiento_seguridad_fisica', methods=['POST'])
@jwt_required()
def submit_mantenimiento_seguridad_fisica():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        form_data = {
            'fecha': request.form.get('fecha'),
            'hora': request.form.get('hora'),
            'sitio': request.form.get('sitio'),
            'equipo': request.form.get('equipo'),
            'id_equipo_serial': request.form.get('id_equipo_serial'),
            'tecnico_responsable': request.form.get('tecnico_responsable'),
            'tipo_servicio': request.form.get('tipo_servicio'),
            'actividad_realizada': request.form.get('actividad_realizada'),
            'resultado': request.form.get('resultado'),
            'downtime_horas': request.form.get('downtime_horas'),
            'repuestos_usados': request.form.get('repuestos_usados'),
            'tipo_alerta_generada': request.form.get('tipo_alerta_generada'),
            'observaciones': request.form.get('observaciones'),
            'descripcion_alerta_critica': request.form.get('descripcion_alerta_critica'),
            'accion_inmediata_critica': request.form.get('accion_inmediata_critica'),
            'accion_correctiva_recomendada': request.form.get('accion_correctiva_recomendada'),
            'responsable_asignado_critica': request.form.get('responsable_asignado_critica'),
            'fecha_limite_cierre_critica': request.form.get('fecha_limite_cierre_critica'),
            'estado_critica': request.form.get('estado_critica'),
            'firma_usuario': request.form.get('firma_usuario'),
            'submitted_by_email': user_email
        }

        conn = get_db_connection()
        cur = conn.cursor()

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO mantenimiento_seguridad_fisica ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(form_data.values()))
        conn.commit()
        cur.close()

        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting mantenimiento: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- MEDICION EXPERIENCIA CLIENTE ---
@app.route('/medicion_experiencia_cliente')
@jwt_required()
def medicion_experiencia_cliente_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'encuesta_cliente.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_medicion_experiencia_cliente', methods=['POST'])
@jwt_required()
def submit_medicion_experiencia_cliente():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma_responsable'),
            'atencion_cliente': request.form.get('atencion_cliente'),
            'comunicacion': request.form.get('comunicacion'),
            'confiabilidad': request.form.get('confiabilidad'),
            'capacidad_reaccion': request.form.get('capacidad_reaccion'),
            'cumplimiento': request.form.get('cumplimiento'),
            'competencia_personal': request.form.get('competencia_personal'),
            'actitud_servicio': request.form.get('actitud_servicio'),
            'atencion_quejas': request.form.get('atencion_quejas'),
            'calificacion_global_nps': request.form.get('calificacion_global_nps'),
            'recomendaria_servicio': request.form.get('recomendaria_servicio'),
            'observaciones_cliente': request.form.get('observaciones_cliente'),
            'encuestado': request.form.get('encuestado'),
            'firma_encuestado': request.form.get('firma_encuestado'),
            'submitted_by_email': user_email
        }


        app_logger.info(f"Submitting customer experience survey for user: {user_email}")

        app_logger.info("Connecting to DB...")
        conn = get_db_connection()
        cur = conn.cursor()

        # Validate columns against the database to prevent errors if schema drifts
        app_logger.info("Fetching schema columns...")
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'medicion_experiencia_cliente'")
        table_columns = [row[0] for row in cur.fetchall()]
        app_logger.info(f"Found {len(table_columns)} columns in schema.")

        valid_form_data = {k: v for k, v in form_data.items() if k in table_columns}
        
        # Log keys for debugging (avoid logging sensitive values or large base64 strings)
        app_logger.debug(f"Inserting into medicion_experiencia_cliente with keys: {list(valid_form_data.keys())}")

        columns = ', '.join(valid_form_data.keys())
        placeholders = ', '.join(['%s'] * len(valid_form_data))
        sql = f"INSERT INTO medicion_experiencia_cliente ({columns}) VALUES ({placeholders})"

        app_logger.info("Executing INSERT...")
        cur.execute(sql, list(valid_form_data.values()))
        app_logger.info("Committing transaction...")
        conn.commit()
        cur.close()

        app_logger.info("Customer experience survey submitted successfully.")
        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting encuesta: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- SUPERVISION PUESTO ---
@app.route('/supervision_puesto')
@jwt_required()
def supervision_puesto_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'supervision_puesto.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_supervision_puesto', methods=['POST'])
@jwt_required()
def submit_supervision_puesto():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    import re
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1. Capture Global Fields
        global_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'fecha_hora': request.form.get('fecha_hora'),
            'supervisor': request.form.get('supervisor'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'firma_supervisor': request.form.get('firma_supervisor'),
            'submitted_by_email': user_email
        }

        # 2. Parse Dynamic Supervisions from request.form
        # Keys are in format: supervisions[index][field_name]
        supervisions_map = {}
        pattern = re.compile(r'supervisions\[(\d+)\]\[(.*)\]')

        for key, value in request.form.items():
            match = pattern.match(key)
            if match:
                index = int(match.group(1))
                field = match.group(2)
                if index not in supervisions_map:
                    supervisions_map[index] = {}
                supervisions_map[index][field] = value

        # 3. Handle Files (supervisions[index][foto_evidencia])
        for key, file_storage in request.files.items():
            match = pattern.match(key)
            if match:
                index = int(match.group(1))
                field = match.group(2)
                if field == 'foto_evidencia':
                    # Upload file
                    url = upload_file_to_gcs(file_storage, GCS_BUCKET_NAME)
                    if url:
                         if index not in supervisions_map:
                             supervisions_map[index] = {}
                         supervisions_map[index]['foto_evidencia_url'] = url

        # 4. Process and Insert Each Supervision
        column_cache = None # Optimization to fetch columns once if needed, but simple query is fine

        for index, sup_data in supervisions_map.items():
            # Merge Global
            row_data = {**global_data, **sup_data}
            
            # Map fields to DB columns (ensure names match what DB expects)
            # Based on reading, DB columns likely match the form names we used:
            # puesto_area_especifica, rol_aplicador, horario_servicio, tipo_servicio
            # nombre_guardia, documento_guardia, porta_arma, serie_arma, cantidad_municion
            # realiza_induccion, conoce_ordenes_consignas, horario_detalles_claros
            # asistencia_puntualidad, presentacion_uniforme, estado_limpieza_puesto
            # equipamiento_completo, conoce_mision_vision, conoce_politica, estado_bitacora
            # observaciones_novedades, nombre_guardia_firma, firma_guardia
            
            # Filter empty strings/None
            filtered_data = {k: v for k, v in row_data.items() if v is not None and v != ''}

            # Reflection to get valid columns (Safety)
            if column_cache is None:
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'supervision_puesto'")
                column_cache = [row[0] for row in cur.fetchall()]
            
            valid_row_data = {k: v for k, v in filtered_data.items() if k in column_cache}
            
            if not valid_row_data:
                continue # Skip empty rows

            columns = ', '.join(valid_row_data.keys())
            placeholders = ', '.join(['%s'] * len(valid_row_data))
            sql = f"INSERT INTO supervision_puesto ({columns}) VALUES ({placeholders})"

            cur.execute(sql, list(valid_row_data.values()))

        conn.commit()
        cur.close()

        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting supervision puesto: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- INFORME NOVEDADES DISCIPLINARIO ---
@app.route('/informe_novedades_disciplinario')
@jwt_required()
def informe_novedades_disciplinario_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'reporte_disciplinario.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_informe_novedades_disciplinario', methods=['POST'])
@jwt_required()
def submit_informe_novedades_disciplinario():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        # [DEBUG] Start of execution
        content_length = request.content_length
        app_logger.info(f"[DEBUG] submit_informe_novedades_disciplinario started. Content-Length: {content_length}")

        anexos_urls = []
        if 'anexos_files' in request.files:
            files = request.files.getlist('anexos_files')
            file_count = len(files)
            app_logger.info(f"[DEBUG] Processing {file_count} files for upload.")
            
            # Use ThreadPoolExecutor for parallel uploads
            start_upload_time = time.time()
            with ThreadPoolExecutor(max_workers=5) as executor:
                # We need to map the function to the files, but we also need to pass bucket_name
                # Partial or lambda is good here
                futures = [executor.submit(upload_file_to_gcs, file, GCS_BUCKET_NAME) for file in files]
                
                for i, future in enumerate(futures):
                    try:
                        url = future.result() # This will block until the specific future is done
                        if url:
                            anexos_urls.append(url)
                            # app_logger.info(f"[DEBUG] File {i+1} uploaded successfully.")
                        else:
                            app_logger.warning(f"[DEBUG] File {i+1} returned None.")
                    except Exception as exc:
                        app_logger.error(f"[DEBUG] File {i+1} generated an exception: {exc}")

            total_upload_time = time.time() - start_upload_time
            app_logger.info(f"[DEBUG] All {file_count} files processed in {total_upload_time:.2f}s. Validation: {len(anexos_urls)}/{file_count} successful.")

        else:
             app_logger.info(f"[DEBUG] No 'anexos_files' in request.")

        anexos_str = "\n".join(anexos_urls) if anexos_urls else "No Aplica" if request.form.get('anexos_na') else ""

        fecha_hora_str = request.form.get('fecha_hora')
        app_logger.info(f"[DEBUG] Parsing fecha_hora: {fecha_hora_str}")
        fecha = None
        hora = None
        if fecha_hora_str:
            try:
                dt_obj = datetime.fromisoformat(fecha_hora_str)
                fecha = dt_obj.date()
                hora = dt_obj.time()
            except ValueError:
                app_logger.error(f"[DEBUG] Error parsing date: {fecha_hora_str}")
                pass
        
        app_logger.info("[DEBUG] Constructing form_data dictionary.")
        form_data = {
            'nombre_responsable': request.form.get('nombre_responsable'),
            'realizado_por_cargo': request.form.get('rol_aplicador'),
            'fecha': fecha,
            'hora': hora,
            'dirigido_a': None,
            'empleado_nombre': request.form.get('empleado_nombre'),
            'empleado_numero': request.form.get('empleado_numero'),
            'empleado_documento': request.form.get('empleado_documento'),
            'empleado_cargo': request.form.get('empleado_cargo'),
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area_especifica': request.form.get('puesto_area_especifica'),
            'tipo_novedad': request.form.get('tipo_novedad'),
            'sitio_ocurrencia': request.form.get('sitio_ocurrencia'),
            'descripcion_novedad': request.form.get('descripcion_novedad'),
            'otras_personas_involucradas': request.form.get('otras_personas_involucradas'),
            'anexos': anexos_str,
            'firma_responsable': request.form.get('firma_responsable'),
            'firma_recibido_revisado': request.form.get('firma_recibido_revisado'),
            'submitted_by_email': user_email,
            'fecha_hora': fecha_hora_str,
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno')
        }

        app_logger.info(f"Submitting disciplinary report for {user_email}, Employee: {form_data.get('empleado_nombre')}")

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'informe_novedades_disciplinario'")
        table_columns = [row[0] for row in cur.fetchall()]

        valid_form_data = {k: v for k, v in form_data.items() if k in table_columns}

        columns = ', '.join(valid_form_data.keys())
        placeholders = ', '.join(['%s'] * len(valid_form_data))
        sql = f"INSERT INTO informe_novedades_disciplinario ({columns}) VALUES ({placeholders})"
        
        # Log the SQL (be careful with sensitive data, or just log valid_form_data keys)
        app_logger.debug(f"Inserting into informe_novedades_disciplinario with keys: {list(valid_form_data.keys())}")

        cur.execute(sql, list(valid_form_data.values()))
        conn.commit()
        cur.close()

        app_logger.info("Disciplinary report submitted successfully.")
        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting informe: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- LOG DE PATRULLAS ---
@app.route('/log_de_patrullas')
@jwt_required()
def log_de_patrullas_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'log_de_patrullas.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_log_de_patrullas', methods=['POST'])
@jwt_required()
def submit_log_de_patrullas():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        form_data = {
            'id_guardia_nombre_guardia': request.form.get('id_guardia_nombre_guardia'),
            'sitio_ubicacion': request.form.get('sitio_ubicacion'),
            'id_patrulla_consecutivo': request.form.get('id_patrulla_consecutivo'),
            'fecha': request.form.get('fecha'),
            'hora_inicio': request.form.get('hora_inicio'),
            'hora_fin': request.form.get('hora_fin'),
            'detalles_incidente': request.form.get('detalles_incidente'),
            'riesgo_detectado': request.form.get('riesgo_detectado'),
            'nivel_riesgo': request.form.get('nivel_riesgo'),
            'estado_patrulla': request.form.get('estado_patrulla'),
            'contexto_observaciones': request.form.get('contexto_observaciones'),
            'firma_guardia': request.form.get('firma_guardia'),
            'firma_supervisor': request.form.get('firma_supervisor'),
            'submitted_by_email': user_email
        }

        conn = get_db_connection()
        cur = conn.cursor()

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO log_de_patrullas ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(form_data.values()))
        conn.commit()
        cur.close()

        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting log: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- REGISTRO DE CAPACITACIONES ---
@app.route('/registro_de_capacitaciones')
@jwt_required()
def registro_de_capacitaciones_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'registro_de_capacitaciones.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_registro_de_capacitaciones', methods=['POST'])
@jwt_required()
def submit_registro_de_capacitaciones():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area_especifica': request.form.get('puesto_area_especifica'),
            'fecha_hora': request.form.get('fecha_hora') or None,
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma_responsable'),
            'nombre_capacitacion': request.form.get('nombre_capacitacion'),
            'objetivo_capacitacion': request.form.get('objetivo_capacitacion'),
            'observaciones_retroalimentacion': request.form.get('observaciones_retroalimentacion'),
            'lista_asistencia': request.form.get('lista_asistencia'),
            'practica_simulacro_realizado': request.form.get('practica_simulacro_realizado'),
            'nivel_comprension': request.form.get('nivel_comprension'),
            'recomendaciones': request.form.get('recomendaciones'),
            'submitted_by_email': user_email
        }

        conn = get_db_connection()
        cur = conn.cursor()

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO registro_de_capacitaciones ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(form_data.values()))
        conn.commit()
        cur.close()

        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting capacitacion: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- REGISTRO Y ACTA DE VISITA ---
@app.route('/registro_y_acta_de_visita')
@jwt_required()
def registro_y_acta_de_visita_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'acta_visita_cliente.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_registro_y_acta_de_visita', methods=['POST'])
@jwt_required()
def submit_registro_y_acta_de_visita():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        # Process dynamic participants
        detalles_participantes = []
        
        # Process default participant (index 0 or no suffix if I didn't add it, but I added _0 in HTML replacement?)
        # Wait, in HTML I added name="nombre_participante_cliente_0" for default?
        # Let me check the HTML replacement again.
        # I added: name="nombre_participante_cliente_0" in the HTML replacement.
        # And for dynamic ones: name="nombre_participante_cliente_${asistenteCount}"
        
        # So I should iterate to find all matching keys.
        
        for key in request.form:
            if key.startswith('nombre_participante_cliente_'):
                suffix = key.split('_')[-1]
                nombre = request.form.get(f'nombre_participante_cliente_{suffix}')
                cargo = request.form.get(f'cargo_participante_cliente_{suffix}')
                firma = request.form.get(f'firma_participante_cliente_{suffix}')
                
                if nombre or cargo or firma:
                    detalles_participantes.append({'nombre': nombre, 'cargo': cargo, 'firma': firma})
        
        import json
        detalles_participantes_json = json.dumps(detalles_participantes)

        form_data = {
            'cliente_instalacion': request.form.get('cliente_visitado'), # Mapped from form field 'cliente_visitado'
            # 'puesto_area': request.form.get('puesto_area'), # Not present in this form
            'fecha_hora': request.form.get('fecha_hora'),
            'motivo_visita': request.form.get('motivo_visita'),
            'nombre_visitante': request.form.get('nombre_visitante'),
            'cargo_visitante': request.form.get('cargo_visitante'),
            'firma_visitante': request.form.get('firma_visitante'),
            'detalles_participantes': detalles_participantes_json, 
            'temas_tratados': request.form.get('temas_tratados'),
            'acuerdos_compromisos': request.form.get('acuerdos_compromisos'),
            'submitted_by_email': user_email
        }

        form_data = {k: v for k, v in form_data.items() if v is not None and v != ''}

        conn = get_db_connection()
        cur = conn.cursor()

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO registro_y_acta_de_visita ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(form_data.values()))
        conn.commit()
        cur.close()

        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting registro y acta de visita: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()



# --- PLANILLA VEHICULAR ---
@app.route('/planilla_vehicular')
@jwt_required()
def planilla_vehicular_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'planilla_vehicular.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_planilla_vehicular', methods=['POST'])
@jwt_required()
def submit_planilla_vehicular():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area_especifica': request.form.get('puesto_area_especifica'),
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma_responsable'),
            'placa_vehiculo': request.form.get('placa_vehiculo'),
            'kilometraje_vehiculo': request.form.get('kilometraje_vehiculo'),
            'estado_rines': request.form.get('estado_rines'),
            'juego_senales_carretera': request.form.get('juego_senales_carretera'),
            'gato_hidraulico': request.form.get('gato_hidraulico'),
            'palanca_gato': request.form.get('palanca_gato'),
            'estado_asientos': request.form.get('estado_asientos'),
            'estado_tapetes_alfombras': request.form.get('estado_tapetes_alfombras'),
            'limpieza_carroceria': request.form.get('limpieza_carroceria'),
            'luces_delanteras': request.form.get('luces_delanteras'),
            'luces_direccionales': request.form.get('luces_direccionales'),
            'luces_traseras': request.form.get('luces_traseras'),
            'parabrisas_delantero': request.form.get('parabrisas_delantero'),
            'parabrisas_trasero': request.form.get('parabrisas_trasero'),
            'defensa_delantera': request.form.get('defensa_delantera'),
            'defensa_trasera': request.form.get('defensa_trasera'),
            'puertas_vidrios': request.form.get('puertas_vidrios'),
            'tapa_radiador': request.form.get('tapa_radiador'),
            'tapa_aceite_motor': request.form.get('tapa_aceite_motor'),
            'bateria_tapa': request.form.get('bateria_tapa'),
            'espejo_retrovisor_interno': request.form.get('espejo_retrovisor_interno'),
            'espejos_retrovisores_externos': request.form.get('espejos_retrovisores_externos'),
            'limpia_brisas': request.form.get('limpia_brisas'),
            'antena_radio': request.form.get('antena_radio'),
            'radio_funciona': request.form.get('radio_funciona'),
            'llanta_repuesto': request.form.get('llanta_repuesto'),
            'aire_acondicionado': request.form.get('aire_acondicionado'),
            'diagrama_danos': request.form.get('diagrama_danos'),
            'novedades_criticas': request.form.get('novedades_criticas'),
            'accion_inmediata': request.form.get('accion_inmediata'),
            'firma_entrega': request.form.get('firma_entrega'),
            'firma_recibe': request.form.get('firma_recibe'),
            'oficial_operaciones_nombre': request.form.get('oficial_operaciones_nombre'),
            'oficial_operaciones_firma': request.form.get('oficial_operaciones_firma'),
            'submitted_by_email': user_email
        }

        conn = get_db_connection()
        cur = conn.cursor()

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO planilla_vehicular ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(form_data.values()))
        conn.commit()
        cur.close()

        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting planilla vehicular: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- PLANILLA MOTOCICLETAS ---
@app.route('/planilla_motocicletas')
@jwt_required()
def planilla_motocicletas_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'planilla_motocicletas.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_planilla_motocicletas', methods=['POST'])
@jwt_required()
def submit_planilla_motocicletas():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area_especifica': request.form.get('puesto_area_especifica'),
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma_responsable'),
            'placa_motocicleta': request.form.get('placa_motocicleta'),
            'kilometraje_motocicleta': request.form.get('kilometraje_motocicleta') or None,
            'diagrama_danos': request.form.get('diagrama_danos'),
            'novedades_criticas_detectadas': request.form.get('novedades_criticas_detectadas'),
            'accion_inmediata_tomada': request.form.get('accion_inmediata_tomada'),
            'firma_entrega': request.form.get('firma_entrega'),
            'firma_recibe': request.form.get('firma_recibe'),
            'oficial_operaciones_nombre': request.form.get('oficial_operaciones_nombre'),
            'oficial_operaciones_firma': request.form.get('oficial_operaciones_firma'),
            'submitted_by_email': user_email
        }

        # Add dynamic checklist items
        for key in request.form.keys():
            if key.startswith('estado_') and key not in form_data:
                form_data[key] = request.form.get(key)

        app_logger.info(f"Submitting motorcycle form for {user_email}")
        
        conn = get_db_connection()
        cur = conn.cursor()

        app_logger.info("Fetching schema columns for planilla_motocicletas...")
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'planilla_motocicletas'")
        table_columns = [row[0] for row in cur.fetchall()]

        valid_form_data = {k: v for k, v in form_data.items() if k in table_columns and v is not None and v != ''}

        columns = ', '.join(valid_form_data.keys())
        placeholders = ', '.join(['%s'] * len(valid_form_data))
        sql = f"INSERT INTO planilla_motocicletas ({columns}) VALUES ({placeholders})"
        
        app_logger.info(f"Inserting into planilla_motocicletas with keys: {list(valid_form_data.keys())}")
        cur.execute(sql, list(valid_form_data.values()))
        conn.commit()
        cur.close()
        app_logger.info("Motorcycle form submitted successfully.")

        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting planilla motocicletas: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- ORDEN DE MANTENIMIENTO ---
@app.route('/orden_mantenimiento')
@jwt_required()
def orden_mantenimiento_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'orden_mantenimiento.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_orden_mantenimiento', methods=['POST'])
@jwt_required()
def submit_orden_mantenimiento():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    import re

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1. Capture Global Fields
        global_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'nombre_tecnico': request.form.get('nombre_tecnico'),
            'firma_tecnico': request.form.get('firma_tecnico'),
            'submitted_by_email': user_email
        }

        # 2. Parse Dynamic Mantenimientos from request.form
        # Keys: mantenimientos[index][field_name]
        mantenimientos_map = {}
        pattern = re.compile(r'mantenimientos\[(\d+)\]\[(.*)\]')

        for key, value in request.form.items():
            match = pattern.match(key)
            if match:
                index = int(match.group(1))
                field = match.group(2)
                if index not in mantenimientos_map:
                    mantenimientos_map[index] = {}
                mantenimientos_map[index][field] = value

        # 3. Handle File Uploads (similar to supervision_puesto)
        for key, file_storage in request.files.items():
            match = pattern.match(key)
            if match:
                index = int(match.group(1))
                field = match.group(2)
                if field == 'foto_evidencia':
                    # Upload file
                    url = upload_file_to_gcs(file_storage, GCS_BUCKET_NAME)
                    if url:
                         if index not in mantenimientos_map:
                             mantenimientos_map[index] = {}
                         mantenimientos_map[index]['foto_evidencia_url'] = url

        # 4. Process and Insert Each Mantenimiento
        column_cache = None 

        for index, mant_data in mantenimientos_map.items():
            # Merge Global
            row_data = {**global_data, **mant_data}

            # Map fields to what we expect in DB.
            if 'puesto_area' in row_data and 'puesto_area_especifica' not in row_data:
                 row_data['puesto_area_especifica'] = row_data['puesto_area']
            
            app_logger.info(f"Processing maintenance item {index} for user {user_email}")

            # New HTML sends: puesto_area_especifica, equipo, id_equipo_serial, tipo_servicio, actividad_realizada, downtime_horas, repuestos_usados, foto_evidencia_url
            
            # Filter empty
            filtered_data = {k: v for k, v in row_data.items() if v is not None and v != ''}

            # Reflection to get valid columns
            if column_cache is None:
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'orden_mantenimiento'")
                column_cache = [row[0] for row in cur.fetchall()]
            
            valid_row_data = {k: v for k, v in filtered_data.items() if k in column_cache}
            
            if not valid_row_data:
                continue

            columns = ', '.join(valid_row_data.keys())
            placeholders = ', '.join(['%s'] * len(valid_row_data))
            sql = f"INSERT INTO orden_mantenimiento ({columns}) VALUES ({placeholders})"

            cur.execute(sql, list(valid_row_data.values()))

        conn.commit()
        cur.close()

        return redirect(url_for('success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting orden mantenimiento: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- CHECKLIST DE CUMPLIMIENTO NORMATIVO (UPDATED ROUTE) ---
@app.route('/checklist_cumplimiento')
@jwt_required()
def checklist_cumplimiento():
    """Renders the updated compliance checklist form."""
    user_name, is_admin = get_user_info_from_jwt()

    return render_template('checklist_cumplimiento.html',
                           name=user_name,
                           is_admin=is_admin,
                           **get_service_urls())

@app.route('/submit_checklist_cumplimiento', methods=['POST'])
@jwt_required()
def submit_checklist_cumplimiento():
    """Handles the submission of the updated compliance checklist form with multiple entries."""
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Header Data (Shared for all rows) - Section 1
        header_data = {
            'submitted_by_email': user_email,
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area_especifica': request.form.get('puesto_area_especifica'),
            'fecha_hora': request.form.get('fecha_hora') or None,
            'rol_aplicador': request.form.get('rol_aplicador'),
            'nombre_auditor': request.form.get('nombre_auditor'),
            'firma_auditor': request.form.get('firma_auditor'),
             # 'turno' is removed/ignored
        }

        # Row Data - Sections 2-5 (Lists)
        # We assume 'agente_nombre_completo[]' exists and controls the number of rows
        agente_nombres = request.form.getlist('agente_nombre_completo[]')
        num_rows = len(agente_nombres)
        app_logger.info(f"Submitting checklist fulfillment for {user_email}. Rows: {num_rows}")

        for i in range(num_rows):
            app_logger.info(f"Processing row {i+1}/{num_rows}")
            # Handle unique file upload per row
            evidencia_key = f'cargue_evidencia_{i}'
            evidencia_url = None
            if evidencia_key in request.files and request.files[evidencia_key].filename != '':
                file = request.files[evidencia_key]
                evidencia_url = upload_file_to_gcs(file, GCS_BUCKET_NAME)

            # Build row data combining header and indexed lists
            row_data = header_data.copy()
            row_data.update({
                # Section 2
                'agente_nombre_completo': agente_nombres[i],
                'agente_tipo_documento': request.form.getlist('agente_tipo_documento[]')[i] if len(request.form.getlist('agente_tipo_documento[]')) > i else None,
                'agente_numero_documento': request.form.getlist('agente_numero_documento[]')[i] if len(request.form.getlist('agente_numero_documento[]')) > i else None,
                'agente_cargo_rol': request.form.getlist('agente_cargo_rol[]')[i] if len(request.form.getlist('agente_cargo_rol[]')) > i else None,
                'agente_puesto': request.form.getlist('agente_puesto[]')[i] if len(request.form.getlist('agente_puesto[]')) > i else None,

                # Section 3
                'curso_certificacion': request.form.getlist('curso_certificacion[]')[i] if len(request.form.getlist('curso_certificacion[]')) > i else None,
                'academia_certifica': request.form.getlist('academia_certifica[]')[i] if len(request.form.getlist('academia_certifica[]')) > i else None,
                'nro_resolucion': request.form.getlist('nro_resolucion[]')[i] if len(request.form.getlist('nro_resolucion[]')) > i else None,
                'fecha_resolucion': (request.form.getlist('fecha_resolucion[]')[i] or None) if len(request.form.getlist('fecha_resolucion[]')) > i else None,
                'vigencia_desde': (request.form.getlist('vigencia_desde[]')[i] or None) if len(request.form.getlist('vigencia_desde[]')) > i else None,
                'vigencia_hasta': (request.form.getlist('vigencia_hasta[]')[i] or None) if len(request.form.getlist('vigencia_hasta[]')) > i else None,
                'evidencia_url': evidencia_url,
                'nivel_cumplimiento': request.form.getlist('nivel_cumplimiento[]')[i] if len(request.form.getlist('nivel_cumplimiento[]')) > i else None,

                # Section 4
                'copia_certificados_fisica': request.form.getlist('copia_certificados_fisica[]')[i] if len(request.form.getlist('copia_certificados_fisica[]')) > i else None,
                'certificados_cargados_sistema': request.form.getlist('certificados_cargados_sistema[]')[i] if len(request.form.getlist('certificados_cargados_sistema[]')) > i else None,
                'documentacion_coincide_hv': request.form.getlist('documentacion_coincide_hv[]')[i] if len(request.form.getlist('documentacion_coincide_hv[]')) > i else None,
                'fechas_vigentes': request.form.getlist('fechas_vigentes[]')[i] if len(request.form.getlist('fechas_vigentes[]')) > i else None,

                # Section 5
                'firma_guarda_supervisado': request.form.getlist('firma_guarda_supervisado[]')[i] if len(request.form.getlist('firma_guarda_supervisado[]')) > i else None,
            })

            # Filter None/Empty
            row_data = {k: v for k, v in row_data.items() if v is not None and v != ''}

            columns = row_data.keys()
            values = [row_data[col] for col in columns]

            insert_query = f"""
                INSERT INTO checklist_cumplimiento ({', '.join(columns)})
                VALUES ({', '.join(['%s'] * len(values))})
            """
            cur.execute(insert_query, values)

        conn.commit()
        cur.close()
        return redirect(url_for('success', message='Checklist(s) enviado(s) exitosamente!'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting updated checklist_cumplimiento: {e}", exc_info=True)
        # Redirect to a generic error page, passing the error message
        return redirect(url_for('error', error=str(e)))
    finally:
        if conn:
            conn.close()


# --- PWA ROUTES ---
@app.route('/offline.html')
def offline():
    return render_template('offline.html')

@app.route('/sw.js')
def service_worker():
    response = send_from_directory('.', 'sw.js')
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/install')
def install_instructions():
    return render_template('install_prompt.html')

@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "SMT SecApp Forms", # Slightly updated name
        "short_name": "SMT Forms",
        "description": "Aplicación para completar formularios de SMT SecApp",
        "start_url": "/select", # Start at selection
        "display": "standalone",
        "background_color": "#1a202c",
        "theme_color": "#2563eb",
        "orientation": "portrait",
        "scope": "/",
        "lang": "es",
        "icons": [
            {
                "src": "https://storage.googleapis.com/smt-misc/SMT-logo.png", # Use your actual logo URL
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable"
            },
            {
                "src": "https://storage.googleapis.com/smt-misc/SMT-logo.png", # Use your actual logo URL
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable"
            }
        ],
         "shortcuts": [
            {
                "name": "Seleccionar Formulario",
                "short_name": "Formularios",
                "description": "Ver la lista de formularios disponibles",
                "url": "/select",
                "icons": [{"src": "https://storage.googleapis.com/smt-misc/SMT-logo.png", "sizes": "96x96"}] # Use your actual logo URL
            }
        ],
        "categories": ["business", "productivity"],
        "prefer_related_applications": False
    })

# --- API (Example - Keep as is or adapt as needed) ---
@app.route('/api/my_reports', methods=['GET'])
@jwt_required()
def get_my_reports():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # This query needs to be updated to fetch from *all* relevant tables
        # or have separate endpoints for each form type.
        # For now, it only fetches from reportes_incidentes as an example.
        cur.execute("""
            SELECT id_reporte_incidente as id, 'Reporte de Incidente' as tipo, fecha_hora, cliente_instalacion, estado
            FROM reportes_incidentes
            WHERE submitted_by_email = %s
            ORDER BY fecha_hora DESC
            LIMIT 20
        """, (user_email,))

        reports = cur.fetchall()
        cur.close()

        reports_list = []
        for report in reports:
            report_dict = dict(report)
            # Convert datetime objects safely
            for key, value in report_dict.items():
                 if isinstance(value, datetime):
                     report_dict[key] = value.isoformat()
            reports_list.append(report_dict)

        return jsonify(reports_list)

    except Exception as e:
        app_logger.error(f"Error retrieving reports: {e}", exc_info=True)
        return jsonify({'error': 'Error retrieving reports'}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/my_reports/<int:report_id>', methods=['GET'])
@jwt_required()
def get_my_report_details(report_id):
    # This example only searches reportes_incidentes. Needs logic to determine table.
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("""
            SELECT * FROM reportes_incidentes
            WHERE id_reporte_incidente = %s AND submitted_by_email = %s
        """, (report_id, user_email))

        report = cur.fetchone()
        cur.close()

        if not report:
            return jsonify({'error': 'Report not found or access denied'}), 404

        report_dict = dict(report)
        for key, value in report_dict.items():
            if isinstance(value, datetime):
                report_dict[key] = value.isoformat()

        return jsonify(report_dict)

    except Exception as e:
        app_logger.error(f"Error retrieving report details: {e}", exc_info=True)
        return jsonify({'error': 'Error retrieving report details'}), 500
    finally:
        if conn:
            conn.close()


@app.errorhandler(503)
def service_unavailable(error):
    return render_template('offline.html'), 503

# --- UTILITY ROUTES ---
@app.route('/logout')
def logout():
    response = redirect(app.config.get('LOGIN_SERVICE_URL'))
    unset_jwt_cookies(response)
    return response

@app.route('/success')
@jwt_required()
def success():
    message = request.args.get('message', 'Formulario enviado exitosamente!') # Generic success message
    user_name, is_admin = get_user_info_from_jwt()

    return render_template('success.html',
                           message=message,
                           name=user_name, # Pass name to success template
                           is_admin=is_admin,
                           select_form_url=url_for('select_form'),
                           **get_service_urls()) # Pass service URLs

@app.route('/error')
def error():
    error_message = request.args.get('error', 'Ha ocurrido un error inesperado.')
    try: # Safely get user info even on error page if logged in
        user_info = get_jwt_identity()
        if isinstance(user_info, str):
            user_name = "Usuario"
            is_admin = False
        else:
            user_name = user_info.get('name', 'Usuario')
            is_admin = user_info.get('is_admin', False)
    except Exception:
        user_name = "Usuario"
        is_admin = False

    return render_template('error.html',
                           error=error_message,
                           name=user_name, # Pass name to error template
                           is_admin=is_admin,
                           select_form_url=url_for('select_form'),
                           **get_service_urls()) # Pass service URLs


if __name__ == '__main__':
    app_logger.info("Starting Flask app in local development mode.")
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))