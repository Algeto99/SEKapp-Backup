import os
import secrets
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, Response, current_app
from flask_jwt_extended import (
    create_access_token, create_refresh_token, unset_jwt_cookies,
    set_access_cookies, set_refresh_cookies, get_jwt_identity, get_jwt, jwt_required
)
from psycopg2 import extras
import psycopg2

from db import get_db_connection
from email_utils import send_email, send_password_reset_email, send_welcome_email, send_registration_notification

# --- Initialize Blueprint ---
login_bp = Blueprint('login_bp', __name__)

def _safe_redirect(next_url, fallback):
    if not next_url:
        return fallback
    parsed = urlparse(next_url)
    if parsed.scheme or parsed.netloc:
        return fallback
    path = parsed.path
    if not path.startswith('/') or path.startswith('//'):
        return fallback
    return path

# Extensions (from main app)
bcrypt = None

def init_login_bp(app_bcrypt):
    global bcrypt
    bcrypt = app_bcrypt

# --- Security and Tokens ---
def generate_reset_token():
    return secrets.token_urlsafe(32)

def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()

def create_password_reset_token(email):
    conn = get_db_connection()
    if not conn:
        return None
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
        expires = result['expires_at']
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
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
            cur.execute('SELECT "id", "email", "password_hash", "name", "is_admin", "is_active" FROM "users" WHERE "email" = %s', (email,))
            user = cur.fetchone()
            auth_entry = None

            if user and not user['is_active']:
                flash("Credenciales inválidas", "danger")
                cur.close()
                return render_template('login.html')

            # Also check authorized_emails for admin status (same as original login service)
            if user:
                cur.execute('SELECT "is_admin", "is_active" FROM "authorized_emails" WHERE "email" = %s', (email,))
                auth_entry = cur.fetchone()

            # Fetch is_super_admin and force_password_change — columns may not exist yet
            is_super_admin = False
            force_password_change = False
            if user:
                try:
                    cur.execute('SELECT "is_super_admin", "force_password_change" FROM "users" WHERE "id" = %s', (user['id'],))
                    row = cur.fetchone()
                    if row:
                        is_super_admin = bool(row['is_super_admin'])
                        force_password_change = bool(row['force_password_change'])
                except Exception:
                    conn.rollback()

            cur.close()

            if user:
                is_valid = bcrypt.check_password_hash(user['password_hash'], password)

                if is_valid:
                    # Resolve is_admin: prefer authorized_emails.is_admin if present and active
                    is_admin = bool(user.get('is_admin'))
                    if auth_entry and auth_entry.get('is_active'):
                        is_admin = bool(auth_entry.get('is_admin', is_admin))
                    current_app.logger.info(f"User {email} logged in. is_admin={is_admin}, is_super_admin={is_super_admin}, force_password_change={force_password_change}")
                    access_token = create_access_token(
                        identity=user['email'],
                        additional_claims={'is_admin': is_admin, 'is_super_admin': is_super_admin, 'name': user['name']}
                    )
                    refresh_token = create_refresh_token(identity=user['email'])

                    if force_password_change:
                        flash('Debes cambiar tu contraseña antes de continuar.', 'warning')
                        response = redirect(url_for('login_bp.change_password', forced='1'))
                        set_access_cookies(response, access_token)
                        set_refresh_cookies(response, refresh_token)
                        return response

                    fallback = '/cgeo/morning-briefing/' if is_admin else url_for('landing_bp.landing_page')
                    redirect_target = _safe_redirect(
                        request.args.get('next') or request.form.get('next'),
                        fallback=fallback
                    )
                    response = redirect(redirect_target)
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
        phone = request.form.get('phone_number') or request.form.get('phone')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not all([name, email, password, confirm_password]):
            flash('Por favor complete todos los campos requeridos.', 'warning')
            return redirect(url_for('login_bp.register'))

        if password != confirm_password:
            flash('Las contraseñas no coinciden.', 'danger')
            return redirect(url_for('login_bp.register'))

        if len(password) < 8:
            flash('La contraseña debe tener al menos 8 caracteres.', 'warning')
            return redirect(url_for('login_bp.register'))

        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        conn = get_db_connection()

        if conn is None:
            flash("Error de conexión a la base de datos.", "danger")
            return redirect(url_for('login_bp.register'))

        cur = None
        try:
            cur = conn.cursor()
            cur.execute('SELECT "is_active" FROM "authorized_emails" WHERE "email" = %s', (email,))
            auth = cur.fetchone()
            if not auth or not auth[0]:
                flash('Este correo no está autorizado para registrarse.', 'danger')
                return redirect(url_for('login_bp.register'))

            cur.execute('SELECT 1 FROM "users" WHERE "email" = %s', (email,))
            if cur.fetchone():
                flash('El correo electrónico ya está registrado.', 'warning')
                return redirect(url_for('login_bp.register'))

            cur.execute(
                'INSERT INTO "users" ("name", "email", "phone_number", "password_hash", "is_admin") VALUES (%s, %s, %s, %s, %s)',
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

        if len(password) < 8:
            flash('La contraseña debe tener al menos 8 caracteres.', 'warning')
            return render_template('reset_password.html', token=token)

        conn = get_db_connection()
        if not conn:
            flash("Error de conexión a la base de datos.", "danger")
            return render_template('reset_password.html', token=token)
            
        try:
            hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
            cur = conn.cursor()
            cur.execute('UPDATE "users" SET "password_hash" = %s, "updated_at" = NOW() WHERE "email" = %s', (hashed_password, email))
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
        forced = request.form.get('forced', '0') == '1'
        # For forced changes the user is already authenticated — read email from JWT
        # to avoid PII in form fields/URLs. Fall back to form value for voluntary changes.
        try:
            from flask_jwt_extended import verify_jwt_in_request
            verify_jwt_in_request(optional=True)
            jwt_email = get_jwt_identity()
        except Exception:
            jwt_email = None
        email = jwt_email if (forced and jwt_email) else request.form.get('email')

        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        if not all([email, current_password, new_password, confirm_password]):
            flash('Por favor complete todos los campos.', 'warning')
            return render_template('change_password.html', email='', forced=forced)

        if new_password != confirm_password:
            flash('Las contraseñas nuevas no coinciden.', 'danger')
            return render_template('change_password.html', email=email, forced=forced)

        conn = get_db_connection()
        if not conn:
            flash('Error de conexión a la base de datos.', 'danger')
            return render_template('change_password.html', email=email, forced=forced)

        try:
            cur = conn.cursor(cursor_factory=extras.DictCursor)
            cur.execute('SELECT "id", "password_hash" FROM "users" WHERE "email" = %s', (email,))
            user = cur.fetchone()

            if not user or not bcrypt.check_password_hash(user['password_hash'], current_password):
                flash('Correo electrónico o contraseña actual incorrectos.', 'danger')
                return render_template('change_password.html', email=email, forced=forced)

            hashed_new = bcrypt.generate_password_hash(new_password).decode('utf-8')
            cur.execute(
                'UPDATE "users" SET "password_hash" = %s, "force_password_change" = FALSE, "updated_at" = NOW() WHERE "email" = %s',
                (hashed_new, email)
            )
            conn.commit()
            cur.close()

            flash('Contraseña actualizada exitosamente.', 'success')
            # If this was a forced change, go to landing; otherwise back to login
            if request.form.get('forced') == '1':
                return redirect(url_for('landing_bp.landing_page'))
            return redirect(url_for('login_bp.login'))

        except Exception as e:
            conn.rollback()
            current_app.logger.error(f'Error changing password: {e}', exc_info=True)
            flash('Ocurrió un error al cambiar la contraseña.', 'danger')
        finally:
            conn.close()

    forced = request.args.get('forced', '0') == '1'
    return render_template('change_password.html', email='', forced=forced)

@login_bp.route('/logout')
def logout():
    response = redirect(url_for('login_bp.login'))
    unset_jwt_cookies(response)
    flash('Has cerrado sesión exitosamente.', 'success')
    return response

# External Services Endpoints removed because internally we don't need CloudRunServiceClient or Health Checks in the monolith. Wait for other blueprints to be completed.
