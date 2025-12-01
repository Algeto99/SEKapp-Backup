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

# --- Logger Setup ---
logging.basicConfig(level=logging.INFO)
app_logger = logging.getLogger('app')

# --- Flask App Setup ---
app = Flask(__name__)
GCS_BUCKET_NAME = 'smt-uploads' # Make sure this bucket exists and permissions are set

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
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        unique_filename = f"{uuid.uuid4()}_{secure_filename(file.filename)}"
        blob = bucket.blob(unique_filename)
        blob.upload_from_file(file, content_type=file.content_type)
        app_logger.info(f"File {unique_filename} uploaded to {bucket_name}.")
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
    try:
        user_info = get_jwt_identity()
        user_name = user_info.get('name', 'Usuario')
        is_admin = user_info.get('is_admin', False)
    except Exception as e:
        app_logger.warning(f"Could not parse JWT claims: {e}")
        user_name = "Usuario"
        is_admin = False

    return render_template(
        'select_form.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

# --- Routes for existing forms ---
# ... (Keep all your existing routes for other forms: reporte_incidente, planilla_de_rondas, etc.) ...
# Example structure:
# @app.route('/reporte_incidente', methods=['GET'])
# @jwt_required()
# def reporte_incidente_form(): ...
#
# @app.route('/submit_incident_report', methods=['POST'])
# @jwt_required()
# def submit_incident_report(): ...
#
# ... (Include all other form routes here) ...

# --- REPORTE DE INCIDENTE ---
@app.route('/reporte_incidente', methods=['GET'])
@jwt_required()
def reporte_incidente_form():
    try:
        user_info = get_jwt_identity()
        user_name = user_info.get('name', 'Usuario')
        is_admin = user_info.get('is_admin', False)
    except Exception as e:
        app_logger.warning(f"Could not parse JWT claims: {e}")
        user_name = "Usuario"
        is_admin = False

    return render_template(
        'reporte_incidente.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_incident_report', methods=['POST'])
@jwt_required()
def submit_incident_report():
    user_email = get_jwt_identity()['email']
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
            'impacto': request.form.get('impacto'),
            'tiempo_resolucion_min': request.form.get('tiempo_resolucion_min'),
            'responsable_asignado': request.form.get('responsable_asignado'),
            'estado': request.form.get('estado'),
            'accion_inmediata': request.form.get('accion_inmediata'),
            'accion_correctiva_preventiva': request.form.get('accion_correctiva_preventiva'),
            'responsable_seguimiento': request.form.get('responsable_seguimiento'),
            'fecha_limite_cierre': request.form.get('fecha_limite_cierre'),
            'foto_evidencia_url': foto_url,
            'submitted_by_email': user_email
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
    try:
        user_info = get_jwt_identity()
        user_name = user_info.get('name', 'Usuario')
        is_admin = user_info.get('is_admin', False)
    except Exception as e:
        user_name = "Usuario"
        is_admin = False

    return render_template(
        'mantenimiento_seguridad_fisica.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_mantenimiento_seguridad_fisica', methods=['POST'])
@jwt_required()
def submit_mantenimiento_seguridad_fisica():
    user_email = get_jwt_identity()['email']
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
    try:
        user_info = get_jwt_identity()
        user_name = user_info.get('name', 'Usuario')
        is_admin = user_info.get('is_admin', False)
    except Exception as e:
        user_name = "Usuario"
        is_admin = False

    return render_template(
        'encuesta_cliente.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_medicion_experiencia_cliente', methods=['POST'])
@jwt_required()
def submit_medicion_experiencia_cliente():
    user_email = get_jwt_identity()['email']
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

        conn = get_db_connection()
        cur = conn.cursor()

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO medicion_experiencia_cliente ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(form_data.values()))
        conn.commit()
        cur.close()

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
    try:
        user_info = get_jwt_identity()
        user_name = user_info.get('name', 'Usuario')
        is_admin = user_info.get('is_admin', False)
    except Exception as e:
        user_name = "Usuario"
        is_admin = False

    return render_template(
        'supervision_puesto.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_supervision_puesto', methods=['POST'])
@jwt_required()
def submit_supervision_puesto():
    user_email = get_jwt_identity()['email']
    conn = None
    try:
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
            'supervisor': request.form.get('supervisor'),
            'firma_supervisor': request.form.get('firma_supervisor'),
            'horario_servicio': request.form.get('horario_servicio'),
            'tipo_servicio': request.form.get('tipo_servicio'),
            'nombre_guardia': request.form.get('nombre_guardia'),
            'documento_guardia': request.form.get('documento_guardia'),
            'fecha_inicio_servicio_guardia': request.form.get('fecha_inicio_servicio_guardia'),
            'serie_arma': request.form.get('serie_arma'),
            'cantidad_municion': request.form.get('cantidad_municion'),
            'constancia_induccion': request.form.get('constancia_induccion'),
            'conoce_ordenes_consignas': request.form.get('conoce_ordenes_consignas'),
            'horario_detalles_claros': request.form.get('horario_detalles_claros'),
            'asistencia_puntualidad': request.form.get('asistencia_puntualidad'),
            'presentacion_uniforme': request.form.get('presentacion_uniforme'),
            'estado_limpieza_puesto': request.form.get('estado_limpieza_puesto'),
            'equipamiento_completo': request.form.get('equipamiento_completo'),
            'cumplimiento_ordenes': request.form.get('cumplimiento_ordenes'),
            'estado_bitacora': request.form.get('estado_bitacora'),
            'observaciones_novedades': request.form.get('observaciones_novedades'),
            'nombre_guardia_firma': request.form.get('nombre_guardia_firma'),
            'firma_guardia': request.form.get('firma_guardia'),
            'submitted_by_email': user_email,
            'foto_evidencia_url': foto_url
        }

        form_data = {k: v for k, v in form_data.items() if v is not None and v != ''}

        conn = get_db_connection()
        cur = conn.cursor()

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO supervision_puesto ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(form_data.values()))
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
    try:
        user_info = get_jwt_identity()
        user_name = user_info.get('name', 'Usuario')
        is_admin = user_info.get('is_admin', False)
    except Exception as e:
        user_name = "Usuario"
        is_admin = False

    return render_template(
        'reporte_disciplinario.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_informe_novedades_disciplinario', methods=['POST'])
@jwt_required()
def submit_informe_novedades_disciplinario():
    user_email = get_jwt_identity()['email']
    conn = None
    try:
        anexos_urls = []
        if 'anexos_files' in request.files:
            files = request.files.getlist('anexos_files')
            for file in files:
                url = upload_file_to_gcs(file, GCS_BUCKET_NAME)
                if url:
                    anexos_urls.append(url)

        anexos_str = "\n".join(anexos_urls) if anexos_urls else "No Aplica" if request.form.get('anexos_na') else ""

        fecha_hora_str = request.form.get('fecha_hora')
        fecha = None
        hora = None
        if fecha_hora_str:
            try:
                dt_obj = datetime.fromisoformat(fecha_hora_str)
                fecha = dt_obj.date()
                hora = dt_obj.time()
            except ValueError:
                pass

        form_data = {
            'nombre_responsable': request.form.get('nombre_responsable'),
            'realizado_por_cargo': request.form.get('rol_aplicador'),
            'fecha': fecha,
            'hora': hora,
            'dirigido_a': None,
            'empleado_nombre': request.form.get('empleado_nombre'),
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

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'informe_novedades_disciplinario'")
        table_columns = [row[0] for row in cur.fetchall()]

        valid_form_data = {k: v for k, v in form_data.items() if k in table_columns}

        columns = ', '.join(valid_form_data.keys())
        placeholders = ', '.join(['%s'] * len(valid_form_data))
        sql = f"INSERT INTO informe_novedades_disciplinario ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(valid_form_data.values()))
        conn.commit()
        cur.close()

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
    try:
        user_info = get_jwt_identity()
        user_name = user_info.get('name', 'Usuario')
        is_admin = user_info.get('is_admin', False)
    except Exception as e:
        user_name = "Usuario"
        is_admin = False

    return render_template(
        'log_de_patrullas.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_log_de_patrullas', methods=['POST'])
@jwt_required()
def submit_log_de_patrullas():
    user_email = get_jwt_identity()['email']
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
    try:
        user_info = get_jwt_identity()
        user_name = user_info.get('name', 'Usuario')
        is_admin = user_info.get('is_admin', False)
    except Exception as e:
        user_name = "Usuario"
        is_admin = False

    return render_template(
        'registro_de_capacitaciones.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_registro_de_capacitaciones', methods=['POST'])
@jwt_required()
def submit_registro_de_capacitaciones():
    user_email = get_jwt_identity()['email']
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
    try:
        user_info = get_jwt_identity()
        user_name = user_info.get('name', 'Usuario')
        is_admin = user_info.get('is_admin', False)
    except Exception as e:
        user_name = "Usuario"
        is_admin = False

    return render_template(
        'registro_y_acta_de_visita.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_registro_y_acta_de_visita', methods=['POST'])
@jwt_required()
def submit_registro_y_acta_de_visita():
    user_email = get_jwt_identity()['email']
    conn = None
    try:
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area': request.form.get('puesto_area'),
            'fecha_hora': request.form.get('fecha_hora'),
            'motivo_visita': request.form.get('motivo_visita'),
            'nombre_visitante': request.form.get('nombre_visitante'),
            'cargo_visitante': request.form.get('cargo_visitante'),
            'firma_visitante': request.form.get('firma_visitante'),
            'nombre_participante_cliente': request.form.get('nombre_participante_cliente'),
            'cargo_participante_cliente': request.form.get('cargo_participante_cliente'),
            'firma_participante_cliente': request.form.get('firma_participante_cliente'),
            'temas_tratados': request.form.get('temas_tratados'),
            'acuerdos_compromisos': request.form.get('acuerdos_compromisos'),
            'observaciones': request.form.get('observaciones'),
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
    try:
        user_info = get_jwt_identity()
        user_name = user_info.get('name', 'Usuario')
        is_admin = user_info.get('is_admin', False)
    except Exception as e:
        user_name = "Usuario"
        is_admin = False

    return render_template(
        'planilla_vehicular.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_planilla_vehicular', methods=['POST'])
@jwt_required()
def submit_planilla_vehicular():
    user_email = get_jwt_identity()['email']
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
            'kilometraje_entrega': request.form.get('kilometraje_entrega'),
            'kilometraje_salida': request.form.get('kilometraje_salida'),
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
    try:
        user_info = get_jwt_identity()
        user_name = user_info.get('name', 'Usuario')
        is_admin = user_info.get('is_admin', False)
    except Exception as e:
        user_name = "Usuario"
        is_admin = False

    return render_template(
        'planilla_motocicletas.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_planilla_motocicletas', methods=['POST'])
@jwt_required()
def submit_planilla_motocicletas():
    user_email = get_jwt_identity()['email']
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
            'kilometraje_entrega': request.form.get('kilometraje_entrega') or None,
            'kilometraje_salida': request.form.get('kilometraje_salida') or None,
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

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'planilla_motocicletas'")
        table_columns = [row[0] for row in cur.fetchall()]

        valid_form_data = {k: v for k, v in form_data.items() if k in table_columns and v is not None and v != ''}

        columns = ', '.join(valid_form_data.keys())
        placeholders = ', '.join(['%s'] * len(valid_form_data))
        sql = f"INSERT INTO planilla_motocicletas ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(valid_form_data.values()))
        conn.commit()
        cur.close()

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
    try:
        user_info = get_jwt_identity()
        user_name = user_info.get('name', 'Usuario')
        is_admin = user_info.get('is_admin', False)
    except Exception as e:
        user_name = "Usuario"
        is_admin = False

    return render_template(
        'orden_mantenimiento.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@app.route('/submit_orden_mantenimiento', methods=['POST'])
@jwt_required()
def submit_orden_mantenimiento():
    user_email = get_jwt_identity()['email']
    conn = None
    try:
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area': request.form.get('puesto_area'),
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'equipo': request.form.get('equipo'),
            'modelo_serie': request.form.get('modelo_serie'),
            'ubicacion_instalacion': request.form.get('ubicacion_instalacion'),
            'tipo_servicio': request.form.get('tipo_servicio'),
            'tipo_mantenimiento': request.form.get('tipo_mantenimiento'),
            'estado_equipo_antes': request.form.get('estado_equipo_antes'),
            'descripcion_trabajo': request.form.get('descripcion_trabajo'),
            'repuestos_usados': request.form.get('repuestos_usados'),
            'observaciones_tecnicas': request.form.get('observaciones_tecnicas'),
            'estado_equipo_despues': request.form.get('estado_equipo_despues'),
            'clasificacion_urgencia': request.form.get('clasificacion_urgencia'),
            'criticidad_impacto': request.form.get('criticidad_impacto'),
            'supervisor_seguridad': request.form.get('supervisor_seguridad'),
            'firma_supervisor_seguridad': request.form.get('firma_supervisor_seguridad'),
            'submitted_by_email': user_email
        }

        form_data = {k: v for k, v in form_data.items() if v is not None and v != ''}

        conn = get_db_connection()
        cur = conn.cursor()

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO orden_mantenimiento ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(form_data.values()))
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
    try:
        user_info = get_jwt_identity()
        user_name = user_info.get('name', 'Usuario')
        is_admin = user_info.get('is_admin', False)
    except Exception as e:
        app_logger.warning(f"Could not parse JWT claims: {e}")
        user_name = "Usuario"
        is_admin = False

    return render_template('checklist_cumplimiento.html',
                           name=user_name,
                           is_admin=is_admin,
                           **get_service_urls())

@app.route('/submit_checklist_cumplimiento', methods=['POST'])
@jwt_required()
def submit_checklist_cumplimiento():
    """Handles the submission of the updated compliance checklist form."""
    user_email = get_jwt_identity()['email']
    conn = None
    try:
        # Handle file upload for evidencia
        evidencia_url = None
        if 'cargue_evidencia' in request.files:
            file = request.files['cargue_evidencia']
            evidencia_url = upload_file_to_gcs(file, GCS_BUCKET_NAME) # Use the helper

        # Map form fields to database columns
        form_data = {
            'submitted_by_email': user_email,
            # Sección 1: Datos Generales
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area_especifica': request.form.get('puesto_area_especifica'),
            'fecha_hora': request.form.get('fecha_hora') or None, # Changed from fecha_auditoria
            'turno': request.form.get('turno'),
            'rol_aplicador': request.form.get('rol_aplicador'), # Added
            'normativa_aplicable': request.form.get('normativa_aplicable'),
            'nombre_auditor': request.form.get('nombre_auditor'), # Renamed
            'firma_auditor': request.form.get('firma_auditor'), # Renamed

            # Sección 2: Datos del Agente de Seguridad
            'agente_nombre_completo': request.form.get('agente_nombre_completo'),
            'agente_tipo_documento': request.form.get('agente_tipo_documento'),
            'agente_numero_documento': request.form.get('agente_numero_documento'),
            'agente_cargo_rol': request.form.get('agente_cargo_rol'),
            'agente_puesto': request.form.get('agente_puesto'),

            # Sección 3: Verificacion de Cursos y Certificaciones Obligatorias
            'curso_certificacion': request.form.get('curso_certificacion'),
            'academia_certifica': request.form.get('academia_certifica'),
            'nro_resolucion': request.form.get('nro_resolucion'),
            'fecha_resolucion': request.form.get('fecha_resolucion') or None, # Changed type
            'vigencia_desde': request.form.get('vigencia_desde') or None,
            'vigencia_hasta': request.form.get('vigencia_hasta') or None,
            'evidencia_url': evidencia_url, # File upload URL
            'nivel_cumplimiento': request.form.get('nivel_cumplimiento'),

            # Sección 4: Checklist de Verificacion
            'copia_certificados_fisica': request.form.get('copia_certificados_fisica'),
            'certificados_cargados_sistema': request.form.get('certificados_cargados_sistema'),
            'documentacion_coincide_hv': request.form.get('documentacion_coincide_hv'),
            'fechas_vigentes': request.form.get('fechas_vigentes'),

            # Sección 5: Firmas
            'firma_guarda_supervisado': request.form.get('firma_guarda_supervisado'),
            'firma_supervisor': request.form.get('firma_supervisor'),
        }

        # Remove empty/None values before inserting
        form_data = {k: v for k, v in form_data.items() if v is not None and v != ''}

        conn = get_db_connection()
        cur = conn.cursor()

        # Dynamically build INSERT statement based on available keys
        columns = form_data.keys()
        values = [form_data[col] for col in columns]

        insert_query = f"""
            INSERT INTO checklist_cumplimiento ({', '.join(columns)})
            VALUES ({', '.join(['%s'] * len(values))})
        """

        cur.execute(insert_query, values)
        conn.commit()

        cur.close()
        # Redirect to a generic success page
        return redirect(url_for('success', message='Checklist enviado exitosamente!'))

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
    user_email = get_jwt_identity()['email']
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
    user_email = get_jwt_identity()['email']
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
    try: # Safely get user info
        user_info = get_jwt_identity()
        user_name = user_info.get('name', 'Usuario')
        is_admin = user_info.get('is_admin', False)
    except Exception:
        user_name = "Usuario"
        is_admin = False

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

