import os
import logging
import traceback
from flask import Flask, render_template, request, redirect, flash, jsonify
from flask_jwt_extended import (
    JWTManager, jwt_required, get_jwt_identity,
    set_access_cookies, unset_jwt_cookies
)
import psycopg2
import psycopg2.extras

# --- Logger Setup ---
logging.basicConfig(level=logging.INFO)
app_logger = logging.getLogger('app')

# --- Flask App Setup ---
app = Flask(__name__)

def configure_app(app):
    is_production = os.getenv("K_SERVICE") is not None

    app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'forms-flask-secret-key')
    app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'jwt-secret-key')
    app.config['BASE_URL'] = os.environ.get('BASE_URL', '/')
    app.config['LOGIN_SERVICE_URL'] = os.environ.get('LOGIN_SERVICE_URL', '/')
    app.config['JWT_TOKEN_LOCATION'] = ['cookies']
    app.config['JWT_ACCESS_COOKIE_PATH'] = '/'
    app.config['JWT_COOKIE_SECURE'] = is_production
    app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
    app.config['JWT_ACCESS_COOKIE_NAME'] = 'access_token_cookie'
    app.config['JWT_COOKIE_CSRF_PROTECT'] = False
    app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', None)

    app.config['DB_HOST'] = os.environ.get('DB_HOST')
    app.config['DB_NAME'] = os.environ.get('DB_NAME')
    app.config['DB_USER'] = os.environ.get('DB_USER')
    app.config['DB_PASSWORD'] = os.environ.get('DB_PASSWORD')
    app.config['DB_PORT'] = os.environ.get('DB_PORT', '5432')

    app_logger.info(f"Forms service configured - Production: {is_production}")

configure_app(app)

jwt = JWTManager(app)
app_logger.info("JWT configured successfully")

import urllib.parse as urlparse

def get_db_connection():
    db_url = os.getenv('DATABASE_URL')
    if not db_url:
        raise Exception("DATABASE_URL environment variable not set")

    urlparse.uses_netloc.append('postgres')
    parsed_url = urlparse.urlparse(db_url)
    query = dict(urlparse.parse_qsl(parsed_url.query))

    return psycopg2.connect(
        dbname=parsed_url.path[1:],  # removes leading /
        user=parsed_url.username,
        password=parsed_url.password,
        host=query.get('host', parsed_url.hostname),
        port=query.get('port', parsed_url.port or '5432')
    )

def check_database():
    try:
        conn = get_db_connection()
        conn.close()
        app_logger.info("Database connection successful")
    except Exception as e:
        app_logger.error(f"Database connection failed: {e}")
        raise

check_database()

@app.route('/health')
def health():
    return "OK", 200

@app.route('/startup')
def startup_check():
    try:
        check_database()
        return "READY", 200
    except Exception as e:
        return f"Database check failed: {e}", 500

@app.route('/')
@jwt_required(optional=True)
def index():
    try:
        current_user = get_jwt_identity()

        # --- Lookup user name from users table ---
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("SELECT name FROM users WHERE email = %s", (current_user,))
        result = cur.fetchone()
        name = result["name"] if result and "name" in result else current_user

        # --- Load dropdown options ---
        cur.execute("SELECT id_tipo_incidencia, nombre FROM tipo_incidencia ORDER BY nombre ASC")
        tipo_incidencia = cur.fetchall()

        cur.execute("SELECT id_tipo_cliente, nombre FROM tipo_cliente ORDER BY nombre ASC")
        tipo_cliente = cur.fetchall()

        cur.execute("SELECT id_lugar_incidente, nombre FROM lugar_incidente ORDER BY nombre ASC")
        lugar_incidente = cur.fetchall()

        cur.execute("SELECT id_supervisor, nombre FROM supervisor ORDER BY nombre ASC")
        supervisor = cur.fetchall()

        cur.close()
        conn.close()

        return render_template(
            'form.html',
            tipo_incidencia=tipo_incidencia,
            tipo_cliente=tipo_cliente,
            lugar_incidente=lugar_incidente,
            supervisor=supervisor,
            name=name,
            login_service_url=app.config.get('LOGIN_SERVICE_URL', '/')
        )
    except Exception as e:
        app_logger.error("Exception on / [GET]: %s", traceback.format_exc())
        return "Internal Server Error", 500

@app.route('/submit_report', methods=['POST'])
@jwt_required()
def submit_report():
    try:
        data = (
            request.form.get('tipo_incidencia'),
            request.form.get('tipo_cliente'),
            request.form.get('lugar_incidente'),
            request.form.get('descripcion_zona_comun'),
            request.form.get('fecha_incidente'),
            request.form.get('hora_incidente'),
            request.form.get('descripcion_incidente'),
            request.form.get('valor_aproximado'),
            request.form.get('pertenencias_sustraidas'),
            request.form.get('nombre_persona'),
            request.form.get('telefono_persona'),
            request.form.get('numero_identidad_persona'),
            request.form.get('numero_local'),
            request.form.get('direccion'),
            request.form.get('imagenes_pdfs'),
            request.form.get('supervisor'),
        )

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO reporte_incidencia (
                tipo_incidencia, tipo_cliente, lugar_incidente, descripcion_zona_comun,
                fecha_incidente, hora_incidente, descripcion_incidente, valor_aproximado,
                pertenencias_sustraidas, nombre_persona, telefono_persona, numero_identidad_persona,
                numero_local, direccion, imagenes_pdfs, supervisor
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, data)

        conn.commit()
        cur.close()
        conn.close()

        flash('Reporte enviado exitosamente.', 'success')
        return redirect('/')
    except Exception as e:
        app_logger.error("Error submitting report: %s", traceback.format_exc())
        flash('Hubo un error al enviar el reporte.', 'error')
        return redirect('/')

# --- JWT Error Handlers ---
@jwt.unauthorized_loader
def unauthorized_callback(err):
    app_logger.warning(f"Missing JWT: {err}")
    return redirect(app.config.get('BASE_URL', '/'))

@jwt.invalid_token_loader
def invalid_token_callback(err):
    app_logger.error(f"Invalid JWT token: {err}")
    return redirect(app.config.get('BASE_URL', '/'))

@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_payload):
    app_logger.info("JWT token expired.")
    return redirect(app.config.get('BASE_URL', '/'))

# --- Main ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
