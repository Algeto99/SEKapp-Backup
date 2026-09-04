import os
import re
import sys
import logging
from datetime import timedelta
from urllib.parse import quote, urlparse
from flask import Flask, jsonify, request, redirect, flash, url_for
from flask_jwt_extended import JWTManager
from flask_cors import CORS
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_bcrypt import Bcrypt
from extensions import limiter
from google.cloud import secretmanager
from google.api_core.exceptions import NotFound

# --- Blueprints ---
from login_bp import login_bp, init_login_bp
from landing_bp import landing_bp
from dashboard_bp import dashboard_bp
from forms_bp import forms_bp
from viewer_bp import viewer_bp
from expediente_bp import expediente_bp
from admin_bp import admin_bp, init_admin_bp
from cgeo_bp import cgeo_bp
from matrices_bp import matrices_bp

# --- Configure Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
app_logger = logging.getLogger(__name__)

# --- Initialize Monolith Flask App ---
app = Flask(__name__)
# Flask ordena las claves del JSON alfabéticamente por defecto. Los registros se
# arman siguiendo el `data_mapping` de cada formulario, que es el orden en que se
# diligencian; ordenarlas alfabéticamente lo destruía y hacía que la vista previa
# de Reportes mostrara los primeros del abecedario en vez de los del encabezado.
app.json.sort_keys = False
is_production = os.environ.get('K_SERVICE') is not None

# --- Global Configs ---
_flask_secret = os.environ.get('FLASK_SECRET_KEY')
if not _flask_secret:
    if is_production:
        raise RuntimeError("FLASK_SECRET_KEY must be set in production")
    _flask_secret = 'default-flask-secret-key'
app.config['SECRET_KEY'] = _flask_secret
app.config['BASE_URL'] = os.environ.get('BASE_URL', '/')

# Since this is a monolith, service URLs generally point back to itself. Keep definitions for backward-compatibility.
base_url_default = 'https://secapp.tzolkintech.com'
app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL', base_url_default)
app.config['LANDING_SERVICE_URL'] = os.environ.get('LANDING_SERVICE_URL', 'https://landing.secapp.tzolkintech.com')
app.config['FORMS_SERVICE_URL'] = os.environ.get('FORMS_SERVICE_URL', 'https://form1.secapp.tzolkintech.com')
app.config['DASHBOARD_SERVICE_URL'] = os.environ.get('DASHBOARD_SERVICE_URL', 'https://dashboard.secapp.tzolkintech.com')
app.config['VIEWER_SERVICE_URL'] = os.environ.get('VIEWER_SERVICE_URL', 'https://viewer.secapp.tzolkintech.com')


app.config['SMTP_SERVER'] = os.environ.get('SMTP_SERVER', 'kanansentinel.com')
app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', 465))
app.config['SMTP_USE_TLS'] = os.environ.get('SMTP_USE_TLS', 'true').lower() == 'true'
app.config['EMAIL_USERNAME'] = os.environ.get('EMAIL_USERNAME', 'no-reply@kanansentinel.com')
app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL', 'no-reply@kanansentinel.com')
app.config['GCP_PROJECT_ID'] = os.environ.get('GCP_PROJECT_ID', os.environ.get('GOOGLE_CLOUD_PROJECT', 'tz-dev-secapp'))
app.config['EMAIL_PASSWORD_SECRET_NAME'] = os.environ.get('EMAIL_PASSWORD_SECRET', 'admin-email-pass')

# --- Secret Manager Setup ---
try:
    secret_manager_client = secretmanager.SecretManagerServiceClient()
except Exception as e:
    app_logger.warning(f"Could not initialize Secret Manager client (maybe local without ADC): {e}")
    secret_manager_client = None

def is_full_secret_path(s, project_id):
    if not s or not project_id:
        return False
    return s.startswith(f"projects/{project_id}/secrets/") and "/versions/" in s

def get_secret(project_id, secret_name_or_path):
    if not secret_manager_client:
        return None
    try:
        if is_full_secret_path(secret_name_or_path, project_id):
            name = secret_name_or_path
        else:
            name = f"projects/{project_id}/secrets/{secret_name_or_path}/versions/latest"
        response = secret_manager_client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except NotFound:
        app_logger.error(f"Secret '{secret_name_or_path}' not found in project '{project_id}'.")
        return None
    except Exception as e:
        app_logger.error(f"Error accessing secret '{secret_name_or_path}': {e}", exc_info=True)
        return None

project_id = app.config['GCP_PROJECT_ID']

# Get JWT Secret
jwt_secret = os.environ.get('JWT_SECRET_KEY')
if not jwt_secret and project_id:
    jwt_secret = get_secret(project_id, 'jwt-secret-key')

if not jwt_secret:
    if is_production:
        raise RuntimeError("JWT secret could not be loaded from environment or Secret Manager — aborting")
    app_logger.warning("Could not find jwt-secret-key, using development default")
    jwt_secret = "dev-secret-key"

app.config['JWT_SECRET_KEY'] = jwt_secret
app.config['JWT_TOKEN_LOCATION'] = ['cookies']
app.config['JWT_ACCESS_COOKIE_PATH'] = '/'
app.config['JWT_COOKIE_SECURE'] = True if is_production else False
app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
app.config['JWT_ACCESS_COOKIE_NAME'] = 'access_token_cookie'
app.config['JWT_COOKIE_CSRF_PROTECT'] = True
app.config['JWT_CSRF_CHECK_FORM'] = True
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', None)
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)
app.config['PASSWORD_RESET_TOKEN_EXPIRES'] = timedelta(hours=1)

jwt = JWTManager(app)
limiter.init_app(app)
_allowed_origins = [o.strip() for o in os.environ.get(
    'ALLOWED_ORIGINS',
    'https://secapp.tzolkintech.com'
).split(',') if o.strip()]
CORS(app, origins=_allowed_origins, supports_credentials=True)
csrf = CSRFProtect(app)
bcrypt = Bcrypt(app)

# --- JWT Error Handlers & Navigation Helpers ---
_SUBMIT_TO_FORM_MAP = {
    'incident_report': 'reporte_incidente',
    'medicion_experiencia_cliente': 'medicion_experiencia_cliente',
    'supervision_puesto': 'supervision_puesto',
    'informe_novedades_disciplinario': 'informe_novedades_disciplinario',
    'log_de_patrullas': 'log_de_patrullas',
    'asistencia_qr': 'asistencia_qr',
    'registro_de_capacitaciones': 'registro_de_capacitaciones',
    'registro_y_acta_de_visita': 'registro_y_acta_de_visita',
    'planilla_vehicular': 'planilla_vehicular',
    'planilla_motocicletas': 'planilla_motocicletas',
    'checklist_cumplimiento': 'checklist_cumplimiento',
    'confiabilidad_equipos': 'confiabilidad_equipos',
}

def _is_api_request():
    """True for requests that must receive JSON (never an HTML redirect).

    AJAX form submissions belong here: a fetch() follows the 302 to the login page
    and lands on 200 HTML, so response.ok is true and the client mistakes an expired
    session for a successful save, discarding the user's typed data. Returning 401
    JSON lets the form show its "tu sesion expiro, tus datos NO se han perdido" banner."""
    return (
        '/api/' in request.path
        or request.headers.get('X-SecApp-Replay') == '1'
        or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    )

def _map_to_safe_get_url(path, referrer=None, fallback='/landing/'):
    """Maps a requested path (including POST endpoints) to a valid GET navigation destination,
    preventing 405 Method Not Allowed when following redirects after login."""
    if not path:
        return fallback

    parsed = urlparse(path)
    clean_path = parsed.path.rstrip('/')

    # Discard pure API routes, logout, or root
    if '/api/' in clean_path or clean_path.endswith('/logout') or clean_path in ('', '/'):
        if referrer:
            ref_parsed = urlparse(referrer)
            ref_path = ref_parsed.path.rstrip('/')
            if ref_path and not ref_path.startswith('/forms/submit_') and '/api/' not in ref_path and ref_path != '/login':
                return ref_path + (f"?{ref_parsed.query}" if ref_parsed.query else "")
        return fallback

    # If it's a form submit route, map it back to the corresponding GET form route
    if clean_path.startswith('/forms/submit_'):
        # Check edit mode: /forms/submit_<name>/<id>/editar
        edit_match = re.match(r'^/forms/submit_([a-zA-Z0-9_]+)/(\d+)/editar$', clean_path)
        if edit_match:
            form_key, rec_id = edit_match.groups()
            form_route = _SUBMIT_TO_FORM_MAP.get(form_key, form_key)
            return f"/forms/{form_route}/{rec_id}/editar"

        # Check standard submit: /forms/submit_<name>
        std_match = re.match(r'^/forms/submit_([a-zA-Z0-9_]+)$', clean_path)
        if std_match:
            form_key = std_match.group(1)
            form_route = _SUBMIT_TO_FORM_MAP.get(form_key, form_key)
            return f"/forms/{form_route}"

    if clean_path.startswith('/admin/users/') or clean_path.startswith('/admin/companies/'):
        return '/admin/'

    return path

def _redirect_to_login(message="Tu sesión ha expirado por inactividad. Por favor, inicia sesión nuevamente para continuar."):
    """Redirect to login preserving the original destination (sanitized to safe GET) as
    ?next=, so login_bp.py can send the user back where they were headed after
    authenticating without triggering 405 Method Not Allowed."""
    dest = request.full_path if request.query_string else request.path
    if dest.endswith('?'):
        dest = dest[:-1]

    safe_dest = _map_to_safe_get_url(dest, referrer=request.referrer, fallback=None)

    if message:
        flash(message, "warning")

    if safe_dest and safe_dest not in ('/', '/login', '/landing/', '/landing'):
        return redirect(f"/?next={quote(safe_dest, safe='')}")
    return redirect("/")

@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_payload):
    if _is_api_request():
        return jsonify({"success": False, "message": "Tu sesión ha expirado. Por favor recarga la página o inicia sesión."}), 401
    return _redirect_to_login(message="Tu sesión ha expirado por inactividad. Inicia sesión nuevamente para continuar.")

@jwt.invalid_token_loader
def invalid_token_callback(error_string):
    if _is_api_request():
        return jsonify({"success": False, "message": "Sesión inválida. Por favor inicia sesión nuevamente."}), 401
    return _redirect_to_login(message="Tu sesión no es válida. Por favor inicia sesión nuevamente.")

@jwt.unauthorized_loader
def unauthorized_callback(error_string):
    if _is_api_request():
        return jsonify({"success": False, "message": "Autenticación requerida."}), 401
    return _redirect_to_login(message="Debes iniciar sesión para acceder a esta página.")

@jwt.revoked_token_loader
def revoked_token_callback(jwt_header, jwt_payload):
    if _is_api_request():
        return jsonify({"success": False, "message": "Sesión cerrada. Por favor inicia sesión nuevamente."}), 401
    return _redirect_to_login(message="Tu sesión fue cerrada. Por favor inicia sesión nuevamente.")

@jwt.needs_fresh_token_loader
def needs_fresh_token_callback(jwt_header, jwt_payload):
    if _is_api_request():
        return jsonify({"success": False, "message": "Autenticación reciente requerida."}), 401
    return _redirect_to_login(message="Por seguridad, debes volver a identificarte.")

# --- Mount Applications/Blueprints Here ---
# Prefixing them is important to prevent route collisions.
init_login_bp(bcrypt)
init_admin_bp(bcrypt)
app.register_blueprint(login_bp, url_prefix='') # Login mounts at root /
app.register_blueprint(landing_bp, url_prefix='/landing')
app.register_blueprint(dashboard_bp, url_prefix='/dashboard')
app.register_blueprint(forms_bp, url_prefix='/forms')
app.register_blueprint(viewer_bp, url_prefix='/viewer')
app.register_blueprint(expediente_bp, url_prefix='')
app.register_blueprint(admin_bp, url_prefix='/admin')
app.register_blueprint(cgeo_bp, url_prefix='/cgeo')
app.register_blueprint(matrices_bp, url_prefix='/matrices')

# JSON API blueprints — all routes are JWT-authenticated fetch calls, not browser forms.
# JWT_COOKIE_CSRF_PROTECT=True provides double-submit protection for the JWT cookie itself.
csrf.exempt(viewer_bp)
csrf.exempt(dashboard_bp)
csrf.exempt(expediente_bp)
csrf.exempt(cgeo_bp)
csrf.exempt(admin_bp)  # JWT double-submit cookie (JWT_COOKIE_CSRF_PROTECT) covers admin forms

# forms_bp: exempt only the specific routes that don't use Flask-WTF CSRF tokens.
# All @jwt_required() submit routes are protected by JWT_COOKIE_CSRF_PROTECT instead.
# The two public QR routes have no auth at all — they are explicitly exempt.
_FORMS_EXEMPT_ENDPOINTS = [
    # Public unauthenticated attendance form (session_token in URL acts as CSRF nonce)
    'forms_bp.asistencia_qr_form',
    'forms_bp.submit_asistencia_qr',
    # JWT-authenticated PWA submit routes — protected via JWT double-submit cookie
    'forms_bp.submit_incident_report',
    'forms_bp.submit_medicion_experiencia_cliente',
    'forms_bp.submit_supervision_puesto',
    'forms_bp.submit_informe_novedades_disciplinario',
    'forms_bp.submit_log_de_patrullas',
    'forms_bp.submit_registro_de_capacitaciones',
    'forms_bp.submit_registro_y_acta_de_visita',
    'forms_bp.submit_planilla_vehicular',
    'forms_bp.submit_planilla_motocicletas',
    'forms_bp.submit_checklist_cumplimiento',
    'forms_bp.submit_confiabilidad_equipos',
    # JWT-authenticated admin edit routes — protected via JWT double-submit cookie
    'forms_bp.submit_incident_report_editar',
    'forms_bp.submit_medicion_experiencia_cliente_editar',
    'forms_bp.submit_supervision_puesto_editar',
    'forms_bp.submit_informe_novedades_disciplinario_editar',
    'forms_bp.submit_log_de_patrullas_editar',
    'forms_bp.submit_registro_de_capacitaciones_editar',
    'forms_bp.submit_registro_y_acta_de_visita_editar',
    'forms_bp.submit_planilla_vehicular_editar',
    'forms_bp.submit_planilla_motocicletas_editar',
    'forms_bp.submit_checklist_cumplimiento_editar',
    'forms_bp.submit_confiabilidad_equipos_editar',
    # JWT-authenticated API endpoints
    'forms_bp.get_csrf_token',
    'forms_bp.get_my_reports',
    'forms_bp.get_my_report_details',
    'forms_bp.api_properties',
    'forms_bp.api_fleet',
    'forms_bp.api_fleet_ultimo_km',
    'forms_bp.customer_hierarchy',
]
for _endpoint in _FORMS_EXEMPT_ENDPOINTS:
    _view = app.view_functions.get(_endpoint)
    if _view:
        csrf.exempt(_view)

# Inject is_super_admin, enabled_modules and the JWT CSRF token into every template from the active JWT.
# jwt_csrf_token is the value of the csrf_access_token cookie — use it in hidden form fields
# for any server-side form POST that sits behind @jwt_required() instead of {{ csrf_token() }}.
# enabled_modules is the set of optional-module keys (e.g. 'log_de_patrullas') enabled for the
# current user's company license — resolved here so every template can gate UI on it without
# each blueprint having to pass it into render_template explicitly.
@app.context_processor
def inject_super_admin():
    try:
        from flask_jwt_extended import get_jwt, get_jwt_identity, verify_jwt_in_request
        verify_jwt_in_request(optional=True)
        claims = get_jwt()
        is_sa = bool(claims.get('is_super_admin', False))
        email = get_jwt_identity()
    except Exception:
        is_sa = False
        email = None

    enabled_modules = set()
    company_name = ''
    if email:
        try:
            from db import get_db_connection
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT c.enabled_modules
                        FROM users u
                        JOIN companies c ON c.id = u.company_id
                        WHERE u.email = %s
                    """, (email,))
                    row = cur.fetchone()
                    if row and row[0]:
                        enabled_modules = set(row[0])

                    # Nombre del tenant, para las vistas que no tienen nada que
                    # elegir y solo muestran a quien pertenecen los datos. Va en
                    # consulta aparte a proposito: el COALESCE cae a la unica
                    # empresa cuando el usuario tiene company_id NULL, y meterlo
                    # en la consulta de arriba cambiaria que modulos ve.
                    cur.execute("""
                        SELECT name
                        FROM companies
                        WHERE id = COALESCE(
                            (SELECT company_id FROM users WHERE email = %s),
                            (SELECT MIN(id) FROM companies)
                        )
                    """, (email,))
                    crow = cur.fetchone()
                    if crow and crow[0]:
                        company_name = crow[0]
                    cur.close()
                finally:
                    conn.close()
        except Exception:
            enabled_modules = set()
            company_name = ''

    jwt_csrf = request.cookies.get('csrf_access_token', '')
    return {'is_super_admin': is_sa, 'jwt_csrf_token': jwt_csrf,
            'enabled_modules': enabled_modules, 'company_name': company_name}

@app.route('/health')
def health_check():
    return jsonify({"status": "healthy", "service": "monolith"}), 200

@app.errorhandler(404)
def page_not_found(e):
    # Un fetch() sigue el redirect automáticamente y termina recibiendo el HTML de
    # la página raíz con status 200, así que response.ok es true y response.json()
    # revienta con "Unexpected token '<'" — ocultando el 404 real. Las peticiones
    # de API deben recibir un 404 en JSON; solo la navegación del navegador se redirige.
    if _is_api_request():
        return jsonify({"success": False, "message": "Recurso no encontrado."}), 404
    return redirect('/')

@app.errorhandler(405)
def method_not_allowed(e):
    app_logger.warning(f"405 Method Not Allowed on {request.method} {request.path}")
    if _is_api_request():
        return jsonify({"success": False, "message": "Método no permitido para la URL solicitada."}), 405

    # If the user or browser refreshed/navigated to a form submit URL or form route via an unexpected method, redirect gracefully
    if request.path.startswith('/forms/'):
        safe_url = _map_to_safe_get_url(request.path, fallback='/forms/select_form')
        return redirect(safe_url)

    # For general web navigation with 405, redirect to landing or login
    token_present = bool(request.cookies.get('access_token_cookie'))
    return redirect(url_for('landing_bp.landing_page') if token_present else '/')

@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    app_logger.warning(f"CSRF Error: {e.description} on {request.method} {request.path}")
    if _is_api_request():
        return jsonify({"success": False, "message": "Token de seguridad inválido o expirado. Por favor recarga la página."}), 400
    flash("Tu sesión o token de seguridad expiró por inactividad. Por favor intenta de nuevo.", "warning")
    if request.referrer and not request.referrer.endswith('/login') and not request.referrer.endswith('/'):
        return redirect(request.referrer)
    return redirect('/')

# --- Main Entry Point ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app_logger.info(f"Starting Monolith Flask app on port {port}")
    app.run(host='0.0.0.0', port=port)
