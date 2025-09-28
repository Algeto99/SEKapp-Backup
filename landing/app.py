# landing/app.py (FIXED VERSION - Correct secret handling)

import os
import sys
import logging
import re
from datetime import timedelta, datetime, timezone

from flask import Flask, render_template, request, jsonify, Response, redirect
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity, get_jwt
from flask_cors import CORS
from google.cloud import secretmanager
from google.api_core.exceptions import NotFound

import google.auth.transport.requests
import google.oauth2.id_token
import requests

# --- Configure Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
app_logger = logging.getLogger(__name__)

# --- Initialize Flask App ---
app = Flask(__name__)
is_production = os.environ.get('K_SERVICE') is not None
app_logger.info(f"Starting Landing Service in {'production' if is_production else 'development'} mode")

# --- Secret Manager Client ---
secret_manager_client = secretmanager.SecretManagerServiceClient()

def get_secret_value(secret_name, project_id):
    """Fetches a secret from Google Secret Manager by its name."""
    if not project_id:
        app_logger.error("GCP_PROJECT_ID environment variable is not set.")
        raise ValueError("GCP_PROJECT_ID is not set.")

    secret_path = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    try:
        app_logger.info(f"Attempting to access secret: {secret_path}")
        response = secret_manager_client.access_secret_version(name=secret_path)
        secret_value = response.payload.data.decode('UTF-8')
        app_logger.info(f"Successfully retrieved secret from: {secret_path}")
        return secret_value
    except NotFound:
        app_logger.critical(f"Secret not found at path: {secret_path}")
        raise
    except Exception as e:
        app_logger.critical(f"Error accessing secret '{secret_path}': {str(e)}", exc_info=True)
        raise

# --- Configuration Functions ---
def configure_app():
    app_logger.info("Configuring Flask application...")
    app.config['GCP_PROJECT_ID'] = os.environ.get('GCP_PROJECT_ID', 'tz-dev-secapp')
    
    if not app.config['GCP_PROJECT_ID']:
        app_logger.critical("GCP_PROJECT_ID not set.")
        raise ValueError("GCP_PROJECT_ID not set.")

    # External URLs for user redirects
    app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com')
    app.config['FORMS_SERVICE_URL'] = os.environ.get('FORMS_SERVICE_URL', 'https://form1.secapp.tzolkintech.com')
    app.config['DASHBOARD_SERVICE_URL'] = os.environ.get('DASHBOARD_SERVICE_URL')
    app.config['VIEWER_SERVICE_URL'] = os.environ.get('VIEWER_SERVICE_URL', 'https://viewer.secapp.tzolkintech.com')

    # Internal URLs for cost-free service-to-service communication
    app.config['INTERNAL_LOGIN_SERVICE_URL'] = os.environ.get('INTERNAL_LOGIN_SERVICE_URL', 'https://login-24309643178.us-central1.run.app')
    app.config['INTERNAL_FORMS_SERVICE_URL'] = os.environ.get('INTERNAL_FORMS_SERVICE_URL', 'https://forms-24309643178.us-central1.run.app')
    app.config['INTERNAL_DASHBOARD_SERVICE_URL'] = os.environ.get('INTERNAL_DASHBOARD_SERVICE_URL', 'https://dashboard-24309643178.us-central1.run.app')
    app.config['INTERNAL_VIEWER_SERVICE_URL'] = os.environ.get('INTERNAL_VIEWER_SERVICE_URL', 'https://viewer-24309643178.us-central1.run.app')

    # JWT configurations
    app.config['JWT_TOKEN_LOCATION'] = ['cookies']
    app.config['JWT_COOKIE_SECURE'] = is_production
    app.config['JWT_COOKIE_CSRF_PROTECT'] = True
    app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(minutes=15)
    app.config['JWT_COOKIE_NAME'] = 'access_token_cookie'

    app_logger.info("Application configuration loaded.")

def setup_jwt_secret():
    """
    Fetches JWT_SECRET_KEY and SECRET_KEY.
    Prioritizes direct environment variables over fetching from Secret Manager.
    """
    app_logger.info("Starting secret loading process...")
    
    # --- Load JWT_SECRET_KEY ---
    jwt_secret = os.environ.get('JWT_SECRET_KEY')
    if jwt_secret:
        app.config['JWT_SECRET_KEY'] = jwt_secret
        app_logger.info("Loaded JWT_SECRET_KEY directly from environment variable.")
    else:
        app_logger.info("JWT_SECRET_KEY not in environment. Fetching from Secret Manager...")
        try:
            app.config['JWT_SECRET_KEY'] = get_secret_value('jwt-secret-key', app.config['GCP_PROJECT_ID'])
            app_logger.info("JWT_SECRET_KEY loaded successfully from Secret Manager.")
        except Exception as e:
            app_logger.critical(f"Failed to load JWT_SECRET_KEY from Secret Manager: {str(e)}", exc_info=True)
            raise

    # --- Load FLASK_SECRET_KEY (for Flask session signing) ---
    flask_secret = os.environ.get('FLASK_SECRET_KEY')
    if flask_secret:
        app.config['SECRET_KEY'] = flask_secret
        app_logger.info("Loaded FLASK_SECRET_KEY directly from environment variable.")
    else:
        app_logger.info("FLASK_SECRET_KEY not in environment. Fetching from Secret Manager...")
        try:
            # Note: The secret name is 'forms-flask-secret-key' as per your original code
            app.config['SECRET_KEY'] = get_secret_value('forms-flask-secret-key', app.config['GCP_PROJECT_ID'])
            app_logger.info("SECRET_KEY loaded successfully from Secret Manager.")
        except Exception as e:
            app_logger.critical(f"Failed to load SECRET_KEY from Secret Manager: {str(e)}", exc_info=True)
            raise

def setup_cors():
    """Configures CORS for the Flask app."""
    CORS(app, supports_credentials=True, resources={r"/*": {"origins": "*"}})
    app_logger.info("CORS initialized.")

# --- CloudRunServiceClient (No changes needed here) ---
class CloudRunServiceClient:
    def __init__(self, internal_service_url, external_service_url=None):
        self.internal_service_url = internal_service_url.rstrip('/')
        self.external_service_url = external_service_url.rstrip('/') if external_service_url else None
        self.request_adapter = google.auth.transport.requests.Request()
        app_logger.info(f"CloudRunServiceClient initialized - Internal: {self.internal_service_url}")

    def _get_id_token(self, target_audience=None):
        try:
            audience = target_audience or self.internal_service_url
            return google.oauth2.id_token.fetch_id_token(self.request_adapter, audience)
        except Exception as e:
            app_logger.error(f"Failed to fetch ID token for {audience}: {e}", exc_info=True)
            raise

    def make_authenticated_request(self, path, method='GET', json_data=None, use_internal=True):
        if use_internal:
            target_url = f"{self.internal_service_url}{path}"
            try:
                id_token = self._get_id_token(self.internal_service_url)
                headers = {'Authorization': f'Bearer {id_token}', 'Content-Type': 'application/json'} if json_data else {'Authorization': f'Bearer {id_token}'}
            except Exception as e:
                app_logger.error(f"Failed to get auth token for internal call: {e}")
                return None
        else:
            if not self.external_service_url:
                app_logger.error("External URL not configured")
                return None
            target_url = f"{self.external_service_url}{path}"
            headers = {'Content-Type': 'application/json'} if json_data else {}

        try:
            response = requests.request(method, target_url, headers=headers, json=json_data)
            response.raise_for_status()
            app_logger.info(f"Request to {target_url} successful. Status: {response.status_code}")
            return response
        except Exception as e:
            app_logger.error(f"Request to {target_url} failed: {e}", exc_info=True)
            return None

# Initialize JWTManager
jwt = JWTManager()
login_service_client = None

# --- JWT Error Handlers (No changes needed here) ---
@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_payload):
    user_email = jwt_payload.get('sub', 'unknown')
    app_logger.info(f"JWT token expired for user {user_email}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.invalid_token_loader
def invalid_token_callback(error_string):
    app_logger.info(f"Invalid JWT token encountered: {error_string}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.unauthorized_loader
def unauthorized_callback(error_string):
    app_logger.info(f"No JWT token found: {error_string}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.revoked_token_loader
def revoked_token_callback(jwt_header, jwt_payload):
    user_email = jwt_payload.get('sub', 'unknown')
    app_logger.info(f"Revoked JWT token for user {user_email}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.needs_fresh_token_loader
def needs_fresh_token_callback(jwt_header, jwt_payload):
    user_email = jwt_payload.get('sub', 'unknown')
    app_logger.info(f"Fresh token required for user {user_email}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

# --- JWT Callbacks (No changes needed here) ---
@jwt.user_identity_loader
def user_identity_lookup(jwt_payload):
    return jwt_payload["sub"]

@jwt.user_lookup_loader
def user_lookup_callback(_jwt_header, jwt_data):
    identity = jwt_data["sub"]
    return {"email": identity, "name": jwt_data.get("user_name", identity)}

# --- Routes (No changes needed here) ---
@app.route('/')
@jwt_required(optional=True)
def index():
    user_email = None
    user_name = None
    is_admin = False
    
    try:
        claims = get_jwt()
        if claims:
            user_email = claims.get('sub')
            user_name = claims.get('name', user_email)
            is_admin = claims.get('is_admin', False)
        else:
            app_logger.info("No valid JWT claims found; user not logged in.")
    except Exception as e:
        app_logger.warning(f"Could not get JWT identity: {e}")
    
    return render_template(
        'index.html',
        user_email=user_email,
        user_name=user_name,
        is_admin=is_admin,
        FORMS_SERVICE_URL=app.config.get('FORMS_SERVICE_URL'),
        LOGIN_SERVICE_URL=app.config.get('LOGIN_SERVICE_URL'),
        DASHBOARD_SERVICE_URL=app.config.get('DASHBOARD_SERVICE_URL'),
        VIEWER_SERVICE_URL=app.config.get('VIEWER_SERVICE_URL')
    )

@app.route('/user_info', methods=['GET'])
@jwt_required()
def user_info():
    try:
        claims = get_jwt()
        user_email = claims.get('sub')
        user_name = claims.get('name', user_email)
        is_admin = claims.get('is_admin', False)
        
        if user_email:
            app_logger.info(f"User info requested for: {user_email} (admin: {is_admin})")
            return jsonify({
                "email": user_email,
                "name": user_name,
                "is_admin": is_admin,
                "roles": ["admin"] if is_admin else ["user"]
            }), 200
        return jsonify({"msg": "Unauthorized: No valid user identity"}), 401
    except Exception as e:
        app_logger.error(f"Error fetching user info: {e}", exc_info=True)
        return jsonify({"msg": "Internal server error"}), 500
    
@app.route('/health')
def health_check():
    return "OK", 200

# --- Error Handlers (No changes needed here) ---
@app.errorhandler(404)
def not_found_error(error):
    app_logger.warning(f"404 Not Found: {request.path}")
    return jsonify({"error": "Not Found"}), 404

@app.errorhandler(500)
def internal_error(error):
    app_logger.error(f"Internal server error: {error}", exc_info=True)
    return jsonify({"error": "Internal Server Error"}), 500

# --- Application Initialization ---
try:
    with app.app_context():
        app_logger.info("Starting application initialization...")
        app_logger.info("Step 1: Configuring app...")
        configure_app()
        app_logger.info("Step 2: Setting up secrets...")
        setup_jwt_secret()
        app_logger.info("Step 3: Setting up CORS...")
        setup_cors()
        app_logger.info("Step 4: Initializing JWTManager...")
        jwt.init_app(app)
        app_logger.info("Step 5: Initializing CloudRunServiceClient...")
        
        login_service_url = app.config.get('LOGIN_SERVICE_URL')
        internal_login_url = app.config.get('INTERNAL_LOGIN_SERVICE_URL')

        if internal_login_url:
            login_service_client = CloudRunServiceClient(
                internal_service_url=internal_login_url,
                external_service_url=login_service_url
            )
            app_logger.info("CloudRunServiceClient initialized with internal URL for login service.")
        elif login_service_url:
            app_logger.warning("Internal login URL not configured, using external URL (higher cost)")
            login_service_client = CloudRunServiceClient(
                internal_service_url=login_service_url,
                external_service_url=login_service_url
            )
        else:
            app_logger.warning("LOGIN_SERVICE_URL not set.")
            login_service_client = None
    
    app_logger.info("Application initialization complete.")
except Exception as e:
    app_logger.critical(f"FATAL ERROR during initialization: {str(e)}", exc_info=True)
    # Re-raise the exception to ensure the Gunicorn worker fails to boot,
    # which is the behavior seen in the logs.
    raise

# --- Main Entry Point ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app_logger.info(f"Starting Flask app on port {port}, debug={not is_production}")
    app.run(host='0.0.0.0', port=port, debug=not is_production)