import os
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_bcrypt import Bcrypt
from flask_jwt_extended import (
    create_access_token, create_refresh_token, unset_jwt_cookies,
    set_access_cookies, set_refresh_cookies, jwt_required,
    get_jwt_identity, JWTManager
)
import psycopg2
from psycopg2 import extras
from datetime import timedelta

app = Flask(__name__)

# --- Flask Config ---
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'dev-secret')
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'dev-jwt')
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
app.config['JWT_REFRESH_TOKEN_EXPIRES'] = timedelta(days=30)
app.config['JWT_TOKEN_LOCATION'] = ['cookies']
app.config['JWT_COOKIE_SECURE'] = True
app.config['JWT_COOKIE_SAMESITE'] = 'Lax'
app.config['JWT_COOKIE_DOMAIN'] = os.environ.get('JWT_COOKIE_DOMAIN', '.run.app')

# --- Extensions ---
bcrypt = Bcrypt(app)
jwt = JWTManager(app)

# --- DB Connection ---
def get_db_connection():
    try:
        return psycopg2.connect(os.environ.get('DATABASE_URL'))
    except Exception as e:
        app.logger.error(f"DB connection error: {e}")
        flash('Error de conexión a la base de datos.', 'danger')
        return None

# --- JWT Error Handling ---
@jwt.unauthorized_loader
@jwt.invalid_token_loader
@jwt.expired_token_loader
def token_error_response(callback):
    flash('Su sesión ha caducado o es inválida. Por favor, inicie sesión de nuevo.', 'danger')
    return redirect(url_for('login'))

# --- CORS (optional for cookie mode) ---
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = os.environ.get('LANDING_SERVICE_URL', '*')
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

# --- Routes ---
@app.route('/')
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        conn = get_db_connection()
        if not conn:
            return render_template('login.html')

        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)
            cur.execute("SELECT id, username, password_hash FROM users WHERE username = %s", (username,))
            user = cur.fetchone()
            cur.close()

            if user and bcrypt.check_password_hash(user['password_hash'], password):
                access_token = create_access_token(identity=user['username'])
                refresh_token = create_refresh_token(identity=user['username'])

                response = redirect(os.environ.get('LANDING_SERVICE_URL', '/'))
                set_access_cookies(response, access_token)
                set_refresh_cookies(response, refresh_token)
                return response
            else:
                flash('Usuario o contraseña incorrectos.', 'danger')
                return render_template('login.html')
        except Exception as e:
            app.logger.error(f"Login error: {e}")
            flash('Error durante el inicio de sesión.', 'danger')
            return render_template('login.html')
        finally:
            conn.close()

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not all([username, password, confirm_password]):
            flash('Rellene todos los campos.', 'warning')
            return render_template('register.html', username=username)

        if password != confirm_password:
            flash('Las contraseñas no coinciden.', 'danger')
            return render_template('register.html', username=username)

        hashed = bcrypt.generate_password_hash(password).decode('utf-8')
        conn = get_db_connection()
        if not conn:
            return render_template('register.html', username=username)

        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)", (username, hashed))
            conn.commit()
            cur.close()
            flash('¡Registro exitoso! Ahora puede iniciar sesión.', 'success')
            return redirect(url_for('login'))
        except psycopg2.errors.UniqueViolation:
            flash('El usuario ya existe.', 'danger')
            conn.rollback()
        except Exception as e:
            app.logger.error(f"Registration error: {e}")
            flash('Error durante el registro.', 'danger')
        finally:
            conn.close()

    return render_template('register.html')

@app.route('/logout')
def logout():
    response = redirect(url_for('login'))
    unset_jwt_cookies(response)
    flash('Sesión cerrada.', 'info')
    return response

# --- Placeholder Routes ---
@app.route('/dashboard_placeholder')
@jwt_required()
def dashboard_placeholder():
    user = get_jwt_identity()
    return f"<h1>Dashboard: Bienvenido {user}</h1>"

@app.route('/forms_placeholder')
@jwt_required()
def forms_placeholder():
    user = get_jwt_identity()
    return f"<h1>Formulario: Bienvenido {user}</h1>"

# --- Run App ---
if __name__ == '__main__':
    os.environ.setdefault('DATABASE_URL', 'postgresql://user:password@localhost:5432/db')
    os.environ.setdefault('LANDING_SERVICE_URL', 'http://localhost:5000')
    os.environ.setdefault('LOGIN_SERVICE_URL', 'http://localhost:8080')
    os.environ.setdefault('JWT_COOKIE_DOMAIN', '.run.app')

    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
