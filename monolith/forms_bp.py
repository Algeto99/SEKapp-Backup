import logging
import os
import re
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import zoneinfo

import psycopg2
import psycopg2.extras
from flask import Blueprint, current_app, render_template, request, redirect, flash, jsonify, url_for, send_from_directory
from flask_jwt_extended import get_jwt_identity, jwt_required, unset_jwt_cookies, get_jwt
from flask_wtf.csrf import generate_csrf
from google.api_core.exceptions import NotFound
from google.cloud import storage, secretmanager
from werkzeug.utils import secure_filename

from db import get_db_connection
from email_utils import send_email
from gcs_utils import resolve_upload_bucket

app_logger = logging.getLogger(__name__)

forms_bp = Blueprint("forms_bp", __name__)

try:
    gcs_client = storage.Client()
    app_logger.info("Global GCS Client initialized successfully.")
except Exception as e:
    app_logger.warning(f"Failed to initialize global GCS Client: {e}")
    gcs_client = None

# El bucket depende del proyecto GCP del despliegue (cada cliente tiene el suyo),
# así que se resuelve en runtime en vez de estar fijo. Ver gcs_utils.
GCS_BUCKET_NAME = resolve_upload_bucket(gcs_client)


# Process-global schema cache. Populated lazily on first request per table.
# IMPORTANT: Adding a new column to a form table requires an app restart (or
# a Cloud Run deploy) to pick it up — the cache is never invalidated at runtime.
# This is intentional: schema changes always require a redeploy anyway.
_SCHEMA_CACHE: dict = {}
_SCHEMA_CACHE_LOCK = threading.Lock()


def _get_table_columns(cur, table_name):
    if table_name not in _SCHEMA_CACHE:
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
        """, (table_name,))
        columns = {row[0] for row in cur.fetchall()}
        with _SCHEMA_CACHE_LOCK:
            if table_name not in _SCHEMA_CACHE:
                _SCHEMA_CACHE[table_name] = columns
    return _SCHEMA_CACHE[table_name]



def _filter_existing_columns(cur, table_name, data):
    table_columns = _get_table_columns(cur, table_name)
    filtered = {
        key: value for key, value in data.items()
        if key in table_columns and value is not None and value != ''
    }
    return filtered


def _parse_float(val):
    try:
        return float(val) if val not in (None, '') else None
    except (ValueError, TypeError):
        return None


def _get_user_company_id(cur, user_email):
    if not user_email:
        return None
    cur.execute('SELECT company_id FROM users WHERE email = %s', (user_email,))
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def _ensure_default_customer_company(cur, company_id):
    if company_id is None:
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


# Columnas cuyo valor decide _resolve_scope_fields a partir de las FKs, nunca el
# formulario. El <select> de Cliente/Empresa postea el NOMBRE ("Sesursa"), no el id,
# así que si el volcado de campos crudos las copiara tal cual, ese string terminaría
# en una columna integer y el INSERT reventaría con InvalidTextRepresentation,
# perdiendo todo lo diligenciado. Cuando el cliente no se puede resolver la columna
# queda NULL, que es lo que hacen el resto de los formularios.
_SCOPE_OWNED_FIELDS = {
    'company_id', 'customer_company_id', 'id_propiedad',
    'cliente_instalacion', 'submitter_timezone',
}


def _resolve_scope_fields(cur, user_email, legacy_customer_value=None, property_id=None, customer_company_id=None):
    scope = {}
    company_id = _get_user_company_id(cur, user_email)
    if company_id is not None:
        scope['company_id'] = company_id

    property_name = None
    property_customer_id = None

    # Resolve by property id (preferred path — set by the property selector)
    if property_id and str(property_id).isdigit():
        cur.execute("""
            SELECT id_propiedad, nombre, customer_company_id
            FROM propiedades
            WHERE id_propiedad = %s
              AND COALESCE(activa, TRUE) = TRUE
        """, (int(property_id),))
        row = cur.fetchone()
        if row:
            scope['id_propiedad'] = row[0]
            property_name = row[1]
            property_customer_id = row[2]

    # Fallback: match property by name when submitted via legacy text input
    if 'id_propiedad' not in scope and legacy_customer_value and str(legacy_customer_value).strip().upper() != 'NO APLICA':
        cur.execute("""
            SELECT id_propiedad, nombre, customer_company_id
            FROM propiedades
            WHERE LOWER(TRIM(nombre)) = LOWER(TRIM(%s))
              AND COALESCE(activa, TRUE) = TRUE
            LIMIT 1
        """, (legacy_customer_value,))
        row = cur.fetchone()
        if row:
            scope['id_propiedad'] = row[0]
            property_name = row[1]
            property_customer_id = row[2]

    if property_name:
        scope['cliente_instalacion'] = property_name
    elif legacy_customer_value:
        scope['cliente_instalacion'] = legacy_customer_value

    # Client (customer company). The property is authoritative — the selector only
    # narrows the property list, so a stale or hand-crafted value never wins over it.
    # Non-numeric values (e.g. the "sin cliente asignado" group) are ignored.
    if property_customer_id is not None:
        scope['customer_company_id'] = property_customer_id
    elif customer_company_id and str(customer_company_id).isdigit():
        scope['customer_company_id'] = int(customer_company_id)
    elif customer_company_id and isinstance(customer_company_id, str) and customer_company_id.strip():
        # Match customer company by name (e.g. "Sesursa")
        # Con company_id en NULL —lo habitual— la condicion (company_id = NULL OR
        # company_id IS NULL) se reduce a company_id IS NULL, y entonces solo matchea
        # clientes sin empresa: el cliente real nunca se encontraba. Sin company_id el
        # nombre resuelve por si solo; con company_id el filtro se mantiene.
        company_clause = "AND (company_id = %s OR company_id IS NULL)" if company_id is not None else ""
        cc_params = [f"%{customer_company_id.strip()}%"]
        if company_id is not None:
            cc_params.append(company_id)
        cur.execute(f"""
            SELECT id FROM customer_companies
            WHERE LOWER(TRIM(name)) LIKE LOWER(TRIM(%s))
              {company_clause}
            ORDER BY id
            LIMIT 1
        """, tuple(cc_params))
        cc_row = cur.fetchone()
        if cc_row:
            scope['customer_company_id'] = cc_row[0]
        elif 'sesursa' in customer_company_id.lower():
            def_cc = _ensure_default_customer_company(cur, company_id)
            if def_cc:
                scope['customer_company_id'] = def_cc['id']

    scope['submitter_timezone'] = request.form.get('submitter_timezone') or 'UTC'

    return scope

# --- Helper Functions ---
def upload_file_to_gcs(file, bucket_name):
    """Uploads a file to Google Cloud Storage."""
    if not file or not file.filename:
        return None
    if not bucket_name:
        app_logger.error(
            f"Subida omitida para '{file.filename}': no hay bucket configurado para este "
            f"despliegue. Definí GCS_BUCKET_NAME en el servicio."
        )
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

def _is_ajax_request():
    return (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest' or
        request.headers.get('X-SecApp-Replay') == '1' or
        (request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html)
    )

def _form_success_response(message=None):
    """Returns JSON for AJAX/replay requests or redirects to success page for standard browser submissions."""
    if _is_ajax_request():
        kwargs = {'message': message} if message else {}
        return jsonify({'success': True, 'redirect_url': url_for('forms_bp.success', **kwargs)}), 200
    kwargs = {'message': message} if message else {}
    return redirect(url_for('forms_bp.success', **kwargs))

def _form_error_response(message="Error interno del servidor. Por favor intente nuevamente.", status=500):
    """Returns JSON error for AJAX requests or renders error template for standard submissions."""
    if _is_ajax_request():
        return jsonify({'success': False, 'message': message}), status
    return render_template('error.html', error=message), status

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


# --- Edición controlada de formularios (Incidentes / Visitas) ---
MOTIVOS_EDICION = [
    'Corrección de información',
    'Error de digitación',
    'Actualización de datos',
    'Ajuste solicitado por el cliente',
    'Complemento de información',
    'Otro',
]


def admin_required(f):
    """Local copy of the admin_required decorator (see dashboard_bp.py / expediente_bp.py)
    to avoid a circular import between forms_bp and dashboard_bp."""
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        is_api = request.path.startswith('/api/') or request.path.startswith('/submit_')
        try:
            email = get_jwt_identity()
            conn = get_db_connection()
            if not conn:
                return jsonify({"error": "Service unavailable"}), 503
            try:
                cur = conn.cursor()
                cur.execute('SELECT is_admin, is_active FROM users WHERE email = %s', (email,))
                row = cur.fetchone()
                cur.close()
            finally:
                conn.close()
            if not row or not row[0] or not row[1]:
                app_logger.warning(f"admin_required denied for {email}")
                if is_api:
                    return jsonify({"error": "Acceso denegado"}), 403
                return redirect('/landing/')
        except Exception as e:
            app_logger.error(f"admin_required error: {e}", exc_info=True)
            return jsonify({"error": "Error de autenticación"}), 500
        return f(*args, **kwargs)
    return decorated


def module_required(module_key):
    """Blocks access to an optional module (e.g. 'log_de_patrullas') unless it's enabled
    for the current user's company license (companies.enabled_modules JSONB)."""
    from functools import wraps

    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            is_api = request.path.startswith('/api/') or request.path.startswith('/submit_')
            try:
                email = get_jwt_identity()
                conn = get_db_connection()
                if not conn:
                    return jsonify({"error": "Service unavailable"}), 503
                try:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT c.enabled_modules
                        FROM users u
                        JOIN companies c ON c.id = u.company_id
                        WHERE u.email = %s
                    """, (email,))
                    row = cur.fetchone()
                    cur.close()
                finally:
                    conn.close()
                enabled = set(row[0]) if row and row[0] else set()
                if module_key not in enabled:
                    app_logger.warning(f"module_required('{module_key}') denied for {email}")
                    if is_api:
                        return jsonify({"error": "Este módulo no está habilitado para su licencia"}), 403
                    flash('Este módulo no está habilitado para su licencia.', 'error')
                    return redirect('/forms/select')
            except Exception as e:
                app_logger.error(f"module_required('{module_key}') error: {e}", exc_info=True)
                return jsonify({"error": "Error de autenticación"}), 500
            return f(*args, **kwargs)
        return decorated
    return decorator


def _validate_not_future(date_val_str, field_label="Fecha", tz_str=None, tolerance_minutes=60):
    """
    Valida que date_val_str no represente una fecha u hora posterior al momento actual.
    Soporta formatos ISO de fecha ('YYYY-MM-DD') y fecha-hora ('YYYY-MM-DDTHH:MM', 'YYYY-MM-DD HH:MM:SS', etc.).
    Retorna None si es válido, o un mensaje de error si es una fecha/hora futura.
    """
    if not date_val_str:
        return None

    val_str = str(date_val_str).strip()
    if not val_str:
        return None

    # Determinar zona horaria (default: America/Costa_Rica)
    tz = None
    if tz_str:
        try:
            tz = zoneinfo.ZoneInfo(tz_str)
        except Exception:
            tz = None
    if tz is None:
        try:
            tz = zoneinfo.ZoneInfo("America/Costa_Rica")
        except Exception:
            tz = None

    now_local = datetime.now(tz) if tz else datetime.now()

    parsed = None
    is_time_included = False

    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            parsed = datetime.strptime(val_str, fmt)
            is_time_included = True
            break
        except ValueError:
            pass

    if not parsed:
        try:
            parsed = datetime.strptime(val_str[:10], '%Y-%m-%d')
            is_time_included = False
        except ValueError:
            # Formato no reconocido, no bloquear
            return None

    if is_time_included:
        if tz:
            parsed = parsed.replace(tzinfo=tz)
        max_allowed = now_local + timedelta(minutes=tolerance_minutes)
        if parsed > max_allowed:
            return f"El campo '{field_label}' no puede ser una fecha u hora posterior a la actual."
    else:
        if parsed.date() > now_local.date():
            return f"El campo '{field_label}' no puede ser posterior a la fecha actual."

    return None


def _return_form_error(message, status=400):
    """Retorna respuesta de error JSON o HTML según el tipo de petición."""
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', ''):
        return jsonify({'error': message, 'message': message}), status
    return render_template('error.html', error=message, message=message), status


def _validate_motivo_edicion(request):
    """Returns (motivo, motivo_detalle, error_response_or_None)."""
    motivo = (request.form.get('motivo') or '').strip()
    motivo_detalle = (request.form.get('motivo_detalle') or '').strip() or None
    if not motivo or motivo not in MOTIVOS_EDICION:
        return None, None, (jsonify({'error': 'Debe indicar un motivo de modificación válido'}), 400)
    if motivo == 'Otro' and not motivo_detalle:
        return None, None, (jsonify({'error': 'Debe detallar el motivo cuando selecciona "Otro"'}), 400)
    return motivo, motivo_detalle, None


def _record_edicion_historial(cur, tabla, registro_id, usuario_email, motivo, motivo_detalle, old_row, new_data):
    """Diffs old_row (dict-like) against new_data (dict of column -> new value) and inserts
    one row per changed field into formulario_edicion_historial."""
    for campo, nuevo_valor in new_data.items():
        valor_anterior = old_row.get(campo) if old_row else None
        # Normalize for comparison (avoid false positives from type differences, e.g. Decimal vs str)
        str_anterior = '' if valor_anterior is None else str(valor_anterior)
        str_nuevo = '' if nuevo_valor is None else str(nuevo_valor)
        if str_anterior == str_nuevo:
            continue
        cur.execute("""
            INSERT INTO formulario_edicion_historial
                (tabla, registro_id, usuario_email, motivo, motivo_detalle, campo, valor_anterior, valor_nuevo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (tabla, registro_id, usuario_email, motivo, motivo_detalle, campo, str_anterior or None, str_nuevo or None))


def _preservar_firmas_existentes(form_data):
    """En edición, un campo de firma vacío significa 'sin cambios': se excluye
    del UPDATE para conservar la firma original del registro."""
    for campo in list(form_data.keys()):
        if (campo.startswith('firma') or campo.endswith('_firma')) and not form_data[campo]:
            del form_data[campo]
    return form_data


def _parse_incident_form_data(request, user_email, foto_url=None):
    """Shared field-parsing for reportes_incidentes create/edit flows."""
    form_data = {
        'cliente_instalacion': request.form.get('cliente_instalacion'),
        'puesto_area_especifica': request.form.get('puesto_area_especifica'),
        'fecha_hora': request.form.get('fecha_hora'),
        'rol_aplicador': request.form.get('rol_aplicador'),
        'turno': request.form.get('turno'),
        'hora_entrada': request.form.get('hora_entrada'),
        'hora_salida': request.form.get('hora_salida'),
        'nombre_responsable': request.form.get('nombre_responsable'),
        'firma_responsable': request.form.get('firma_responsable'),
        'categoria': request.form.get('categoria'),
        'tipo_incidente': request.form.get('tipo_incidente'),
        'descripcion_incidente': request.form.get('descripcion'),
        'nivel_severidad': request.form.get('nivel_severidad'),

        'numero_empleado': request.form.get('numero_empleado'),
        'razon_ausentismo': request.form.get('razon_ausentismo'),
        'cubre_puesto': request.form.get('cubre_puesto'),
        'nombre_persona_cubre': request.form.get('nombre_persona_cubre'),
        'numero_empleado_cubre': request.form.get('numero_empleado_cubre'),
        'impacto': ", ".join(request.form.getlist('impacto')),
        'descripcion_impacto': request.form.get('descripcion_impacto'),
        'reportado_autoridades': request.form.get('reportado_autoridades'),
        'numero_reporte_autoridades': request.form.get('numero_reporte_autoridades') or None,
        'plan_accion': request.form.get('plan_accion'),
        'nombre_responsable_plan': request.form.get('nombre_responsable_plan'),
        'fecha_cumplimiento_plan': request.form.get('fecha_cumplimiento_plan') or None,
        'estado_seguimiento_plan': request.form.get('estado_seguimiento_plan'),
        'user_email': user_email,
        'latitude': _parse_float(request.form.get('latitude')),
        'longitude': _parse_float(request.form.get('longitude')),
        'location_accuracy': _parse_float(request.form.get('location_accuracy')),
    }
    if foto_url is not None:
        form_data['foto_evidencia_url'] = foto_url
    return form_data


def _upload_incident_photos(request):
    foto_urls = []
    for file in request.files.getlist('foto_evidencia'):
        if file and file.filename:
            url = upload_file_to_gcs(file, GCS_BUCKET_NAME)
            if url:
                foto_urls.append(url)
    return "\n".join(foto_urls) if foto_urls else None


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


@forms_bp.route('/api/properties')
@jwt_required()
def api_form_properties():
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT p.id_propiedad,
                   p.nombre,
                   p.customer_company_id,
                   COALESCE(cc.name, '') AS cliente
            FROM propiedades p
            LEFT JOIN customer_companies cc ON cc.id = p.customer_company_id
            WHERE COALESCE(p.activa, TRUE) = TRUE
            ORDER BY cliente, p.nombre
        """)
        rows = cur.fetchall()

        # Third level of the hierarchy: cliente → propiedad → puesto. Guarded on the
        # table existing so a database that has not been migrated yet still serves
        # the property list instead of failing the whole endpoint.
        puestos_by_property = {}
        cur.execute("SELECT to_regclass('puestos')")
        if cur.fetchone()[0] is not None:
            cur.execute("""
                SELECT id_puesto, id_propiedad, nombre
                FROM puestos
                WHERE COALESCE(activo, TRUE) = TRUE
                  AND NULLIF(TRIM(nombre), '') IS NOT NULL
                ORDER BY nombre
            """)
            for p in cur.fetchall():
                puestos_by_property.setdefault(p['id_propiedad'], []).append({
                    'id': p['id_puesto'],
                    'name': p['nombre'],
                })

        return jsonify({
            'properties': [
                {
                    'id': r['id_propiedad'],
                    'name': r['nombre'],
                    'cliente': r['cliente'],
                    # Drives the client selector that filters this list in the forms.
                    'customer_company_id': r['customer_company_id'],
                    # Drives the "Puesto o Área Específica" selector, scoped to
                    # whichever property is chosen. Empty means free-text entry.
                    'puestos': puestos_by_property.get(r['id_propiedad'], []),
                }
                for r in rows
            ]
        })
    except Exception as e:
        app_logger.error(f"api_form_properties error: {e}", exc_info=True)
        return jsonify({'properties': []}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


# --- FLOTA (fleet of vehicles and motorcycles) ---
# `flota.tipo` is free text, so classification is deliberately tolerant: anything
# that is not recognisably a motorcycle or a vehicle shows up in BOTH forms rather
# than vanishing from one. An unexpected spelling can never empty a dropdown.
_MOTO_HINTS = ('moto', 'scooter')
_VEHICLE_HINTS = ('veh', 'carro', 'auto', 'camion', 'camioneta', 'pick', 'suv',
                  'bus', 'furgon', 'sedan', 'jeep', 'panel')


def _normalize_text(value):
    text = unicodedata.normalize('NFD', str(value or ''))
    text = ''.join(ch for ch in text if unicodedata.category(ch) != 'Mn')
    return ' '.join(text.lower().split())


def _fleet_kind(tipo):
    """'moto', 'vehiculo', or None when the type is unrecognised (shown in both)."""
    text = _normalize_text(tipo)
    if not text:
        return None
    if text in ('m',) or any(hint in text for hint in _MOTO_HINTS):
        return 'moto'
    if text in ('v',) or any(hint in text for hint in _VEHICLE_HINTS):
        return 'vehiculo'
    return None


def _fleet_matches(tipo, wanted):
    kind = _fleet_kind(tipo)
    return kind is None or kind == wanted


def _normalize_plate(placa):
    return ' '.join(str(placa or '').split()).upper()


def _register_fleet_asset(cur, placa, kind, company_id):
    """Add a manually entered plate to the fleet. No-op when it already exists.

    Matching ignores case and surrounding whitespace so "abc 123 " does not create
    a twin of "ABC 123". Scoped to company_id only — the asset belongs to the
    security company, not to whichever client it was inspected at.
    Uses SAVEPOINT so fleet registration failure never aborts the parent transaction.
    """
    try:
        plate = _normalize_plate(placa)
        if not plate:
            return

        cur.execute("SAVEPOINT fleet_asset_reg")
        try:
            columns = _get_table_columns(cur, 'flota')
            if not columns or 'placa' not in columns:
                app_logger.warning("flota table missing or has no 'placa' column; skipping fleet registration")
                cur.execute("RELEASE SAVEPOINT fleet_asset_reg")
                return

            cur.execute("SELECT 1 FROM flota WHERE UPPER(TRIM(placa)) = %s LIMIT 1", (plate,))
            if cur.fetchone():
                cur.execute("RELEASE SAVEPOINT fleet_asset_reg")
                return

            values = {'placa': plate}
            if 'tipo' in columns:
                values['tipo'] = 'Motocicleta' if kind == 'moto' else 'Vehículo'
            if 'estado' in columns:
                values['estado'] = 'Activo'
            if 'company_id' in columns and company_id is not None:
                values['company_id'] = company_id

            names = ', '.join(values.keys())
            placeholders = ', '.join(['%s'] * len(values))
            cur.execute(f"INSERT INTO flota ({names}) VALUES ({placeholders})", list(values.values()))
            cur.execute("RELEASE SAVEPOINT fleet_asset_reg")
            app_logger.info(f"Fleet asset registered from form submission: {plate} ({kind})")
        except Exception as inner_e:
            cur.execute("ROLLBACK TO SAVEPOINT fleet_asset_reg")
            app_logger.warning(f"Could not register fleet asset '{placa}': {inner_e}")
    except Exception as e:
        app_logger.warning(f"Error in fleet asset registration wrapper for '{placa}': {e}")


@forms_bp.route('/api/fleet')
@jwt_required()
def api_fleet():
    """Fleet assets for the plate selector. ?tipo=moto|vehiculo narrows the list."""
    wanted = _normalize_text(request.args.get('tipo'))
    wanted = wanted if wanted in ('moto', 'vehiculo') else None

    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT id, placa, tipo, marca, modelo, anio, estado
            FROM flota
            WHERE placa IS NOT NULL AND TRIM(placa) <> ''
            ORDER BY placa
        """)
        rows = cur.fetchall()

        assets = []
        for row in rows:
            if wanted and not _fleet_matches(row['tipo'], wanted):
                continue
            # Out-of-service units stay listed but are flagged, so a Supervisor can
            # still file the inspection that takes one out of service.
            assets.append({
                'placa': _normalize_plate(row['placa']),
                'tipo': row['tipo'] or '',
                'marca': row['marca'] or '',
                'modelo': row['modelo'] or '',
                'anio': row['anio'],
                'estado': row['estado'] or '',
            })
        return jsonify({'assets': assets})
    except Exception as e:
        app_logger.error(f"api_fleet error: {e}", exc_info=True)
        return jsonify({'assets': []}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


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
        if company_id is None:
            return jsonify({'company_id': company_id, 'customers': []})

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

@forms_bp.route('/submit_incident_report', methods=['GET', 'POST'])
@jwt_required()
def submit_incident_report():
    if request.method == 'GET':
        return redirect(url_for('forms_bp.reporte_incidente_form'))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha_hora'), 'Fecha y Hora del Incidente', tz)
        if date_err:
            return _return_form_error(date_err, 400)

        conn = get_db_connection()
        cur = conn.cursor()

        foto_url = _upload_incident_photos(request)
        form_data = _parse_incident_form_data(request, user_email, foto_url=foto_url)
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
        cur.execute("SELECT lastval()")
        report_id = cur.fetchone()[0]

        tipos = request.form.getlist('persona_tipo[]')
        nombres = request.form.getlist('persona_nombre[]')
        for tipo, nombre in zip(tipos, nombres):
            if tipo or nombre:
                cur.execute(
                    "INSERT INTO reportes_incidentes_personas (id_reporte_incidente, persona_tipo, persona_nombre) VALUES (%s, %s, %s)",
                    (report_id, tipo or None, nombre or None)
                )

        conn.commit()
        cur.close()

        return _form_success_response()

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting incident report: {e}", exc_info=True)
        app_logger.error(f"Unhandled form error: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor. Por favor intente nuevamente.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/reporte_incidente/<int:id>/editar', methods=['GET'])
@jwt_required()
@admin_required
def reporte_incidente_editar_form(id):
    user_name, is_admin = get_user_info_from_jwt()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM reportes_incidentes WHERE id_reporte_incidente = %s", (id,))
        record = cur.fetchone()
        cur.close()
        if not record:
            return render_template('error.html', error='Registro no encontrado.'), 404

        return render_template(
            'reporte_incidente.html',
            name=user_name,
            is_admin=is_admin,
            edit_mode=True,
            record_id=id,
            record=dict(record),
            motivos_edicion=MOTIVOS_EDICION,
            **get_service_urls()
        )
    except Exception as e:
        app_logger.error(f"Error loading incident report {id} for edit: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/submit_incident_report/<int:id>/editar', methods=['GET', 'POST'])
@jwt_required()
@admin_required
def submit_incident_report_editar(id):
    if request.method == 'GET':
        return redirect(url_for('forms_bp.reporte_incidente_editar_form', id=id))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        motivo, motivo_detalle, error = _validate_motivo_edicion(request)
        if error:
            return error

        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha_hora'), 'Fecha y Hora del Incidente', tz)
        if date_err:
            return _return_form_error(date_err, 400)

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("SELECT * FROM reportes_incidentes WHERE id_reporte_incidente = %s", (id,))
        old_record = cur.fetchone()
        if not old_record:
            cur.close()
            return jsonify({'error': 'Registro no encontrado'}), 404
        old_record = dict(old_record)

        foto_url = _upload_incident_photos(request)
        form_data = _parse_incident_form_data(request, user_email, foto_url=foto_url)
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))
        # Don't overwrite the existing photo evidence if no new files were uploaded
        if foto_url is None:
            form_data.pop('foto_evidencia_url', None)
        # user_email above represents the editor, not the original submitter — do not overwrite it
        form_data.pop('user_email', None)

        form_data = _preservar_firmas_existentes(form_data)
        valid_form_data = _filter_existing_columns(cur, 'reportes_incidentes', form_data)

        _record_edicion_historial(
            cur, 'reportes_incidentes', id, user_email, motivo, motivo_detalle,
            old_record, valid_form_data
        )

        valid_form_data['editado'] = True
        valid_form_data['editado_en'] = datetime.utcnow()
        valid_form_data['editado_por'] = user_email

        set_clause = ', '.join(f"{k} = %s" for k in valid_form_data.keys())
        sql = f"UPDATE reportes_incidentes SET {set_clause} WHERE id_reporte_incidente = %s"
        cur.execute(sql, list(valid_form_data.values()) + [id])

        conn.commit()
        cur.close()

        return _form_success_response()
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error editing incident report {id}: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor. Por favor intente nuevamente.'), 500
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

@forms_bp.route('/submit_medicion_experiencia_cliente', methods=['GET', 'POST'])
@jwt_required()
def submit_medicion_experiencia_cliente():
    if request.method == 'GET':
        return redirect(url_for('forms_bp.medicion_experiencia_cliente_form'))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha_hora'), 'Fecha y Hora', tz)
        if date_err:
            return _return_form_error(date_err, 400)

        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma_responsable'),
            'atencion_cliente': int(v) if (v := request.form.get('atencion_cliente', '').strip()) else None,
            'comunicacion': int(v) if (v := request.form.get('comunicacion', '').strip()) else None,
            'confiabilidad': int(v) if (v := request.form.get('confiabilidad', '').strip()) else None,
            'capacidad_reaccion': int(v) if (v := request.form.get('capacidad_reaccion', '').strip()) else None,
            'cumplimiento': int(v) if (v := request.form.get('cumplimiento', '').strip()) else None,
            'competencia_personal': int(v) if (v := request.form.get('competencia_personal', '').strip()) else None,
            'actitud_servicio': int(v) if (v := request.form.get('actitud_servicio', '').strip()) else None,
            'atencion_quejas': int(v) if (v := request.form.get('atencion_quejas', '').strip()) else None,
            'calificacion_global_nps': round(float(v)) if (v := request.form.get('calificacion_global_nps', '').strip()) else None,
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
        return _form_success_response()

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting encuesta: {e}", exc_info=True)
        app_logger.error(f"Unhandled form error: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor. Por favor intente nuevamente.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/medicion_experiencia_cliente/<int:id>/editar', methods=['GET'])
@jwt_required()
@admin_required
def medicion_experiencia_cliente_editar_form(id):
    user_name, is_admin = get_user_info_from_jwt()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM medicion_experiencia_cliente WHERE id_encuesta = %s", (id,))
        record = cur.fetchone()
        cur.close()
        if not record:
            return render_template('error.html', error='Registro no encontrado.'), 404

        return render_template(
            'encuesta_cliente.html',
            name=user_name,
            is_admin=is_admin,
            edit_mode=True,
            record_id=id,
            record=dict(record),
            motivos_edicion=MOTIVOS_EDICION,
            **get_service_urls()
        )
    except Exception as e:
        app_logger.error(f"Error loading encuesta {id} for edit: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/submit_medicion_experiencia_cliente/<int:id>/editar', methods=['GET', 'POST'])
@jwt_required()
@admin_required
def submit_medicion_experiencia_cliente_editar(id):
    if request.method == 'GET':
        return redirect(url_for('forms_bp.medicion_experiencia_cliente_editar_form', id=id))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        motivo, motivo_detalle, error = _validate_motivo_edicion(request)
        if error:
            return error

        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha_hora'), 'Fecha y Hora', tz)
        if date_err:
            return _return_form_error(date_err, 400)

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("SELECT * FROM medicion_experiencia_cliente WHERE id_encuesta = %s", (id,))
        old_record = cur.fetchone()
        if not old_record:
            cur.close()
            return jsonify({'error': 'Registro no encontrado'}), 404
        old_record = dict(old_record)

        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma_responsable'),
            'atencion_cliente': int(v) if (v := request.form.get('atencion_cliente', '').strip()) else None,
            'comunicacion': int(v) if (v := request.form.get('comunicacion', '').strip()) else None,
            'confiabilidad': int(v) if (v := request.form.get('confiabilidad', '').strip()) else None,
            'capacidad_reaccion': int(v) if (v := request.form.get('capacidad_reaccion', '').strip()) else None,
            'cumplimiento': int(v) if (v := request.form.get('cumplimiento', '').strip()) else None,
            'competencia_personal': int(v) if (v := request.form.get('competencia_personal', '').strip()) else None,
            'actitud_servicio': int(v) if (v := request.form.get('actitud_servicio', '').strip()) else None,
            'atencion_quejas': int(v) if (v := request.form.get('atencion_quejas', '').strip()) else None,
            'calificacion_global_nps': round(float(v)) if (v := request.form.get('calificacion_global_nps', '').strip()) else None,
            'recomendaria_servicio': request.form.get('recomendaria_servicio'),
            'observaciones_cliente': request.form.get('observaciones_cliente'),
            'encuestado': request.form.get('encuestado'),
            'firma_encuestado': request.form.get('firma_encuestado'),
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

        form_data = _preservar_firmas_existentes(form_data)
        valid_form_data = _filter_existing_columns(cur, 'medicion_experiencia_cliente', form_data)

        _record_edicion_historial(
            cur, 'medicion_experiencia_cliente', id, user_email, motivo, motivo_detalle,
            old_record, valid_form_data
        )

        valid_form_data['editado'] = True
        valid_form_data['editado_en'] = datetime.utcnow()
        valid_form_data['editado_por'] = user_email

        set_clause = ', '.join(f"{k} = %s" for k in valid_form_data.keys())
        sql = f"UPDATE medicion_experiencia_cliente SET {set_clause} WHERE id_encuesta = %s"
        cur.execute(sql, list(valid_form_data.values()) + [id])

        conn.commit()
        cur.close()

        return _form_success_response()
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error editing encuesta {id}: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor. Por favor intente nuevamente.'), 500
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

@forms_bp.route('/submit_supervision_puesto', methods=['GET', 'POST'])
@jwt_required()
def submit_supervision_puesto():
    if request.method == 'GET':
        return redirect(url_for('forms_bp.supervision_puesto_form'))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    import re
    
    try:
        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha_hora'), 'Fecha y Hora', tz)
        if not date_err:
            date_err = _validate_not_future(request.form.get('fecha_ultimo_mtto_arma'), 'Fecha último mtto (arma)', tz)
        if not date_err:
            date_err = _validate_not_future(request.form.get('fecha_ultimo_mtto_radio'), 'Fecha último mtto (radio)', tz)
        if date_err:
            return _return_form_error(date_err, 400)

        conn = get_db_connection()
        cur = conn.cursor()

        # 1. Capture Global Fields
        global_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'fecha_hora': request.form.get('fecha_hora'),
            'supervisor': request.form.get('supervisor'),
            'rol_aplicador': request.form.get('rol_aplicador'),
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
        # supervision_puesto now uses cliente_instalacion (renamed from cliente)

        # 2. Parse Dynamic Supervisions from request.form
        # Keys are in format: supervisions[index][field_name]
        supervisions_map = {}
        pattern = re.compile(r'supervisions\[(\d+)\]\[(.*)\]')

        for key in request.form.keys():
            match = pattern.match(key)
            if match:
                index = int(match.group(1))
                field = match.group(2)
                
                # Check for array fields (e.g., problemas_uniforme[])
                if field.endswith('[]'):
                    field_name = field[:-2]
                    # Join multiple checkbox values with a comma
                    value = ', '.join(request.form.getlist(key))
                else:
                    field_name = field
                    value = request.form.get(key)
                
                if index not in supervisions_map:
                    supervisions_map[index] = {}
                supervisions_map[index][field_name] = value

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
            
            # cliente_instalacion is preserved from global_data / _resolve_scope_fields
            
            if not valid_row_data:
                continue # Skip empty rows

            columns = ', '.join(valid_row_data.keys())
            placeholders = ', '.join(['%s'] * len(valid_row_data))
            sql = f"INSERT INTO supervision_puesto ({columns}) VALUES ({placeholders})"

            cur.execute(sql, list(valid_row_data.values()))

        conn.commit()
        cur.close()

        return _form_success_response()

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting supervision puesto: {e}", exc_info=True)
        app_logger.error(f"Unhandled form error: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor. Por favor intente nuevamente.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/supervision_puesto/<int:id>/editar', methods=['GET'])
@jwt_required()
@admin_required
def supervision_puesto_editar_form(id):
    """Edita una sola fila de supervision_puesto (cada envío de creación genera N filas
    independientes; la edición opera sobre una fila puntual ya existente, no sobre el lote)."""
    user_name, is_admin = get_user_info_from_jwt()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM supervision_puesto WHERE id_supervision = %s", (id,))
        record = cur.fetchone()
        cur.close()
        if not record:
            return render_template('error.html', error='Registro no encontrado.'), 404

        return render_template(
            'supervision_puesto.html',
            name=user_name,
            is_admin=is_admin,
            edit_mode=True,
            record_id=id,
            record=dict(record),
            motivos_edicion=MOTIVOS_EDICION,
            **get_service_urls()
        )
    except Exception as e:
        app_logger.error(f"Error loading supervision puesto {id} for edit: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/submit_supervision_puesto/<int:id>/editar', methods=['GET', 'POST'])
@jwt_required()
@admin_required
def submit_supervision_puesto_editar(id):
    if request.method == 'GET':
        return redirect(url_for('forms_bp.supervision_puesto_editar_form', id=id))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        motivo, motivo_detalle, error = _validate_motivo_edicion(request)
        if error:
            return error

        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha_hora'), 'Fecha y Hora', tz)
        if not date_err:
            date_err = _validate_not_future(request.form.get('fecha_ultimo_mtto_arma'), 'Fecha último mtto (arma)', tz)
        if not date_err:
            date_err = _validate_not_future(request.form.get('fecha_ultimo_mtto_radio'), 'Fecha último mtto (radio)', tz)
        if date_err:
            return _return_form_error(date_err, 400)

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("SELECT * FROM supervision_puesto WHERE id_supervision = %s", (id,))
        old_record = cur.fetchone()
        if not old_record:
            cur.close()
            return jsonify({'error': 'Registro no encontrado'}), 404
        old_record = dict(old_record)

        # Edición de una fila puntual: campos planos (no indexados como en la creación).
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'fecha_hora': request.form.get('fecha_hora'),
            'supervisor': request.form.get('supervisor'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'detalles_puestos': request.form.get('detalles_puestos'),
            'horario_servicio': request.form.get('horario_servicio'),
            'hora_entrada': request.form.get('hora_entrada'),
            'hora_salida': request.form.get('hora_salida'),
            'tipo_servicio': request.form.get('tipo_servicio'),
            'modalidad_servicio': request.form.get('modalidad_servicio'),
            'nombre_guardia': request.form.get('nombre_guardia'),
            'numero_empleado': request.form.get('numero_empleado'),
            'documento_guardia': request.form.get('documento_guardia'),
            'tiempo_en_puesto': request.form.get('tiempo_en_puesto'),
            'licencia_portar_arma': request.form.get('licencia_portar_arma'),
            'realiza_induccion': request.form.get('realiza_induccion'),
            'conoce_ordenes_consignas': request.form.get('conoce_ordenes_consignas'),
            'horario_detalles_claros': request.form.get('horario_detalles_claros'),
            'porta_arma': request.form.get('porta_arma'),
            'tipo_arma': request.form.get('tipo_arma'),
            'serie_arma': request.form.get('serie_arma'),
            'matricula_arma': request.form.get('matricula_arma'),
            'cantidad_municion': request.form.get('cantidad_municion'),
            'fecha_vencimiento_permiso_porte': request.form.get('fecha_vencimiento_permiso_porte') or None,
            'fecha_ultimo_mtto_arma': request.form.get('fecha_ultimo_mtto_arma') or None,
            'radio_asignado_serial': request.form.get('radio_asignado_serial'),
            'marca_radio': request.form.get('marca_radio'),
            'tipo_radio': request.form.get('tipo_radio'),
            'fecha_ultimo_mtto_radio': request.form.get('fecha_ultimo_mtto_radio') or None,
            'presentacion_uniforme': request.form.get('presentacion_uniforme'),
            'problemas_uniforme': ', '.join(request.form.getlist('problemas_uniforme[]')),
            'observaciones_novedades': request.form.get('observaciones_novedades'),
            'firma_supervisor': request.form.get('firma_supervisor'),
            'firma_guardia': request.form.get('firma_guardia'),
        }
        if 'foto_evidencia' in request.files and request.files['foto_evidencia'].filename:
            url = upload_file_to_gcs(request.files['foto_evidencia'], GCS_BUCKET_NAME)
            if url:
                form_data['foto_evidencia_url'] = url
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))

        form_data = _preservar_firmas_existentes(form_data)
        valid_form_data = _filter_existing_columns(cur, 'supervision_puesto', form_data)
        valid_form_data = {k: v for k, v in valid_form_data.items() if v is not None and v != ''}

        _record_edicion_historial(
            cur, 'supervision_puesto', id, user_email, motivo, motivo_detalle,
            old_record, valid_form_data
        )

        valid_form_data['editado'] = True
        valid_form_data['editado_en'] = datetime.utcnow()
        valid_form_data['editado_por'] = user_email

        set_clause = ', '.join(f"{k} = %s" for k in valid_form_data.keys())
        sql = f"UPDATE supervision_puesto SET {set_clause} WHERE id_supervision = %s"
        cur.execute(sql, list(valid_form_data.values()) + [id])

        conn.commit()
        cur.close()

        return _form_success_response()
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error editing supervision puesto {id}: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor. Por favor intente nuevamente.'), 500
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

@forms_bp.route('/submit_informe_novedades_disciplinario', methods=['GET', 'POST'])
@jwt_required()
def submit_informe_novedades_disciplinario():
    if request.method == 'GET':
        return redirect(url_for('forms_bp.informe_novedades_disciplinario_form'))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha_hora'), 'Fecha y Hora', tz)
        if date_err:
            return _return_form_error(date_err, 400)

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
        app_logger.info("[DEBUG] Constructing form_data dictionary.")
        form_data = {
            'nombre_responsable': request.form.get('nombre_responsable'),
            'realizado_por_cargo': request.form.get('rol_aplicador'),
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
            'hora_entrada': request.form.get('hora_entrada'),
            'hora_salida': request.form.get('hora_salida'),
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
        return _form_success_response()

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting informe: {e}", exc_info=True)
        app_logger.error(f"Unhandled form error: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor. Por favor intente nuevamente.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/informe_novedades_disciplinario/<int:id>/editar', methods=['GET'])
@jwt_required()
@admin_required
def informe_novedades_disciplinario_editar_form(id):
    user_name, is_admin = get_user_info_from_jwt()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM informe_novedades_disciplinario WHERE id_informe = %s", (id,))
        record = cur.fetchone()
        cur.close()
        if not record:
            return render_template('error.html', error='Registro no encontrado.'), 404

        return render_template(
            'reporte_disciplinario.html',
            name=user_name,
            is_admin=is_admin,
            edit_mode=True,
            record_id=id,
            record=dict(record),
            motivos_edicion=MOTIVOS_EDICION,
            **get_service_urls()
        )
    except Exception as e:
        app_logger.error(f"Error loading informe {id} for edit: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/submit_informe_novedades_disciplinario/<int:id>/editar', methods=['GET', 'POST'])
@jwt_required()
@admin_required
def submit_informe_novedades_disciplinario_editar(id):
    if request.method == 'GET':
        return redirect(url_for('forms_bp.informe_novedades_disciplinario_editar_form', id=id))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        motivo, motivo_detalle, error = _validate_motivo_edicion(request)
        if error:
            return error

        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha_hora'), 'Fecha y Hora', tz)
        if date_err:
            return _return_form_error(date_err, 400)

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("SELECT * FROM informe_novedades_disciplinario WHERE id_informe = %s", (id,))
        old_record = cur.fetchone()
        if not old_record:
            cur.close()
            return jsonify({'error': 'Registro no encontrado'}), 404
        old_record = dict(old_record)

        anexos_urls = []
        if 'anexos_files' in request.files:
            for file in request.files.getlist('anexos_files'):
                if file and file.filename:
                    url = upload_file_to_gcs(file, GCS_BUCKET_NAME)
                    if url:
                        anexos_urls.append(url)
        anexos_str = "\n".join(anexos_urls) if anexos_urls else None

        form_data = {
            'nombre_responsable': request.form.get('nombre_responsable'),
            'realizado_por_cargo': request.form.get('rol_aplicador'),
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
            'firma_responsable': request.form.get('firma_responsable'),
            'firma_recibido_revisado': request.form.get('firma_recibido_revisado'),
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'hora_entrada': request.form.get('hora_entrada'),
            'hora_salida': request.form.get('hora_salida'),
            'empleado_niega_firmar': True if request.form.get('empleado_niega_firmar') else False,
            'nombre_testigo': request.form.get('nombre_testigo'),
            'firma_testigo': request.form.get('firma_testigo'),
            'latitude': _parse_float(request.form.get('latitude')),
            'longitude': _parse_float(request.form.get('longitude')),
            'location_accuracy': _parse_float(request.form.get('location_accuracy')),
        }
        if anexos_urls:
            form_data['anexos'] = anexos_str
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))

        form_data = _preservar_firmas_existentes(form_data)
        valid_form_data = _filter_existing_columns(cur, 'informe_novedades_disciplinario', form_data)

        _record_edicion_historial(
            cur, 'informe_novedades_disciplinario', id, user_email, motivo, motivo_detalle,
            old_record, valid_form_data
        )

        valid_form_data['editado'] = True
        valid_form_data['editado_en'] = datetime.utcnow()
        valid_form_data['editado_por'] = user_email

        set_clause = ', '.join(f"{k} = %s" for k in valid_form_data.keys())
        sql = f"UPDATE informe_novedades_disciplinario SET {set_clause} WHERE id_informe = %s"
        cur.execute(sql, list(valid_form_data.values()) + [id])

        conn.commit()
        cur.close()

        return _form_success_response()
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error editing informe {id}: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor. Por favor intente nuevamente.'), 500
    finally:
        if conn:
            conn.close()

# --- LOG DE PATRULLAS (módulo opcional, activable por licencia) ---
@forms_bp.route('/log_de_patrullas')
@jwt_required()
@module_required('log_de_patrullas')
def log_de_patrullas_form():
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'log_de_patrullas.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@forms_bp.route('/submit_log_de_patrullas', methods=['GET', 'POST'])
@jwt_required()
@module_required('log_de_patrullas')
def submit_log_de_patrullas():
    if request.method == 'GET':
        return redirect(url_for('forms_bp.log_de_patrullas_form'))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha'), 'Fecha de la Patrulla', tz)
        if date_err:
            return _return_form_error(date_err, 400)

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

        return _form_success_response()

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting log de patrullas: {e}", exc_info=True)
        app_logger.error(f"Unhandled form error: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor. Por favor intente nuevamente.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/log_de_patrullas/<int:id>/editar', methods=['GET'])
@jwt_required()
@admin_required
@module_required('log_de_patrullas')
def log_de_patrullas_editar_form(id):
    user_name, is_admin = get_user_info_from_jwt()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM log_de_patrullas WHERE id_patrulla = %s", (id,))
        record = cur.fetchone()
        cur.close()
        if not record:
            return render_template('error.html', error='Registro no encontrado.'), 404

        return render_template(
            'log_de_patrullas.html',
            name=user_name,
            is_admin=is_admin,
            edit_mode=True,
            record_id=id,
            record=dict(record),
            motivos_edicion=MOTIVOS_EDICION,
            **get_service_urls()
        )
    except Exception as e:
        app_logger.error(f"Error loading log de patrullas {id} for edit: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/submit_log_de_patrullas/<int:id>/editar', methods=['GET', 'POST'])
@jwt_required()
@admin_required
@module_required('log_de_patrullas')
def submit_log_de_patrullas_editar(id):
    if request.method == 'GET':
        return redirect(url_for('forms_bp.log_de_patrullas_editar_form', id=id))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        motivo, motivo_detalle, error = _validate_motivo_edicion(request)
        if error:
            return error

        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha'), 'Fecha de la Patrulla', tz)
        if date_err:
            return _return_form_error(date_err, 400)

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("SELECT * FROM log_de_patrullas WHERE id_patrulla = %s", (id,))
        old_record = cur.fetchone()
        if not old_record:
            cur.close()
            return jsonify({'error': 'Registro no encontrado'}), 404
        old_record = dict(old_record)

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
        }
        form_data.update(_resolve_scope_fields(cur, user_email))

        form_data = _preservar_firmas_existentes(form_data)
        valid_form_data = _filter_existing_columns(cur, 'log_de_patrullas', form_data)

        _record_edicion_historial(
            cur, 'log_de_patrullas', id, user_email, motivo, motivo_detalle,
            old_record, valid_form_data
        )

        valid_form_data['editado'] = True
        valid_form_data['editado_en'] = datetime.utcnow()
        valid_form_data['editado_por'] = user_email

        set_clause = ', '.join(f"{k} = %s" for k in valid_form_data.keys())
        sql = f"UPDATE log_de_patrullas SET {set_clause} WHERE id_patrulla = %s"
        cur.execute(sql, list(valid_form_data.values()) + [id])

        conn.commit()
        cur.close()

        return _form_success_response()
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error editing log de patrullas {id}: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor. Por favor intente nuevamente.'), 500
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

@forms_bp.route('/submit_asistencia_qr/<session_token>', methods=['GET', 'POST'])
def submit_asistencia_qr(session_token):
    """Save a guest attendance entry; no JWT needed."""
    if request.method == 'GET':
        return redirect(url_for('forms_bp.asistencia_qr_form', session_token=session_token))
    conn = None
    try:
        nombre = request.form.get('nombre', '').strip()
        if not nombre:
            return 'Nombre requerido', 400

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO capacitacion_asistencia (session_token, nombre, cargo, numero_empleado, documento, firma) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                session_token,
                nombre,
                request.form.get('cargo', ''),
                request.form.get('numero_empleado', ''),
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
@forms_bp.route('/registro_de_capacitaciones', methods=['GET', 'POST'])
@jwt_required()
def registro_de_capacitaciones_form():
    if request.method == 'POST':
        return submit_registro_de_capacitaciones()
    user_name, is_admin = get_user_info_from_jwt()

    return render_template(
        'registro_de_capacitaciones.html',
        name=user_name,
        is_admin=is_admin,
        **get_service_urls()
    )

@forms_bp.route('/submit_registro_de_capacitaciones', methods=['GET', 'POST'])
@jwt_required()
def submit_registro_de_capacitaciones():
    if request.method == 'GET':
        return redirect(url_for('forms_bp.registro_de_capacitaciones_form'))
    import json as _json
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        tz = request.form.get('submitter_timezone')
        raw_fh = request.form.get('fecha_hora') or request.form.get('fecha')
        date_err = _validate_not_future(raw_fh, 'Fecha y Hora de la Capacitación', tz)
        if date_err:
            return _return_form_error(date_err, 400)

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
                    "SELECT nombre, cargo, numero_empleado, documento, firma FROM capacitacion_asistencia WHERE session_token = %s",
                    (session_token,)
                )
                guest_rows = cur.fetchall()
                for row in guest_rows:
                    lista_manual.append({
                        'nombre': row[0], 'cargo': row[1],
                        'numero_empleado': row[2], 'documento': row[3],
                        'firma': row[4], 'via': 'QR'
                    })
            except Exception as qr_err:
                app_logger.warning(f"Could not fetch QR attendees: {qr_err}")

        lista_asistencia_json = psycopg2.extras.Json(lista_manual)

        # Upload attached files (photos/documents)
        capacitacion_urls = []
        if 'capacitacion_files' in request.files:
            files = request.files.getlist('capacitacion_files')
            for file in files:
                if file and file.filename:
                    url = upload_file_to_gcs(file, GCS_BUCKET_NAME)
                    if url:
                        capacitacion_urls.append(url)
        foto_evidencia_url = "\n".join(capacitacion_urls) if capacitacion_urls else None

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
            'foto_evidencia_url': foto_evidencia_url,
            'submitter_timezone': request.form.get('submitter_timezone'),
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

        return _form_success_response(message='Control de Capacitaciones enviado exitosamente!')

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting capacitacion: {e}", exc_info=True)
        return _form_error_response(f'Error al guardar la capacitación: {str(e)}', status=500)
    finally:
        if conn:
            conn.close()


@forms_bp.route('/registro_de_capacitaciones/<int:id>/editar', methods=['GET', 'POST'])
@jwt_required()
@admin_required
def registro_de_capacitaciones_editar_form(id):
    if request.method == 'POST':
        return submit_registro_de_capacitaciones_editar(id)
    user_name, is_admin = get_user_info_from_jwt()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM registro_de_capacitaciones WHERE id_capacitacion = %s", (id,))
        record = cur.fetchone()
        cur.close()
        if not record:
            return render_template('error.html', error='Registro no encontrado.'), 404

        return render_template(
            'registro_de_capacitaciones.html',
            name=user_name,
            is_admin=is_admin,
            edit_mode=True,
            record_id=id,
            record=dict(record),
            motivos_edicion=MOTIVOS_EDICION,
            **get_service_urls()
        )
    except Exception as e:
        app_logger.error(f"Error loading capacitacion {id} for edit: {e}", exc_info=True)
        return _form_error_response(f'Error al cargar la capacitación para edición: {str(e)}', status=500)
    finally:
        if conn:
            conn.close()


@forms_bp.route('/submit_registro_de_capacitaciones/<int:id>/editar', methods=['GET', 'POST'])
@jwt_required()
@admin_required
def submit_registro_de_capacitaciones_editar(id):
    if request.method == 'GET':
        return redirect(url_for('forms_bp.registro_de_capacitaciones_editar_form', id=id))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        motivo, motivo_detalle, error = _validate_motivo_edicion(request)
        if error:
            return error

        tz = request.form.get('submitter_timezone')
        raw_fh = request.form.get('fecha_hora') or request.form.get('fecha')
        date_err = _validate_not_future(raw_fh, 'Fecha y Hora de la Capacitación', tz)
        if date_err:
            return _return_form_error(date_err, 400)

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("SELECT * FROM registro_de_capacitaciones WHERE id_capacitacion = %s", (id,))
        old_record = cur.fetchone()
        if not old_record:
            cur.close()
            return jsonify({'error': 'Registro no encontrado'}), 404
        old_record = dict(old_record)

        capacitacion_urls = []
        if 'capacitacion_files' in request.files:
            for file in request.files.getlist('capacitacion_files'):
                if file and file.filename:
                    url = upload_file_to_gcs(file, GCS_BUCKET_NAME)
                    if url:
                        capacitacion_urls.append(url)

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
            'practica_simulacro_realizado': request.form.get('practica_simulacro_realizado'),
            'nivel_comprension': request.form.get('nivel_comprension'),
            'recomendaciones': request.form.get('recomendaciones'),
        }
        if capacitacion_urls:
            form_data['foto_evidencia_url'] = "\n".join(capacitacion_urls)
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))

        form_data = _preservar_firmas_existentes(form_data)
        valid_form_data = _filter_existing_columns(cur, 'registro_de_capacitaciones', form_data)

        _record_edicion_historial(
            cur, 'registro_de_capacitaciones', id, user_email, motivo, motivo_detalle,
            old_record, valid_form_data
        )

        valid_form_data['editado'] = True
        valid_form_data['editado_en'] = datetime.utcnow()
        valid_form_data['editado_por'] = user_email

        set_clause = ', '.join(f"{k} = %s" for k in valid_form_data.keys())
        sql = f"UPDATE registro_de_capacitaciones SET {set_clause} WHERE id_capacitacion = %s"
        cur.execute(sql, list(valid_form_data.values()) + [id])

        conn.commit()
        cur.close()

        return _form_success_response(message='Registro de Capacitación modificado exitosamente!')
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error editing capacitacion {id}: {e}", exc_info=True)
        return _form_error_response(f'Error al editar la capacitación: {str(e)}', status=500)
    finally:
        if conn:
            conn.close()

# --- REGISTRO Y ACTA DE VISITA ---
@forms_bp.route('/registro_y_acta_de_visita')
@jwt_required()
def registro_y_acta_de_visita_form():
    user_name, is_admin = get_user_info_from_jwt()
    ctx = {
        'pre_propiedad': request.args.get('id_propiedad', ''),
        'pre_motivo':    request.args.get('motivo', ''),
        'pre_temas':     request.args.get('temas', ''),
    }
    return render_template(
        'acta_visita_cliente.html',
        name=user_name,
        is_admin=is_admin,
        **ctx,
        **get_service_urls()
    )

def _parse_visit_form_data(request, user_email):
    """Shared field-parsing for registro_y_acta_de_visita create/edit flows."""
    import json

    # Process dynamic participants
    detalles_participantes = []
    for key in request.form:
        if key.startswith('nombre_participante_cliente_'):
            suffix = key.split('_')[-1]
            nombre = request.form.get(f'nombre_participante_cliente_{suffix}')
            cargo = request.form.get(f'cargo_participante_cliente_{suffix}')
            firma = request.form.get(f'firma_participante_cliente_{suffix}')

            if nombre or cargo or firma:
                detalles_participantes.append({'nombre': nombre, 'cargo': cargo, 'firma': firma})

    detalles_participantes_json = json.dumps(detalles_participantes)

    # Collect all repeatable block data (indexed temas_tratados_N, acuerdos_compromisos_N, etc.)
    bloques = {}
    for key in request.form:
        for prefix in ('temas_tratados_', 'acuerdos_compromisos_', 'nombre_responsable_', 'fecha_cumplimiento_', 'estado_seguimiento_'):
            if key.startswith(prefix):
                idx = key[len(prefix):]
                if idx not in bloques:
                    bloques[idx] = {}
                bloques[idx][prefix.rstrip('_')] = request.form.get(key)

    # Merge blocks into combined strings preserving positional alignment per block index
    sorted_keys = sorted(bloques.keys(), key=lambda x: int(x))
    # Filter to indices that have at least some content
    active_keys = [
        i for i in sorted_keys
        if any((bloques[i].get(f) or '').strip() for f in ('temas_tratados', 'acuerdos_compromisos', 'nombre_responsable', 'fecha_cumplimiento', 'estado_seguimiento'))
    ]

    temas_list = [(bloques[i].get('temas_tratados') or '').strip() for i in active_keys]
    acuerdos_list = [(bloques[i].get('acuerdos_compromisos') or '').strip() for i in active_keys]
    responsables_list = [
        {
            'nombre': (bloques[i].get('nombre_responsable') or '').strip(),
            'fecha': (bloques[i].get('fecha_cumplimiento') or '').strip(),
            'estado': (bloques[i].get('estado_seguimiento') or '').strip()
        }
        for i in active_keys
    ]

    temas_combined = '\n---\n'.join(temas_list) if any(temas_list) else None
    acuerdos_combined = '\n---\n'.join(acuerdos_list) if any(acuerdos_list) else None
    responsables_json = json.dumps(responsables_list) if any(r['nombre'] or r['fecha'] or r['estado'] for r in responsables_list) else None

    return {
        'cliente_instalacion': request.form.get('cliente_visitado'),
        'fecha_hora': request.form.get('fecha_hora'),
        'motivo_visita': request.form.get('motivo_visita'),
        'nombre_visitante': request.form.get('nombre_visitante'),
        'cargo_visitante': request.form.get('cargo_visitante'),
        'firma_visitante': request.form.get('firma_visitante'),
        'detalles_participantes': detalles_participantes_json,
        'temas_tratados': temas_combined,
        'acuerdos_compromisos': acuerdos_combined,
        'nombre_responsable': responsables_json,
        'submitted_by_email': user_email,
        'latitude': _parse_float(request.form.get('latitude')),
        'longitude': _parse_float(request.form.get('longitude')),
        'location_accuracy': _parse_float(request.form.get('location_accuracy')),
    }


@forms_bp.route('/submit_registro_y_acta_de_visita', methods=['GET', 'POST'])
@jwt_required()
def submit_registro_y_acta_de_visita():
    if request.method == 'GET':
        return redirect(url_for('forms_bp.registro_y_acta_de_visita_form'))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        form_data = _parse_visit_form_data(request, user_email)
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

        return _form_success_response()

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting registro y acta de visita: {e}", exc_info=True)
        app_logger.error(f"Unhandled form error: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor. Por favor intente nuevamente.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/registro_y_acta_de_visita/<int:id>/editar', methods=['GET'])
@jwt_required()
@admin_required
def registro_y_acta_de_visita_editar_form(id):
    user_name, is_admin = get_user_info_from_jwt()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM registro_y_acta_de_visita WHERE id_visita = %s", (id,))
        record = cur.fetchone()
        cur.close()
        if not record:
            return render_template('error.html', error='Registro no encontrado.'), 404

        return render_template(
            'acta_visita_cliente.html',
            name=user_name,
            is_admin=is_admin,
            edit_mode=True,
            record_id=id,
            record=dict(record),
            motivos_edicion=MOTIVOS_EDICION,
            **get_service_urls()
        )
    except Exception as e:
        app_logger.error(f"Error loading visita {id} for edit: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/submit_registro_y_acta_de_visita/<int:id>/editar', methods=['GET', 'POST'])
@jwt_required()
@admin_required
def submit_registro_y_acta_de_visita_editar(id):
    if request.method == 'GET':
        return redirect(url_for('forms_bp.registro_y_acta_de_visita_editar_form', id=id))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        motivo, motivo_detalle, error = _validate_motivo_edicion(request)
        if error:
            return error

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("SELECT * FROM registro_y_acta_de_visita WHERE id_visita = %s", (id,))
        old_record = cur.fetchone()
        if not old_record:
            cur.close()
            return jsonify({'error': 'Registro no encontrado'}), 404
        old_record = dict(old_record)

        form_data = _parse_visit_form_data(request, user_email)
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))
        # user_email above represents the editor, not the original submitter
        form_data.pop('submitted_by_email', None)

        form_data = _preservar_firmas_existentes(form_data)
        valid_form_data = _filter_existing_columns(cur, 'registro_y_acta_de_visita', form_data)

        _record_edicion_historial(
            cur, 'registro_y_acta_de_visita', id, user_email, motivo, motivo_detalle,
            old_record, valid_form_data
        )

        valid_form_data['editado'] = True
        valid_form_data['editado_en'] = datetime.utcnow()
        valid_form_data['editado_por'] = user_email

        set_clause = ', '.join(f"{k} = %s" for k in valid_form_data.keys())
        sql = f"UPDATE registro_y_acta_de_visita SET {set_clause} WHERE id_visita = %s"
        cur.execute(sql, list(valid_form_data.values()) + [id])

        conn.commit()
        cur.close()

        return _form_success_response()
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error editing visita {id}: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor. Por favor intente nuevamente.'), 500
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

@forms_bp.route('/submit_planilla_vehicular', methods=['GET', 'POST'])
@jwt_required()
def submit_planilla_vehicular():
    if request.method == 'GET':
        return redirect(url_for('forms_bp.planilla_vehicular_form'))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha_hora'), 'Fecha y Hora', tz)
        if not date_err:
            date_err = _validate_not_future(request.form.get('fecha_ultimo_mantenimiento'), 'Fecha de Último Mantenimiento', tz)
        if date_err:
            return _return_form_error(date_err, 400)

        conn = get_db_connection()
        cur = conn.cursor()
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion') or 'NO APLICA',
            'puesto_area_especifica': request.form.get('puesto_area_especifica') or 'NO APLICA',
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'hora_entrada': request.form.get('hora_entrada'),
            'hora_salida': request.form.get('hora_salida'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'numero_empleado': request.form.get('numero_empleado'),
            'fecha_ultimo_mantenimiento': request.form.get('fecha_ultimo_mantenimiento'),
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
            customer_company_id=request.form.get('customer_company_id') or 'Sesursa',
        ))
        for key in request.form.keys():
            if (key not in form_data and key != 'csrf_token'
                    and key not in _SCOPE_OWNED_FIELDS):
                form_data[key] = request.form.get(key)
        form_data = _filter_existing_columns(cur, 'planilla_vehicular', form_data)
        columns = ', '.join(form_data.keys())
        placeholders = ', '.join(['%s'] * len(form_data))
        sql = f"INSERT INTO planilla_vehicular ({columns}) VALUES ({placeholders})"

        cur.execute(sql, list(form_data.values()))
        # A plate typed by hand is a fleet asset we did not know about yet.
        # Registered in the same transaction as the inspection, so an offline
        # form that replays later still records the unit exactly once.
        _register_fleet_asset(cur, form_data.get('placa_vehiculo'), 'vehiculo', form_data.get('company_id'))
        conn.commit()
        cur.close()

        return _form_success_response()

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting planilla vehicular: {e}", exc_info=True)
        return render_template('error.html', message=f"Error al guardar la Planilla de Chequeo Pre-Operacional Vehicular: {e}", error=str(e)), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/planilla_vehicular/<int:id>/editar', methods=['GET'])
@jwt_required()
@admin_required
def planilla_vehicular_editar_form(id):
    user_name, is_admin = get_user_info_from_jwt()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM planilla_vehicular WHERE id_planilla_vehicular = %s", (id,))
        record = cur.fetchone()
        cur.close()
        if not record:
            return render_template('error.html', error='Registro no encontrado.'), 404

        return render_template(
            'planilla_vehicular.html',
            name=user_name,
            is_admin=is_admin,
            edit_mode=True,
            record_id=id,
            record=dict(record),
            motivos_edicion=MOTIVOS_EDICION,
            **get_service_urls()
        )
    except Exception as e:
        app_logger.error(f"Error loading planilla vehicular {id} for edit: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/submit_planilla_vehicular/<int:id>/editar', methods=['GET', 'POST'])
@jwt_required()
@admin_required
def submit_planilla_vehicular_editar(id):
    if request.method == 'GET':
        return redirect(url_for('forms_bp.planilla_vehicular_editar_form', id=id))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        motivo, motivo_detalle, error = _validate_motivo_edicion(request)
        if error:
            return error

        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha_hora'), 'Fecha y Hora', tz)
        if not date_err:
            date_err = _validate_not_future(request.form.get('fecha_ultimo_mantenimiento'), 'Fecha de Último Mantenimiento', tz)
        if date_err:
            return _return_form_error(date_err, 400)

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("SELECT * FROM planilla_vehicular WHERE id_planilla_vehicular = %s", (id,))
        old_record = cur.fetchone()
        if not old_record:
            cur.close()
            return jsonify({'error': 'Registro no encontrado'}), 404
        old_record = dict(old_record)

        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion') or 'NO APLICA',
            'puesto_area_especifica': request.form.get('puesto_area_especifica') or 'NO APLICA',
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'hora_entrada': request.form.get('hora_entrada'),
            'hora_salida': request.form.get('hora_salida'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'numero_empleado': request.form.get('numero_empleado'),
            'fecha_ultimo_mantenimiento': request.form.get('fecha_ultimo_mantenimiento'),
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
        }
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id') or 'Sesursa',
        ))

        form_data = _preservar_firmas_existentes(form_data)
        valid_form_data = _filter_existing_columns(cur, 'planilla_vehicular', form_data)

        _record_edicion_historial(
            cur, 'planilla_vehicular', id, user_email, motivo, motivo_detalle,
            old_record, valid_form_data
        )

        valid_form_data['editado'] = True
        valid_form_data['editado_en'] = datetime.utcnow()
        valid_form_data['editado_por'] = user_email

        set_clause = ', '.join(f"{k} = %s" for k in valid_form_data.keys())
        sql = f"UPDATE planilla_vehicular SET {set_clause} WHERE id_planilla_vehicular = %s"
        cur.execute(sql, list(valid_form_data.values()) + [id])
        # A plate typed by hand is a fleet asset we did not know about yet.
        # Registered in the same transaction as the inspection, so an offline
        # form that replays later still records the unit exactly once.
        _register_fleet_asset(cur, valid_form_data.get('placa_vehiculo'), 'vehiculo', form_data.get('company_id'))

        conn.commit()
        cur.close()

        return _form_success_response()
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error editing planilla vehicular {id}: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor. Por favor intente nuevamente.'), 500
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

@forms_bp.route('/submit_planilla_motocicletas', methods=['GET', 'POST'])
@jwt_required()
def submit_planilla_motocicletas():
    if request.method == 'GET':
        return redirect(url_for('forms_bp.planilla_motocicletas_form'))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha_hora'), 'Fecha y Hora', tz)
        if not date_err:
            date_err = _validate_not_future(request.form.get('fecha_ultimo_mantenimiento'), 'Fecha de Último Mantenimiento', tz)
        if date_err:
            return _return_form_error(date_err, 400)

        conn = get_db_connection()
        cur = conn.cursor()
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion') or 'NO APLICA',
            'puesto_area_especifica': request.form.get('puesto_area_especifica') or 'NO APLICA',
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'hora_entrada': request.form.get('hora_entrada'),
            'hora_salida': request.form.get('hora_salida'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma_responsable'),
            'placa_motocicleta': request.form.get('placa_motocicleta'),
            'kilometraje_motocicleta': request.form.get('kilometraje_motocicleta') or None,
            'numero_empleado': request.form.get('numero_empleado'),
            'fecha_ultimo_mantenimiento': request.form.get('fecha_ultimo_mantenimiento') or None,
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
            customer_company_id=request.form.get('customer_company_id') or 'Sesursa',
        ))

        # Add all form fields (checklist and inspection properties)
        for key in request.form.keys():
            if (key not in form_data and key != 'csrf_token'
                    and key not in _SCOPE_OWNED_FIELDS):
                form_data[key] = request.form.get(key)

        app_logger.info(f"Submitting motorcycle form for {user_email}")
        valid_form_data = _filter_existing_columns(cur, 'planilla_motocicletas', form_data)

        columns = ', '.join(valid_form_data.keys())
        placeholders = ', '.join(['%s'] * len(valid_form_data))
        sql = f"INSERT INTO planilla_motocicletas ({columns}) VALUES ({placeholders})"
        
        app_logger.info(f"Inserting into planilla_motocicletas with keys: {list(valid_form_data.keys())}")
        cur.execute(sql, list(valid_form_data.values()))
        # A plate typed by hand is a fleet asset we did not know about yet.
        # Registered in the same transaction as the inspection, so an offline
        # form that replays later still records the unit exactly once.
        _register_fleet_asset(cur, valid_form_data.get('placa_motocicleta'), 'moto', form_data.get('company_id'))
        conn.commit()
        cur.close()
        app_logger.info("Motorcycle form submitted successfully.")

        return _form_success_response()

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting planilla motocicletas: {e}", exc_info=True)
        return render_template('error.html', message=f"Error al guardar la Planilla de Chequeo Pre-Operacional de Motocicletas: {e}", error=str(e)), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/planilla_motocicletas/<int:id>/editar', methods=['GET'])
@jwt_required()
@admin_required
def planilla_motocicletas_editar_form(id):
    user_name, is_admin = get_user_info_from_jwt()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM planilla_motocicletas WHERE id = %s", (id,))
        record = cur.fetchone()
        cur.close()
        if not record:
            return render_template('error.html', error='Registro no encontrado.'), 404

        return render_template(
            'planilla_motocicletas.html',
            name=user_name,
            is_admin=is_admin,
            edit_mode=True,
            record_id=id,
            record=dict(record),
            motivos_edicion=MOTIVOS_EDICION,
            **get_service_urls()
        )
    except Exception as e:
        app_logger.error(f"Error loading planilla motocicletas {id} for edit: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/submit_planilla_motocicletas/<int:id>/editar', methods=['GET', 'POST'])
@jwt_required()
@admin_required
def submit_planilla_motocicletas_editar(id):
    if request.method == 'GET':
        return redirect(url_for('forms_bp.planilla_motocicletas_editar_form', id=id))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        motivo, motivo_detalle, error = _validate_motivo_edicion(request)
        if error:
            return error

        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha_hora'), 'Fecha y Hora', tz)
        if not date_err:
            date_err = _validate_not_future(request.form.get('fecha_ultimo_mantenimiento'), 'Fecha de Último Mantenimiento', tz)
        if date_err:
            return _return_form_error(date_err, 400)

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("SELECT * FROM planilla_motocicletas WHERE id = %s", (id,))
        old_record = cur.fetchone()
        if not old_record:
            cur.close()
            return jsonify({'error': 'Registro no encontrado'}), 404
        old_record = dict(old_record)

        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion') or 'NO APLICA',
            'puesto_area_especifica': request.form.get('puesto_area_especifica') or 'NO APLICA',
            'fecha_hora': request.form.get('fecha_hora'),
            'rol_aplicador': request.form.get('rol_aplicador'),
            'turno': request.form.get('turno'),
            'hora_entrada': request.form.get('hora_entrada'),
            'hora_salida': request.form.get('hora_salida'),
            'nombre_responsable': request.form.get('nombre_responsable'),
            'firma_responsable': request.form.get('firma_responsable'),
            'placa_motocicleta': request.form.get('placa_motocicleta'),
            'kilometraje_motocicleta': request.form.get('kilometraje_motocicleta') or None,
            'numero_empleado': request.form.get('numero_empleado'),
            'fecha_ultimo_mantenimiento': request.form.get('fecha_ultimo_mantenimiento') or None,
            'diagrama_danos': request.form.get('diagrama_danos'),
            'novedades_criticas_detectadas': request.form.get('novedades_criticas_detectadas'),
            'accion_inmediata_tomada': request.form.get('accion_inmediata_tomada'),
            'firma_entrega': request.form.get('firma_entrega'),
            'firma_recibe': request.form.get('firma_recibe'),
            'oficial_operaciones_nombre': request.form.get('oficial_operaciones_nombre'),
            'oficial_operaciones_firma': request.form.get('oficial_operaciones_firma'),
        }
        for key in request.form.keys():
            if (key not in form_data and key != 'csrf_token'
                    and key not in _SCOPE_OWNED_FIELDS):
                form_data[key] = request.form.get(key)
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id') or 'Sesursa',
        ))

        form_data = _preservar_firmas_existentes(form_data)
        valid_form_data = _filter_existing_columns(cur, 'planilla_motocicletas', form_data)

        _record_edicion_historial(
            cur, 'planilla_motocicletas', id, user_email, motivo, motivo_detalle,
            old_record, valid_form_data
        )

        valid_form_data['editado'] = True
        valid_form_data['editado_en'] = datetime.utcnow()
        valid_form_data['editado_por'] = user_email

        set_clause = ', '.join(f"{k} = %s" for k in valid_form_data.keys())
        sql = f"UPDATE planilla_motocicletas SET {set_clause} WHERE id = %s"
        cur.execute(sql, list(valid_form_data.values()) + [id])
        # A plate typed by hand is a fleet asset we did not know about yet.
        # Registered in the same transaction as the inspection, so an offline
        # form that replays later still records the unit exactly once.
        _register_fleet_asset(cur, valid_form_data.get('placa_motocicleta'), 'moto', form_data.get('company_id'))

        conn.commit()
        cur.close()

        return _form_success_response()
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error editing planilla motocicletas {id}: {e}", exc_info=True)
        return render_template('error.html', message=f"Error al editar la Planilla de Chequeo Pre-Operacional de Motocicletas: {e}", error=str(e)), 500
    finally:
        if conn:
            conn.close()

# --- CHECKLIST DE CUMPLIMIENTO NORMATIVO (UPDATED ROUTE) ---
@forms_bp.route('/checklist_cumplimiento', methods=['GET', 'POST'])
@jwt_required()
def checklist_cumplimiento():
    """Renders the updated compliance checklist form or handles POST submission."""
    if request.method == 'POST':
        return submit_checklist_cumplimiento()
    user_name, is_admin = get_user_info_from_jwt()

    return render_template('checklist_cumplimiento.html',
                           name=user_name,
                           is_admin=is_admin,
                           **get_service_urls())

@forms_bp.route('/submit_checklist_cumplimiento', methods=['GET', 'POST'])
@jwt_required()
def submit_checklist_cumplimiento():
    """Handles the submission of the updated compliance checklist form with multiple entries."""
    if request.method == 'GET':
        return redirect(url_for('forms_bp.checklist_cumplimiento'))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha_hora'), 'Fecha y Hora', tz)
        if not date_err:
            fechas_res = request.form.getlist('fecha_resolucion[]') or [request.form.get('fecha_resolucion')]
            for fr in fechas_res:
                if fr:
                    date_err = _validate_not_future(fr, 'Fecha de Resolución', tz)
                    if date_err:
                        break
        if date_err:
            return _return_form_error(date_err, 400)

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
            'submitter_timezone': request.form.get('submitter_timezone'),
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
        num_rows = len(agente_nombres) if agente_nombres else 1
        app_logger.info(f"Submitting checklist fulfillment for {user_email}. Rows: {num_rows}")

        for i in range(num_rows):
            app_logger.info(f"Processing row {i+1}/{num_rows}")
            # Handle unique file upload per row
            evidencia_key = f'cargue_evidencia_{i}'
            evidencia_url = None
            if evidencia_key in request.files and request.files[evidencia_key].filename != '':
                file = request.files[evidencia_key]
                evidencia_url = upload_file_to_gcs(file, GCS_BUCKET_NAME)
            elif 'cargue_evidencia' in request.files and request.files['cargue_evidencia'].filename != '':
                file = request.files['cargue_evidencia']
                evidencia_url = upload_file_to_gcs(file, GCS_BUCKET_NAME)

            # Build row data combining header and indexed lists
            row_data = header_data.copy()
            agente_nom = agente_nombres[i] if (agente_nombres and len(agente_nombres) > i) else request.form.get('agente_nombre_completo')
            row_data.update({
                # Section 2
                'agente_nombre_completo': agente_nom,
                'agente_tipo_documento': request.form.getlist('agente_tipo_documento[]')[i] if len(request.form.getlist('agente_tipo_documento[]')) > i else request.form.get('agente_tipo_documento'),
                'agente_numero_documento': request.form.getlist('agente_numero_documento[]')[i] if len(request.form.getlist('agente_numero_documento[]')) > i else request.form.get('agente_numero_documento'),
                'agente_cargo_rol': request.form.getlist('agente_cargo_rol[]')[i] if len(request.form.getlist('agente_cargo_rol[]')) > i else request.form.get('agente_cargo_rol'),
                'agente_numero_empleado': request.form.getlist('agente_numero_empleado[]')[i] if len(request.form.getlist('agente_numero_empleado[]')) > i else request.form.get('agente_numero_empleado'),
                'agente_puesto': request.form.getlist('agente_puesto[]')[i] if len(request.form.getlist('agente_puesto[]')) > i else request.form.get('agente_puesto'),

                # Section 3
                'curso_certificacion': request.form.getlist('curso_certificacion[]')[i] if len(request.form.getlist('curso_certificacion[]')) > i else request.form.get('curso_certificacion'),
                'academia_certifica': request.form.getlist('academia_certifica[]')[i] if len(request.form.getlist('academia_certifica[]')) > i else request.form.get('academia_certifica'),
                'nro_resolucion': request.form.getlist('nro_resolucion[]')[i] if len(request.form.getlist('nro_resolucion[]')) > i else request.form.get('nro_resolucion'),
                'fecha_resolucion': (request.form.getlist('fecha_resolucion[]')[i] or None) if len(request.form.getlist('fecha_resolucion[]')) > i else (request.form.get('fecha_resolucion') or None),
                'vigencia_desde': (request.form.getlist('vigencia_desde[]')[i] or None) if len(request.form.getlist('vigencia_desde[]')) > i else (request.form.get('vigencia_desde') or None),
                'vigencia_hasta': (request.form.getlist('vigencia_hasta[]')[i] or None) if len(request.form.getlist('vigencia_hasta[]')) > i else (request.form.get('vigencia_hasta') or None),
                'evidencia_url': evidencia_url,
                'nivel_cumplimiento': request.form.getlist('nivel_cumplimiento[]')[i] if len(request.form.getlist('nivel_cumplimiento[]')) > i else request.form.get('nivel_cumplimiento'),

                # Section 4
                'copia_certificados_fisica': request.form.getlist('copia_certificados_fisica[]')[i] if len(request.form.getlist('copia_certificados_fisica[]')) > i else request.form.get('copia_certificados_fisica'),
                'certificados_cargados_sistema': request.form.getlist('certificados_cargados_sistema[]')[i] if len(request.form.getlist('certificados_cargados_sistema[]')) > i else request.form.get('certificados_cargados_sistema'),
                'documentacion_coincide_hv': request.form.getlist('documentacion_coincide_hv[]')[i] if len(request.form.getlist('documentacion_coincide_hv[]')) > i else request.form.get('documentacion_coincide_hv'),
                'fechas_vigentes': request.form.getlist('fechas_vigentes[]')[i] if len(request.form.getlist('fechas_vigentes[]')) > i else request.form.get('fechas_vigentes'),

                # Section 5
                'firma_auditor': request.form.getlist('firma_auditor[]')[i] if len(request.form.getlist('firma_auditor[]')) > i else request.form.get('firma_auditor'),
                'firma_guarda_supervisado': request.form.getlist('firma_guarda_supervisado[]')[i] if len(request.form.getlist('firma_guarda_supervisado[]')) > i else request.form.get('firma_guarda_supervisado'),
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
        return _form_success_response(message='Checklist(s) de cumplimiento guardado(s) exitosamente!')

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting updated checklist_cumplimiento: {e}", exc_info=True)
        return _form_error_response(f'Error al guardar el checklist: {str(e)}', status=500)
    finally:
        if conn:
            conn.close()


@forms_bp.route('/checklist_cumplimiento/<int:id>/editar', methods=['GET', 'POST'])
@jwt_required()
@admin_required
def checklist_cumplimiento_editar_form(id):
    """Edita una sola fila de checklist_cumplimiento (cada envío de creación genera N filas
    independientes; la edición opera sobre una fila puntual ya existente, no sobre el lote)."""
    if request.method == 'POST':
        return submit_checklist_cumplimiento_editar(id)
    user_name, is_admin = get_user_info_from_jwt()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM checklist_cumplimiento WHERE id = %s", (id,))
        record = cur.fetchone()
        cur.close()
        if not record:
            return render_template('error.html', error='Registro no encontrado.'), 404

        return render_template(
            'checklist_cumplimiento.html',
            name=user_name,
            is_admin=is_admin,
            edit_mode=True,
            record_id=id,
            record=dict(record),
            motivos_edicion=MOTIVOS_EDICION,
            **get_service_urls()
        )
    except Exception as e:
        app_logger.error(f"Error loading checklist_cumplimiento {id} for edit: {e}", exc_info=True)
        return _form_error_response(f'Error al cargar el checklist para edición: {str(e)}', status=500)
    finally:
        if conn:
            conn.close()


@forms_bp.route('/submit_checklist_cumplimiento/<int:id>/editar', methods=['GET', 'POST'])
@jwt_required()
@admin_required
def submit_checklist_cumplimiento_editar(id):
    if request.method == 'GET':
        return redirect(url_for('forms_bp.checklist_cumplimiento_editar_form', id=id))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        motivo, motivo_detalle, error = _validate_motivo_edicion(request)
        if error:
            return error

        tz = request.form.get('submitter_timezone')
        date_err = _validate_not_future(request.form.get('fecha_hora'), 'Fecha y Hora', tz)
        if not date_err:
            fechas_res = request.form.getlist('fecha_resolucion[]') or [request.form.get('fecha_resolucion')]
            for fr in fechas_res:
                if fr:
                    date_err = _validate_not_future(fr, 'Fecha de Resolución', tz)
                    if date_err:
                        break
        if date_err:
            return _return_form_error(date_err, 400)

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("SELECT * FROM checklist_cumplimiento WHERE id = %s", (id,))
        old_record = cur.fetchone()
        if not old_record:
            cur.close()
            return jsonify({'error': 'Registro no encontrado'}), 404
        old_record = dict(old_record)

        # Edición de una fila puntual: campos planos (no listas indexadas como en la creación).
        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'puesto_area_especifica': request.form.get('puesto_area_especifica'),
            'fecha_hora': request.form.get('fecha_hora') or None,
            'rol_aplicador': request.form.get('rol_aplicador'),
            'nombre_auditor': request.form.get('nombre_auditor'),
            'agente_nombre_completo': request.form.get('agente_nombre_completo'),
            'agente_tipo_documento': request.form.get('agente_tipo_documento'),
            'agente_numero_documento': request.form.get('agente_numero_documento'),
            'agente_cargo_rol': request.form.get('agente_cargo_rol'),
            'agente_numero_empleado': request.form.get('agente_numero_empleado'),
            'agente_puesto': request.form.get('agente_puesto'),
            'curso_certificacion': request.form.get('curso_certificacion'),
            'academia_certifica': request.form.get('academia_certifica'),
            'nro_resolucion': request.form.get('nro_resolucion'),
            'fecha_resolucion': request.form.get('fecha_resolucion') or None,
            'vigencia_desde': request.form.get('vigencia_desde') or None,
            'vigencia_hasta': request.form.get('vigencia_hasta') or None,
            'nivel_cumplimiento': request.form.get('nivel_cumplimiento'),
            'copia_certificados_fisica': request.form.get('copia_certificados_fisica'),
            'certificados_cargados_sistema': request.form.get('certificados_cargados_sistema'),
            'documentacion_coincide_hv': request.form.get('documentacion_coincide_hv'),
            'fechas_vigentes': request.form.get('fechas_vigentes'),
            'firma_auditor': request.form.get('firma_auditor'),
            'firma_guarda_supervisado': request.form.get('firma_guarda_supervisado'),
        }
        if 'cargue_evidencia' in request.files and request.files['cargue_evidencia'].filename:
            url = upload_file_to_gcs(request.files['cargue_evidencia'], GCS_BUCKET_NAME)
            if url:
                form_data['evidencia_url'] = url
        form_data.update(_resolve_scope_fields(
            cur,
            user_email,
            legacy_customer_value=form_data.get('cliente_instalacion'),
            property_id=request.form.get('id_propiedad'),
            customer_company_id=request.form.get('customer_company_id'),
        ))

        form_data = _preservar_firmas_existentes(form_data)
        valid_form_data = _filter_existing_columns(cur, 'checklist_cumplimiento', form_data)
        valid_form_data = {k: v for k, v in valid_form_data.items() if v is not None and v != ''}

        _record_edicion_historial(
            cur, 'checklist_cumplimiento', id, user_email, motivo, motivo_detalle,
            old_record, valid_form_data
        )

        valid_form_data['editado'] = True
        valid_form_data['editado_en'] = datetime.utcnow()
        valid_form_data['editado_por'] = user_email

        set_clause = ', '.join(f"{k} = %s" for k in valid_form_data.keys())
        sql = f"UPDATE checklist_cumplimiento SET {set_clause} WHERE id = %s"
        cur.execute(sql, list(valid_form_data.values()) + [id])

        conn.commit()
        cur.close()

        return _form_success_response(message='Checklist de cumplimiento modificado exitosamente!')
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error editing checklist_cumplimiento {id}: {e}", exc_info=True)
        return _form_error_response(f'Error al editar el checklist: {str(e)}', status=500)
    finally:
        if conn:
            conn.close()


# --- CONFIABILIDAD DE EQUIPOS ---
def _validate_inventario_confiabilidad(inventario_list):
    """Valida que en cada fila de inventario el número de equipos operativos no sea mayor al total."""
    msg = 'El número de equipos operativos no puede ser mayor al total de equipos registrados. Verifique la información ingresada.'
    for row in inventario_list:
        total_raw = row.get('total_equipos')
        func_raw = row.get('equipos_operativos')
        t_val = None
        f_val = None

        if total_raw is not None and str(total_raw).strip() != '':
            try:
                t_val = int(total_raw)
                if t_val < 0:
                    return 'El total de equipos no puede ser un número negativo.'
            except (ValueError, TypeError):
                return 'El total de equipos debe ser un número entero válido.'

        if func_raw is not None and str(func_raw).strip() != '':
            try:
                f_val = int(func_raw)
                if f_val < 0:
                    return 'El número de equipos operativos no puede ser un número negativo.'
            except (ValueError, TypeError):
                return 'El número de equipos operativos debe ser un número entero válido.'

        if t_val is not None and f_val is not None:
            if f_val > t_val:
                return msg
            row['equipos_con_falla'] = str(max(0, t_val - f_val))
        elif t_val is not None and f_val is None:
            row['equipos_con_falla'] = str(t_val)

    return None


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

@forms_bp.route('/submit_confiabilidad_equipos', methods=['GET', 'POST'])
@jwt_required()
def submit_confiabilidad_equipos():
    if request.method == 'GET':
        return redirect(url_for('forms_bp.confiabilidad_equipos_form'))
    import json as _json
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        tz = request.form.get('submitter_timezone')
        fecha = request.form.get('fecha')
        hora = request.form.get('hora')
        fh_to_check = f"{fecha} {hora}" if (fecha and hora) else fecha
        date_err = _validate_not_future(fh_to_check, 'Fecha', tz)
        if date_err:
            return _return_form_error(date_err, 400)

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

        inv_err = _validate_inventario_confiabilidad(inventario_list)
        if inv_err:
            if cur: cur.close()
            if conn: conn.close()
            return _return_form_error(inv_err, 400)

        inventario_json = psycopg2.extras.Json(inventario_list)

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
        return _form_success_response()

    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error submitting confiabilidad_equipos: {e}", exc_info=True)
        app_logger.error(f"Unhandled form error: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor. Por favor intente nuevamente.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/confiabilidad_equipos/<int:id>/editar', methods=['GET'])
@jwt_required()
@admin_required
def confiabilidad_equipos_editar_form(id):
    user_name, is_admin = get_user_info_from_jwt()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT * FROM confiabilidad_equipos WHERE id = %s", (id,))
        record = cur.fetchone()
        cur.close()
        if not record:
            return render_template('error.html', error='Registro no encontrado.'), 404

        return render_template(
            'confiabilidad_equipos.html',
            name=user_name,
            is_admin=is_admin,
            edit_mode=True,
            record_id=id,
            record=dict(record),
            motivos_edicion=MOTIVOS_EDICION,
            **get_service_urls()
        )
    except Exception as e:
        app_logger.error(f"Error loading confiabilidad_equipos {id} for edit: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor.'), 500
    finally:
        if conn:
            conn.close()


@forms_bp.route('/submit_confiabilidad_equipos/<int:id>/editar', methods=['GET', 'POST'])
@jwt_required()
@admin_required
def submit_confiabilidad_equipos_editar(id):
    if request.method == 'GET':
        return redirect(url_for('forms_bp.confiabilidad_equipos_editar_form', id=id))
    identity = get_jwt_identity()
    user_email = identity if isinstance(identity, str) else identity['email']
    conn = None
    try:
        motivo, motivo_detalle, error = _validate_motivo_edicion(request)
        if error:
            return error

        tz = request.form.get('submitter_timezone')
        fecha = request.form.get('fecha')
        hora = request.form.get('hora')
        fh_to_check = f"{fecha} {hora}" if (fecha and hora) else fecha
        date_err = _validate_not_future(fh_to_check, 'Fecha', tz)
        if date_err:
            return _return_form_error(date_err, 400)

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("SELECT * FROM confiabilidad_equipos WHERE id = %s", (id,))
        old_record = cur.fetchone()
        if not old_record:
            cur.close()
            return jsonify({'error': 'Registro no encontrado'}), 404
        old_record = dict(old_record)

        inventario_map = {}
        pattern = re.compile(r'inventario\[(\d+)\]\[(.+)\]')
        for key, value in request.form.items():
            match = pattern.match(key)
            if match:
                idx = int(match.group(1))
                field = match.group(2)
                if idx not in inventario_map:
                    inventario_map[idx] = {}
                inventario_map[idx][field] = value

        inventario_list = []
        for idx in sorted(inventario_map.keys()):
            row = {k: v for k, v in inventario_map[idx].items() if v}
            if row:
                inventario_list.append(row)

        inv_err = _validate_inventario_confiabilidad(inventario_list)
        if inv_err:
            if cur: cur.close()
            if conn: conn.close()
            return _return_form_error(inv_err, 400)

        form_data = {
            'cliente_instalacion': request.form.get('cliente_instalacion'),
            'fecha': request.form.get('fecha') or None,
            'hora': request.form.get('hora') or None,
            'sitio': request.form.get('sitio'),
            'inventario': psycopg2.extras.Json(inventario_list),
            'tecnico_mantenimiento': request.form.get('tecnico_mantenimiento'),
            'firma_tecnico': request.form.get('firma_tecnico'),
            'supervisor_seguridad': request.form.get('supervisor_seguridad'),
            'firma_supervisor': request.form.get('firma_supervisor'),
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

        form_data = _preservar_firmas_existentes(form_data)
        valid_form_data = _filter_existing_columns(cur, 'confiabilidad_equipos', form_data)

        # inventario is JSON — diff against the raw list, not the Json() wrapper
        old_record_for_diff = dict(old_record)
        diff_new_data = dict(valid_form_data)
        diff_new_data['inventario'] = inventario_list
        _record_edicion_historial(
            cur, 'confiabilidad_equipos', id, user_email, motivo, motivo_detalle,
            old_record_for_diff, diff_new_data
        )

        valid_form_data['editado'] = True
        valid_form_data['editado_en'] = datetime.utcnow()
        valid_form_data['editado_por'] = user_email

        set_clause = ', '.join(f"{k} = %s" for k in valid_form_data.keys())
        sql = f"UPDATE confiabilidad_equipos SET {set_clause} WHERE id = %s"
        cur.execute(sql, list(valid_form_data.values()) + [id])

        conn.commit()
        cur.close()

        return _form_success_response()
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error editing confiabilidad_equipos {id}: {e}", exc_info=True)
        return render_template('error.html', error='Error interno del servidor. Por favor intente nuevamente.'), 500
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
        "name": "Kanan SekApp",
        "short_name": "SekApp",
        "description": "Aplicación para completar formularios de Kanan SekApp",
        "start_url": "/forms/select",
        "display": "standalone",
        "background_color": "#1a202c",
        "theme_color": "#2563eb",
        "orientation": "portrait",
        "scope": "/",
        "lang": "es",
        "icons": [
            {
                "src": "/static/img/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable"
            },
            {
                "src": "/static/img/icon-512.png",
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
                "url": "/forms/select",
                "icons": [{"src": "https://storage.googleapis.com/smt-misc/SMT-logo.png", "sizes": "96x96"}]
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
            WHERE id_reporte_incidente = %s AND user_email = %s
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
    error_message = 'Ha ocurrido un error inesperado. Por favor intente nuevamente.'
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
