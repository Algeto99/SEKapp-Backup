# landing/app.py (REVISED for tz-dev-secapp with enhanced secret validation)

import os
import sys
import logging
import re
from datetime import timedelta, datetime, timezone

from flask import Flask, render_template, request, jsonify, Response
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity, get_jwt # Ensure get_jwt is imported
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
    # A full path should start with projects/{project_id}/secrets/ and end with /versions/
    return s.startswith(f"projects/{project_id}/secrets/") and "/versions/" in s

# --- App Configuration ---
def configure_app():
    try:
        app.config['LANDING_SERVICE_URL'] = os.environ.get('LANDING_SERVICE_URL')
        app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL')
        app.config['FORMS_SERVICE_URL'] = os.environ.get('FORMS_SERVICE_URL')
        app.config['DASHBOARD_SERVICE_URL'] = os.environ.get('DASHBOARD_SERVICE_URL')

        app.config['JWT_TOKEN_LOCATION'] = ['cookies']
        app.config['JWT_COOKIE_HTTPONLY'] = True
        app.config['JWT_COOKIE_SECURE'] = is_production
        app.config['JWT_COOKIE_SAMESITE'] = 'None' if is_production else 'Lax' # Ensure this matches Login Service
        app.config['JWT_ACCESS_TOKEN_EXPIRES_MINUTES'] = int(os.environ.get('JWT_ACCESS_TOKEN_EXPIRES_MINUTES', 15))
        app.config['JWT_REFRESH_TOKEN_EXPIRES_DAYS'] = int(os.environ.get('JWT_REFRESH_TOKEN_EXPIRES_DAYS', 30))
        app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(minutes=app.config['JWT_ACCESS_TOKEN_EXPIRES_MINUTES'])
        app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=app.config['JWT_REFRESH_TOKEN_EXPIRES_DAYS'])
        app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', '.secapp.tzolkintech.com') # Ensure this matches Login Service

        app.config['GCP_PROJECT_ID'] = os.environ.get('GCP_PROJECT', os.environ.get('GOOGLE_CLOUD_PROJECT'))
        app.config['JWT_SECRET_MANAGER_NAME'] = os.environ.get('JWT_SECRET_MANAGER_NAME', 'jwt-secret-key') # Default name
        app_logger.info(f"JWT_SECRET_MANAGER_NAME: {app.config['JWT_SECRET_MANAGER_NAME']}")

        app_logger.info("App configuration loaded.")
        app_logger.info(f"LANDING_SERVICE_URL: {app.config['LANDING_SERVICE_URL']}")
        app_logger.info(f"LOGIN_SERVICE_URL: {app.config['LOGIN_SERVICE_URL']}")
        app_logger.info(f"JWT_COOKIE_SECURE: {app.config['JWT_COOKIE_SECURE']}")
        app_logger.info(f"JWT_COOKIE_SAMESITE: {app.config['JWT_COOKIE_SAMESITE']}")
        app_logger.info(f"JWT_COOKIE_DOMAIN: {app.config['JWT_COOKIE_DOMAIN']}")


    except Exception as e:
        app_logger.critical(f"Error during app configuration: {e}", exc_info=True)
        sys.exit(1) # Critical exit if basic config fails

# --- Secret Manager Helper ---
def get_secret_value(secret_path_or_name, project_id):
    if not project_id:
        app_logger.error(f"Cannot retrieve secret '{secret_path_or_name}': GCP_PROJECT_ID is not set.")
        raise ValueError(f"GCP_PROJECT_ID is required to access Secret Manager for '{secret_path_or_name}'.")

    if not is_full_secret_path(secret_path_or_name, project_id):
        # Assume it's just the secret name, construct the full path
        name = f"projects/{project_id}/secrets/{secret_path_or_name}/versions/latest"
    else:
        name = secret_path_or_name # Use as is if it's already a full path

    try:
        response = secret_manager_client.access_secret_version(request={"name": name})
        secret_value = response.payload.data.decode("UTF-8")
        app_logger.info(f"Successfully retrieved secret: {secret_path_or_name}")
        return secret_value
    except NotFound:
        app_logger.error(f"Secret '{secret_path_or_name}' not found at '{name}'.")
        raise ValueError(f"Secret '{secret_path_or_name}' not found.")
    except Exception as e:
        app_logger.error(f"Error accessing secret '{secret_path_or_name}' at '{name}': {e}", exc_info=True)
        raise RuntimeError(f"Failed to retrieve secret '{secret_path_or_name}'.") from e

# --- JWT Secret Setup ---
def setup_jwt_secret():
    # First, try environment variable
    jwt_secret_key = os.environ.get('JWT_SECRET_KEY')
    if jwt_secret_key:
        app.config['JWT_SECRET_KEY'] = jwt_secret_key
        app_logger.info("Using JWT_SECRET_KEY from environment variable.")
        return
    
    # If no env var, try Secret Manager with explicit name
    secret_name = os.environ.get('JWT_SECRET_MANAGER_NAME', 'jwt-secret-key')
    project_id = app.config['GCP_PROJECT_ID']
    
    if not project_id:
        app_logger.critical("GCP_PROJECT_ID not set and JWT_SECRET_KEY not provided. Cannot retrieve JWT secret.")
        sys.exit(1)
    
    try:
        app.config['JWT_SECRET_KEY'] = get_secret_value(secret_name, project_id)
        app_logger.info(f"JWT_SECRET_KEY configured from Secret Manager using secret: {secret_name}")
    except Exception as e:
        app_logger.critical(f"FATAL: Failed to retrieve JWT_SECRET_KEY from Secret Manager: {e}. Exiting.")
        sys.exit(1)

# --- CORS Configuration ---
def setup_cors():
    landing_service_url = app.config.get('LANDING_SERVICE_URL')
    allowed_origins = [landing_service_url] if landing_service_url else []
    if not is_production:
        allowed_origins.extend([
            "http://localhost:5001", "http://localhost:3000", "http://localhost:8081",
            "http://127.0.0.1:5001", "http://127.0.0.1:3000", "http://127.0.0.1:8081"
        ])
    
    CORS(app,
         supports_credentials=True,
         origins=allowed_origins,
         allow_headers=['Content-Type', 'Authorization', 'X-Requested-With'],
         methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
         expose_headers=['Set-Cookie'])
    app_logger.info(f"CORS configured with origins: {allowed_origins}")

# --- Initialize Flask-JWT-Extended ---
jwt = JWTManager()


class CloudRunServiceClient:
    def __init__(self, service_url):
        self.service_url = service_url.rstrip('/')
        self.request_adapter = google.auth.transport.requests.Request()
        app_logger.info(f"CloudRunServiceClient initialized for URL: {self.service_url}")

    def _get_id_token(self):
        try:
            return google.oauth2.id_token.fetch_id_token(self.request_adapter, self.service_url)
        except Exception as e:
            app_logger.error(f"Failed to fetch ID token for {self.service_url}: {e}", exc_info=True)
            raise

    def call_service(self, endpoint, method='GET', data=None):
        url = f"{self.service_url}{endpoint}"
        try:
            id_token = self._get_id_token()
        except Exception as e:
            app_logger.error(f"Skipping call to {url} due to ID token fetching failure: {e}")
            return None

        headers = {
            'Authorization': f'Bearer {id_token}',
            'Content-Type': 'application/json',
            'User-Agent': 'LandingService/1.0'
        }
        app_logger.info(f"Making {method} request to {url}")
        try:
            if method.upper() == 'GET':
                response = requests.get(url, headers=headers, timeout=10)
            elif method.upper() == 'POST':
                response = requests.post(url, headers=headers, json=data, timeout=10)
            else:
                app_logger.error(f"Unsupported HTTP method: {method} for {url}")
                return None
            response.raise_for_status()
            app_logger.info(f"Successfully called {url}, status: {response.status_code}")
            return response.json() if response.content else None
        except requests.exceptions.RequestException as e:
            app_logger.error(f"Error calling service at {url}: {e}", exc_info=True)
            if e.response is not None:
                app_logger.error(f"Response body from {url}: {e.response.text}")
            return None

login_service_client = None # Will be initialized in app context

# --- Routes ---
@app.route('/')
@jwt_required(optional=True) # Make JWT optional for the index page
def index():
    user_email = None
    user_name = None

    app_logger.info("Landing Service: index route hit.") # New log
    identity = get_jwt_identity()
    app_logger.info(f"Landing Service: get_jwt_identity() returned: {identity}") # New log

    if identity: # Check if a valid token is present (user is logged in)
        try:
            claims = get_jwt()
            app_logger.info(f"Landing Service: Claims from JWT: {claims}") # New log for all claims
            # Prefer 'email' claim from JWT, fallback to identity (sub)
            user_email = claims.get('email', identity)
            # Get 'user_name' from claims first, fallback to 'name' (if applicable)
            user_name = claims.get('user_name', claims.get('name'))
            app_logger.info(f"Rendering index for user: {user_email} (Name: {user_name})")
        except Exception as e:
            # Log any issues retrieving claims but allow page to render
            app_logger.warning(f"Could not retrieve JWT claims for index page: {e}", exc_info=True)
    else:
        app_logger.info("Landing Service: No JWT identity found for index page.")

    return render_template('index.html',
                           LOGIN_SERVICE_URL=app.config.get('LOGIN_SERVICE_URL', ''),
                           FORMS_SERVICE_URL=app.config.get('FORMS_SERVICE_URL', ''),
                           DASHBOARD_SERVICE_URL=app.config.get('DASHBOARD_SERVICE_URL', ''),
                           user_email=user_email, # Pass user_email to the template
                           user_name=user_name)   # Pass user_name to the template

@app.route('/health')
def health_check():
    status = {'status': 'ok', 'service': 'landing-service', 'timestamp': datetime.now(timezone.utc).isoformat()}
    return jsonify(status), 200

@app.route('/get_user_info', methods=['GET'])
@jwt_required()
def get_user_info():
    app_logger.info("GET /get_user_info called")
    current_user_email = get_jwt_identity()
    jwt_claims = get_jwt() # This gets all claims from the JWT

    # Log the claims for debugging purposes
    app_logger.info(f"Claims from JWT in Landing Service: {jwt_claims}")

    # CORRECTED: Retrieve 'user_name' from claims and map it to 'name' for the frontend
    # Use .get() with a default in case the claim is missing
    user_info_to_return = {
        "email": jwt_claims.get('email', current_user_email), # Prefer 'email' claim, fallback to 'sub'
        "name": jwt_claims.get('user_name', jwt_claims.get('name', 'Usuario Desconocido')), # Try 'user_name', then 'name', then default
        "user_id": jwt_claims.get('user_id')
    }
    app_logger.info(f"Returning user info: {user_info_to_return}")
    return jsonify(user_info_to_return), 200


@app.route('/dashboard_access', methods=['GET'])
@jwt_required()
def dashboard_access():
    current_user_email = get_jwt_identity()
    jwt_claims = get_jwt()
    is_admin = jwt_claims.get('is_admin', False) # Safely get is_admin

    if is_admin:
        app_logger.info(f"Admin user {current_user_email} granted dashboard access.")
        return jsonify({"message": "Access granted", "is_admin": True}), 200
    else:
        app_logger.warning(f"Non-admin user {current_user_email} attempted dashboard access.")
        return jsonify({"message": "Access denied. Not an administrator."}, {"is_admin": False}), 403


# Error Handlers
@app.errorhandler(404)
def not_found_error(error):
    app_logger.warning(f"404 Not Found: {request.path}")
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    app_logger.error(f"Internal server error: {error}", exc_info=True)
    return jsonify({"error": "Internal Server Error"}), 500

# --- Application Initialization ---
with app.app_context():
    try:
        app_logger.info("Starting application initialization...")
        app_logger.info("Step 1: Configuring app...")
        configure_app()
        app_logger.info("Step 2: Setting up JWT secrets...")
        setup_jwt_secret() # This will set app.config['JWT_SECRET_KEY']
        app_logger.info("Step 3: Setting up CORS...")
        setup_cors()
        app_logger.info("Step 4: Initializing JWTManager...")
        # Now JWTManager will correctly use app.config['JWT_SECRET_KEY']
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

# Step 1: Add debug logging to both services

# LOGIN SERVICE (main.py) - Add this after setup_jwt_secret()
def debug_jwt_secret():
    secret = app.config.get('JWT_SECRET_KEY', 'NOT SET')
    app_logger.info(f"LOGIN SERVICE JWT Secret length: {len(secret)}")
    app_logger.info(f"LOGIN SERVICE JWT Secret hash: {hash(secret)}")
    app_logger.info(f"LOGIN SERVICE JWT Secret first 10 chars: {secret[:10]}...")
    
# Call this in your with app.app_context() block in main.py
debug_jwt_secret()

# LANDING SERVICE (app.py) - Add this after setup_jwt_secret()
def debug_jwt_secret():
    secret = app.config.get('JWT_SECRET_KEY', 'NOT SET')
    app_logger.info(f"LANDING SERVICE JWT Secret length: {len(secret)}")
    app_logger.info(f"LANDING SERVICE JWT Secret hash: {hash(secret)}")
    app_logger.info(f"LANDING SERVICE JWT Secret first 10 chars: {secret[:10]}...")

# Call this in your with app.app_context() block in app.py
debug_jwt_secret()

# Step 2: Enhanced JWT secret setup for both services
def setup_jwt_secret_enhanced():
    """Enhanced JWT secret setup with detailed logging"""
    app_logger.info("=== JWT SECRET SETUP START ===")
    
    # Method 1: Environment variable
    jwt_secret_key = os.environ.get('JWT_SECRET_KEY')
    if jwt_secret_key:
        app.config['JWT_SECRET_KEY'] = jwt_secret_key
        app_logger.info(f"✓ Using JWT_SECRET_KEY from environment variable (length: {len(jwt_secret_key)})")
        return
    
    # Method 2: Secret Manager
    secret_name = app.config.get('JWT_SECRET_MANAGER_NAME', 'jwt-secret-key')
    project_id = app.config.get('GCP_PROJECT_ID')
    
    app_logger.info(f"Attempting to retrieve secret: {secret_name} from project: {project_id}")
    
    if not project_id:
        app_logger.critical("❌ GCP_PROJECT_ID not set and JWT_SECRET_KEY not provided")
        sys.exit(1)
    
    try:
        retrieved_secret = get_secret_value(secret_name, project_id)
        app.config['JWT_SECRET_KEY'] = retrieved_secret
        app_logger.info(f"✓ JWT_SECRET_KEY from Secret Manager (length: {len(retrieved_secret)})")
        app_logger.info(f"✓ Secret name: {secret_name}, Project: {project_id}")
        app_logger.info("=== JWT SECRET SETUP COMPLETE ===")
    except Exception as e:
        app_logger.critical(f"❌ Failed to retrieve JWT_SECRET_KEY: {e}")
        sys.exit(1)

# Step 3: Create a test endpoint to verify JWT secrets match
@app.route('/debug/jwt-secret-info')
def debug_jwt_secret_info():
    """Debug endpoint to check JWT secret configuration"""
    if is_production:
        return "Debug endpoint disabled in production", 403
    
    secret = app.config.get('JWT_SECRET_KEY', 'NOT SET')
    return jsonify({
        'service': 'LOGIN_SERVICE',  # Change this to 'LANDING_SERVICE' in app.py
        'secret_length': len(secret),
        'secret_hash': hash(secret),
        'secret_preview': secret[:10] + '...' if len(secret) > 10 else secret,
        'secret_source': 'env_var' if os.environ.get('JWT_SECRET_KEY') else 'secret_manager',
        'secret_manager_name': app.config.get('JWT_SECRET_MANAGER_NAME'),
        'project_id': app.config.get('GCP_PROJECT_ID')
    })

# Step 4: Enhanced token creation logging in LOGIN SERVICE
def create_token_with_debug(user_email, additional_claims):
    """Create token with debug logging"""
    app_logger.info(f"=== TOKEN CREATION START ===")
    app_logger.info(f"Creating token for user: {user_email}")
    app_logger.info(f"Additional claims: {additional_claims}")
    
    secret = app.config.get('JWT_SECRET_KEY')
    app_logger.info(f"Using secret (hash): {hash(secret)}")
    
    access_token = create_access_token(
        identity=user_email,
        additional_claims=additional_claims
    )
    
    app_logger.info(f"Created token preview: {access_token[:50]}...")
    app_logger.info(f"=== TOKEN CREATION COMPLETE ===")
    return access_token

# Step 5: Enhanced token verification logging in LANDING SERVICE
@app.route('/debug/token-verify')
@jwt_required()
def debug_token_verify():
    """Debug token verification"""
    if is_production:
        return "Debug endpoint disabled in production", 403
    
    try:
        identity = get_jwt_identity()
        claims = get_jwt()
        secret = app.config.get('JWT_SECRET_KEY')
        
        return jsonify({
            'verification_status': 'SUCCESS',
            'identity': identity,
            'claims': claims,
            'secret_hash': hash(secret),
            'service': 'LANDING_SERVICE'
        })
    except Exception as e:
        return jsonify({
            'verification_status': 'FAILED',
            'error': str(e),
            'service': 'LANDING_SERVICE'
        })

# Step 6: Temporary hardcoded secret for testing
# Add this to BOTH services for immediate testing
def set_temporary_shared_secret():
    """TEMPORARY: Set the same hardcoded secret for both services"""
    TEMP_SECRET = "shared-secret-for-testing-12345"
    app.config['JWT_SECRET_KEY'] = TEMP_SECRET
    app_logger.warning(f"⚠️ USING TEMPORARY HARDCODED SECRET: {TEMP_SECRET}")
    app_logger.warning("⚠️ REMOVE THIS BEFORE PRODUCTION DEPLOYMENT")

# Step 7: Environment variable verification
def verify_environment():
    """Verify critical environment variables"""
    required_vars = ['GCP_PROJECT_ID', 'JWT_SECRET_MANAGER_NAME']
    optional_vars = ['JWT_SECRET_KEY']
    
    app_logger.info("=== ENVIRONMENT VERIFICATION ===")
    
    for var in required_vars:
        value = os.environ.get(var)
        if value:
            app_logger.info(f"✓ {var}: {value}")
        else:
            app_logger.error(f"❌ {var}: NOT SET")
    
    for var in optional_vars:
        value = os.environ.get(var)
        if value:
            app_logger.info(f"✓ {var}: SET (length: {len(value)})")
        else:
            app_logger.info(f"○ {var}: NOT SET (will use Secret Manager)")
    
    app_logger.info("=== ENVIRONMENT VERIFICATION COMPLETE ===")

# --- Main Entry Point ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8081))
    debug_mode = not is_production
    app_logger.info(f"Starting Landing Service on port {port}, debug={debug_mode}")
    app.run(host='0.0.0.0', port=port, debug=debug_mode, threaded=True)