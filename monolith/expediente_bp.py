import json
import logging
import math
import os
from datetime import datetime, timezone
from functools import wraps
from io import BytesIO

import psycopg2
from psycopg2 import extras
from flask import (Blueprint, current_app, jsonify, redirect,
                   render_template, request, send_file)
from flask_jwt_extended import get_jwt, get_jwt_identity, jwt_required
from itsdangerous import BadData, URLSafeSerializer

try:
    import qrcode
    QRCODE_AVAILABLE = True
except ImportError:
    QRCODE_AVAILABLE = False

from db import get_db_connection
from email_utils import send_email

app_logger = logging.getLogger(__name__)

expediente_bp = Blueprint('expediente', __name__)

# --- Constants ---
QR_SALT           = 'sekapp-evidence-qr-v1'
EXPEDIENTE_QR_SALT = 'sekapp-expediente-qr-v1'
GEOFENCE_RADIUS_M = 100
CRITICAL_SEVERITIES = {'CRITICO', 'CRÍTICO', 'ALTO'}
OPEN_INCIDENT_STATUSES = {'ABIERTO', 'EN PROCESO', 'EN_PROCESO', 'PENDIENTE', 'REPORTADO'}


def _get_user_company_id(cur, user_email):
    cur.execute('SELECT company_id FROM users WHERE email = %s', (user_email,))
    row = cur.fetchone()
    return row['company_id'] if row and row['company_id'] is not None else None


def _get_user_info(user_email):
    try:
        claims = get_jwt()
        user_name = claims.get('name', user_email.split('@')[0])
        is_admin = claims.get('is_admin', False)
    except Exception:
        user_name = user_email.split('@')[0]
        is_admin = False

    conn = cur = None
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute('SELECT "name" FROM "users" WHERE email = %s', (user_email,))
            row = cur.fetchone()
            if row and row[0]:
                user_name = row[0]
    except Exception as e:
        app_logger.error(f"_get_user_info error: {e}", exc_info=True)
    finally:
        if cur: cur.close()
        if conn: conn.close()

    return user_name, is_admin


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        is_api = '/api/' in request.path or request.path.startswith('/expediente/api/')
        try:
            claims = get_jwt()
            if not claims.get('is_admin', False):
                return jsonify({"error": "Acceso denegado"}), 403 if is_api else redirect('/')

            # DB verification — JWT claim may be stale if admin was revoked after token issuance
            email = get_jwt_identity()
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute('SELECT is_admin, is_active FROM users WHERE email = %s', (email,))
                    row = cur.fetchone()
                    cur.close()
                    if not row or not row[0] or not row[1]:
                        app_logger.warning(f"admin_required DB check failed for {email}: row={row}")
                        return jsonify({"error": "Acceso denegado"}), 403 if is_api else redirect('/')
                finally:
                    conn.close()
        except Exception as e:
            app_logger.error(f"admin_required error: {e}", exc_info=True)
            return jsonify({"error": "Error de autenticación"}), 500 if is_api else redirect('/')
        return f(*args, **kwargs)
    return decorated


# ─── Geo helpers ─────────────────────────────────────────────────────────────

def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres between two GPS coordinates."""
    R = 6_371_000
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dlam = math.radians(float(lon2) - float(lon1))
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


MIN_RECORDS_FOR_AUTO_CENTER = 3

def _resolve_geofence_center(cur, cliente_instalacion):
    """
    Returns (lat, lng, source) for the geofence center of a client installation.

    Priority:
      1. Manual coordinates on propiedades matched by name.
      2. Median GPS of supervision_puesto records for this client
         — self-calibrates from historical data, no config required.
      3. None if no GPS data exists at all.
    """
    # Try propiedades name match first (manual override)
    cur.execute("""
        SELECT latitude, longitude FROM propiedades
        WHERE LOWER(TRIM(nombre)) = LOWER(TRIM(%s))
          AND latitude IS NOT NULL AND longitude IS NOT NULL
        LIMIT 1
    """, (cliente_instalacion,))
    row = cur.fetchone()
    if row:
        return float(row['latitude']), float(row['longitude']), 'manual'

    # Fall back to median GPS from supervision history (supervision_puesto uses `cliente`)
    cur.execute("""
        SELECT
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latitude)::float  AS med_lat,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY longitude)::float AS med_lng,
            COUNT(*) AS gps_count
        FROM supervision_puesto
        WHERE LOWER(TRIM(cliente_instalacion)) = LOWER(TRIM(%s))
          AND latitude  IS NOT NULL
          AND longitude IS NOT NULL
    """, (cliente_instalacion,))
    row = cur.fetchone()
    if row and row['gps_count'] >= MIN_RECORDS_FOR_AUTO_CENTER:
        return float(row['med_lat']), float(row['med_lng']), 'auto'

    return None, None, 'none'


# ─── Semaphore logic (hardcoded business rules) ───────────────────────────────

def _compute_status(event, prop_lat, prop_lng):
    module = event.get('module')

    if module == 'SUPERVISION':
        lat = event.get('latitude')
        lng = event.get('longitude')
        if lat and lng and prop_lat and prop_lng:
            dist = haversine_m(lat, lng, prop_lat, prop_lng)
            event['distance_m'] = round(dist)
            return 'RED' if dist > GEOFENCE_RADIUS_M else 'GREEN'
        return 'GREY'

    if module == 'INCIDENTE':
        severity = str(event.get('nivel_severidad') or '').upper()
        estado = str(event.get('estado') or '').upper()
        is_critical = severity in CRITICAL_SEVERITIES
        is_open = estado in OPEN_INCIDENT_STATUSES
        return 'RED' if (is_critical and is_open) else 'GREEN'

    if module == 'ACUERDO':
        fecha_limite = event.get('compromisos_fecha_limite')
        estados = str(event.get('compromisos_estados') or '').upper()
        if fecha_limite:
            today = datetime.now(timezone.utc).date()
            limit = fecha_limite if isinstance(fecha_limite, type(today)) else fecha_limite
            if hasattr(limit, 'date'):
                limit = limit.date()
            if (today - limit).days > 1 and 'CUMPLIDO' not in estados:
                return 'RED'
        return 'GREEN'

    if module == 'ENCUESTA':
        # nivel_severidad carries the NPS score as a string
        try:
            nps = int(event.get('nivel_severidad') or -1)
        except (ValueError, TypeError):
            nps = -1
        if nps >= 0:
            return 'GREEN' if nps >= 7 else 'RED'
        return 'GREEN'

    return 'GREEN'


def _serialize_event(row, prop_lat, prop_lng):
    e = dict(row)
    ts = e.get('event_ts')
    e['timestamp'] = ts.isoformat() if ts and hasattr(ts, 'isoformat') else str(ts or '')

    for k in ('compromisos_fecha_limite',):
        v = e.get(k)
        if v and hasattr(v, 'isoformat'):
            e[k] = v.isoformat()

    e.setdefault('distance_m', None)
    e['status'] = _compute_status(e, prop_lat, prop_lng)
    return e


# ─── QR token helpers ────────────────────────────────────────────────────────

def _make_qr_token(id_supervision, company_id):
    s = URLSafeSerializer(current_app.config['SECRET_KEY'], salt=QR_SALT)
    return s.dumps({'sid': int(id_supervision), 'cid': int(company_id or 0)})


def _decode_qr_token(token):
    s = URLSafeSerializer(current_app.config['SECRET_KEY'], salt=QR_SALT)
    try:
        return s.loads(token)
    except BadData:
        return None


def _make_expediente_token(cliente, days, module_filter, company_id):
    s = URLSafeSerializer(current_app.config['SECRET_KEY'], salt=EXPEDIENTE_QR_SALT)
    return s.dumps({'c': cliente, 'd': int(days), 'm': module_filter, 'cid': int(company_id or 0)})


def _decode_expediente_token(token):
    s = URLSafeSerializer(current_app.config['SECRET_KEY'], salt=EXPEDIENTE_QR_SALT)
    try:
        return s.loads(token)
    except BadData:
        return None


# ─── Routes ──────────────────────────────────────────────────────────────────

@expediente_bp.route('/expediente/')
@jwt_required()
@admin_required
def expediente_index():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    initial_cliente = request.args.get('cliente', '')
    return render_template('expediente_instalacion.html',
                           current_user=user_email,
                           user_name=user_name,
                           is_admin=is_admin,
                           initial_cliente=initial_cliente)


@expediente_bp.route('/expediente/api/clientes')
@jwt_required()
@admin_required
def api_clientes():
    """Distinct client names across supervision, incidents and visit records."""
    user_email = get_jwt_identity()
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB unavailable'}), 503
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)
        company_id = _get_user_company_id(cur, user_email)

        cf = 'AND company_id = %s' if company_id is not None else ''
        params = (company_id,) * 4 if company_id is not None else ()

        cur.execute(f"""
            SELECT DISTINCT TRIM(cliente_name) AS nombre
            FROM (
                SELECT NULLIF(TRIM(cliente_instalacion), '') AS cliente_name
                FROM supervision_puesto
                WHERE cliente_instalacion IS NOT NULL {cf}

                UNION

                SELECT NULLIF(TRIM(cliente_instalacion), '')
                FROM reportes_incidentes
                WHERE cliente_instalacion IS NOT NULL {cf}

                UNION

                SELECT NULLIF(TRIM(cliente_instalacion), '')
                FROM registro_y_acta_de_visita
                WHERE cliente_instalacion IS NOT NULL {cf}

                UNION

                SELECT NULLIF(TRIM(cliente_instalacion), '')
                FROM medicion_experiencia_cliente
                WHERE cliente_instalacion IS NOT NULL {cf}
            ) AS t
            WHERE cliente_name IS NOT NULL
            ORDER BY nombre
        """, params)

        rows = cur.fetchall()
        return jsonify([r['nombre'] for r in rows])
    except Exception as e:
        app_logger.error(f"api_clientes error: {e}", exc_info=True)
        return jsonify({'error': 'Error interno'}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@expediente_bp.route('/expediente/api/feed')
@jwt_required()
@admin_required
def api_feed():
    """Aggregated timeline for one client. Query params: ?cliente=X&days=90 (max 365)."""
    user_email = get_jwt_identity()
    cliente = (request.args.get('cliente') or '').strip()
    if not cliente:
        return jsonify({'error': 'Parámetro cliente requerido'}), 400
    try:
        days = max(1, min(int(request.args.get('days', 90)), 365))
    except (ValueError, TypeError):
        days = 90

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB unavailable'}), 503
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)
        company_id = _get_user_company_id(cur, user_email)

        prop_lat, prop_lng, gps_source = _resolve_geofence_center(cur, cliente)

        cf_sup = 'AND sp.company_id = %s' if company_id is not None else ''
        cf_inc = 'AND ri.company_id = %s' if company_id is not None else ''
        cf_vis = 'AND rav.company_id = %s' if company_id is not None else ''
        cf_enc = 'AND mec.company_id = %s' if company_id is not None else ''

        days_int = int(days)

        def bp(text_val):
            return (text_val, company_id, days_int) if company_id is not None else (text_val, days_int)

        query = f"""
            SELECT
                sp.id_supervision::text                           AS source_id,
                'SUPERVISION'::text                               AS module,
                COALESCE(sp.fecha_hora, sp.creado_en)            AS event_ts,
                sp.latitude,
                sp.longitude,
                sp.location_accuracy,
                sp.foto_evidencia_url                            AS foto_url,
                sp.supervisor                                    AS actor,
                COALESCE(sp.observaciones_novedades, '')         AS summary,
                sp.estado_bitacora                               AS estado,
                NULL::text                                       AS nivel_severidad,
                NULL::date                                       AS compromisos_fecha_limite,
                NULL::text                                       AS compromisos_estados
            FROM supervision_puesto sp
            WHERE LOWER(TRIM(sp.cliente_instalacion)) = LOWER(TRIM(%s)) {cf_sup}
              AND COALESCE(sp.fecha_hora, sp.creado_en) >= NOW() - (%s * INTERVAL '1 day')

            UNION ALL

            SELECT
                ri.id_reporte_incidente::text,
                'INCIDENTE'::text,
                COALESCE(ri.fecha_hora, ri.creado_en),
                NULL::numeric, NULL::numeric, NULL::numeric,
                ri.foto_evidencia_url,
                ri.responsable_asignado,
                COALESCE(ri.descripcion_incidente, ''),
                ri.estado,
                ri.nivel_severidad,
                NULL::date,
                NULL::text
            FROM reportes_incidentes ri
            WHERE LOWER(TRIM(ri.cliente_instalacion)) = LOWER(TRIM(%s)) {cf_inc}
              AND COALESCE(ri.fecha_hora, ri.creado_en) >= NOW() - (%s * INTERVAL '1 day')

            UNION ALL

            SELECT
                rav.id_visita::text,
                'ACUERDO'::text,
                rav.creado_en,
                NULL::numeric, NULL::numeric, NULL::numeric,
                NULL::text,
                rav.nombre_responsable,
                COALESCE(rav.acuerdos_compromisos, ''),
                rav.compromisos_estados,
                NULL::text,
                NULL::date,
                rav.compromisos_estados
            FROM registro_y_acta_de_visita rav
            WHERE LOWER(TRIM(rav.cliente_instalacion)) = LOWER(TRIM(%s)) {cf_vis}
              AND rav.creado_en >= NOW() - (%s * INTERVAL '1 day')

            UNION ALL

            SELECT
                mec.id_encuesta::text,
                'ENCUESTA'::text,
                COALESCE(mec.fecha_hora, mec.creado_en),
                NULL::numeric, NULL::numeric, NULL::numeric,
                NULL::text,
                mec.nombre_responsable,
                COALESCE(mec.observaciones_cliente, ''),
                mec.encuestado,
                mec.calificacion_global_nps::text,
                NULL::date,
                NULL::text
            FROM medicion_experiencia_cliente mec
            WHERE LOWER(TRIM(mec.cliente_instalacion)) = LOWER(TRIM(%s)) {cf_enc}
              AND COALESCE(mec.fecha_hora, mec.creado_en) >= NOW() - (%s * INTERVAL '1 day')

            ORDER BY event_ts DESC NULLS LAST
        """

        cur.execute(query, (*bp(cliente), *bp(cliente), *bp(cliente), *bp(cliente)))
        rows = cur.fetchall()
        events = [_serialize_event(dict(r), prop_lat, prop_lng) for r in rows]

        return jsonify({
            'cliente': cliente,
            'events': events,
            'total': len(events),
            'prop_has_gps': gps_source != 'none',
            'gps_source': gps_source,
        })
    except Exception as e:
        app_logger.error(f"api_feed error (cliente '{cliente}'): {e}", exc_info=True)
        return jsonify({'error': 'Error interno'}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


_EQ_TOTAL = "CASE WHEN elem->>'total_equipos' ~ '^[0-9]+$' THEN (elem->>'total_equipos')::int ELSE 0 END"
_EQ_FUNC  = "CASE WHEN elem->>'equipos_operativos' ~ '^[0-9]+$' THEN (elem->>'equipos_operativos')::int ELSE 0 END"


def _eq_estado_label(pct):
    if pct is None: return 'Sin datos'
    f = float(pct)
    if f >= 95: return 'Operativo'
    if f >= 85: return 'Operativo c/obs.'
    if f >= 70: return 'Riesgo operativo'
    return 'No confiable'


def _eq_estado_color(pct):
    if pct is None: return 'grey'
    f = float(pct)
    if f >= 95: return 'green'
    if f >= 85: return 'yellow'
    if f >= 70: return 'orange'
    return 'red'


def _fetch_equipos_data(cur, cliente, days=None, company_id=None):
    """Shared equipment reliability query for both authenticated and public views."""
    conds = [
        "c.inventario IS NOT NULL",
        "c.inventario != 'null'::jsonb",
        "jsonb_array_length(c.inventario) > 0",
        "LOWER(TRIM(c.cliente_instalacion)) = LOWER(TRIM(%s))",
    ]
    params = [cliente]
    if days:
        conds.append("c.fecha >= CURRENT_DATE - (%s * INTERVAL '1 day')")
        params.append(int(days))
    if company_id is not None:
        conds.append("c.company_id = %s")
        params.append(company_id)

    where = "WHERE " + " AND ".join(conds)
    lateral = f"FROM confiabilidad_equipos c, LATERAL jsonb_array_elements(c.inventario) AS elem {where}"

    cur.execute(f"""
        SELECT
            COUNT(DISTINCT c.id)   AS total_registros,
            MAX(c.fecha)           AS ultima_inspeccion,
            SUM({_EQ_TOTAL})       AS sum_total,
            SUM({_EQ_FUNC})        AS sum_func
        {lateral}
    """, params)
    krow = cur.fetchone()
    if not krow or not krow['total_registros']:
        return None

    sum_total = int(krow['sum_total'] or 0)
    sum_func  = int(krow['sum_func']  or 0)
    pct_gen   = round(sum_func / sum_total * 100, 1) if sum_total else None
    ultima    = krow['ultima_inspeccion']
    ultima_str = ultima.strftime('%d/%m/%Y') if ultima and hasattr(ultima, 'strftime') else str(ultima or '—')

    cur.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(elem->>'tipo'), ''), 'Sin tipo') AS tipo,
            SUM({_EQ_TOTAL})  AS sum_total,
            SUM({_EQ_FUNC})   AS sum_func,
            ROUND(SUM({_EQ_FUNC})::numeric / NULLIF(SUM({_EQ_TOTAL}), 0) * 100, 1) AS pct
        {lateral}
          AND NULLIF(TRIM(elem->>'tipo'), '') IS NOT NULL
        GROUP BY COALESCE(NULLIF(TRIM(elem->>'tipo'), ''), 'Sin tipo')
        HAVING SUM({_EQ_TOTAL}) > 0
        ORDER BY pct ASC NULLS LAST
    """, params)
    por_tipo = [
        {
            'tipo':   r['tipo'],
            'total':  int(r['sum_total'] or 0),
            'func':   int(r['sum_func']  or 0),
            'pct':    float(r['pct']) if r['pct'] is not None else None,
            'estado': _eq_estado_label(r['pct']),
            'color':  _eq_estado_color(r['pct']),
        }
        for r in cur.fetchall()
    ]

    return {
        'pct_general':      pct_gen,
        'estado_general':   _eq_estado_label(pct_gen),
        'color_general':    _eq_estado_color(pct_gen),
        'total_equipos':    sum_total,
        'equipos_operativos': sum_func,
        'total_registros':  int(krow['total_registros']),
        'ultima_inspeccion': ultima_str,
        'por_tipo':         por_tipo,
    }


@expediente_bp.route('/expediente/api/equipos')
@jwt_required()
@admin_required
def api_equipos():
    """Equipment reliability summary for one client. Query params: ?cliente=X&days=N"""
    user_email = get_jwt_identity()
    cliente = (request.args.get('cliente') or '').strip()
    if not cliente:
        return jsonify({'error': 'Parámetro cliente requerido'}), 400
    try:
        days = max(1, min(int(request.args.get('days', 365)), 730))
    except (ValueError, TypeError):
        days = 365

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB unavailable'}), 503
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)
        company_id = _get_user_company_id(cur, user_email)
        data = _fetch_equipos_data(cur, cliente, days, company_id)
        return jsonify(data or {})
    except Exception as e:
        app_logger.error(f"api_equipos error (cliente '{cliente}'): {e}", exc_info=True)
        return jsonify({'error': 'Error interno'}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@expediente_bp.route('/expediente/api/kpi')
@jwt_required()
@admin_required
def api_kpi():
    """Monthly KPI aggregates for the last 6 months. Query param: ?cliente=X"""
    user_email = get_jwt_identity()
    cliente = (request.args.get('cliente') or '').strip()
    if not cliente:
        return jsonify({'error': 'Parámetro cliente requerido'}), 400

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB unavailable'}), 503
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)
        company_id = _get_user_company_id(cur, user_email)

        prop_lat, prop_lng, gps_source = _resolve_geofence_center(cur, cliente)

        cf = 'AND company_id = %s' if company_id is not None else ''

        def p(text_val):
            return (text_val, company_id) if company_id is not None else (text_val,)

        if prop_lat and prop_lng:
            geofence_sql = f"""
                SUM(CASE
                    WHEN latitude IS NOT NULL AND longitude IS NOT NULL
                     AND (2 * 6371000 * ASIN(SQRT(
                           POWER(SIN(RADIANS(latitude  - {prop_lat}) / 2), 2) +
                           COS(RADIANS(latitude)) * COS(RADIANS({prop_lat})) *
                           POWER(SIN(RADIANS(longitude - {prop_lng}) / 2), 2)
                         ))) <= 100
                    THEN 1 ELSE 0
                END) AS en_geocerca"""
        else:
            geofence_sql = "NULL::bigint AS en_geocerca"

        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', COALESCE(fecha_hora, creado_en)), 'YYYY-MM') AS mes,
                COUNT(*) AS supervisiones,
                {geofence_sql}
            FROM supervision_puesto
            WHERE LOWER(TRIM(cliente_instalacion)) = LOWER(TRIM(%s)) {cf}
              AND COALESCE(fecha_hora, creado_en) >= NOW() - INTERVAL '6 months'
            GROUP BY mes ORDER BY mes
        """, p(cliente))
        sup = {r['mes']: dict(r) for r in cur.fetchall()}

        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', COALESCE(fecha_hora, creado_en)), 'YYYY-MM') AS mes,
                COUNT(*) AS incidentes,
                COUNT(*) FILTER (WHERE nivel_severidad ILIKE ANY(ARRAY['CRITICO','CRÍTICO','ALTO']))
                         AS criticos,
                COUNT(*) FILTER (WHERE estado ILIKE ANY(ARRAY['ABIERTO','EN PROCESO','EN_PROCESO','PENDIENTE','REPORTADO']))
                         AS abiertos
            FROM reportes_incidentes
            WHERE LOWER(TRIM(cliente_instalacion)) = LOWER(TRIM(%s)) {cf}
              AND COALESCE(fecha_hora, creado_en) >= NOW() - INTERVAL '6 months'
            GROUP BY mes ORDER BY mes
        """, p(cliente))
        inc = {r['mes']: dict(r) for r in cur.fetchall()}

        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', creado_en), 'YYYY-MM') AS mes,
                COUNT(*) AS acuerdos,
                COUNT(*) FILTER (
                    WHERE fecha_cumplimiento < CURRENT_DATE - INTERVAL '1 day'
                      AND (compromisos_estados IS NULL
                           OR compromisos_estados NOT ILIKE '%CUMPLIDO%')
                ) AS vencidos
            FROM registro_y_acta_de_visita
            WHERE LOWER(TRIM(cliente_instalacion)) = LOWER(TRIM(%s)) {cf}
              AND creado_en >= NOW() - INTERVAL '6 months'
            GROUP BY mes ORDER BY mes
        """, p(cliente))
        vis = {r['mes']: dict(r) for r in cur.fetchall()}

        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', COALESCE(fecha_hora, creado_en)), 'YYYY-MM') AS mes,
                COUNT(*) AS encuestas,
                ROUND(AVG(calificacion_global_nps)::numeric, 1) AS nps_promedio
            FROM medicion_experiencia_cliente
            WHERE LOWER(TRIM(cliente_instalacion)) = LOWER(TRIM(%s)) {cf}
              AND COALESCE(fecha_hora, creado_en) >= NOW() - INTERVAL '6 months'
            GROUP BY mes ORDER BY mes
        """, p(cliente))
        enc = {r['mes']: dict(r) for r in cur.fetchall()}

        all_months = sorted(set(list(sup) + list(inc) + list(vis) + list(enc)))
        kpi = []
        for m in all_months:
            s = sup.get(m, {})
            i = inc.get(m, {})
            v = vis.get(m, {})
            e = enc.get(m, {})
            en_geo = s.get('en_geocerca')
            nps_val = e.get('nps_promedio')
            kpi.append({
                'mes': m,
                'supervisiones': int(s.get('supervisiones', 0)),
                'en_geocerca': int(en_geo) if en_geo is not None else None,
                'incidentes': int(i.get('incidentes', 0)),
                'incidentes_criticos': int(i.get('criticos', 0)),
                'incidentes_abiertos': int(i.get('abiertos', 0)),
                'acuerdos': int(v.get('acuerdos', 0)),
                'acuerdos_vencidos': int(v.get('vencidos', 0)),
                'encuestas': int(e.get('encuestas', 0)),
                'nps_promedio': float(nps_val) if nps_val is not None else None,
            })

        return jsonify({'kpi': kpi, 'gps_source': gps_source})
    except Exception as e:
        app_logger.error(f"api_kpi error (cliente '{cliente}'): {e}", exc_info=True)
        return jsonify({'error': 'Error interno'}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@expediente_bp.route('/expediente/qr/<int:id_supervision>')
@jwt_required()
@admin_required
def generate_supervision_qr(id_supervision):
    """Returns a QR code PNG whose URL points to the public evidence viewer."""
    if not QRCODE_AVAILABLE:
        return jsonify({'error': 'QR generation not available — install qrcode[pil]'}), 501

    user_email = get_jwt_identity()
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB unavailable'}), 503
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)
        company_id = _get_user_company_id(cur, user_email)

        q = "SELECT id_supervision FROM supervision_puesto WHERE id_supervision = %s"
        qp = [id_supervision]
        if company_id is not None:
            q += " AND company_id = %s"
            qp.append(company_id)
        cur.execute(q, qp)
        if not cur.fetchone():
            return jsonify({'error': 'Registro no encontrado'}), 404

        token = _make_qr_token(id_supervision, company_id)
        public_url = request.host_url.rstrip('/') + f'/v/{token}'

        img = qrcode.make(public_url)
        buf = BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png',
                         download_name=f'evidencia_{id_supervision}.png')
    except Exception as e:
        app_logger.error(f"generate_supervision_qr error: {e}", exc_info=True)
        return jsonify({'error': 'Error interno'}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@expediente_bp.route('/expediente/api/qr-url/<int:id_supervision>')
@jwt_required()
@admin_required
def api_qr_url(id_supervision):
    """Returns the signed public URL for a supervision record's evidence page."""
    user_email = get_jwt_identity()
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB unavailable'}), 503
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)
        company_id = _get_user_company_id(cur, user_email)

        q = "SELECT id_supervision FROM supervision_puesto WHERE id_supervision = %s"
        qp = [id_supervision]
        if company_id is not None:
            q += " AND company_id = %s"
            qp.append(company_id)
        cur.execute(q, qp)
        if not cur.fetchone():
            return jsonify({'error': 'Registro no encontrado'}), 404

        token = _make_qr_token(id_supervision, company_id)
        public_url = request.host_url.rstrip('/') + f'/v/{token}'
        return jsonify({'url': public_url})
    except Exception as e:
        app_logger.error(f"api_qr_url error: {e}", exc_info=True)
        return jsonify({'error': 'Error interno'}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@expediente_bp.route('/expediente/api/qr-expediente-url')
@jwt_required()
@admin_required
def api_qr_expediente_url():
    """Returns signed token + public URL for the combined expediente viewer."""
    user_email = get_jwt_identity()
    cliente = (request.args.get('cliente') or '').strip()
    module_filter = (request.args.get('module') or 'ALL').strip().upper()
    try:
        days = max(1, min(int(request.args.get('days', 30)), 365))
    except (ValueError, TypeError):
        days = 30

    if not cliente:
        return jsonify({'error': 'Parámetro cliente requerido'}), 400

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB unavailable'}), 503
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)
        company_id = _get_user_company_id(cur, user_email)

        token = _make_expediente_token(cliente, days, module_filter, company_id)
        public_url = request.host_url.rstrip('/') + f'/vexp/{token}'
        return jsonify({'url': public_url, 'token': token})
    except Exception as e:
        app_logger.error(f"api_qr_expediente_url error: {e}", exc_info=True)
        return jsonify({'error': 'Error interno'}), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@expediente_bp.route('/expediente/qr-expediente/<string:token>')
def qr_expediente_png(token):
    """Returns QR PNG encoding the combined public expediente URL. No auth needed — token is signed."""
    if not QRCODE_AVAILABLE:
        return jsonify({'error': 'QR generation not available — install qrcode[pil]'}), 501

    payload = _decode_expediente_token(token)
    if not payload:
        return jsonify({'error': 'Token inválido'}), 400

    public_url = request.host_url.rstrip('/') + f'/vexp/{token}'
    img = qrcode.make(public_url)
    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png', download_name='expediente_qr.png')


@expediente_bp.route('/expediente/api/qr-expediente-email', methods=['POST'])
@jwt_required()
@admin_required
def api_qr_expediente_email():
    """Send the expediente QR link by email to one or more recipients."""
    user_email = get_jwt_identity()
    body = request.get_json() or {}
    token    = (body.get('token') or '').strip()
    cliente  = (body.get('cliente') or '').strip()
    to_email = (body.get('to_email') or '').strip()

    if not token or not to_email:
        return jsonify({'error': 'Faltan campos requeridos: token, to_email'}), 400

    payload = _decode_expediente_token(token)
    if not payload:
        return jsonify({'error': 'Token inválido o expirado'}), 400

    public_url = request.host_url.rstrip('/') + f'/vexp/{token}'
    nombre_instalacion = cliente or payload.get('cliente', 'la instalación')

    # Resolve sender display name
    try:
        from psycopg2 import extras as _extras
        conn2 = get_db_connection()
        sender_name = user_email
        if conn2:
            with conn2.cursor(cursor_factory=_extras.RealDictCursor) as cur2:
                cur2.execute('SELECT name FROM users WHERE email = %s', (user_email,))
                row2 = cur2.fetchone()
                if row2:
                    sender_name = row2['name']
            conn2.close()
    except Exception:
        sender_name = user_email

    from html import escape as _esc
    subject = f"[SEKApp] Expediente de instalación – {nombre_instalacion}"
    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#111827;">
      <div style="background:#1e3a5f;padding:20px 24px;border-radius:6px 6px 0 0;">
        <p style="margin:0;font-size:18px;font-weight:700;color:#ffffff;">SEKApp</p>
        <p style="margin:4px 0 0;font-size:13px;color:#93c5fd;">Expediente de instalación compartido</p>
      </div>
      <div style="background:#ffffff;padding:24px;border:1px solid #e5e7eb;border-top:none;
                  border-radius:0 0 6px 6px;">
        <p style="margin:0 0 16px;font-size:15px;color:#111827;">
          <strong>{_esc(sender_name)}</strong> te ha compartido el expediente de instalación de
          <strong>{_esc(nombre_instalacion)}</strong>.
        </p>
        <p style="margin:0 0 8px;font-size:13px;color:#374151;">
          Accede al expediente completo con el siguiente enlace o escaneando el código QR:
        </p>
        <p style="margin:0 0 20px;font-size:12px;word-break:break-all;">
          <a href="{_esc(public_url)}" style="color:#1d4ed8;">{_esc(public_url)}</a>
        </p>
        <div style="text-align:center;margin-bottom:24px;">
          <a href="{_esc(public_url)}"
             style="display:inline-block;padding:11px 28px;background:#1e3a5f;color:#ffffff;
                    text-decoration:none;border-radius:5px;font-size:14px;font-weight:600;">
            Ver expediente
          </a>
        </div>
        <p style="margin:0;font-size:11px;color:#9ca3af;text-align:center;">
          Kanan · SEKApp — este enlace tiene vigencia limitada según la configuración del QR.
        </p>
      </div>
    </div>
    """

    try:
        sent = send_email(to_emails=to_email, subject=subject, body=html_body, is_html=True)
    except Exception as e:
        app_logger.error(f"api_qr_expediente_email send error: {e}", exc_info=True)
        return jsonify({'error': 'Error al enviar el correo'}), 500

    if not sent:
        return jsonify({'error': 'El correo no pudo ser enviado (configuración SMTP)'}), 500

    return jsonify({'success': True})


@expediente_bp.route('/vexp/<string:token>')
def public_expediente_viewer(token):
    """Public no-auth combined timeline viewer — linked from expediente QR codes."""
    payload = _decode_expediente_token(token)
    if not payload:
        return render_template('error.html', message='Enlace inválido o expirado.'), 400

    cliente       = payload.get('c', '')
    days          = payload.get('d', 30)
    module_filter = payload.get('m', 'ALL')
    company_id    = payload.get('cid') or None
    if company_id == 0:
        company_id = None

    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return render_template('error.html', message='Servicio temporalmente no disponible.'), 503
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)

        prop_lat, prop_lng, gps_source = _resolve_geofence_center(cur, cliente)

        cf_sup = 'AND sp.company_id = %s' if company_id is not None else ''
        cf_inc = 'AND ri.company_id = %s' if company_id is not None else ''
        cf_vis = 'AND rav.company_id = %s' if company_id is not None else ''
        cf_enc = 'AND mec.company_id = %s' if company_id is not None else ''

        days_int = int(days)

        def bp(text_val):
            return (text_val, company_id, days_int) if company_id is not None else (text_val, days_int)

        query = f"""
            SELECT
                sp.id_supervision::text  AS source_id,
                'SUPERVISION'::text      AS module,
                COALESCE(sp.fecha_hora, sp.creado_en) AS event_ts,
                sp.latitude, sp.longitude, sp.location_accuracy,
                sp.foto_evidencia_url    AS foto_url,
                sp.supervisor            AS actor,
                COALESCE(sp.observaciones_novedades, '') AS summary,
                sp.estado_bitacora       AS estado,
                NULL::text               AS nivel_severidad,
                NULL::date               AS compromisos_fecha_limite,
                NULL::text               AS compromisos_estados
            FROM supervision_puesto sp
            WHERE LOWER(TRIM(sp.cliente_instalacion)) = LOWER(TRIM(%s)) {cf_sup}
              AND COALESCE(sp.fecha_hora, sp.creado_en) >= NOW() - (%s * INTERVAL '1 day')

            UNION ALL

            SELECT
                ri.id_reporte_incidente::text, 'INCIDENTE'::text,
                COALESCE(ri.fecha_hora, ri.creado_en),
                NULL::numeric, NULL::numeric, NULL::numeric,
                ri.foto_evidencia_url, ri.responsable_asignado,
                COALESCE(ri.descripcion_incidente, ''),
                ri.estado, ri.nivel_severidad, NULL::date, NULL::text
            FROM reportes_incidentes ri
            WHERE LOWER(TRIM(ri.cliente_instalacion)) = LOWER(TRIM(%s)) {cf_inc}
              AND COALESCE(ri.fecha_hora, ri.creado_en) >= NOW() - (%s * INTERVAL '1 day')

            UNION ALL

            SELECT
                rav.id_visita::text, 'ACUERDO'::text,
                rav.creado_en,
                NULL::numeric, NULL::numeric, NULL::numeric,
                NULL::text, rav.nombre_responsable,
                COALESCE(rav.acuerdos_compromisos, ''),
                rav.compromisos_estados, NULL::text,
                NULL::date, rav.compromisos_estados
            FROM registro_y_acta_de_visita rav
            WHERE LOWER(TRIM(rav.cliente_instalacion)) = LOWER(TRIM(%s)) {cf_vis}
              AND rav.creado_en >= NOW() - (%s * INTERVAL '1 day')

            UNION ALL

            SELECT
                mec.id_encuesta::text, 'ENCUESTA'::text,
                COALESCE(mec.fecha_hora, mec.creado_en),
                NULL::numeric, NULL::numeric, NULL::numeric,
                NULL::text, mec.nombre_responsable,
                COALESCE(mec.observaciones_cliente, ''),
                mec.encuestado,
                mec.calificacion_global_nps::text,
                NULL::date, NULL::text
            FROM medicion_experiencia_cliente mec
            WHERE LOWER(TRIM(mec.cliente_instalacion)) = LOWER(TRIM(%s)) {cf_enc}
              AND COALESCE(mec.fecha_hora, mec.creado_en) >= NOW() - (%s * INTERVAL '1 day')

            ORDER BY event_ts DESC NULLS LAST
        """
        cur.execute(query, (*bp(cliente), *bp(cliente), *bp(cliente), *bp(cliente)))
        rows = cur.fetchall()
        events = [_serialize_event(dict(r), prop_lat, prop_lng) for r in rows]

        if module_filter != 'ALL':
            events = [e for e in events if e['module'] == module_filter]

        # Pre-parse photo lists and sanitize for JSON embedding
        for e in events:
            raw = e.get('foto_url') or ''
            if raw.startswith('['):
                try:
                    e['foto_list'] = json.loads(raw)
                except Exception:
                    e['foto_list'] = [raw] if raw else []
            elif raw:
                e['foto_list'] = [u.strip() for u in raw.split(',') if u.strip()]
            else:
                e['foto_list'] = []
            # Convert Decimal GPS values to float for JSON serialization
            for k in ('latitude', 'longitude', 'location_accuracy'):
                if e.get(k) is not None:
                    e[k] = float(e[k])
            # Remove the raw datetime key (_serialize_event already put it in 'timestamp')
            e.pop('event_ts', None)

        stats = {
            'supervisiones': sum(1 for e in events if e['module'] == 'SUPERVISION'),
            'incidentes':    sum(1 for e in events if e['module'] == 'INCIDENTE'),
            'acuerdos':      sum(1 for e in events if e['module'] == 'ACUERDO'),
            'encuestas':     sum(1 for e in events if e['module'] == 'ENCUESTA'),
            'alertas':       sum(1 for e in events if e['status'] == 'RED'),
        }

        equipos_data = None
        try:
            equipos_data = _fetch_equipos_data(cur, cliente, days=None, company_id=company_id)
        except Exception as eq_err:
            app_logger.warning(f"Could not fetch equipos for public viewer: {eq_err}")

        MODULE_LABELS = {'ALL': 'Todos los módulos', 'SUPERVISION': 'Supervisiones',
                         'INCIDENTE': 'Incidentes', 'ACUERDO': 'Acuerdos',
                         'ENCUESTA': 'Encuestas a cliente'}

        return render_template('expediente_public_combined.html',
                               cliente=cliente,
                               days=days,
                               module_label=MODULE_LABELS.get(module_filter, module_filter),
                               events=events,
                               stats=stats,
                               gps_source=gps_source,
                               prop_lat=prop_lat,
                               prop_lng=prop_lng,
                               equipos_data=equipos_data,
                               generated_at=datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC'))
    except Exception as e:
        app_logger.error(f"public_expediente_viewer error: {e}", exc_info=True)
        return render_template('error.html', message='Error interno del servidor.'), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()


@expediente_bp.route('/v/<string:hash_token>')
def public_evidence_viewer(hash_token):
    """Public no-auth evidence viewer — linked from QR codes sent to clients."""
    payload = _decode_qr_token(hash_token)
    if not payload:
        return render_template('error.html', message='Enlace inválido o expirado.'), 400

    id_supervision = payload.get('sid')
    conn = cur = None
    try:
        conn = get_db_connection()
        if not conn:
            return render_template('error.html',
                                   message='Servicio temporalmente no disponible.'), 503
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)

        cur.execute("""
            SELECT
                sp.id_supervision,
                sp.id_propiedad,
                sp.creado_en,
                sp.fecha_hora,
                sp.latitude,
                sp.longitude,
                sp.location_accuracy,
                sp.foto_evidencia_url,
                sp.supervisor,
                sp.cliente_instalacion,
                sp.observaciones_novedades,
                p.nombre       AS propiedad_nombre,
                p.direccion    AS propiedad_direccion
            FROM supervision_puesto sp
            LEFT JOIN propiedades p ON p.id_propiedad = sp.id_propiedad
            WHERE sp.id_supervision = %s
        """, (id_supervision,))
        row = cur.fetchone()
        if not row:
            return render_template('error.html', message='Registro no encontrado.'), 404

        record = dict(row)

        # Resolve geofence center — auto-derives from supervision history if not manually set
        center_lat, center_lng, _ = _resolve_geofence_center(
            cur, record.get('cliente_instalacion', '')
        )

        distance_m = None
        geofence_status = 'SIN_GPS'
        if record.get('latitude') and record.get('longitude') and center_lat and center_lng:
            distance_m = haversine_m(
                record['latitude'], record['longitude'],
                center_lat, center_lng
            )
            geofence_status = 'VERDE' if distance_m <= GEOFENCE_RADIUS_M else 'ROJO'
            distance_m = round(distance_m)
            record['prop_lat'] = center_lat
            record['prop_lng'] = center_lng

        ts = record.get('fecha_hora') or record.get('creado_en')
        timestamp_str = ts.strftime('%d/%m/%Y %H:%M:%S') if ts else 'N/D'

        raw_fotos = record.get('foto_evidencia_url') or ''
        foto_urls = []
        if raw_fotos.startswith('['):
            try:
                foto_urls = json.loads(raw_fotos)
            except Exception:
                foto_urls = [raw_fotos] if raw_fotos else []
        elif raw_fotos:
            foto_urls = [u.strip() for u in raw_fotos.split(',') if u.strip()]

        return render_template('expediente_public_viewer.html',
                               record=record,
                               distance_m=distance_m,
                               geofence_status=geofence_status,
                               timestamp_str=timestamp_str,
                               foto_urls=foto_urls)
    except Exception as e:
        app_logger.error(f"public_evidence_viewer error: {e}", exc_info=True)
        return render_template('error.html', message='Error interno del servidor.'), 500
    finally:
        if cur: cur.close()
        if conn: conn.close()
