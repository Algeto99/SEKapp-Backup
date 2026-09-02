import logging
import os
import traceback
from flask import Blueprint, render_template, request, redirect, url_for, flash
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
        cur.execute("""
            SELECT id, name, email, phone_number,
                   is_admin, is_super_admin, is_active, company_id, created_at,
                   force_password_change
            FROM users ORDER BY created_at DESC
        """)
        users = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT id, name, enabled_modules FROM companies WHERE is_active = TRUE ORDER BY name")
        companies = [dict(r) for r in cur.fetchall()]
        for c in companies:
            c['enabled_modules'] = list(c['enabled_modules']) if c.get('enabled_modules') else []
        cur.close()
        claims = get_jwt()
        return render_template('admin_panel.html', users=users, companies=companies,
                               user_name=claims.get('name', get_jwt_identity()))
    except Exception as e:
        return _error_page(e, 'Cargar panel de administración')
    finally:
        if conn:
            conn.close()


def _default_company_id(cur):
    """SEKapp corre una instancia por empresa: id de la única empresa activa."""
    cur.execute("SELECT id FROM companies WHERE is_active = TRUE ORDER BY id LIMIT 1")
    row = cur.fetchone()
    return row[0] if row else None


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

        # Si no se eligió empresa, asignar la única empresa activa por defecto.
        if not company_id:
            company_id = _default_company_id(cur)

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


OPTIONAL_MODULES = {
    'log_de_patrullas': 'Log de Patrullas',
}


@admin_bp.route('/companies/<int:company_id>/toggle-module', methods=['POST'])
@jwt_required()
def toggle_company_module(company_id):
    if not _is_super_admin():
        return redirect('/landing/')

    module_key = request.form.get('module_key', '')
    if module_key not in OPTIONAL_MODULES:
        flash('Módulo desconocido.', 'error')
        return redirect(url_for('admin_bp.panel'))

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute('SELECT name, enabled_modules FROM companies WHERE id = %s', (company_id,))
        company = cur.fetchone()
        if not company:
            flash('Licencia no encontrada.', 'error')
            return redirect(url_for('admin_bp.panel'))

        modules = set(company['enabled_modules'] or [])
        if module_key in modules:
            modules.discard(module_key)
            action = 'desactivado'
        else:
            modules.add(module_key)
            action = 'activado'

        cur.execute(
            'UPDATE companies SET enabled_modules = %s WHERE id = %s',
            (psycopg2.extras.Json(sorted(modules)), company_id)
        )
        conn.commit()
        cur.close()
        app_logger.info(f"Super admin {action} module '{module_key}' for company {company['name']} ({company_id})")
        flash(f'{OPTIONAL_MODULES[module_key]} {action} para {company["name"]}.', 'success')
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error toggling company module: {e}", exc_info=True)
        flash('Error al actualizar el módulo. Intente nuevamente.', 'error')
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
        cur.execute('SELECT email, is_active, is_super_admin, company_id FROM users WHERE id = %s', (user_id,))
        user = cur.fetchone()
        if not user:
            flash('Usuario no encontrado.', 'error')
            return redirect(url_for('admin_bp.panel'))
        if user['is_super_admin']:
            flash('No se puede desactivar a un super administrador.', 'error')
            return redirect(url_for('admin_bp.panel'))
        new_val = not user['is_active']
        # Al activar, asignar la empresa si el usuario no tiene (una instancia por empresa).
        if new_val and user['company_id'] is None:
            cur.execute(
                'UPDATE users SET is_active = %s, company_id = %s, updated_at = NOW() WHERE id = %s',
                (new_val, _default_company_id(cur), user_id)
            )
        else:
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


@admin_bp.route('/users/assign-company', methods=['POST'])
@jwt_required()
def assign_company_all():
    """Asigna la única empresa activa a todos los usuarios que no tienen empresa.
    SEKapp corre una instancia por empresa, así que el vínculo es permanente."""
    if not _is_super_admin():
        return redirect('/landing/')

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        company_id = _default_company_id(cur)
        if company_id is None:
            flash('No hay una empresa activa configurada.', 'error')
            return redirect(url_for('admin_bp.panel'))
        cur.execute('UPDATE users SET company_id = %s, updated_at = NOW() WHERE company_id IS NULL', (company_id,))
        n = cur.rowcount
        conn.commit()
        cur.close()
        app_logger.info(f"Super admin asignó empresa {company_id} a {n} usuario(s)")
        flash(f'{n} usuario(s) asignados a la empresa.', 'success')
    except Exception as e:
        if conn:
            conn.rollback()
        app_logger.error(f"Error assigning company: {e}", exc_info=True)
        flash('Error al asignar empresa. Intente nuevamente.', 'error')
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
    'supervision_meta',
    'equipos_verde_max',
    'equipos_amarillo_min',
    'equipos_amarillo_max',
    'equipos_rojo_min',
    'dias_sin_supervision_alerta',
    'horas_incidente_escalar',
    'dias_certificacion_vencer',
    'dias_compromiso_vencer',
    'dias_backup_frecuencia',
    'visita_verde_min',
    'visita_amarillo_min',
    'visita_amarillo_max',
    'visita_rojo_max',
    'visita_meta',
    # Pesos de los 4 ejes del Estatus de Cliente. Viven aquí, y no en una tabla
    # propia, para reusar el guardado y la lectura que ya tiene kpi_thresholds.
    'estatus_peso_satisfaccion',
    'estatus_peso_atencion',
    'estatus_peso_servicio',
    'estatus_peso_eventos',
    # Bandas del semáforo del Estatus de Cliente. Son propias porque la escala
    # `supervision_*` mide cumplimiento de supervisiones, no una nota compuesta,
    # y moverla afectaría al Morning Briefing.
    'estatus_banda_optimo',
    'estatus_banda_observacion',
    'estatus_banda_seguimiento',
]

# Bandas en orden descendente: (clave, etiqueta, tono).
_ESTATUS_BANDAS = [
    ('estatus_banda_optimo',      'Óptimo',               'green'),
    ('estatus_banda_observacion', 'En observación',       'yellow'),
    ('estatus_banda_seguimiento', 'Requiere seguimiento', 'orange'),
]
_ESTATUS_BANDA_INFERIOR = ('Crítico', 'red')

# (clave de umbral, eje) — el orden es el que se muestra en pantalla.
_ESTATUS_EJES = [
    ('estatus_peso_satisfaccion', 'satisfaccion'),
    ('estatus_peso_atencion',     'atencion'),
    ('estatus_peso_servicio',     'servicio'),
    ('estatus_peso_eventos',      'eventos'),
]

# Claves cuyo valor es texto (no numérico)
_THRESHOLD_TEXT_KEYS = ['fecha_inicio_operacion', 'supervision_periodicidad', 'visita_periodicidad']

_PERIODICIDAD_VALUES = ('diario', 'semanal', 'mensual')

_THRESHOLD_DEFAULTS = {
    'supervision_verde_min':       90,
    'supervision_amarillo_min':    70,
    'supervision_amarillo_max':    89,
    'supervision_rojo_max':        70,
    'supervision_meta':            25,
    'equipos_verde_max':            5,
    'equipos_amarillo_min':         5,
    'equipos_amarillo_max':        15,
    'equipos_rojo_min':            15,
    'dias_sin_supervision_alerta':  2,
    'horas_incidente_escalar':     24,
    'dias_certificacion_vencer':   30,
    'dias_compromiso_vencer':       5,
    # Cada cuántos días debe repetirse el Backup de Información antes de que el
    # Morning Briefing lo marque como pendiente.
    'dias_backup_frecuencia':       7,
    'visita_verde_min':            90,
    'visita_amarillo_min':         70,
    'visita_amarillo_max':         89,
    'visita_rojo_max':             70,
    'visita_meta':                 20,
    'estatus_peso_satisfaccion':   30,
    'estatus_peso_atencion':       25,
    'estatus_peso_servicio':       25,
    'estatus_peso_eventos':        20,
    'estatus_banda_optimo':        90,
    'estatus_banda_observacion':   75,
    'estatus_banda_seguimiento':   60,
}


def _ensure_thresholds_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kpi_thresholds (
            key        VARCHAR(100) PRIMARY KEY,
            value      NUMERIC      NOT NULL,
            updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_by TEXT,
            text_value TEXT
        )
    """)
    # Auto-reparar tablas creadas por versiones anteriores que no tienen
    # la columna text_value ni una restricción única sobre `key`.
    cur.execute("ALTER TABLE kpi_thresholds ADD COLUMN IF NOT EXISTS text_value TEXT")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS kpi_thresholds_key_uidx ON kpi_thresholds (key)")
    for k, v in _THRESHOLD_DEFAULTS.items():
        cur.execute(
            "INSERT INTO kpi_thresholds (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
            (k, v)
        )
    conn.commit()
    cur.close()


def _periodo_inicio_actual(periodicidad):
    """Fecha de inicio del período vigente (día/semana/mes) según la periodicidad
    configurada para la meta de supervisiones."""
    from datetime import date, timedelta
    today = date.today()
    if periodicidad == 'semanal':
        return today - timedelta(days=today.weekday())
    if periodicidad == 'mensual':
        return today.replace(day=1)
    return today


# ── Programación de supervisiones por Cliente / Empresa ──────────────────────

def _ensure_supervision_programacion(conn):
    """La migración viaja con el código, como en asignaciones_hallazgo."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS supervision_programacion (
                customer_company_id INTEGER PRIMARY KEY,
                periodicidad        TEXT    NOT NULL DEFAULT 'semanal',
                meta                INTEGER NOT NULL DEFAULT 0,
                actualizado_en      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                actualizado_por     TEXT
            )
        """)
    conn.commit()


def get_supervision_programacion(cur):
    """
    Programación vigente por cliente, con todos los clientes activos aunque no
    tengan fila propia (meta 0), para que el formulario los liste completos.
    """
    cur.execute("SELECT to_regclass('supervision_programacion') AS t")
    row = cur.fetchone()
    tiene_tabla = bool(row and (row[0] if not isinstance(row, dict) else row.get('t')))

    if tiene_tabla:
        cur.execute("""
            SELECT cc.id, cc.name,
                   COALESCE(sp.periodicidad, 'semanal') AS periodicidad,
                   COALESCE(sp.meta, 0)                 AS meta
            FROM customer_companies cc
            LEFT JOIN supervision_programacion sp ON sp.customer_company_id = cc.id
            ORDER BY cc.name
        """)
    else:
        cur.execute("SELECT id, name, 'semanal' AS periodicidad, 0 AS meta "
                    "FROM customer_companies ORDER BY name")
    return [dict(r) for r in cur.fetchall()]


def calcular_supervisiones(cur, cliente=None, propiedad=None):
    """
    Programadas / realizadas / % de cumplimiento de supervisiones.

    La fuente es la programación por cliente: cada uno aporta su meta medida
    sobre SU PROPIA ventana, porque un cliente semanal y uno mensual no se
    pueden sumar sobre el mismo período. Mientras ningún cliente tenga
    programación se usa la meta global, de modo que el KPI no cambia hasta que
    el Administrador configure el primero.

    `realizadas` cuenta registros de supervisión, no instalaciones distintas:
    es lo que hace comparable "4 de 5 programadas".

    Devuelve dict con programadas, realizadas, pendientes, pct y por_cliente.
    """
    from dashboard_bp import _add_scope_filters

    # Días que cubre cada ventana, para repartir la meta en la gráfica diaria:
    # una meta semanal no es un objetivo de cada día.
    _DIAS_PERIODO = {'diario': 1, 'semanal': 7, 'mensual': 30}

    def _contar(conds, params):
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        cur.execute(f"SELECT COUNT(*) AS n FROM supervision_puesto {where}", tuple(params))
        r = cur.fetchone()
        return int((r[0] if not isinstance(r, dict) else r.get('n')) or 0)

    programacion = [p for p in get_supervision_programacion(cur) if (p['meta'] or 0) > 0]

    # Filtro de pantalla: si se está viendo un cliente concreto, solo ese cuenta.
    if cliente and str(cliente).isdigit():
        programacion = [p for p in programacion if str(p['id']) == str(cliente)]

    if not programacion:
        # Camino heredado: meta única global. Se conserva intacto salvo el conteo,
        # que pasa a ser de registros para alinearse con la programación por cliente.
        t = get_thresholds()
        meta = int(t.get('supervision_meta') or 0)
        periodicidad = t.get('supervision_periodicidad') or 'diario'
        conds, params = ["fecha_hora::date >= %s"], [_periodo_inicio_actual(periodicidad)]
        _add_scope_filters(conds, params, cliente=cliente, propiedad=propiedad,
                           col_puesto=None)
        realizadas = _contar(conds, params)
        pct = round(realizadas / meta * 100, 1) if meta else None
        return {
            'programadas': meta, 'realizadas': realizadas,
            'pendientes': max(0, meta - realizadas), 'pct': pct,
            'programadas_dia': round(meta / _DIAS_PERIODO.get(periodicidad, 1), 1),
            'origen': 'global', 'periodicidad': periodicidad, 'por_cliente': [],
        }

    total_prog = total_real = 0
    prog_dia = 0.0
    por_cliente = []
    for p in programacion:
        inicio = _periodo_inicio_actual(p['periodicidad'])
        conds, params = ["fecha_hora::date >= %s"], [inicio]
        _add_scope_filters(conds, params, cliente=str(p['id']), propiedad=propiedad,
                           col_puesto=None)
        realizadas = _contar(conds, params)
        meta = int(p['meta'] or 0)
        total_prog += meta
        total_real += realizadas
        prog_dia   += meta / _DIAS_PERIODO.get(p['periodicidad'], 1)
        por_cliente.append({
            'cliente_id': p['id'], 'cliente': p['name'],
            'periodicidad': p['periodicidad'], 'programadas': meta,
            'realizadas': realizadas, 'pendientes': max(0, meta - realizadas),
            'pct': round(realizadas / meta * 100, 1) if meta else None,
            'desde': inicio.isoformat(),
        })

    return {
        'programadas': total_prog, 'realizadas': total_real,
        'pendientes': max(0, total_prog - total_real),
        'pct': round(total_real / total_prog * 100, 1) if total_prog else None,
        'programadas_dia': round(prog_dia, 1),
        'origen': 'por_cliente', 'periodicidad': None, 'por_cliente': por_cliente,
    }


def get_estatus_pesos(thresholds=None):
    """Pesos de los 4 ejes del Estatus de Cliente, normalizados para sumar 100.

    El formulario ya rechaza cualquier combinación que no sume 100, así que la
    normalización es solo una red: cubre filas viejas de `kpi_thresholds` y
    ediciones hechas por fuera de la pantalla. Si todo viene en cero se vuelve
    a los pesos por defecto, porque un total de 0 dejaría el score sin definir.
    """
    t = thresholds if thresholds is not None else get_thresholds()
    crudos = {}
    for key, eje in _ESTATUS_EJES:
        raw = t.get(key)
        if raw is None:
            raw = _THRESHOLD_DEFAULTS[key]
        try:
            crudos[eje] = max(0.0, float(raw))
        except (TypeError, ValueError):
            crudos[eje] = float(_THRESHOLD_DEFAULTS[key])
    total = sum(crudos.values())
    if total <= 0:
        return {eje: float(_THRESHOLD_DEFAULTS[key]) for key, eje in _ESTATUS_EJES}
    return {eje: round(v / total * 100, 2) for eje, v in crudos.items()}


def get_estatus_bandas(thresholds=None):
    """Bandas del Estatus de Cliente, de mayor a menor.

    Devuelve [{'min', 'label', 'tone'}...] más la banda inferior implícita, que
    arranca en 0. Si los mínimos vienen desordenados se ordenan, para que el
    semáforo nunca quede sin una banda alcanzable.
    """
    t = thresholds if thresholds is not None else get_thresholds()
    valores = []
    for key, label, tone in _ESTATUS_BANDAS:
        try:
            v = float(t.get(key, _THRESHOLD_DEFAULTS[key]))
        except (TypeError, ValueError):
            v = float(_THRESHOLD_DEFAULTS[key])
        valores.append([max(0.0, min(100.0, v)), label, tone])
    valores.sort(key=lambda x: x[0], reverse=True)
    bandas = [{'min': v, 'label': label, 'tone': tone} for v, label, tone in valores]
    bandas.append({'min': 0.0, 'label': _ESTATUS_BANDA_INFERIOR[0], 'tone': _ESTATUS_BANDA_INFERIOR[1]})
    return bandas


def get_thresholds():
    """Return current thresholds as a dict, falling back to defaults on error."""
    conn = None
    try:
        conn = get_db_connection()
        _ensure_thresholds_table(conn)
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        all_keys = _THRESHOLD_KEYS + _THRESHOLD_TEXT_KEYS
        cur.execute("SELECT key, value, text_value FROM kpi_thresholds WHERE key = ANY(%s)", (all_keys,))
        rows = {}
        for r in cur.fetchall():
            if r['key'] in _THRESHOLD_TEXT_KEYS:
                rows[r['key']] = r['text_value']
            else:
                rows[r['key']] = float(r['value'])
        cur.close()
        result = dict(_THRESHOLD_DEFAULTS)
        result['fecha_inicio_operacion'] = None
        result['supervision_periodicidad'] = 'diario'
        result['visita_periodicidad'] = 'mensual'
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
        all_keys = _THRESHOLD_KEYS + _THRESHOLD_TEXT_KEYS
        cur.execute("SELECT key, value, text_value FROM kpi_thresholds WHERE key = ANY(%s)", (all_keys,))
        rows = {}
        for r in cur.fetchall():
            if r['key'] in _THRESHOLD_TEXT_KEYS:
                rows[r['key']] = r['text_value']
            else:
                rows[r['key']] = float(r['value'])
        cur.close()
        t = dict(_THRESHOLD_DEFAULTS)
        t['fecha_inicio_operacion'] = None
        t['supervision_periodicidad'] = 'diario'
        t['visita_periodicidad'] = 'mensual'
        t.update(rows)
        try:
            _ensure_supervision_programacion(conn)
            cur2 = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            programacion = get_supervision_programacion(cur2)
            cur2.close()
        except Exception as pe:
            app_logger.warning(f"No se pudo leer la programación de supervisiones: {pe}")
            programacion = []

        from flask import request as _req
        jwt_csrf = _req.cookies.get('csrf_access_token', '')
        return render_template(
            'admin_thresholds.html',
            thresholds=t,
            supervision_programacion=programacion,
            jwt_csrf_token=jwt_csrf,
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

        # Se valida antes de escribir nada: un total distinto de 100 deja la
        # calificación consolidada sin escala y no habría forma de interpretarla.
        pesos_form = {k: request.form.get(k, '').strip() for k, _ in _ESTATUS_EJES}
        if any(v != '' for v in pesos_form.values()):
            try:
                total_pesos = sum(float(v or 0) for v in pesos_form.values())
            except ValueError:
                flash('Los pesos del Estatus de Cliente deben ser numéricos.', 'error')
                return redirect(url_for('admin_bp.thresholds'))
            if abs(total_pesos - 100) > 0.01:
                flash(f'Los pesos del Estatus de Cliente deben sumar 100%. '
                      f'Actualmente suman {total_pesos:g}%.', 'error')
                return redirect(url_for('admin_bp.thresholds'))

        # Las bandas tienen que ser estrictamente descendientes; si no, alguna
        # queda inalcanzable y el estado que muestra la pantalla deja de tener
        # relación con la calificación.
        bandas_form = [request.form.get(k, '').strip() for k, _, _ in _ESTATUS_BANDAS]
        if any(v != '' for v in bandas_form):
            try:
                vals = [float(v or 0) for v in bandas_form]
            except ValueError:
                flash('Las bandas del Estatus de Cliente deben ser numéricas.', 'error')
                return redirect(url_for('admin_bp.thresholds'))
            if not (vals[0] > vals[1] > vals[2]):
                flash('Las bandas del Estatus de Cliente deben ir de mayor a menor: '
                      'Óptimo > En observación > Requiere seguimiento.', 'error')
                return redirect(url_for('admin_bp.thresholds'))

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
        # Guardar clave de texto: fecha_inicio_operacion
        fecha_raw = request.form.get('fecha_inicio_operacion', '').strip()
        if fecha_raw:
            import re
            if re.match(r'^\d{4}-\d{2}-\d{2}$', fecha_raw):
                cur.execute(
                    """INSERT INTO kpi_thresholds (key, value, text_value, updated_at, updated_by)
                       VALUES (%s, 0, %s, NOW(), %s)
                       ON CONFLICT (key) DO UPDATE
                       SET text_value = EXCLUDED.text_value, updated_at = NOW(), updated_by = EXCLUDED.updated_by""",
                    ('fecha_inicio_operacion', fecha_raw, email)
                )
            else:
                flash('Fecha de inicio inválida. Use el formato YYYY-MM-DD.', 'error')
                return redirect(url_for('admin_bp.thresholds'))

        # Guardar claves de texto: periodicidad de supervisiones y de visitas
        for periodicidad_key in ('supervision_periodicidad', 'visita_periodicidad'):
            periodicidad_raw = request.form.get(periodicidad_key, '').strip().lower()
            if periodicidad_raw:
                if periodicidad_raw in _PERIODICIDAD_VALUES:
                    cur.execute(
                        """INSERT INTO kpi_thresholds (key, value, text_value, updated_at, updated_by)
                           VALUES (%s, 0, %s, NOW(), %s)
                           ON CONFLICT (key) DO UPDATE
                           SET text_value = EXCLUDED.text_value, updated_at = NOW(), updated_by = EXCLUDED.updated_by""",
                        (periodicidad_key, periodicidad_raw, email)
                    )
                else:
                    flash('Periodicidad inválida.', 'error')
                    return redirect(url_for('admin_bp.thresholds'))

        # Programación por Cliente / Empresa. Los campos llegan como
        # sup_prog_meta_<id> y sup_prog_periodicidad_<id>; meta 0 significa
        # "sin programación propia" y se guarda igual, para poder volver atrás.
        _ensure_supervision_programacion(conn)
        for campo, valor in request.form.items():
            if not campo.startswith('sup_prog_meta_'):
                continue
            try:
                cliente_id = int(campo[len('sup_prog_meta_'):])
                meta = max(0, int(valor or 0))
            except (ValueError, TypeError):
                continue
            periodicidad = (request.form.get(f'sup_prog_periodicidad_{cliente_id}', '')
                            .strip().lower())
            if periodicidad not in _PERIODICIDAD_VALUES:
                periodicidad = 'semanal'
            cur.execute(
                """INSERT INTO supervision_programacion
                       (customer_company_id, periodicidad, meta, actualizado_en, actualizado_por)
                   VALUES (%s, %s, %s, NOW(), %s)
                   ON CONFLICT (customer_company_id) DO UPDATE
                   SET periodicidad = EXCLUDED.periodicidad,
                       meta = EXCLUDED.meta,
                       actualizado_en = NOW(),
                       actualizado_por = EXCLUDED.actualizado_por""",
                (cliente_id, periodicidad, meta, email)
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
