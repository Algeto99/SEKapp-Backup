# Secapp/login/main.py
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
        db_url = os.environ.get('DATABASE_URL')
        if not db_url:
            app.logger.error("DATABASE_URL environment variable not set.")
            raise ValueError("DATABASE_URL environment variable not set.")
        conn = psycopg2.connect(db_url)
        return conn
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
    # Ensure LANDING_SERVICE_URL is properly set in Cloud Run
    allowed_origin = os.environ.get('LANDING_SERVICE_URL', '*')
    response.headers['Access-Control-Allow-Origin'] = allowed_origin
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

# --- Routes ---
@app.route('/')
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')  # This is actually the email
        password = request.form.get('password')

        conn = get_db_connection()
        if not conn:
            return render_template('login.html', username=username)

        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)
            # Since username is now email, we search by email (which is the username)
            cur.execute("SELECT id, email, password_hash FROM users WHERE email = %s", (username,))
            user = cur.fetchone()
            cur.close()

            if user and bcrypt.check_password_hash(user['password_hash'], password):
                access_token = create_access_token(identity=user['email'])
                refresh_token = create_refresh_token(identity=user['email'])

                response = redirect(os.environ.get('LANDING_SERVICE_URL', '/'))
                set_access_cookies(response, access_token)
                set_refresh_cookies(response, refresh_token)
                flash('Inicio de sesión exitoso.', 'success')
                return response
            else:
                flash('Usuario o contraseña incorrectos.', 'danger')
                return render_template('login.html', username=username)
        except Exception as e:
            app.logger.error(f"Login error: {e}")
            flash('Error durante el inicio de sesión.', 'danger')
            return render_template('login.html', username=username)
        finally:
            if conn:
                conn.close()

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    # Capture form data if it's a POST request, to pre-fill form on error
    email = request.form.get('email', '')
    name = request.form.get('name', '')
    phone_number = request.form.get('phone_number', '')

    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        # Basic validation for required fields (removed username from validation)
        if not all([email, name, password, confirm_password]):
            flash('Todos los campos obligatorios son requeridos.', 'warning')
            return render_template('register.html', email=email, name=name, phone_number=phone_number)

        # Password confirmation check
        if password != confirm_password:
            flash('Las contraseñas no coinciden.', 'danger')
            return render_template('register.html', email=email, name=name, phone_number=phone_number)

        conn = get_db_connection()
        if not conn:
            return render_template('register.html', email=email, name=name, phone_number=phone_number)

        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)

            # --- 1. Check if email is authorized ---
            cur.execute("SELECT id FROM authorized_emails WHERE email = %s AND is_active = TRUE", (email,))
            authorized_email_entry = cur.fetchone()

            if not authorized_email_entry:
                flash('No estás autorizado para registrarte. Por favor, contacta a tu administrador.', 'danger')
                app.logger.warning(f"Registration attempt by unauthorized email: {email}")
                return render_template('register.html', email=email, name=name, phone_number=phone_number)

            # --- 2. Check if email already exists in users table ---
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            existing_user_email = cur.fetchone()
            if existing_user_email:
                flash('Este correo electrónico ya está registrado. Por favor, inicia sesión.', 'danger')
                return render_template('register.html', email=email, name=name, phone_number=phone_number)

            # --- 3. Hash the password ---
            hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')

            # --- 4. Insert new user into the database (removed username field) ---
            # Since username = email, we store email in both username and email fields for compatibility
            cur.execute(
                "INSERT INTO users (username, email, name, phone_number, password_hash) VALUES (%s, %s, %s, %s, %s)",
                (email, email, name, phone_number if phone_number else None, hashed_password)
            )
            conn.commit()
            cur.close()

            flash('¡Registro exitoso! Ahora puedes iniciar sesión.', 'success')
            app.logger.info(f"User {email} registered successfully.")
            return redirect(url_for('login'))

        except psycopg2.errors.UniqueViolation as e:
            # This catch handles unique violations for username or email
            conn.rollback()
            if "users_username_key" in str(e) or "users_email_key" in str(e):
                flash('Este correo electrónico ya está registrado. Por favor, inicia sesión.', 'danger')
            else:
                flash('Error de registro: un valor duplicado ya existe.', 'danger')
            app.logger.error(f"Unique violation during registration: {e}")
            return render_template('register.html', email=email, name=name, phone_number=phone_number)

        except Exception as e:
            conn.rollback()
            app.logger.error(f"Error during registration: {e}")
            flash('Ocurrió un error inesperado durante el registro. Por favor, inténtalo de nuevo.', 'danger')
            return render_template('register.html', email=email, name=name, phone_number=phone_number)
        finally:
            if conn:
                conn.close()

    # For GET request, just render the empty form
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
    # Ensure these are set in your Cloud Run environment variables for deployment
    # For local testing, these provide defaults
    os.environ.setdefault('DATABASE_URL', 'postgresql://user:password@localhost:5432/db')
    os.environ.setdefault('LANDING_SERVICE_URL', 'http://localhost:5000')
    os.environ.setdefault('LOGIN_SERVICE_URL', 'http://localhost:8080')
    os.environ.setdefault('JWT_COOKIE_DOMAIN', '.run.app')

    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))