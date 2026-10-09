"""Perfil Coordinador: rol intermedio entre Supervisor de Seguridad y Administrador.

Pedido de KANAN el 2026-09-28. El Coordinador hace seguimiento y gestión de las
novedades y hallazgos de un ámbito acotado (clientes e instalaciones que se le
asignan) desde Matrices → Alertas / Novedades, sin acceso al Morning Briefing
ni a la información general de la licencia.

Cómo se impone el ámbito
------------------------
- `users.is_coordinador` marca el rol y viaja como claim del JWT, igual que
  `is_admin`. Si un usuario es Administrador y Coordinador a la vez, manda
  Administrador: sin ámbito.
- `coordinador_ambito` guarda una fila por cliente o por instalación asignada.
  Un cliente cubre todas sus instalaciones; una instalación acota más.
- `ambito_activo()` carga el ámbito del Coordinador en sesión una sola vez por
  petición (en `g`). Los helpers de alcance de cgeo_bp (`_add_scope`) y de
  dashboard_bp (`_add_scope_filters`) le agregan `condicion_ambito` a toda
  consulta que pase por ellos: así las reglas de alertas, las asignaciones
  pendientes y las matrices quedan acotadas sin tocar cada consulta.
- Los endpoints de detalle y de escritura verifican `registro_en_ambito` antes
  de responder al Coordinador; fuera del ámbito devuelven 403, que Auditoría
  registra como Rechazado.
- Un Coordinador sin ámbito no ve nada: `condicion_ambito` agrega FALSE.

Esquema
-------
Se crea solo al iniciar sesión (`asegurar_esquema`). `ALTER TABLE users` exige
ser dueño de la tabla: si en una instancia el rol de la app no lo es, el login
sigue funcionando (el rol se lee como False, ver `rol_disponible`) y hay que
correr sql/create_coordinador.sql en Cloud SQL Studio.
"""
import logging
import time
from functools import wraps

from flask import flash, g, has_request_context, jsonify, redirect, request
from flask_jwt_extended import get_jwt, get_jwt_identity

from db import get_db_connection

app_logger = logging.getLogger(__name__)

RUTA_INICIO = '/matrices/alertas'   # a dónde aterriza el Coordinador al entrar

# form_type -> (tabla, pk, tiene cliente_instalacion). Las 11 tablas tienen
# id_propiedad y customer_company_id; log_de_patrullas no tiene cliente_instalacion.
ORIGEN = {
    'reporte_incidente':               ('reportes_incidentes',             'id_reporte_incidente',  True),
    'supervision_puesto':              ('supervision_puesto',              'id_supervision',        True),
    'medicion_experiencia_cliente':    ('medicion_experiencia_cliente',    'id_encuesta',           True),
    'informe_novedades_disciplinario': ('informe_novedades_disciplinario', 'id_informe',            True),
    'log_de_patrullas':                ('log_de_patrullas',                'id_patrulla',           False),
    'registro_de_capacitaciones':      ('registro_de_capacitaciones',      'id_capacitacion',       True),
    'registro_y_acta_de_visita':       ('registro_y_acta_de_visita',       'id_visita',             True),
    'planilla_vehicular':              ('planilla_vehicular',              'id_planilla_vehicular', True),
    'planilla_motocicletas':           ('planilla_motocicletas',           'id',                    True),
    'checklist_cumplimiento':          ('checklist_cumplimiento',          'id',                    True),
    'confiabilidad_equipos':           ('confiabilidad_equipos',           'id',                    True),
}

DDL = """
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_coordinador BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS coordinador_ambito (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    customer_company_id INTEGER REFERENCES customer_companies(id) ON DELETE CASCADE,
    id_propiedad        INTEGER REFERENCES propiedades(id_propiedad) ON DELETE CASCADE,
    creado_en           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    creado_por          VARCHAR(255),
    CHECK (customer_company_id IS NOT NULL OR id_propiedad IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_coordinador_ambito_user ON coordinador_ambito (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_coordinador_ambito
    ON coordinador_ambito (user_id, COALESCE(customer_company_id, 0), COALESCE(id_propiedad, 0));
"""

_esquema_listo = False
_reintento_en = 0.0
_ESPERA_SEGUNDOS = 600
_rol_disponible = None


def asegurar_esquema(conn):
    """Columna de rol y tabla de ámbito. Idempotente; una vez por proceso."""
    global _esquema_listo, _reintento_en, _rol_disponible
    if _esquema_listo or time.monotonic() < _reintento_en:
        return _esquema_listo
    try:
        cur = conn.cursor()
        cur.execute(DDL)
        conn.commit()
        cur.close()
        _esquema_listo = True
        _rol_disponible = True
    except Exception as e:
        conn.rollback()
        _reintento_en = time.monotonic() + _ESPERA_SEGUNDOS
        _rol_disponible = None   # que se vuelva a comprobar contra information_schema
        app_logger.error(f"No se pudo asegurar el esquema del Coordinador: {e}")
    return _esquema_listo


def rol_disponible(conn=None):
    """True si `users.is_coordinador` existe. Se cachea por proceso."""
    global _rol_disponible
    if _rol_disponible is not None:
        return _rol_disponible
    propia = conn is None
    if propia:
        conn = get_db_connection()
        if not conn:
            return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'users' AND column_name = 'is_coordinador'")
        _rol_disponible = cur.fetchone() is not None
        cur.close()
    except Exception as e:
        conn.rollback()
        app_logger.warning(f"No se pudo comprobar users.is_coordinador: {e}")
        return False
    finally:
        if propia:
            conn.close()
    return _rol_disponible


def _valor(fila, clave):
    if fila is None:
        return None
    if hasattr(fila, 'keys'):
        return fila[clave]
    return fila[0]


def leer_rol(cur, email=None, user_id=None):
    """is_coordinador del usuario; False si la columna todavía no existe."""
    if not rol_disponible(cur.connection):
        return False
    if user_id is not None:
        cur.execute("SELECT is_coordinador FROM users WHERE id = %s", (user_id,))
    else:
        cur.execute("SELECT is_coordinador FROM users WHERE email = %s", (email,))
    return bool(_valor(cur.fetchone(), 'is_coordinador'))


def es_coordinador():
    """True si el JWT en curso es de un Coordinador que no es Administrador."""
    try:
        claims = get_jwt() or {}
    except Exception:
        return False
    return bool(claims.get('is_coordinador')) and not bool(claims.get('is_admin'))


# --- Ámbito -------------------------------------------------------------------

def _ambito_vacio():
    return {'clientes': [], 'propiedades': [], 'clientes_de_propiedades': [],
            'nombres': [], 'items': []}


def cargar_ambito(conn, email):
    """Ámbito asignado a un usuario: ids de clientes e instalaciones, los nombres
    en minúsculas para las filas antiguas que guardan texto, y `items` para la UI."""
    ambito = _ambito_vacio()
    cur = conn.cursor()
    cur.execute("""
        SELECT a.customer_company_id, a.id_propiedad,
               cc.name, p.nombre, p.customer_company_id
          FROM coordinador_ambito a
          JOIN users u ON u.id = a.user_id
          LEFT JOIN customer_companies cc ON cc.id = a.customer_company_id
          LEFT JOIN propiedades p ON p.id_propiedad = a.id_propiedad
         WHERE LOWER(TRIM(u.email)) = LOWER(TRIM(%s))
         ORDER BY cc.name NULLS LAST, p.nombre
    """, (email,))
    filas = [tuple(f) for f in cur.fetchall()]
    cur.close()
    for cid, pid, cliente, propiedad, cliente_de_p in filas:
        if cid is not None:
            ambito['clientes'].append(int(cid))
            ambito['items'].append({'tipo': 'cliente', 'id': int(cid), 'nombre': cliente or f'Cliente {cid}'})
            if cliente:
                ambito['nombres'].append(cliente.strip().lower())
        elif pid is not None:
            ambito['propiedades'].append(int(pid))
            ambito['items'].append({'tipo': 'instalacion', 'id': int(pid), 'nombre': propiedad or f'Instalación {pid}'})
            if propiedad:
                ambito['nombres'].append(propiedad.strip().lower())
            if cliente_de_p is not None:
                ambito['clientes_de_propiedades'].append(int(cliente_de_p))
    # Instalaciones de los clientes del ámbito: sus nombres también cuentan.
    if ambito['clientes']:
        cur = conn.cursor()
        cur.execute("SELECT LOWER(TRIM(nombre)) FROM propiedades "
                    "WHERE customer_company_id = ANY(%s) AND nombre IS NOT NULL", (ambito['clientes'],))
        ambito['nombres'].extend(f[0] for f in cur.fetchall() if f[0])
        cur.close()
    ambito['nombres'] = sorted(set(ambito['nombres']))
    return ambito


def ambito_activo():
    """Ámbito del Coordinador en sesión, cargado una vez por petición.
    None para cualquiera que no sea Coordinador (Administrador incluido)."""
    if not has_request_context():
        return None
    if '_ambito' in g:
        return g._ambito
    g._ambito = None
    if es_coordinador():
        conn = get_db_connection()
        if not conn:
            g._ambito = _ambito_vacio()
            return g._ambito
        try:
            g._ambito = cargar_ambito(conn, get_jwt_identity())
        except Exception as e:
            app_logger.error(f"No se pudo cargar el ámbito del Coordinador: {e}", exc_info=True)
            g._ambito = _ambito_vacio()
        finally:
            conn.close()
    return g._ambito


def condicion_ambito(conds, params, ambito=None, col_prop='id_propiedad',
                     col_inst='cliente_instalacion', col_cust=None, prefix=''):
    """Agrega a conds/params la condición "el registro pertenece al ámbito".

    Cubre las tres formas en que un registro nombra a su cliente: id de
    instalación, id de cliente y el texto de cliente_instalacion de las filas
    antiguas. Pasar None en una columna la omite. Sin ámbito asignado agrega
    FALSE: el Coordinador sin ámbito no ve nada.
    """
    if ambito is None:
        ambito = ambito_activo()
    if ambito is None:
        return
    clientes, props = ambito['clientes'], ambito['propiedades']
    if not clientes and not props:
        conds.append("FALSE")
        return
    partes = []
    if col_prop:
        sub = []
        if clientes:
            sub.append("SELECT id_propiedad FROM propiedades WHERE customer_company_id = ANY(%s)")
            params.append(list(clientes))
        if props:
            sub.append("SELECT unnest(%s::int[])")
            params.append(list(props))
        partes.append(f"{prefix}{col_prop} IN ({' UNION '.join(sub)})")
    if col_cust and clientes:
        partes.append(f"{prefix}{col_cust} = ANY(%s)")
        params.append(list(clientes))
    if col_inst and ambito['nombres']:
        partes.append(f"LOWER(TRIM({prefix}{col_inst})) = ANY(%s)")
        params.append(list(ambito['nombres']))
    conds.append("(" + " OR ".join(partes) + ")" if partes else "FALSE")


def registro_en_ambito(form_type, record_id, conn=None, ambito=None):
    """True si el registro cae dentro del ámbito, o si quien pregunta no es Coordinador."""
    if ambito is None:
        ambito = ambito_activo()
    if ambito is None:
        return True
    origen = ORIGEN.get(form_type or '')
    try:
        rid = int(record_id)
    except (TypeError, ValueError):
        return False
    if not origen:
        return False
    tabla, pk, con_inst = origen
    conds, params = [f"{pk} = %s"], [rid]
    condicion_ambito(conds, params, ambito, col_prop='id_propiedad',
                     col_inst='cliente_instalacion' if con_inst else None,
                     col_cust='customer_company_id')
    propia = conn is None
    if propia:
        conn = get_db_connection()
        if not conn:
            return False
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT 1 FROM {tabla} WHERE {' AND '.join(conds)} LIMIT 1", params)
        dentro = cur.fetchone() is not None
        cur.close()
        return dentro
    except Exception as e:
        conn.rollback()
        app_logger.error(f"registro_en_ambito({form_type}, {record_id}) falló: {e}")
        return False
    finally:
        if propia:
            conn.close()


def fuera_de_ambito(form_type, record_id, conn=None):
    """Respuesta 403 lista para devolver si el registro no está en el ámbito; None si sí."""
    if registro_en_ambito(form_type, record_id, conn=conn):
        return None
    return jsonify({"error": "El registro está fuera de su ámbito de responsabilidad"}), 403


# --- Clientes inactivos ----------------------------------------------------------
#
# Un cliente desactivado desde /admin/clientes deja de contar en KPIs, alertas,
# Estatus de Cliente, Cumplimiento y Reportes, y sale de los selectores, sin borrar
# ni reescribir nada: la exclusión se suma a las consultas en los mismos puntos por
# los que ya entra el ámbito del Coordinador (_add_scope_filters en dashboard_bp,
# _add_scope en cgeo_bp), con las tres formas en que un registro nombra a su
# cliente. Reactivarlo lo devuelve todo tal cual.

def cargar_clientes_inactivos(conn):
    """ids de los clientes inactivos, ids de sus instalaciones y, para las filas
    antiguas que sólo guardan texto, los nombres en minúsculas de unos y otras.
    None cuando no hay ninguno: entonces no se agrega condición alguna."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, LOWER(TRIM(name)) FROM customer_companies WHERE is_active IS FALSE")
        filas = [tuple(f) for f in cur.fetchall()]
        if not filas:
            return None
        clientes = [int(f[0]) for f in filas]
        nombres = {f[1] for f in filas if f[1]}
        cur.execute("SELECT id_propiedad, LOWER(TRIM(nombre)) FROM propiedades "
                    "WHERE customer_company_id = ANY(%s)", (clientes,))
        props = [tuple(f) for f in cur.fetchall()]
    finally:
        cur.close()
    nombres.update(f[1] for f in props if f[1])
    return {'clientes': clientes,
            'propiedades': [int(f[0]) for f in props],
            'nombres': sorted(nombres)}


def clientes_inactivos():
    """Clientes inactivos, cargados una sola vez por petición (como el ámbito).
    None fuera de una petición, si la base no responde o si no hay ninguno."""
    if not has_request_context():
        return None
    if '_clientes_inactivos' in g:
        return g._clientes_inactivos
    g._clientes_inactivos = None
    conn = get_db_connection()
    if conn:
        try:
            g._clientes_inactivos = cargar_clientes_inactivos(conn)
        except Exception as e:
            app_logger.error(f"No se pudo cargar la lista de clientes inactivos: {e}", exc_info=True)
        finally:
            conn.close()
    return g._clientes_inactivos


def condicion_cliente_inactivo(inactivos, col_prop='id_propiedad', col_inst='cliente_instalacion',
                               col_cust=None, prefix=''):
    """(sql, params) que es verdadero cuando el registro pertenece a un cliente
    inactivo, o (None, []) si con esas columnas no hay cómo saberlo. Pasar None
    en una columna la omite (log_de_patrullas no tiene cliente_instalacion)."""
    partes, params = [], []
    if col_cust and inactivos['clientes']:
        partes.append(f"{prefix}{col_cust} = ANY(%s)")
        params.append(list(inactivos['clientes']))
    if col_prop and inactivos['propiedades']:
        partes.append(f"{prefix}{col_prop} = ANY(%s)")
        params.append(list(inactivos['propiedades']))
    if col_inst and inactivos['nombres']:
        # Por texto sólo cuando el registro no trae ninguna clave: una instalación
        # homónima de un cliente activo no debe esconderse por el nombre.
        sin_fk = [f"{prefix}{c} IS NULL" for c in (col_cust, col_prop) if c]
        texto = f"LOWER(TRIM({prefix}{col_inst})) = ANY(%s)"
        partes.append("(" + " AND ".join(sin_fk + [texto]) + ")")
        params.append(list(inactivos['nombres']))
    if not partes:
        return None, []
    # COALESCE: con una clave NULL el OR daría NULL y el NOT de afuera escondería la fila.
    return "COALESCE((" + " OR ".join(partes) + "), FALSE)", params


def _elegido_a_proposito(inactivos, cliente, propiedad):
    """True si el filtro apunta justamente a un cliente (o instalación) inactivo:
    Reportes los ofrece en su propio grupo para consultar su historia."""
    c = str(cliente).strip() if cliente is not None else ''
    if c and c.lower() not in ('todos', 'todas'):
        if c.isdigit():
            return int(c) in inactivos['clientes']
        return c.lower() in inactivos['nombres']
    p = str(propiedad).strip() if propiedad is not None else ''
    if p and p.lower() not in ('todos', 'todas'):
        if p.isdigit():
            return int(p) in inactivos['propiedades']
        return p.lower() in inactivos['nombres']
    return False


def excluir_clientes_inactivos(conds, params, col_prop='id_propiedad', col_inst='cliente_instalacion',
                               col_cust=None, prefix='', cliente=None, propiedad=None):
    """Agrega a conds/params "el registro NO es de un cliente inactivo". Sin
    clientes inactivos no agrega nada. Si el filtro de cliente o instalación es
    uno de los inactivos, tampoco: ahí el usuario quiere ver su historia."""
    inactivos = clientes_inactivos()
    if not inactivos:
        return
    if _elegido_a_proposito(inactivos, cliente, propiedad):
        return
    sql, p = condicion_cliente_inactivo(inactivos, col_prop=col_prop, col_inst=col_inst,
                                        col_cust=col_cust, prefix=prefix)
    if sql:
        conds.append("NOT " + sql)
        params.extend(p)


def _entero(valor):
    try:
        return int(str(valor).strip())
    except (TypeError, ValueError):
        return None


def acotar_filtros(conn, ambito, cliente=None, propiedad=None):
    """Un cliente o instalación pedido por URL fuera del ámbito se ignora (queda
    el ámbito completo) en vez de responder vacío. Devuelve (cliente, propiedad)."""
    if ambito is None:
        return cliente, propiedad
    clientes = set(ambito['clientes']) | set(ambito['clientes_de_propiedades'])
    if propiedad not in (None, '', 'Todos', 'Todas'):
        pid = _entero(propiedad)
        if pid is not None:
            dentro = pid in ambito['propiedades']
            if not dentro and ambito['clientes']:
                cur = conn.cursor()
                cur.execute("SELECT customer_company_id FROM propiedades WHERE id_propiedad = %s", (pid,))
                fila = cur.fetchone()
                cur.close()
                dentro = bool(fila and fila[0] in ambito['clientes'])
        else:
            dentro = str(propiedad).strip().lower() in ambito['nombres']
        if not dentro:
            propiedad = None
    if cliente not in (None, '', 'Todos', 'Todas'):
        cid = _entero(cliente)
        dentro = cid in clientes if cid is not None else str(cliente).strip().lower() in ambito['nombres']
        if not dentro:
            cliente = None
    return cliente, propiedad


# --- Decorador ------------------------------------------------------------------

def _denegar(is_api, mensaje="Acceso denegado", status=403):
    if is_api:
        return jsonify({"error": mensaje}), status
    flash('No tienes permisos para acceder a esta sección.', 'error')
    return redirect('/landing/')


def coordinador_o_admin(f):
    """Deja pasar al Administrador o al Coordinador (claim + verificación en base).
    Va después de @jwt_required(), como los decoradores de administrador."""
    @wraps(f)
    def decorated(*args, **kwargs):
        is_api = '/api/' in request.path
        try:
            email = get_jwt_identity()
            if not email:
                return _denegar(is_api)
            conn = get_db_connection()
            if not conn:
                return _denegar(is_api, "Service unavailable", 503)
            try:
                cur = conn.cursor()
                cur.execute('SELECT is_admin, is_active FROM users WHERE email = %s', (email,))
                fila = cur.fetchone()
                permitido = bool(fila and fila[1] and (fila[0] or leer_rol(cur, email=email)))
                cur.close()
            finally:
                conn.close()
            if not permitido:
                app_logger.warning(f"coordinador_o_admin denegado para {email}")
                return _denegar(is_api)
        except Exception as e:
            app_logger.error(f"coordinador_o_admin error: {e}", exc_info=True)
            return _denegar(is_api, "Error de autenticación", 500)
        return f(*args, **kwargs)
    return decorated
