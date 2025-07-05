import os
from flask import Flask, render_template, request, redirect, url_for, flash, make_response
from flask_jwt_extended import JWTManager, create_access_token, set_access_cookies, unset_jwt_cookies
from datetime import timedelta
import psycopg2
import psycopg2.extras
import logging

logging.basicConfig(level=logging.INFO)
app_logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev_login_secret')

# JWT config
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'dev-secret-key-for-local-testing')
app.config['JWT_TOKEN_LOCATION'] = ['cookies']
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
app.config['JWT_COOKIE_SECURE'] = True
app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', '.run.app')  # Important for Cloud Run

jwt = JWTManager(app)

FORMS_SERVICE_URL = os.environ.get('FORMS_SERVICE_URL', 'http://localhost:8081')
DASHBOARD_SERVICE_URL = os.environ.get('DASHBOARD_SERVICE_URL', 'http://localhost:8082')
LANDING_SERVICE_URL = os.environ.get('LANDING_SERVICE_URL', 'http://localhost:8083')

# --- Database ---
def get_db_connection():
    try:
        db_url = os.environ.get('DATABASE_URL')
        return psycopg2.connect(db_url)
    except Exception as e:
        app_logger.error(f"DB connection error: {e}")
        return None

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        conn = get_db_connection()
        if not conn:
            flash("Database unavailable", 'error')
            return redirect(url_for('login'))

        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            cur.execute("SELECT password FROM users WHERE email = %s", (email,))
            user = cur.fetchone()
            cur.close()
            conn.close()

            if user and password == user['password']:
                access_token = create_access_token(identity=email)
                response = make_response(redirect(LANDING_SERVICE_URL))
                set_access_cookies(response, access_token)
                return response
            else:
                flash('Credenciales incorrectas', 'danger')
        except Exception as e:
            app_logger.error(f"Login error: {e}")
            flash("Error en el inicio de sesión", 'error')

    return render_template('login.html')

@app.route('/logout')
def logout():
    response = make_response(redirect(url_for('login')))
    unset_jwt_cookies(response)
    return response

@app.route('/health')
def health():
    return {'status': 'ok', 'service': 'login'}, 200

if __name__ == '__main__':
    os.environ.setdefault('FLASK_SECRET_KEY', 'dev_login_secret')
    os.environ.setdefault('JWT_SECRET_KEY', 'dev-secret-key')
    os.environ.setdefault('JWT_COOKIE_DOMAIN', '.run.app')
    os.environ.setdefault('DATABASE_URL', 'postgresql://user:pass@localhost/db')
    os.environ.setdefault('FORMS_SERVICE_URL', 'http://localhost:8081')
    os.environ.setdefault('DASHBOARD_SERVICE_URL', 'http://localhost:8082')
    os.environ.setdefault('LANDING_SERVICE_URL', 'http://localhost:8083')

    app.run(debug=True, host='0.0.0.0', port=8080)
