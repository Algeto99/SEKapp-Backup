import os
import smtplib
import socket
import ssl
import secrets
import hashlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, Response, current_app
from flask_jwt_extended import (
    create_access_token, create_refresh_token, unset_jwt_cookies,
    set_access_cookies, set_refresh_cookies, get_jwt_identity, get_jwt, jwt_required
)
from psycopg2 import extras
import psycopg2

# --- Initialize Blueprint ---
login_bp = Blueprint('login_bp', __name__)

# Extensions (from main app)
bcrypt = None

def init_login_bp(app_bcrypt):
    global bcrypt
    bcrypt = app_bcrypt

import urllib.parse as urlparse

# --- Database Helper (Using main app context) ---
def get_db_connection():
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        current_app.logger.critical("DATABASE_URL environment variable NOT SET.")
        return None
        
    urlparse.uses_netloc.append('postgres')
    parsed_url = urlparse.urlparse(db_url)
    query = dict(urlparse.parse_qsl(parsed_url.query))
    
    try:
        conn = psycopg2.connect(
            dbname=parsed_url.path[1:],
            user=parsed_url.username,
            password=parsed_url.password,
            host=query.get('host', parsed_url.hostname),
            port=query.get('port', parsed_url.port or '5432')
        )
        return conn
    except Exception as e:
        current_app.logger.error(f"Database connection error: {e}", exc_info=True)
        return None
    try:
        conn = psycopg2.connect(db_url)
        return conn
    except Exception as e:
        current_app.logger.error(f"Database connection error: {e}", exc_info=True)
        return None

# --- Security and Tokens ---
def generate_reset_token():
    return secrets.token_urlsafe(32)

def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()

def ensure_password_reset_table(conn):
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) NOT NULL,
                token_hash VARCHAR(64) NOT NULL,
                expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_password_reset_token_hash ON password_reset_tokens(token_hash);
            CREATE INDEX IF NOT EXISTS idx_password_reset_email ON password_reset_tokens(email);
        """)
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"Error checking/creating password_reset_tokens table: {e}", exc_info=True)
        return False

def create_password_reset_token(email):
    conn = get_db_connection()
    if not conn:
        return None
    ensure_password_reset_table(conn)
    try:
        token = generate_reset_token()
        token_hash = hash_token(token)
        expires_at = datetime.now(timezone.utc) + current_app.config['PASSWORD_RESET_TOKEN_EXPIRES']
        cur = conn.cursor()
        cur.execute("DELETE FROM password_reset_tokens WHERE email = %s", (email,))
        cur.execute(
            "INSERT INTO password_reset_tokens (email, token_hash, expires_at) VALUES (%s, %s, %s)",
            (email, token_hash, expires_at)
        )
        conn.commit()
        cur.close()
        return token
    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"Error creating password reset token for {email}: {e}")
        return None
    finally:
        if conn: conn.close()

def verify_reset_token(token):
    conn = get_db_connection()
    if not conn: return None
    try:
        token_hash = hash_token(token)
        cur = conn.cursor(cursor_factory=extras.DictCursor)
        cur.execute("SELECT email, expires_at FROM password_reset_tokens WHERE token_hash = %s", (token_hash,))
        result = cur.fetchone()
        cur.close()
        
        if not result: return None
        if datetime.now(timezone.utc) > result['expires_at'].replace(tzinfo=timezone.utc):
            cur = conn.cursor()
            cur.execute("DELETE FROM password_reset_tokens WHERE token_hash = %s", (token_hash,))
            conn.commit()
            cur.close()
            return None
        return result['email']
    except Exception as e:
        current_app.logger.error(f"Error verifying reset token: {e}")
        return None
    finally:
        if conn: conn.close()

def delete_reset_token(token):
    conn = get_db_connection()
    if not conn: return
    try:
        token_hash = hash_token(token)
        cur = conn.cursor()
        cur.execute("DELETE FROM password_reset_tokens WHERE token_hash = %s", (token_hash,))
        conn.commit()
        cur.close()
    except Exception as e:
        current_app.logger.error(f"Error deleting reset token: {e}")
    finally:
        if conn: conn.close()

# --- Email System ---
# Note: get_email_password implies sharing the secret lookup from the main context
def get_email_password():
    password = os.environ.get('EMAIL_PASSWORD') or os.environ.get('EMAIL_PASSWORD_SECRET')
    if password: return password

    project_id = current_app.config.get('GCP_PROJECT_ID')
    secret_name = current_app.config.get('EMAIL_PASSWORD_SECRET_NAME')
    if not project_id or not secret_name: return None
    
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        current_app.logger.warning(f"Could not retrieve email password: {e}")
        return None

def send_email(to_email, subject, body, is_html=False):
    email_username = current_app.config.get('SENDER_EMAIL')
    smtp_server = current_app.config.get('SMTP_SERVER')
    smtp_port = current_app.config.get('SMTP_PORT')
    email_password = get_email_password()

    if not all([email_username, email_password, smtp_server, smtp_port]):
        if request: flash("Error en la configuración de envío de email.", "danger")
        return False
    
    try:
        msg = MIMEMultipart()
        msg['From'] = email_username
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html' if is_html else 'plain'))

        context = ssl.create_default_context()
        try:
             server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
             server.ehlo()
             server.starttls(context=context)
             server.ehlo()
        except Exception:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
            server.starttls()

        server.login(email_username, email_password)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        current_app.logger.error(f"Email failure: {e}", exc_info=True)
        if request: flash("El servidor de email falló.", "danger")
        return False

def send_password_reset_email(email, reset_token):
    reset_url = url_for('login_bp.reset_password', token=reset_token, _external=True)
    subject = "Restablecer Contraseña - Kanan SecApp"
    html_body = f"""<div style="font-family: sans-serif;"><a href="{reset_url}">Restablecer</a></div>"""
    return send_email(email, subject, html_body, is_html=True)

def send_welcome_email(user_email, user_name, is_admin=False):
    subject = "¡Bienvenido a Kanan SecApp!"
    login_url = url_for('login_bp.login', _external=True)
    html_body = f"""<div style="font-family: sans-serif;">¡Hola {user_name}! <a href="{login_url}">Iniciar Sesión</a></div>"""
    return send_email(user_email, subject, html_body, is_html=True)

def send_registration_notification(user_email, user_name, phone_number):
    admin_email = current_app.config.get('ADMIN_EMAIL')
    if admin_email:
        subject = f"Nuevo Usuario Registrado - {user_name}"
        html_body = f"<div>Registrado: {user_name} - {user_email}</div>"
        send_email(admin_email, subject, html_body, is_html=True)
    return send_welcome_email(user_email, user_name)

# --- Routes Blueprint ---

@login_bp.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('username') or request.form.get('email')
        password = request.form.get('password')
        conn = get_db_connection()
        if not conn:
            flash("Service unavailable (DB connection failed)", "danger")
            return render_template('login.html')

        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)
            cur.execute('SELECT "id", "email", "password_hash", "name", "is_admin" FROM "users" WHERE "email" = %s', (email,))
            user = cur.fetchone()

            # Also check authorized_emails for admin status (same as original login service)
            if user:
                cur.execute('SELECT "is_admin", "is_active" FROM "authorized_emails" WHERE "email" = %s', (email,))
                auth_entry = cur.fetchone()
            cur.close()

            if user:
                is_valid = bcrypt.check_password_hash(user['password_hash'], password)
                
                if is_valid:
                    # Resolve is_admin: prefer authorized_emails.is_admin if present and active
                    is_admin = bool(user.get('is_admin'))
                    if auth_entry and auth_entry.get('is_active'):
                        is_admin = bool(auth_entry.get('is_admin', is_admin))

                    current_app.logger.info(f"User {email} logged in. is_admin={is_admin}")
                    access_token = create_access_token(
                        identity=user['email'],
                        additional_claims={'is_admin': is_admin, 'name': user['name']}
                    )
                    refresh_token = create_refresh_token(identity=user['email'])
                    
                    # In Monolith, route internally to landing_bp rather than an external URL
                    response = redirect(url_for('landing_bp.landing_page'))
                    set_access_cookies(response, access_token)
                    set_refresh_cookies(response, refresh_token)
                    return response
                else:
                    flash("Credenciales inválidas", "danger")
            else:
                flash("Credenciales inválidas", "danger")


        except Exception as e:
            current_app.logger.error(f"Login error: {e}", exc_info=True)
            flash("Error processing login", "danger")
        finally:
            conn.close()

    return render_template('login.html')

@login_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not all([name, email, password, confirm_password]):
            flash('Por favor complete todos los campos requeridos.', 'warning')
            return redirect(url_for('login_bp.register'))

        if password != confirm_password:
            flash('Las contraseñas no coinciden.', 'danger')
            return redirect(url_for('login_bp.register'))

        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        conn = get_db_connection()
        
        if conn is None:
            flash("Error de conexión a la base de datos.", "danger")
            return redirect(url_for('login_bp.register'))

        try:
            cur = conn.cursor()
            cur.execute('SELECT 1 FROM "users" WHERE "email" = %s', (email,))
            if cur.fetchone():
                flash('El correo electrónico ya está registrado.', 'warning')
                return redirect(url_for('login_bp.register'))

            cur.execute(
                'INSERT INTO "users" ("name", "email", "phone", "password_hash", "is_admin") VALUES (%s, %s, %s, %s, %s)',
                (name, email, phone, hashed_password, False)
            )
            conn.commit()
            flash('Registro exitoso. Revise su correo (y spam) para la bienvenida.', 'success')
            
            try:
                send_registration_notification(email, name, phone)
            except Exception as e:
                current_app.logger.error(f"Failed to send welcome email: {e}")

            return redirect(url_for('login_bp.login'))

        except Exception as e:
            conn.rollback()
            current_app.logger.error(f'Error registering user: {e}')
            flash('Ocurrió un error al registrar el usuario.', 'danger')
        finally:
            if cur: cur.close()
            conn.close()

    return render_template('register.html')

@login_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        if not email:
            flash('Por favor ingrese su correo electrónico.', 'warning')
            return redirect(url_for('login_bp.forgot_password'))
            
        conn = get_db_connection()
        if not conn:
            flash("Error de conexión a la base de datos.", "danger")
            return redirect(url_for('login_bp.forgot_password'))
            
        try:
            cur = conn.cursor()
            cur.execute('SELECT "id", "name" FROM "users" WHERE "email" = %s', (email,))
            user = cur.fetchone()
            cur.close()
            
            if user:
                token = create_password_reset_token(email)
                if token:
                    send_password_reset_email(email, token)
            
            flash('Si existe una cuenta con ese correo, se ha enviado un enlace para restablecer la contraseña.', 'info')
            return redirect(url_for('login_bp.login'))
            
        except Exception as e:
            current_app.logger.error(f'Error in forgot password request: {e}')
            flash('Ocurrió un error. Intente nuevamente más tarde.', 'danger')
        finally:
            conn.close()
            
    return render_template('forgot_password.html')

@login_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    email = verify_reset_token(token)
    
    if not email:
        flash('El enlace para restablecer la contraseña es inválido o ha expirado.', 'danger')
        return redirect(url_for('login_bp.forgot_password'))
        
    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if not password or not confirm_password:
            flash('Por favor complete todos los campos.', 'warning')
            return render_template('reset_password.html', token=token)
            
        if password != confirm_password:
            flash('Las contraseñas no coinciden.', 'danger')
            return render_template('reset_password.html', token=token)
            
        conn = get_db_connection()
        if not conn:
            flash("Error de conexión a la base de datos.", "danger")
            return render_template('reset_password.html', token=token)
            
        try:
            hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
            cur = conn.cursor()
            cur.execute('UPDATE "users" SET "password_hash" = %s WHERE "email" = %s', (hashed_password, email))
            conn.commit()
            cur.close()
            
            delete_reset_token(token)
            flash('Tu contraseña ha sido actualizada exitosamente. Ya puedes iniciar sesión.', 'success')
            return redirect(url_for('login_bp.login'))
            
        except Exception as e:
            conn.rollback()
            current_app.logger.error(f'Error updating password: {e}')
            flash('Ocurrió un error al actualizar la contraseña.', 'danger')
        finally:
            conn.close()
            
    return render_template('reset_password.html', token=token)

@login_bp.route('/change-password', methods=['GET', 'POST'])
def change_password():
    if request.method == 'POST':
        email = request.form.get('email')
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        if not all([email, current_password, new_password, confirm_password]):
            flash('Por favor complete todos los campos.', 'warning')
            return render_template('change_password.html', email=email)

        if new_password != confirm_password:
            flash('Las contraseñas nuevas no coinciden.', 'danger')
            return render_template('change_password.html', email=email)

        conn = get_db_connection()
        if not conn:
            flash('Error de conexión a la base de datos.', 'danger')
            return render_template('change_password.html', email=email)

        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)
            cur.execute('SELECT "id", "password_hash" FROM "users" WHERE "email" = %s', (email,))
            user = cur.fetchone()

            if not user or not bcrypt.check_password_hash(user['password_hash'], current_password):
                flash('Correo electrónico o contraseña actual incorrectos.', 'danger')
                return render_template('change_password.html', email=email)

            hashed_new = bcrypt.generate_password_hash(new_password).decode('utf-8')
            cur.execute('UPDATE "users" SET "password_hash" = %s WHERE "email" = %s', (hashed_new, email))
            conn.commit()
            cur.close()

            flash('Contraseña actualizada exitosamente.', 'success')
            return redirect(url_for('login_bp.login'))

        except Exception as e:
            conn.rollback()
            current_app.logger.error(f'Error changing password: {e}', exc_info=True)
            flash('Ocurrió un error al cambiar la contraseña.', 'danger')
        finally:
            conn.close()

    return render_template('change_password.html', email=request.args.get('email', ''))

@login_bp.route('/logout')
def logout():
    response = redirect(url_for('login_bp.login'))
    unset_jwt_cookies(response)
    flash('Has cerrado sesión exitosamente.', 'success')
    return response

# External Services Endpoints removed because internally we don't need CloudRunServiceClient or Health Checks in the monolith. Wait for other blueprints to be completed.
