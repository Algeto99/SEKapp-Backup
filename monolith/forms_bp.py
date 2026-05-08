import os
import logging
from flask import Blueprint, current_app, Flask, render_template, request, redirect, flash, jsonify, url_for, send_from_directory
from flask_jwt_extended import JWTManager, get_jwt_identity, jwt_required, unset_jwt_cookies, get_jwt
from flask_wtf.csrf import generate_csrf
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
forms_bp = Blueprint("forms_bp", __name__)
GCS_BUCKET_NAME = 'smt-uploads' # Make sure this bucket exists and permissions are set

# Initialize GCS client lazily or globally if environment is ready
try:
    gcs_client = storage.Client()
    logging.info("Global GCS Client initialized successfully.")
except Exception as e:
    logging.warning(f"Failed to initialize global GCS Client: {e}")
    gcs_client = None

# App config and JWT handlers are managed centrally in the monolith's main app_bp

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


_SCHEMA_CACHE = {}


def _get_table_columns(cur, table_name):
    cache_key = table_name
    if cache_key not in _SCHEMA_CACHE:
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s
        """, (table_name,))
        _SCHEMA_CACHE[cache_key] = {row[0] for row in cur.fetchall()}
    return _SCHEMA_CACHE[cache_key]


def _table_has_column(cur, table_name, column_name):
    return column_name in _get_table_columns(cur, table_name)


def _table_exists(cur, table_name):
    cur.execute("""
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
    """, (table_name,))
    return cur.fetchone() is not None


def _filter_existing_columns(cur, table_name, data):
    table_columns = _get_table_columns(cur, table_name)
    return {
        key: value for key, value in data.items()
        if key in table_columns and value is not None and value != ''
    }


def _parse_float(val):
    try:
        return float(val) if val not in (None, '') else None
    except (ValueError, TypeError):
        return None


def _get_user_company_id(cur, user_email):
    if not user_email or not _table_has_column(cur, 'users', 'company_id'):
        return None
    cur.execute('SELECT company_id FROM users WHERE email = %s', (user_email,))
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def _ensure_default_customer_company(cur, company_id):
    if company_id is None or not _table_exists(cur, 'customer_companies'):
        return None

    cur.execute("""
        SELECT id, name
        FROM customer_companies
        WHERE company_id = %s
        ORDER BY id
        LIMIT 1
    """, (company_id,))
    row = cur.fetchone()
    if row:
        return {'id': row[0], 'name': row[1]}

    cur.execute("SELECT name FROM companies WHERE id = %s", (company_id,))
    company_row = cur.fetchone()
    company_name = company_row[0] if company_row and company_row[0] else f'Company {company_id}'
    default_name = f"{company_name} - Cliente Principal"

    cur.execute("""
        INSERT INTO customer_companies (company_id, name, code, is_active)
        VALUES (%s, %s, %s, TRUE)
        RETURNING id, name
    """, (company_id, default_name, 'DEFAULT'))
    row = cur.fetchone()
    return {'id': row[0], 'name': row[1]} if row else None


def _resolve_scope_fields(cur, user_email, legacy_customer_value=None, property_id=None, customer_company_id=None):
    scope = {}
    company_id = _get_user_company_id(cur, user_email)
    if company_id is not None:
        scope['company_id'] = company_id

    if not (_table_exists(cur, 'customer_companies') and _table_has_column(cur, 'propiedades', 'customer_company_id')):
        if company_id is not None and _table_exists(cur, 'customer_companies'):
            default_customer = _ensure_default_customer_company(cur, company_id)
            if default_customer:
                scope['customer_company_id'] = default_customer['id']
                if not legacy_customer_value:
                    scope['cliente_instalacion'] = default_customer['name']
        return scope

    property_name = None
    customer_name = None

    if property_id and str(property_id).isdigit():
        cur.execute("""
            SELECT p.id_propiedad, p.nombre, p.customer_company_id, cc.name
            FROM propiedades p
            LEFT JOIN customer_companies cc ON cc.id = p.customer_company_id
            WHERE p.id_propiedad = %s
              AND (%s IS NULL OR cc.company_id = %s)
        """, (int(property_id), company_id, company_id))
        row = cur.fetchone()
        if row:
            scope['id_propiedad'] = row[0]
            if row[2] is not None:
                scope['customer_company_id'] = row[2]
            property_name = row[1]
            customer_name = row[3]

    if 'customer_company_id' not in scope and customer_company_id and str(customer_company_id).isdigit():
        cur.execute("""
            SELECT id, name
            FROM customer_companies
            WHERE id = %s
              AND (%s IS NULL OR company_id = %s)
        """, (int(customer_company_id), company_id, company_id))
        row = cur.fetchone()
        if row:
            scope['customer_company_id'] = row[0]
            customer_name = row[1]

    if 'id_propiedad' not in scope and legacy_customer_value:
        cur.execute("""
            SELECT p.id_propiedad, p.nombre, p.customer_company_id, cc.name
            FROM propiedades p
            LEFT JOIN customer_companies cc ON cc.id = p.customer_company_id
            WHERE LOWER(TRIM(p.nombre)) = LOWER(TRIM(%s))
              AND (%s IS NULL OR cc.company_id = %s)
            LIMIT 1
        """, (legacy_customer_value, company_id, company_id))
        row = cur.fetchone()
        if row:
            scope['id_propiedad'] = row[0]
            if row[2] is not None:
                scope['customer_company_id'] = row[2]
            property_name = row[1]
            customer_name = row[3]

    if 'customer_company_id' not in scope and legacy_customer_value:
        cur.execute("""
            SELECT id, name
            FROM customer_companies
            WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
              AND (%s IS NULL OR company_id = %s)
            LIMIT 1
        """, (legacy_customer_value, company_id, company_id))
        row = cur.fetchone()
        if row:
            scope['customer_company_id'] = row[0]
            customer_name = row[1]

    if 'customer_company_id' not in scope and company_id is not None:
        default_customer = _ensure_default_customer_company(cur, company_id)
        if default_customer:
            scope['customer_company_id'] = default_customer['id']
            customer_name = customer_name or default_customer['name']

    if property_name:
        scope['cliente_instalacion'] = property_name
    elif customer_name:
        scope['cliente_instalacion'] = customer_name

    return scope

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
    project_id = current_app.config.get('GCP_PROJECT_ID')
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
            return get_secret_value(current_app.config.get('EMAIL_PASSWORD_SECRET_NAME'))
    except Exception as e:
        app_logger.warning(f"Could not retrieve email password: {e}")
        return None

def send_email(to_emails, subject, body, is_html=False, cc_emails=None):
    # ... (send_email function remains the same) ...
    pass # Keep existing implementation

def get_service_urls():
    """Helper to get all service URLs for templates."""
    return {
        'login_service_url': '/',
        'landing_service_url': '/landing/',
        'dashboard_service_url': '/dashboard',
        'viewer_service_url': '/viewer'
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
@forms_bp.route('/health')
def health():
    return "OK", 200

# --- Root and Form Selection ---
@forms_bp.route('/')
@jwt_required()
def root_redirect():
    return redirect('/select')

@forms_bp.route('/select_form')
@forms_bp.route('/select')
@jwt_required()
def select_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'select_form.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )


@forms_bp.route('/api/customer-hierarchy')
@jwt_required()
def customer_hierarchy():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        company_id = _get_user_company_id(cur, user_email)
        if company_id is None or not _table_exists(cur, 'customer_companies'):
            return jsonify({'company_id': company_id, 'customers': []})

        has_property_customer_link = _table_has_column(cur, 'propiedades', 'customer_company_id')
        customers = []

        cur.execute("""
            SELECT id, name
            FROM customer_companies
            WHERE company_id = %s
              AND COALESCE(is_active, TRUE) = TRUE
            ORDER BY name
        """, (company_id,))
        customer_rows = cur.fetchall()

        for customer in customer_rows:
            properties = []
            if has_property_customer_link:
                cur.execute("""
                    SELECT id_propiedad, nombre
                    FROM propiedades
                    WHERE customer_company_id = %s
                      AND COALESCE(activa, TRUE) = TRUE
                    ORDER BY nombre
                """, (customer['id'],))
                properties = [
                    {'id': row['id_propiedad'], 'name': row['nombre']}
                    for row in cur.fetchall()
                ]

            customers.append({
                'id': customer['id'],
                'name': customer['name'],
                'properties': properties,
            })

        return jsonify({'company_id': company_id, 'customers': customers})
    except Exception as e:
        app_logger.error(f"customer_hierarchy error: {e}", exc_info=True)
        return jsonify({'company_id': None, 'customers': []}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# --- REPORTE DE INCIDENTE ---
@forms_bp.route('/reporte_incidente', methods=['GET'])
@jwt_required()
def reporte_incidente_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'reporte_incidente.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@forms_bp.route('/submit_incident_report', methods=['POST'])
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
            'user_email': user_email,
            'latitude': _parse_float(request.form.get('latitude')),
            'longitude': _parse_float(request.form.get('longitude')),
            'location_accuracy': _parse_float(request.form.get('location_accuracy')),
        }
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))
        valid_form_data = _filter_existing_columns(cur, 'reportes_incidentes', form_data)

        columns = ', '.join(valid_form_data.keys())
        placeholders = ', '.join(['%s'] * len(valid_form_data))
        sql = f"INSERT INTO reportes_incidentes ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(valid_form_data.values()))
        conn.commit()
        cur.close()

        return redirect(url_for('forms_bp.success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting incident report: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- MEDICION EXPERIENCIA CLIENTE ---
@forms_bp.route('/medicion_experiencia_cliente')
@jwt_required()
def medicion_experiencia_cliente_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'encuesta_cliente.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@forms_bp.route('/submit_medicion_experiencia_cliente', methods=['POST'])
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
            'submitted_by_email': user_email,
            'latitude': _parse_float(request.form.get('latitude')),
            'longitude': _parse_float(request.form.get('longitude')),
            'location_accuracy': _parse_float(request.form.get('location_accuracy')),
        }
        conn = get_db_connection()
        cur = conn.cursor()
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))

        app_logger.info(f"Submitting customer experience survey for user: {user_email}")
        valid_form_data = _filter_existing_columns(cur, 'medicion_experiencia_cliente', form_data)
        
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
        return redirect(url_for('forms_bp.success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting encuesta: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- SUPERVISION PUESTO ---
@forms_bp.route('/supervision_puesto')
@jwt_required()
def supervision_puesto_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'supervision_puesto.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@forms_bp.route('/submit_supervision_puesto', methods=['POST'])
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
            'submitted_by_email': user_email,
            'latitude': _parse_float(request.form.get('latitude')),
            'longitude': _parse_float(request.form.get('longitude')),
            'location_accuracy': _parse_float(request.form.get('location_accuracy')),
        }
        global_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=global_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))
        if global_data.get('cliente_instalacion'):
            global_data['cliente'] = global_data['cliente_instalacion']

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
                column_cache = _get_table_columns(cur, 'supervision_puesto')
            
            valid_row_data = {k: v for k, v in filtered_data.items() if k in column_cache}
            
            if not valid_row_data:
                continue # Skip empty rows

            columns = ', '.join(valid_row_data.keys())
            placeholders = ', '.join(['%s'] * len(valid_row_data))
            sql = f"INSERT INTO supervision_puesto ({columns}) VALUES ({placeholders})"

            cur.execute(sql, list(valid_row_data.values()))

        conn.commit()
        cur.close()

        return redirect(url_for('forms_bp.success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting supervision puesto: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- INFORME NOVEDADES DISCIPLINARIO ---
@forms_bp.route('/informe_novedades_disciplinario')
@jwt_required()
def informe_novedades_disciplinario_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'reporte_disciplinario.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@forms_bp.route('/submit_informe_novedades_disciplinario', methods=['POST'])
@jwt_required()
def submit_informe_novedades_disciplinario():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
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
            'turno': request.form.get('turno'),
            'empleado_niega_firmar': True if request.form.get('empleado_niega_firmar') else False,
            'nombre_testigo': request.form.get('nombre_testigo'),
            'firma_testigo': request.form.get('firma_testigo'),
            'latitude': _parse_float(request.form.get('latitude')),
            'longitude': _parse_float(request.form.get('longitude')),
            'location_accuracy': _parse_float(request.form.get('location_accuracy')),
        }
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))

        app_logger.info(f"Submitting disciplinary report for {user_email}, Employee: {form_data.get('empleado_nombre')}")

        valid_form_data = _filter_existing_columns(cur, 'informe_novedades_disciplinario', form_data)

        columns = ', '.join(valid_form_data.keys())
        placeholders = ', '.join(['%s'] * len(valid_form_data))
        sql = f"INSERT INTO informe_novedades_disciplinario ({columns}) VALUES ({placeholders})"
        
        # Log the SQL (be careful with sensitive data, or just log valid_form_data keys)
        app_logger.debug(f"Inserting into informe_novedades_disciplinario with keys: {list(valid_form_data.keys())}")

        cur.execute(sql, list(valid_form_data.values()))
        conn.commit()
        cur.close()

        app_logger.info("Disciplinary report submitted successfully.")
        return redirect(url_for('forms_bp.success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting informe: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- LOG DE PATRULLAS ---
@forms_bp.route('/log_de_patrullas')
@jwt_required()
def log_de_patrullas_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'log_de_patrullas.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@forms_bp.route('/submit_log_de_patrullas', methods=['POST'])
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
        company_scope = _resolve_scope_fields(cur, user_email)
        form_data.update(company_scope)
        form_data = _filter_existing_columns(cur, 'log_de_patrullas', form_data)

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO log_de_patrullas ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(form_data.values()))
        conn.commit()
        cur.close()

        return redirect(url_for('forms_bp.success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting log: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- REGISTRO DE CAPACITACIONES ---
# --- ASISTENCIA QR (PUBLIC – no JWT) ---
@forms_bp.route('/asistencia_qr/<session_token>')
def asistencia_qr_form(session_token):
    """Public guest attendance form, no login required."""
    topic = request.args.get('topic', '')
    return render_template('asistencia_qr.html', session_token=session_token, topic=topic)

@forms_bp.route('/submit_asistencia_qr/<session_token>', methods=['POST'])
def submit_asistencia_qr(session_token):
    """Save a guest attendance entry; no JWT needed."""
    conn = None
    try:
        nombre = request.form.get('nombre', '').strip()
        if not nombre:
            return 'Nombre requerido', 400

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO capacitacion_asistencia (session_token, nombre, cargo, documento, firma) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                session_token,
                nombre,
                request.form.get('cargo', ''),
                request.form.get('documento', ''),
                request.form.get('firma', '')
            )
        )
        conn.commit()
        cur.close()
        return '', 200
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error saving QR attendance: {e}", exc_info=True)
        return 'Error interno', 500
    finally:
        if conn:
            conn.close()

# --- REGISTRO DE CAPACITACIONES ---
@forms_bp.route('/registro_de_capacitaciones')
@jwt_required()
def registro_de_capacitaciones_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'registro_de_capacitaciones.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@forms_bp.route('/submit_registro_de_capacitaciones', methods=['POST'])
@jwt_required()
def submit_registro_de_capacitaciones():
    import json as _json
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Merge manually-entered attendees with any QR guest entries
        lista_manual_raw = request.form.get('lista_asistencia', '[]')
        try:
            lista_manual = _json.loads(lista_manual_raw) if lista_manual_raw else []
        except Exception:
            lista_manual = []

        session_token = request.form.get('session_token', '')
        if session_token:
            try:
                cur.execute(
                    "SELECT nombre, cargo, documento, firma FROM capacitacion_asistencia WHERE session_token = %s",
                    (session_token,)
                )
                guest_rows = cur.fetchall()
                for row in guest_rows:
                    lista_manual.append({
                        'nombre': row[0], 'cargo': row[1],
                        'documento': row[2], 'firma': row[3],
                        'via': 'QR'
                    })
            except Exception as qr_err:
                app_logger.warning(f"Could not fetch QR attendees: {qr_err}")

        lista_asistencia_json = _json.dumps(lista_manual)

        fecha_hora = request.form.get('fecha_hora') or None
        if not fecha_hora:
            fecha = (request.form.get('fecha') or '').strip()
            hora_inicio = (request.form.get('hora_inicio') or '').strip()
            if fecha:
                fecha_hora = f"{fecha} {hora_inicio or '00:00'}"

        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area_especifica': request.form.get('puesto_area_especifica'),
            'fecha_hora': fecha_hora,
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma_responsable'),
            'nombre_capacitacion': request.form.get('nombre_capacitacion') or request.form.get('tema_capacitacion'),
            'objetivo_capacitacion': request.form.get('objetivo_capacitacion'),
            'observaciones_retroalimentacion': request.form.get('observaciones_retroalimentacion'),
            'lista_asistencia': lista_asistencia_json,
            'practica_simulacro_realizado': request.form.get('practica_simulacro_realizado'),
            'nivel_comprension': request.form.get('nivel_comprension'),
            'recomendaciones': request.form.get('recomendaciones'),
            'submitted_by_email': user_email
        }
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))
        form_data = _filter_existing_columns(cur, 'registro_de_capacitaciones', form_data)
        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO registro_de_capacitaciones ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(form_data.values()))
        conn.commit()
        cur.close()

        return redirect(url_for('forms_bp.success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting capacitacion: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- REGISTRO Y ACTA DE VISITA ---
@forms_bp.route('/registro_y_acta_de_visita')
@jwt_required()
def registro_y_acta_de_visita_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'acta_visita_cliente.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@forms_bp.route('/submit_registro_y_acta_de_visita', methods=['POST'])
@jwt_required()
def submit_registro_y_acta_de_visita():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
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

        # Collect all repeatable block data (indexed temas_tratados_N, acuerdos_compromisos_N, etc.)
        bloques = {}
        for key in request.form:
            for prefix in ('temas_tratados_', 'acuerdos_compromisos_', 'nombre_responsable_', 'fecha_cumplimiento_'):
                if key.startswith(prefix):
                    idx = key[len(prefix):]
                    if idx not in bloques:
                        bloques[idx] = {}
                    bloques[idx][prefix.rstrip('_')] = request.form.get(key)

        # Merge blocks into combined strings for storage in existing columns
        temas_list = [bloques[i].get('temas_tratados', '') for i in sorted(bloques.keys(), key=lambda x: int(x)) if bloques[i].get('temas_tratados')]
        acuerdos_list = [bloques[i].get('acuerdos_compromisos', '') for i in sorted(bloques.keys(), key=lambda x: int(x)) if bloques[i].get('acuerdos_compromisos')]
        responsables_list = [
            {'nombre': bloques[i].get('nombre_responsable', ''), 'fecha': bloques[i].get('fecha_cumplimiento', '')}
            for i in sorted(bloques.keys(), key=lambda x: int(x))
            if bloques[i].get('nombre_responsable') or bloques[i].get('fecha_cumplimiento')
        ]

        import json
        temas_combined = '\n---\n'.join(temas_list) if temas_list else None
        acuerdos_combined = '\n---\n'.join(acuerdos_list) if acuerdos_list else None
        responsables_json = json.dumps(responsables_list) if responsables_list else None

        form_data = {
            'cliente_instalacion': request.form.get('cliente_visitado'),
            'fecha_hora': request.form.get('fecha_hora'),
            'motivo_visita': request.form.get('motivo_visita'),
            'nombre_visitante': request.form.get('nombre_visitante'),
            'cargo_visitante': request.form.get('cargo_visitante'),
            'firma_visitante': request.form.get('firma_visitante'),
            'detalles_participantes': detalles_participantes_json,
            'temas_tratados': temas_combined,
            'acuerdos_compromisos': acuerdos_combined,
            'compromisos_responsable': responsables_json,
            'submitted_by_email': user_email,
            'latitude': _parse_float(request.form.get('latitude')),
            'longitude': _parse_float(request.form.get('longitude')),
            'location_accuracy': _parse_float(request.form.get('location_accuracy')),
        }
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))
        form_data = _filter_existing_columns(cur, 'registro_y_acta_de_visita', form_data)

        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO registro_y_acta_de_visita ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(form_data.values()))
        conn.commit()
        cur.close()

        return redirect(url_for('forms_bp.success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting registro y acta de visita: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()



# --- PLANILLA VEHICULAR ---
@forms_bp.route('/planilla_vehicular')
@jwt_required()
def planilla_vehicular_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'planilla_vehicular.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@forms_bp.route('/submit_planilla_vehicular', methods=['POST'])
@jwt_required()
def submit_planilla_vehicular():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
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
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))
        form_data = _filter_existing_columns(cur, 'planilla_vehicular', form_data)
        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO planilla_vehicular ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(form_data.values()))
        conn.commit()
        cur.close()

        return redirect(url_for('forms_bp.success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting planilla vehicular: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- PLANILLA MOTOCICLETAS ---
@forms_bp.route('/planilla_motocicletas')
@jwt_required()
def planilla_motocicletas_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'planilla_motocicletas.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@forms_bp.route('/submit_planilla_motocicletas', methods=['POST'])
@jwt_required()
def submit_planilla_motocicletas():
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
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
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))

        # Add dynamic checklist items
        for key in request.form.keys():
            if key.startswith('estado_') and key not in form_data:
                form_data[key] = request.form.get(key)

        app_logger.info(f"Submitting motorcycle form for {user_email}")

        app_logger.info("Fetching schema columns for planilla_motocicletas...")
        valid_form_data = _filter_existing_columns(cur, 'planilla_motocicletas', form_data)

        columns = ', '.join(valid_form_data.keys())
        placeholders = ', '.join(['%s'] * len(valid_form_data))
        sql = f"INSERT INTO planilla_motocicletas ({columns}) VALUES ({placeholders})"
        
        app_logger.info(f"Inserting into planilla_motocicletas with keys: {list(valid_form_data.keys())}")
        cur.execute(sql, list(valid_form_data.values()))
        conn.commit()
        cur.close()
        app_logger.info("Motorcycle form submitted successfully.")

        return redirect(url_for('forms_bp.success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting planilla motocicletas: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- CHECKLIST DE CUMPLIMIENTO NORMATIVO (UPDATED ROUTE) ---
@forms_bp.route('/checklist_cumplimiento')
@jwt_required()
def checklist_cumplimiento():
    """Renders the updated compliance checklist form."""
    user_name, is_admin = get_user_info_from_jwt()

    return render_template('checklist_cumplimiento.html',
                           name=user_name,
                           is_admin=is_admin,
                           **get_service_urls())

@forms_bp.route('/submit_checklist_cumplimiento', methods=['POST'])
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
        header_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=header_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))

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
            row_data = _filter_existing_columns(cur, 'checklist_cumplimiento', row_data)

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


# --- CONFIABILIDAD DE EQUIPOS ---
@forms_bp.route('/confiabilidad_equipos')
@jwt_required()
def confiabilidad_equipos_form():
    user_name, is_admin = get_user_info_from_jwt()
    return render_template(
        'confiabilidad_equipos.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@forms_bp.route('/submit_confiabilidad_equipos', methods=['POST'])
@jwt_required()
def submit_confiabilidad_equipos():
    import json as _json
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        # Parse dynamic inventario rows from form data
        # Keys follow the pattern: inventario[N][field]
        inventario_map = {}
        pattern = re.compile(r'inventario\[(\d+)\]\[(.+)\]')
        for key, value in request.form.items():
            match = pattern.match(key)
            if match:
                idx   = int(match.group(1))
                field = match.group(2)
                if idx not in inventario_map:
                    inventario_map[idx] = {}
                inventario_map[idx][field] = value

        # Convert to an ordered list (drop empty rows)
        inventario_list = []
        for idx in sorted(inventario_map.keys()):
            row = {k: v for k, v in inventario_map[idx].items() if v}
            if row:
                inventario_list.append(row)

        inventario_json = _json.dumps(inventario_list, ensure_ascii=False)

        form_data = {
            'cliente_instalacion':  request.form.get('cliente_instalacion'),
            'fecha':                request.form.get('fecha')  or None,
            'hora':                 request.form.get('hora')   or None,
            'sitio':                request.form.get('sitio'),
            'inventario':           inventario_json,
            'tecnico_mantenimiento':request.form.get('tecnico_mantenimiento'),
            'firma_tecnico':        request.form.get('firma_tecnico'),
            'supervisor_seguridad': request.form.get('supervisor_seguridad'),
            'firma_supervisor':     request.form.get('firma_supervisor'),
            'submitted_by_email':   user_email,
            'latitude':             _parse_float(request.form.get('latitude')),
            'longitude':            _parse_float(request.form.get('longitude')),
            'location_accuracy':    _parse_float(request.form.get('location_accuracy')),
        }
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))

        valid_data = _filter_existing_columns(cur, 'confiabilidad_equipos', form_data)

        columns      = ', '.join(valid_data.keys())
        placeholders = ', '.join(['%s'] * len(valid_data))
        sql = f"INSERT INTO confiabilidad_equipos ({columns}) VALUES ({placeholders})"
        cur.execute(sql, list(valid_data.values()))
        conn.commit()
        cur.close()

        app_logger.info(f"Confiabilidad de Equipos submitted by {user_email}")
        return redirect(url_for('forms_bp.success'))

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting confiabilidad_equipos: {e}", exc_info=True)
        return render_template('error.html', error=str(e)), 500
    finally:
        if conn:
            conn.close()


# --- PWA ROUTES ---
@forms_bp.route('/offline.html')
def offline():
    return render_template('offline.html')

@forms_bp.route('/sw.js')
def service_worker():
    response = send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'sw.js')
    response.headers['Content-Type'] = 'application/javascript'
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@forms_bp.route('/install')
def install_instructions():
    return render_template('install_prompt.html')

@forms_bp.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "Kanan SecApp Forms", # Slightly updated name
        "short_name": "Kanan Forms",
        "description": "Aplicación para completar formularios de Kanan SecApp",
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

@forms_bp.route('/api/csrf_token')
@jwt_required()
def get_csrf_token():
    """Returns a fresh CSRF token for the current session.
    Used by the offline-sync client to replay queued form submissions."""
    return jsonify({'csrf_token': generate_csrf()})

# --- API (Example - Keep as is or adapt as needed) ---
@forms_bp.route('/api/my_reports', methods=['GET'])
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

@forms_bp.route('/api/my_reports/<int:report_id>', methods=['GET'])
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


@forms_bp.errorhandler(503)
def service_unavailable(error):
    return render_template('offline.html'), 503

# --- UTILITY ROUTES ---
@forms_bp.route('/logout')
def logout():
    response = redirect(current_app.config.get('LOGIN_SERVICE_URL'))
    unset_jwt_cookies(response)
    return response

@forms_bp.route('/success')
@jwt_required()
def success():
    message = request.args.get('message', 'Formulario enviado exitosamente!') # Generic success message
    user_name, is_admin = get_user_info_from_jwt()

    return render_template('success.html',
                           message=message,
                           name=user_name, # Pass name to success template
                           is_admin=is_admin,
                           select_form_url=url_for('.select_form'),
                           **get_service_urls()) # Pass service URLs

@forms_bp.route('/error')
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
                           select_form_url=url_for('.select_form'),
                           **get_service_urls()) # Pass service URLs


# Forms routes initialized
