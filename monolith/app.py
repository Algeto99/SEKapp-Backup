import os
import sys
import logging
from datetime import timedelta
from flask import Flask, jsonify, request, redirect
from flask_jwt_extended import JWTManager
from flask_cors import CORS
from flask_wtf.csrf import CSRFProtect
from flask_bcrypt import Bcrypt
from google.cloud import secretmanager
from google.api_core.exceptions import NotFound
import google.auth.transport.requests
import google.oauth2.id_token
import requests

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
is_production = os.environ.get('K_SERVICE') is not None

# --- Global Configs ---
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'default-flask-secret-key')
app.config['BASE_URL'] = os.environ.get('BASE_URL', '/')

# Since this is a monolith, service URLs generally point back to itself. Keep definitions for backward-compatibility.
base_url_default = 'https://secapp.tzolkintech.com'
app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL', base_url_default)
app.config['LANDING_SERVICE_URL'] = os.environ.get('LANDING_SERVICE_URL', 'https://landing.secapp.tzolkintech.com')
app.config['FORMS_SERVICE_URL'] = os.environ.get('FORMS_SERVICE_URL', 'https://form1.secapp.tzolkintech.com')
app.config['DASHBOARD_SERVICE_URL'] = os.environ.get('DASHBOARD_SERVICE_URL', 'https://dashboard.secapp.tzolkintech.com')
app.config['VIEWER_SERVICE_URL'] = os.environ.get('VIEWER_SERVICE_URL', 'https://viewer.secapp.tzolkintech.com')

app.config['INTERNAL_LOGIN_SERVICE_URL'] = os.environ.get('INTERNAL_LOGIN_SERVICE_URL', 'https://login-24309643178.us-central1.run.app')
app.config['INTERNAL_LANDING_SERVICE_URL'] = os.environ.get('INTERNAL_LANDING_SERVICE_URL', 'https://landing-24309643178.us-central1.run.app')
app.config['INTERNAL_FORMS_SERVICE_URL'] = os.environ.get('INTERNAL_FORMS_SERVICE_URL', 'https://forms-24309643178.us-central1.run.app')
app.config['INTERNAL_DASHBOARD_SERVICE_URL'] = os.environ.get('INTERNAL_DASHBOARD_SERVICE_URL', 'https://dashboard-24309643178.us-central1.run.app')
app.config['INTERNAL_VIEWER_SERVICE_URL'] = os.environ.get('INTERNAL_VIEWER_SERVICE_URL', 'https://viewer-24309643178.us-central1.run.app')

app.config['SMTP_SERVER'] = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', 587))
app.config['SMTP_USE_TLS'] = os.environ.get('SMTP_USE_TLS', 'true').lower() == 'true'
app.config['EMAIL_USERNAME'] = os.environ.get('EMAIL_USERNAME', 'no-reply@tzolkintech.com')
app.config['ADMIN_EMAIL'] = os.environ.get('ADMIN_EMAIL', 'rcanton@tzolkintech.com')
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
    app_logger.warning("Could not find jwt-secret-key, using development default")
    jwt_secret = "dev-secret-key"

app.config['JWT_SECRET_KEY'] = jwt_secret
app.config['JWT_TOKEN_LOCATION'] = ['cookies']
app.config['JWT_ACCESS_COOKIE_PATH'] = '/'
app.config['JWT_COOKIE_SECURE'] = True if is_production else False
app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
app.config['JWT_ACCESS_COOKIE_NAME'] = 'access_token_cookie'
app.config['JWT_COOKIE_CSRF_PROTECT'] = False
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', None)
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)
app.config['PASSWORD_RESET_TOKEN_EXPIRES'] = timedelta(hours=1)

jwt = JWTManager(app)
CORS(app)
csrf = CSRFProtect(app)
bcrypt = Bcrypt(app)

# --- JWT Error Handlers ---
def _is_api_request():
    return '/api/' in request.path

@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_payload):
    if _is_api_request():
        return jsonify({"success": False, "message": "Session expired. Please refresh the page."}), 401
    return redirect('/')

@jwt.invalid_token_loader
def invalid_token_callback(error_string):
    if _is_api_request():
        return jsonify({"success": False, "message": "Invalid session. Please log in again."}), 401
    return redirect('/')

@jwt.unauthorized_loader
def unauthorized_callback(error_string):
    if _is_api_request():
        return jsonify({"success": False, "message": "Authentication required."}), 401
    return redirect('/')

@jwt.revoked_token_loader
def revoked_token_callback(jwt_header, jwt_payload):
    if _is_api_request():
        return jsonify({"success": False, "message": "Session revoked. Please log in again."}), 401
    return redirect('/')

@jwt.needs_fresh_token_loader
def needs_fresh_token_callback(jwt_header, jwt_payload):
    if _is_api_request():
        return jsonify({"success": False, "message": "Fresh authentication required."}), 401
    return redirect('/')

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

# JWT-authenticated blueprints use their own auth — exempt from CSRF
csrf.exempt(viewer_bp)
csrf.exempt(dashboard_bp)
csrf.exempt(expediente_bp)
csrf.exempt(admin_bp)
csrf.exempt(cgeo_bp)

# Inject is_super_admin into every template from the active JWT
@app.context_processor
def inject_super_admin():
    try:
        from flask_jwt_extended import get_jwt, verify_jwt_in_request
        verify_jwt_in_request(optional=True)
        claims = get_jwt()
        return {'is_super_admin': bool(claims.get('is_super_admin', False))}
    except Exception:
        return {'is_super_admin': False}

@app.route('/health')
def health_check():
    return jsonify({"status": "healthy", "service": "monolith"}), 200

@app.errorhandler(404)
def page_not_found(e):
    return redirect('/')

# --- Main Entry Point ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app_logger.info(f"Starting Monolith Flask app on port {port}")
    app.run(host='0.0.0.0', port=port)
