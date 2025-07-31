# landing/app.py (REVISED for tz-dev-secapp with JWT redirect handlers)

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

# Helper to determine if a string is a full secret path
def is_full_secret_path(s, project_id):
    """
    Checks if a given string is a well-formed full Google Secret Manager path.
    This helps differentiate between a secret name and a full path.
    """
    if not s or not project_id:
        return False
    # A full path should start with projects/{project_id}/secrets/ and end with /versions/latest
    # The regex ensures the structure is correct, including non-empty segments.
    return s.startswith(f"projects/{project_id}/secrets/") and \
           re.match(r'^projects/[^/]+/secrets/[^/]+/versions/[^/]+$', s)

def get_secret_value(secret_identifier, project_id):
    """
    Fetches a secret from Google Secret Manager.
    secret_identifier can be either the secret name (e.g., "my-secret")
    or the full secret path (e.g., "projects/PROJECT_ID/secrets/my-secret/versions/latest").
    """
    if not project_id:
        app_logger.error("GCP_PROJECT_ID environment variable is not set.")
        raise ValueError("GCP_PROJECT_ID is not set.")

    secret_path = ""
    if is_full_secret_path(secret_identifier, project_id):
        # If the identifier is already a full path, use it directly
        secret_path = secret_identifier
        app_logger.info(f"Secret identifier '{secret_identifier}' is a full path. Using it directly.")
    else:
        # Otherwise, assume the identifier is just the secret name and construct the full path
        secret_name = secret_identifier
        if not secret_name:
            app_logger.error("Secret name provided is empty.")
            raise ValueError("Secret name is empty.")
        
        # Basic validation for secret name: ensure it doesn't contain path separators
        # if it's supposed to be just a name. This is a warning, not an error,
        # to allow for some flexibility but flag potential misconfigurations.
        if '/' in secret_name or '\\' in secret_name:
            app_logger.warning(
                f"Secret identifier '{secret_name}' appears to contain path separators "
                f"but is not a full secret path. Assuming it's a secret name and constructing path."
            )
        
        secret_path = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        app_logger.info(f"Secret identifier '{secret_identifier}' is a name. Constructed path: {secret_path}")

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
    app_logger.info(f"Raw GCP_PROJECT_ID env: {os.environ.get('GCP_PROJECT_ID')}")
    if not app.config['GCP_PROJECT_ID']:
        app_logger.critical("GCP_PROJECT_ID not set.")
        raise ValueError("GCP_PROJECT_ID not set.")

    # These environment variables should ideally contain just the secret name (e.g., "jwt-secret-key").
    # The get_secret_value function is now robust enough to handle full paths if provided,
    # but it's best practice to provide just the name in the environment variable.
    app.config['JWT_SECRET_KEY_IDENTIFIER'] = os.environ.get(
        'JWT_SECRET_KEY',
        'jwt-secret-key' # Default to just the name if env var is not set
    )
    app.config['FLASK_SECRET_KEY_IDENTIFIER'] = os.environ.get(
        'FLASK_SECRET_KEY',
        'forms-flask-secret-key' # Default to just the name if env var is not set
    )

    app_logger.info(f"Raw JWT_SECRET_KEY env: {os.environ.get('JWT_SECRET_KEY')}")
    app_logger.info(f"Raw FLASK_SECRET_KEY env: {os.environ.get('FLASK_SECRET_KEY')}")
    app_logger.info(f"JWT_SECRET_KEY_IDENTIFIER (after config): {app.config['JWT_SECRET_KEY_IDENTIFIER']}")
    app_logger.info(f"FLASK_SECRET_KEY_IDENTIFIER (after config): {app.config['FLASK_SECRET_KEY_IDENTIFIER']}")

    app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com')
    app_logger.info(f"Raw LOGIN_SERVICE_URL env: {os.environ.get('LOGIN_SERVICE_URL')}")

    app.config['FORMS_SERVICE_URL'] = os.environ.get('FORMS_SERVICE_URL', 'https://form1.secapp.tzolkintech.com')
    app_logger.info(f"Raw FORMS_SERVICE_URL env: {os.environ.get('FORMS_SERVICE_URL')}")

    app.config['DASHBOARD_SERVICE_URL'] = os.environ.get('DASHBOARD_SERVICE_URL')
    app_logger.info(f"Raw DASHBOARD_SERVICE_URL env: {os.environ.get('DASHBOARD_SERVICE_URL')}")

    # Add the VIEWER_SERVICE_URL configuration
    app.config['VIEWER_SERVICE_URL'] = os.environ.get('VIEWER_SERVICE_URL', 'https://viewer.secapp.tzolkintech.com')
    app_logger.info(f"Raw VIEWER_SERVICE_URL env: {os.environ.get('VIEWER_SERVICE_URL')}")


    app.config['JWT_TOKEN_LOCATION'] = ['cookies']
    app.config['JWT_COOKIE_SECURE'] = is_production
    app.config['JWT_COOKIE_CSRF_PROTECT'] = True
    app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(minutes=15)
    app.config['JWT_COOKIE_NAME'] = 'access_token_cookie'  # Ensure cookie name matches login service

    app_logger.info("Application configuration loaded.")

def setup_jwt_secret():
    """Fetches JWT_SECRET and SECRET_KEY from Secret Manager."""
    app_logger.info("Starting secret loading process...")
    try:
        # Fetch JWT secret using the identifier (name or full path)
        jwt_secret_identifier = app.config['JWT_SECRET_KEY_IDENTIFIER']
        app_logger.info(f"Attempting to fetch JWT secret using identifier: {jwt_secret_identifier}")
        try:
            app.config['JWT_SECRET'] = get_secret_value(jwt_secret_identifier, app.config['GCP_PROJECT_ID'])
            app_logger.info("JWT_SECRET loaded successfully into app config.")
        except Exception as e:
            app_logger.critical(f"Failed to load JWT_SECRET: {str(e)}", exc_info=True)
            raise

        # Fetch Flask secret using the identifier (name or full path)
        flask_secret_identifier = app.config['FLASK_SECRET_KEY_IDENTIFIER']
        app_logger.info(f"Attempting to fetch Flask secret using identifier: {flask_secret_identifier}")
        try:
            app.config['SECRET_KEY'] = get_secret_value(flask_secret_identifier, app.config['GCP_PROJECT_ID'])
            app_logger.info("SECRET_KEY loaded successfully into app config.")
        except Exception as e:
            app_logger.critical(f"Failed to load SECRET_KEY: {str(e)}", exc_info=True)
            raise
    except Exception as e:
        app_logger.critical(f"Failed to load critical secrets during setup: {str(e)}", exc_info=True)
        raise RuntimeError(f"Critical secrets could not be loaded: {str(e)}")

def setup_cors():
    """Configures CORS for the Flask app."""
    CORS(app, supports_credentials=True, resources={r"/*": {"origins": "*"}})
    app_logger.info("CORS initialized.")

# --- CloudRunServiceClient ---
class CloudRunServiceClient:
    def __init__(self, service_url):
        self.service_url = service_url.rstrip('/')
        self.request_adapter = google.auth.transport.requests.Request()
        app_logger.info(f"CloudRunServiceClient initialized for: {self.service_url}")

    def _get_id_token(self):
        try:
            return google.oauth2.id_token.fetch_id_token(self.request_adapter, self.service_url)
        except Exception as e:
            app_logger.error(f"Failed to fetch ID token for {self.service_url}: {e}", exc_info=True)
            raise

    def make_authenticated_request(self, path, method='GET', json_data=None):
        target_url = f"{self.service_url}{path}"
        try:
            id_token = self._get_id_token()
            headers = {'Authorization': f'Bearer {id_token}', 'Content-Type': 'application/json'} if json_data else {'Authorization': f'Bearer {id_token}'}
            response = requests.request(method, target_url, headers=headers, json=json_data)
            response.raise_for_status()
            app_logger.info(f"Request to {target_url} successful. Status: {response.status_code}")
            return response
        except Exception as e:
            app_logger.error(f"Request to {target_url} failed: {e}", exc_info=True)
            raise

# Initialize JWTManager and CloudRunServiceClient
jwt = JWTManager()
login_service_client = None

# --- JWT Error Handlers for Automatic Redirect ---
@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_payload):
    """
    Called when an access token has expired.
    Always redirect user to login service for both web and API requests.
    """
    user_email = jwt_payload.get('sub', 'unknown')
    app_logger.info(f"JWT token expired for user {user_email}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.invalid_token_loader
def invalid_token_callback(error_string):
    """
    Called when an invalid token is encountered.
    Always redirect user to login service for both web and API requests.
    """
    app_logger.info(f"Invalid JWT token encountered: {error_string}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.unauthorized_loader
def unauthorized_callback(error_string):
    """
    Called when no JWT token is present in the request.
    Always redirect user to login service for both web and API requests.
    """
    app_logger.info(f"No JWT token found: {error_string}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.revoked_token_loader
def revoked_token_callback(jwt_header, jwt_payload):
    """
    Called when a revoked token is encountered.
    Always redirect user to login service for both web and API requests.
    """
    user_email = jwt_payload.get('sub', 'unknown')
    app_logger.info(f"Revoked JWT token for user {user_email}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

@jwt.needs_fresh_token_loader
def needs_fresh_token_callback(jwt_header, jwt_payload):
    """
    Called when a fresh token is required but not provided.
    Always redirect user to login service for both web and API requests.
    """
    user_email = jwt_payload.get('sub', 'unknown')
    app_logger.info(f"Fresh token required for user {user_email}. Redirecting to login.")
    return redirect(app.config.get('LOGIN_SERVICE_URL', 'https://secapp.tzolkintech.com'))

# --- JWT Callbacks ---
@jwt.user_identity_loader
def user_identity_lookup(jwt_payload):
    return jwt_payload["sub"]

@jwt.user_lookup_loader
def user_lookup_callback(_jwt_header, jwt_data):
    identity = jwt_data["sub"]
    return {"email": identity, "name": jwt_data.get("user_name", identity)}

# --- Routes ---
@app.route('/')
@jwt_required(optional=True)
def index():
    user_email = None
    user_name = None
    # Try to get JWT from cookie explicitly if not present in request context
    try:
        claims = get_jwt()
        token = request.cookies.get(app.config.get('JWT_COOKIE_NAME', 'access_token_cookie'))
        app_logger.info(f"access_token_cookie value: {token}")
        if not claims and token:
            from flask_jwt_extended import decode_token
            try:
                claims = decode_token(token)
            except Exception as e:
                app_logger.warning(f"Manual decode_token failed: {e}")
        if claims:
            user_email = claims.get('sub')
            user_name = claims.get('name', user_email)
        else:
            app_logger.info("No valid JWT claims found; user not logged in.")
    except Exception as e:
        app_logger.warning(f"Could not get JWT identity: {e}")
    # Always pass user_name and service URLs to the template, even if None
    return render_template(
        'index.html',
        user_email=user_email,
        user_name=user_name,
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
        if user_email:
            app_logger.info(f"User info requested for: {user_email}")
            return jsonify({
                "email": user_email,
                "name": user_name,
                "roles": ["user"]
            }), 200
        return jsonify({"msg": "Unauthorized: No valid user identity"}), 401
    except Exception as e:
        app_logger.error(f"Error fetching user info: {e}", exc_info=True)
        return jsonify({"msg": "Internal server error"}), 500

@app.route('/health')
def health_check():
    health_status = {'status': 'not_ready', 'service': 'landing-service', 'checks': {}}
    status_code = 503

    try:
        # Use the identifier to check secret manager access
        get_secret_value(app.config.get('JWT_SECRET_KEY_IDENTIFIER'), app.config.get('GCP_PROJECT_ID'))
        health_status['checks']['secret_manager_access'] = 'ready'
    except Exception as e:
        health_status['checks']['secret_manager_access'] = f'error: {str(e)}'
        app_logger.error(f"Secret Manager health check failed: {str(e)}")

    if all(check == 'ready' for check in health_status['checks'].values()):
        health_status['status'] = 'ready'
        status_code = 200

    health_status['timestamp'] = datetime.now(timezone.utc).isoformat()
    return jsonify(health_status), status_code

@app.route('/startup')
def startup_check():
    startup_status = {'status': 'not_ready', 'service': 'landing-service', 'checks': {}}
    status_code = 503

    startup_status['checks']['gcp_project_id_env'] = 'ready' if app.config.get('GCP_PROJECT_ID') else 'not_set'
    startup_status['checks']['jwt_secret_key_env'] = 'ready' if app.config.get('JWT_SECRET_KEY_IDENTIFIER') else 'not_set'
    startup_status['checks']['flask_secret_key_env'] = 'ready' if app.config.get('FLASK_SECRET_KEY_IDENTIFIER') else 'not_set'
    startup_status['checks']['login_service_url_env'] = 'ready' if app.config.get('LOGIN_SERVICE_URL') else 'not_set'
    startup_status['checks']['jwt_secret_loaded'] = 'ready' if app.config.get('JWT_SECRET') else 'not_loaded'
    startup_status['checks']['flask_secret_loaded'] = 'ready' if app.config.get('SECRET_KEY') else 'not_loaded'

    if all(check == 'ready' for check in startup_status['checks'].values()):
        startup_status['status'] = 'ready'
        status_code = 200

    startup_status['timestamp'] = datetime.now(timezone.utc).isoformat()
    return jsonify(startup_status), status_code

# --- Error Handlers ---
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
    app_logger.info("Starting application initialization...")
    app_logger.info("Step 1: Configuring app...")
    configure_app()
    app_logger.info("Step 2: Setting up JWT secrets...")
    setup_jwt_secret()
    app_logger.info("Step 3: Setting up CORS...")
    setup_cors()
    app_logger.info("Step 4: Initializing JWTManager...")
    # This line should use the actual secret value, not the identifier/path
    app.config['JWT_SECRET_KEY'] = app.config.get('JWT_SECRET') 
    jwt.init_app(app)
    app_logger.info("Step 5: Initializing CloudRunServiceClient...")
    login_service_url = app.config.get('LOGIN_SERVICE_URL')
    if login_service_url:
        login_service_client = CloudRunServiceClient(login_service_url)
        app_logger.info("CloudRunServiceClient initialized for login service.")
    else:
        app_logger.warning("LOGIN_SERVICE_URL not set.")
    app_logger.info("Application initialization complete.")
except Exception as e:
    app_logger.critical(f"FATAL ERROR during initialization: {str(e)}", exc_info=True)
    raise

# --- Main Entry Point ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app_logger.info(f"Starting Flask app on port {port}, debug={not is_production}")
    app.run(host='0.0.0.0', port=port, debug=not is_production)