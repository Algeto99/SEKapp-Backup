import logging
import os
import traceback
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
import psycopg2
import psycopg2.extras

from db import get_db_connection

app_logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin_bp", __name__)

bcrypt = None

def init_admin_bp(app_bcrypt):
    global bcrypt
    bcrypt = app_bcrypt


def _error_page(e, context='Panel de Administración'):
    """Render a user-facing error page. Error ID links to server logs; details stay server-side."""
    error_id = os.urandom(4).hex().upper()
    app_logger.error(f"[{error_id}] Error in {context}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    claims = get_jwt()
    return render_template(
        'admin_error.html',
        error_id=error_id,
        error_detail=f"Error interno del servidor. Referencia: {error_id}",
        context=context,
        user_name=claims.get('name', get_jwt_identity()),
    ), 500


def _is_super_admin():
    """DB-only check — used as fallback when JWT claim is False."""
    email = get_jwt_identity()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute('SELECT is_super_admin FROM users WHERE email = %s', (email,))
        row = cur.fetchone()
        cur.close()
        result = bool(row and row['is_super_admin'])
        app_logger.info(f"DB super_admin fallback for {email}: {result}")
        return result
    except Exception as e:
        app_logger.error(f"DB super_admin check error for {email}: {e}")
        return False
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@admin_bp.route('/debug')
@jwt_required()
def debug():
    import os as _os
    if _os.environ.get('FLASK_ENV', 'production') == 'production' and not _os.environ.get('ENABLE_DEBUG_ROUTE'):
        return jsonify({'error': 'Not found'}), 404
    if not _is_super_admin():
        return jsonify({'error': 'Forbidden'}), 403
    email = get_jwt_identity()
    claims = get_jwt()
    conn = None
    db_info = {}
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'users' AND column_name = 'is_super_admin'
        """)
        col_exists = cur.fetchone() is not None
        db_info['column_exists'] = col_exists
        if col_exists:
            cur.execute('SELECT is_super_admin FROM users WHERE email = %s', (email,))
            row = cur.fetchone()
            db_info['db_value'] = bool(row and row['is_super_admin'])
        cur.close()
    except Exception as e:
        app_logger.error(f"admin debug route DB check error: {e}", exc_info=True)
        db_info['error'] = 'DB check failed'
    finally:
        if conn:
            conn.close()
    return jsonify({
        'email': email,
        'jwt_is_super_admin': claims.get('is_super_admin'),
        'jwt_is_admin': claims.get('is_admin'),
        'db': db_info
    })


@admin_bp.route('/')
@jwt_required()
def panel():
    email = get_jwt_identity()
    claims = get_jwt()
    jwt_flag = claims.get('is_super_admin', False)
    db_flag = _is_super_admin()
    app_logger.info(f"Admin panel attempt — email={email} jwt_super={jwt_flag} db_super={db_flag} all_claims={dict(claims)}")
    if not (jwt_flag or db_flag):
        app_logger.warning(f"Unauthorized admin panel access by {email}")
        flash('No tienes permisos para acceder a esta sección.', 'error')
        return redirect('/landing/')

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        try:
            cur.execute("""
                SELECT id, name, email, phone_number,
                       is_admin, is_super_admin, is_active, company_id, created_at,
                       force_password_change
                FROM users ORDER BY created_at DESC
            """)
        except Exception:
            conn.rollback()
            cur.execute("""
                SELECT id, name, email, phone_number,
                       is_admin, is_super_admin, is_active, company_id, created_at,
                       FALSE AS force_password_change
                FROM users ORDER BY created_at DESC
            """)
        users = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT id, name FROM companies WHERE is_active = TRUE ORDER BY name")
        companies = [dict(r) for r in cur.fetchall()]
        cur.close()
        claims = get_jwt()
        return render_template('admin_panel.html', users=users, companies=companies,
                               user_name=claims.get('name', get_jwt_identity()))
    except Exception as e:
        return _error_page(e, 'Cargar panel de administración')
    finally:
        if conn:
            conn.close()


@admin_bp.route('/users/create', methods=['POST'])
@jwt_required()
def create_user():
    if not _is_super_admin():
        return redirect('/landing/')


    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    phone = request.form.get('phone_number', '').strip()
    password = request.form.get('password', '').strip()
    is_admin = request.form.get('is_admin') == '1'
    company_id = request.form.get('company_id') or None

    if not all([name, email, password]):
        flash('Nombre, correo y contraseña son requeridos.', 'error')
        return redirect(url_for('admin_bp.panel'))

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute('SELECT id FROM users WHERE email = %s', (email,))
        if cur.fetchone():
            flash(f'Ya existe un usuario con el correo {email}.', 'error')
            return redirect(url_for('admin_bp.panel'))

        force_pw = request.form.get('force_password_change') == '1'
        hashed = bcrypt.generate_password_hash(password).decode('utf-8')
        cur.execute(
            """INSERT INTO users (name, email, phone_number, password_hash,
                                  is_admin, is_active, company_id, force_password_change)
               VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s)""",
            (name, email, phone or None, hashed, is_admin, company_id, force_pw)
        )
        conn.commit()
        cur.close()
        app_logger.info(f"Super admin created user {email}")
        flash(f'Usuario {email} creado exitosamente.', 'success')
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error creating user: {e}", exc_info=True)
        flash('Error al crear el usuario. Intente nuevamente.', 'error')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('admin_bp.panel'))


@admin_bp.route('/users/<int:user_id>/toggle-admin', methods=['POST'])
@jwt_required()
def toggle_admin(user_id):
    if not _is_super_admin():
        return redirect('/landing/')

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute('SELECT email, is_admin, is_super_admin FROM users WHERE id = %s', (user_id,))
        user = cur.fetchone()
        if not user:
            flash('Usuario no encontrado.', 'error')
            return redirect(url_for('admin_bp.panel'))
        if user['is_super_admin']:
            flash('No se puede modificar el rol de un super administrador.', 'error')
            return redirect(url_for('admin_bp.panel'))
        new_val = not user['is_admin']
        cur.execute('UPDATE users SET is_admin = %s, updated_at = NOW() WHERE id = %s', (new_val, user_id))
        conn.commit()
        cur.close()
        label = 'administrador' if new_val else 'usuario regular'
        app_logger.info(f"Super admin set user {user['email']} is_admin={new_val}")
        flash(f'{user["email"]} ahora es {label}.', 'success')
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error toggling admin: {e}", exc_info=True)
        flash('Error al actualizar el rol. Intente nuevamente.', 'error')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('admin_bp.panel'))


@admin_bp.route('/users/<int:user_id>/toggle-active', methods=['POST'])
@jwt_required()
def toggle_active(user_id):
    if not _is_super_admin():
        return redirect('/landing/')

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute('SELECT email, is_active, is_super_admin FROM users WHERE id = %s', (user_id,))
        user = cur.fetchone()
        if not user:
            flash('Usuario no encontrado.', 'error')
            return redirect(url_for('admin_bp.panel'))
        if user['is_super_admin']:
            flash('No se puede desactivar a un super administrador.', 'error')
            return redirect(url_for('admin_bp.panel'))
        new_val = not user['is_active']
        cur.execute('UPDATE users SET is_active = %s, updated_at = NOW() WHERE id = %s', (new_val, user_id))
        conn.commit()
        cur.close()
        label = 'activado' if new_val else 'desactivado'
        app_logger.info(f"Super admin {label} user {user['email']}")
        flash(f'Usuario {user["email"]} {label}.', 'success')
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error toggling active: {e}", exc_info=True)
        flash('Error al actualizar el estado. Intente nuevamente.', 'error')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('admin_bp.panel'))


@admin_bp.route('/users/<int:user_id>/toggle-force-password', methods=['POST'])
@jwt_required()
def toggle_force_password(user_id):
    if not _is_super_admin():
        return redirect('/landing/')
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute('SELECT email, force_password_change FROM users WHERE id = %s', (user_id,))
        user = cur.fetchone()
        if not user:
            flash('Usuario no encontrado.', 'error')
            return redirect(url_for('admin_bp.panel'))
        new_val = not bool(user['force_password_change'])
        cur.execute('UPDATE users SET force_password_change = %s, updated_at = NOW() WHERE id = %s', (new_val, user_id))
        conn.commit()
        cur.close()
        label = 'activado' if new_val else 'desactivado'
        app_logger.info(f"Super admin {label} force_password_change for {user['email']}")
        flash(f'Cambio de contraseña obligatorio {label} para {user["email"]}.', 'success')
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error toggling force_password_change: {e}", exc_info=True)
        flash('Error al actualizar cambio de contraseña. Intente nuevamente.', 'error')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('admin_bp.panel'))


@admin_bp.route('/users/<int:user_id>/reset-password', methods=['POST'])
@jwt_required()
def reset_password(user_id):
    if not _is_super_admin():
        return redirect('/landing/')


    new_password = request.form.get('new_password', '').strip()
    if len(new_password) < 8:
        flash('La contraseña debe tener al menos 8 caracteres.', 'error')
        return redirect(url_for('admin_bp.panel'))

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute('SELECT email FROM users WHERE id = %s', (user_id,))
        user = cur.fetchone()
        if not user:
            flash('Usuario no encontrado.', 'error')
            return redirect(url_for('admin_bp.panel'))
        hashed = bcrypt.generate_password_hash(new_password).decode('utf-8')
        cur.execute('UPDATE users SET password_hash = %s, updated_at = NOW() WHERE id = %s', (hashed, user_id))
        conn.commit()
        cur.close()
        app_logger.info(f"Super admin reset password for {user['email']}")
        flash(f'Contraseña de {user["email"]} actualizada.', 'success')
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error resetting password: {e}", exc_info=True)
        flash('Error al actualizar la contraseña. Intente nuevamente.', 'error')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('admin_bp.panel'))


# ---------------------------------------------------------------------------
# KPI Thresholds
# ---------------------------------------------------------------------------

_THRESHOLD_KEYS = [
    'supervision_verde_min',
    'supervision_amarillo_min',
    'supervision_amarillo_max',
    'supervision_rojo_max',
    'equipos_verde_max',
    'equipos_amarillo_min',
    'equipos_amarillo_max',
    'equipos_rojo_min',
    'dias_sin_supervision_alerta',
    'horas_incidente_escalar',
    'dias_certificacion_vencer',
]

_THRESHOLD_DEFAULTS = {
    'supervision_verde_min':       90,
    'supervision_amarillo_min':    70,
    'supervision_amarillo_max':    89,
    'supervision_rojo_max':        70,
    'equipos_verde_max':            5,
    'equipos_amarillo_min':         5,
    'equipos_amarillo_max':        15,
    'equipos_rojo_min':            15,
    'dias_sin_supervision_alerta':  2,
    'horas_incidente_escalar':     24,
    'dias_certificacion_vencer':   30,
}


def _ensure_thresholds_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kpi_thresholds (
            key        VARCHAR(100) PRIMARY KEY,
            value      NUMERIC      NOT NULL,
            updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_by TEXT
        )
    """)
    for k, v in _THRESHOLD_DEFAULTS.items():
        cur.execute(
            "INSERT INTO kpi_thresholds (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
            (k, v)
        )
    conn.commit()
    cur.close()


def get_thresholds():
    """Return current thresholds as a dict, falling back to defaults on error."""
    conn = None
    try:
        conn = get_db_connection()
        _ensure_thresholds_table(conn)
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT key, value FROM kpi_thresholds WHERE key = ANY(%s)", (_THRESHOLD_KEYS,))
        rows = {r['key']: float(r['value']) for r in cur.fetchall()}
        cur.close()
        result = dict(_THRESHOLD_DEFAULTS)
        result.update(rows)
        return result
    except Exception as e:
        app_logger.error(f"Error fetching thresholds: {e}", exc_info=True)
        return dict(_THRESHOLD_DEFAULTS)
    finally:
        if conn:
            conn.close()


@admin_bp.route('/thresholds', methods=['GET'])
@jwt_required()
def thresholds():
    claims = get_jwt()
    is_admin = claims.get('is_admin', False) or _is_super_admin()
    if not is_admin:
        return redirect('/landing/')
    conn = None
    try:
        conn = get_db_connection()
        _ensure_thresholds_table(conn)
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT key, value FROM kpi_thresholds WHERE key = ANY(%s)", (_THRESHOLD_KEYS,))
        rows = {r['key']: float(r['value']) for r in cur.fetchall()}
        cur.close()
        t = dict(_THRESHOLD_DEFAULTS)
        t.update(rows)
        return render_template(
            'admin_thresholds.html',
            thresholds=t,
            user_name=claims.get('name', get_jwt_identity()),
            is_admin=True,
        )
    except Exception as e:
        return _error_page(e, 'Umbrales KPI')
    finally:
        if conn:
            conn.close()


@admin_bp.route('/thresholds', methods=['POST'])
@jwt_required()
def save_thresholds():
    claims = get_jwt()
    is_admin = claims.get('is_admin', False) or _is_super_admin()
    if not is_admin:
        return redirect('/landing/')
    email = get_jwt_identity()
    conn = None
    try:
        conn = get_db_connection()
        _ensure_thresholds_table(conn)
        cur = conn.cursor()
        for key in _THRESHOLD_KEYS:
            raw = request.form.get(key, '').strip()
            if raw == '':
                continue
            try:
                val = float(raw)
            except ValueError:
                flash(f'Valor inválido para {key}.', 'error')
                return redirect(url_for('admin_bp.thresholds'))
            cur.execute(
                """INSERT INTO kpi_thresholds (key, value, updated_at, updated_by)
                   VALUES (%s, %s, NOW(), %s)
                   ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value, updated_at = NOW(), updated_by = EXCLUDED.updated_by""",
                (key, val, email)
            )
        conn.commit()
        cur.close()
        app_logger.info(f"Thresholds updated by {email}")
        flash('Umbrales actualizados correctamente.', 'success')
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error saving thresholds: {e}", exc_info=True)
        flash('Error al guardar umbrales. Intente nuevamente.', 'error')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('admin_bp.thresholds'))
