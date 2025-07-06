# forms/app.py - Version without Google Cloud dependencies
import os
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity
import logging
import secrets

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
app_logger = logging.getLogger(__name__)

app = Flask(__name__)

# Global variables
is_production = False
jwt = None
db_available = False

# --- Configuration ---
def configure_app():
    """Configure the Flask app with proper error handling"""
    global is_production
    try:
        # Dynamic environment detection
        is_production = os.environ.get('K_SERVICE') is not None
        
        # --- Flask Config ---
        app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(32))
        
        # --- JWT Configuration ---
        app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'dev-secret-key-for-local-testing')
        app.config['JWT_TOKEN_LOCATION'] = ['cookies']
        app.config['JWT_COOKIE_SECURE'] = is_production
        app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
        app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', 'localhost')
        app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
        app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)
        
        # Service URLs with defaults
        app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL', 'http://localhost:8080')
        app.config['DASHBOARD_SERVICE_URL'] = os.environ.get('DASHBOARD_SERVICE_URL', 'http://localhost:8082')
        app.config['LANDING_SERVICE_URL'] = os.environ.get('LANDING_SERVICE_URL', 'http://localhost:8081')
        
        app_logger.info(f"Forms service configured - Production: {is_production}")
        return True
        
    except Exception as e:
        app_logger.error(f"Configuration error: {e}", exc_info=True)
        return False

def setup_jwt():
    """Setup JWT with proper error handling"""
    global jwt
    try:
        jwt = JWTManager(app)
        app_logger.info("JWT configured successfully")
        return True
    except Exception as e:
        app_logger.error(f"JWT setup error: {e}", exc_info=True)
        return False

def check_database():
    """Check if database is available"""
    global db_available
    try:
        db_url = os.environ.get('DATABASE_URL')
        if not db_url:
            app_logger.warning("DATABASE_URL not set - database features disabled")
            db_available = False
            return False
            
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        db_available = True
        app_logger.info("Database connection successful")
        return True
    except Exception as e:
        app_logger.warning(f"Database not available: {e}")
        db_available = False
        return False

# Initialize app
configure_app()
setup_jwt()
check_database()

# --- Database Connection ---
def get_db_connection():
    """Get database connection with better error handling"""
    if not db_available:
        return None
    try:
        db_url = os.environ.get('DATABASE_URL')
        conn = psycopg2.connect(db_url)
        return conn
    except Exception as e:
        app_logger.error(f"Error connecting to DB: {e}")
        return None

# --- JWT Error Handlers ---
if jwt:
    @jwt.unauthorized_loader
    def unauthorized_response(callback):
        """Handle unauthorized access"""
        login_url = app.config['LOGIN_SERVICE_URL']
        if not login_url.endswith('/login'):
            login_url = f"{login_url.rstrip('/')}/login"
        return redirect(login_url)

    @jwt.invalid_token_loader
    def invalid_token_response(callback):
        """Handle invalid token"""
        login_url = app.config['LOGIN_SERVICE_URL']
        if not login_url.endswith('/login'):
            login_url = f"{login_url.rstrip('/')}/login"
        return redirect(login_url)

    @jwt.expired_token_loader
    def expired_token_response(jwt_header, jwt_payload):
        """Handle expired token"""
        login_url = app.config['LOGIN_SERVICE_URL']
        if not login_url.endswith('/login'):
            login_url = f"{login_url.rstrip('/')}/login"
        return redirect(login_url)

# --- Routes ---
@app.route('/')
def index():
    """Main page - works with or without JWT"""
    app_logger.info("Forms service index page accessed")
    if db_available:
        return redirect(url_for('show_report_form'))
    else:
        return jsonify({
            'status': 'running',
            'service': 'forms-service',
            'message': 'Database not available - form submission disabled',
            'timestamp': datetime.now().isoformat()
        })

@app.route('/report_form', methods=['GET'])
def show_report_form():
    """Display the incident report form"""
    if not db_available:
        return jsonify({
            'status': 'error',
            'message': 'Database not available - form submission disabled'
        })
    
    try:
        # If JWT is working, try to get user identity
        current_user_identity = get_jwt_identity() if jwt else "anonymous"
    except:
        current_user_identity = "anonymous"
    
    app_logger.info(f"User {current_user_identity} accessing report form.")

    conn = get_db_connection()
    if conn is None:
        return jsonify({
            'status': 'error',
            'message': 'Database connection failed'
        })

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Load form data
        cur.execute("SELECT id_tipo_incidencia AS id, nombre FROM tipo_incidencia ORDER BY nombre;")
        tipo_incidencia_data = cur.fetchall()
        
        cur.execute("SELECT id_tipo_cliente AS id, nombre FROM tipo_cliente ORDER BY nombre;")
        tipo_cliente_data = cur.fetchall()
        
        cur.execute("SELECT id_lugar_incidente AS id, nombre FROM lugar_incidente ORDER BY nombre;")
        lugar_incidente_data = cur.fetchall()
        
        cur.execute("SELECT id_supervisor AS id, nombre FROM supervisor ORDER BY nombre;")
        supervisor_data = cur.fetchall()
        
        cur.close()
        
        return render_template(
            'form.html',
            tipo_incidencia=tipo_incidencia_data,
            tipo_cliente=tipo_cliente_data,
            lugar_incidente=lugar_incidente_data,
            supervisor=supervisor_data,
            # Removed username, login_service_url, and dashboard_service_url
            # to eliminate the welcome message and navigation links
        )
    except Exception as e:
        app_logger.error(f"Error loading form: {e}")
        return jsonify({
            'status': 'error',
            'message': f'Error loading form data: {str(e)}'
        })
    finally:
        if conn:
            conn.close()

@app.route('/submit_report', methods=['POST'])
def submit_report():
    """Submit an incident report - simplified version"""
    if not db_available:
        return jsonify({
            'status': 'error',
            'message': 'Database not available'
        })
    
    try:
        current_user_email = get_jwt_identity() if jwt else "anonymous"
    except:
        current_user_email = "anonymous"
    
    app_logger.info(f"User {current_user_email} submitting report.")

    data = request.form
    if not data:
        return jsonify({
            'status': 'error',
            'message': 'No form data received'
        })

    # Basic validation
    required_fields = ['tipo_incidencia', 'tipo_cliente', 'lugar_incidente', 
                      'fecha_incidente', 'hora_incidente', 'descripcion_incidente', 
                      'nombre_persona', 'supervisor']
    
    missing_fields = [field for field in required_fields if not data.get(field)]
    
    if missing_fields:
        return jsonify({
            'status': 'error',
            'message': f'Missing required fields: {", ".join(missing_fields)}'
        })

    conn = get_db_connection()
    if conn is None:
        return jsonify({
            'status': 'error',
            'message': 'Database connection failed'
        })

    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO reportes_incidentes (
                id_tipo_incidencia, id_tipo_cliente, id_lugar_incidente,
                descripcion_zona_comun, fecha_incidente, hora_incidente,
                descripcion_incidente, valor_aproximado, pertenencias_sustraidas,
                nombre_persona, telefono_persona, numero_identidad_persona,
                numero_local, direccion, imagenes_pdfs, id_supervisor,
                user_email, creado_en
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            data.get('tipo_incidencia'), data.get('tipo_cliente'), data.get('lugar_incidente'),
            data.get('descripcion_zona_comun') or None,
            data.get('fecha_incidente'), data.get('hora_incidente'),
            data.get('descripcion_incidente'),
            float(data.get('valor_aproximado')) if data.get('valor_aproximado') else None,
            data.get('pertenencias_sustraidas') or None,
            data.get('nombre_persona'), data.get('telefono_persona') or None,
            data.get('numero_identidad_persona') or None, data.get('numero_local') or None,
            data.get('direccion') or None, data.get('imagenes_pdfs') or None,
            data.get('supervisor'),
            current_user_email,
            datetime.now()
        ))
        conn.commit()
        cur.close()
        
        app_logger.info(f"Report submitted successfully by {current_user_email}.")
        return jsonify({
            'status': 'success',
            'message': 'Report submitted successfully'
        })
        
    except Exception as e:
        conn.rollback()
        app_logger.error(f"Error saving report: {e}")
        return jsonify({
            'status': 'error',
            'message': f'Error saving report: {str(e)}'
        })
    finally:
        if conn:
            conn.close()

# --- Health Check Routes ---
@app.route('/health')
def health_check():
    """Health check endpoint"""
    health_status = {
        'status': 'healthy',
        'service': 'forms-service',
        'timestamp': datetime.now().isoformat(),
        'database': 'connected' if db_available else 'disconnected',
        'jwt': 'configured' if jwt else 'not configured'
    }
    
    status_code = 200
    if not db_available:
        health_status['status'] = 'degraded'
        health_status['message'] = 'Database unavailable but service running'
    
    return health_status, status_code

@app.route('/startup')
def startup_check():
    """Startup check endpoint"""
    return {
        'status': 'ready',
        'service': 'forms-service',
        'port': os.environ.get('PORT', '8080'),
        'timestamp': datetime.now().isoformat()
    }, 200

# --- Error Handlers ---
@app.errorhandler(404)
def not_found_error(error):
    """Handle 404 errors"""
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors"""
    app_logger.error(f"Internal server error: {error}")
    return jsonify({'error': 'Internal server error'}), 500

# --- Main Application Entry Point ---
if __name__ == '__main__':
    try:
        port = int(os.environ.get('PORT', 8080))
        debug_mode = not is_production
        
        app_logger.info(f"Starting Forms service on port {port}")
        app_logger.info(f"Database available: {db_available}")
        app_logger.info(f"JWT configured: {jwt is not None}")
        
        app.run(
            host='0.0.0.0',
            port=port,
            debug=debug_mode
        )
        
    except Exception as e:
        app_logger.critical(f"Failed to start Forms service: {e}", exc_info=True)
        raise