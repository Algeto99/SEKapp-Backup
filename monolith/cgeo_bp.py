"""
Centro de Gestión Ejecutiva y Operativa (CGEO)
Blueprint with two sub-modules:
  - Gestión de Recursos y Confiabilidad  (/cgeo/recursos/)
  - Operación e Incidentes     (/cgeo/operacion/)
Each sub-module exposes an Informe Ejecutivo and a Resumen Operativo tab.
"""

import base64
import logging
import os
from datetime import date, timedelta, datetime
from html import escape
from functools import wraps
from io import BytesIO

import psycopg2
from psycopg2 import extras, sql
from flask import Blueprint, render_template, jsonify, request, redirect, send_file
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt

try:
    from weasyprint import HTML as _WeasyprintHTML
    _WEASYPRINT_AVAILABLE = True
except OSError:
    _WeasyprintHTML = None
    _WEASYPRINT_AVAILABLE = False

from db import get_db_connection
from email_utils import send_email

cgeo_bp = Blueprint("cgeo_bp", __name__)
app_logger = logging.getLogger(__name__)


def _get_conn():
    return get_db_connection()


def _get_user_company_id(cur, user_email):
    """Returns company_id for tenant isolation, or None for super-admins (no filter)."""
    if not user_email:
        return None
    cur.execute('SELECT company_id FROM users WHERE email = %s', (user_email,))
    row = cur.fetchone()
    return row['company_id'] if row and row['company_id'] is not None else None


def _get_user_info(user_email):
    try:
        claims = get_jwt()
        user_name = claims.get("name", user_email.split("@")[0])
        is_admin = claims.get("is_admin", False)
    except Exception:
        user_name = user_email.split("@")[0]
        is_admin = False
    conn = _get_conn()
    if conn:
        try:
            cur = conn.cursor(cursor_factory=extras.RealDictCursor)
            cur.execute('SELECT "name" FROM "users" WHERE email = %s', (user_email,))
            row = cur.fetchone()
            if row and row["name"]:
                user_name = row["name"]
        except Exception:
            pass
        finally:
            conn.close()
    return user_name, is_admin


def _admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        is_api = request.path.startswith("/cgeo/api/")
        try:
            claims = get_jwt()
            if not claims.get("is_admin", False):
                return jsonify({"error": "Acceso denegado"}), 403 if is_api else redirect("/landing/")

            # DB verification — JWT claim may be stale if admin was revoked after token issuance
            email = get_jwt_identity()
            conn = _get_conn()
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute('SELECT is_admin, is_active FROM users WHERE email = %s', (email,))
                    row = cur.fetchone()
                    cur.close()
                    if not row or not row[0] or not row[1]:
                        app_logger.warning(f"_admin_required DB check failed for {email}: row={row}")
                        return jsonify({"error": "Acceso denegado"}), 403 if is_api else redirect("/landing/")
                finally:
                    conn.close()
        except Exception as e:
            app_logger.error(f"_admin_required error: {e}", exc_info=True)
            return jsonify({"error": "Error de autenticación"}), 500 if is_api else redirect("/landing/")
        return f(*args, **kwargs)
    return decorated


def _cliente_cond(cliente):
    """Returns (cond_str, value) for client filtering.
    Numeric string → filter by id_propiedad (int).
    Name string     → filter by cliente_instalacion (text).
    """
    if not cliente:
        return None, None
    try:
        return "id_propiedad = %s", int(cliente)
    except (ValueError, TypeError):
        return "TRIM(cliente_instalacion) = TRIM(%s)", cliente


def _add_cliente(conds, params, cliente, alias=''):
    """Append client filter to existing conds/params lists. alias: e.g. 'c.'"""
    if not cliente:
        return
    try:
        c_val = int(cliente)
        conds.append(f"{alias}id_propiedad = %s")
        params.append(c_val)
    except (ValueError, TypeError):
        # We check whether the condition should use TRIM
        conds.append(f"TRIM({alias}cliente_instalacion) = %s")
        params.append(str(cliente).strip())


# ── SQL constants (mirrors dashboard_bp) ────────────────────────────────────

_EQ_TOTAL_SQL = (
    "CASE WHEN elem->>'total_equipos' ~ '^[0-9]+$' "
    "THEN (elem->>'total_equipos')::int ELSE 0 END"
)
_EQ_FUNC_SQL = (
    "CASE WHEN elem->>'equipos_operativos' ~ '^[0-9]+$' "
    "THEN (elem->>'equipos_operativos')::int ELSE 0 END"
)
_VEH_FAULT_COLS = [
    "estado_rines", "juego_senales_carretera", "gato_hidraulico", "palanca_gato",
    "estado_asientos", "estado_tapetes_alfombras", "limpieza_carroceria",
    "luces_delanteras", "luces_direccionales", "luces_traseras",
    "parabrisas_delantero", "parabrisas_trasero", "defensa_delantera", "defensa_trasera",
    "puertas_vidrios", "tapa_radiador", "tapa_aceite_motor", "bateria_tapa",
    "espejo_retrovisor_interno", "espejos_retrovisores_externos", "limpia_brisas",
    "antena_radio", "radio_funciona", "llanta_repuesto", "aire_acondicionado",
]
_VEH_FAULT_EXPR = " OR ".join(
    f"LOWER(COALESCE({c},''))='malo'" for c in _VEH_FAULT_COLS
)


def _where(conds):
    return ("WHERE " + " AND ".join(conds)) if conds else ""


def _veh_date():
    return "COALESCE(fecha_hora::timestamp, creado_en::timestamp)"


def _capac_safe_len(col="lista_asistencia"):
    return (
        f"CASE WHEN {col} IS NOT NULL AND {col}::TEXT ~ '^\\s*\\[' "
        f"THEN jsonb_array_length({col}) ELSE 0 END"
    )


def _capac_date():
    return "COALESCE(fecha_hora, creado_en::timestamp)"


# ── Page routes ──────────────────────────────────────────────────────────────

@cgeo_bp.route("/")
@jwt_required()
@_admin_required
def cgeo_hub():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    return render_template(
        "cgeo_hub.html",
        current_user=user_email,
        user_name=user_name,
        is_admin=is_admin,
    )


@cgeo_bp.route("/recursos/")
@jwt_required()
@_admin_required
def cgeo_recursos():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    return render_template(
        "cgeo_recursos.html",
        current_user=user_email,
        user_name=user_name,
        is_admin=is_admin,
    )


@cgeo_bp.route("/operacion/")
@jwt_required()
@_admin_required
def cgeo_operacion():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    return render_template(
        "cgeo_operacion.html",
        current_user=user_email,
        user_name=user_name,
        is_admin=is_admin,
    )


@cgeo_bp.route("/morning-briefing/")
@jwt_required()
@_admin_required
def cgeo_morning_briefing():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    return render_template(
        "cgeo_morning_briefing.html",
        current_user=user_email,
        user_name=user_name,
        is_admin=is_admin,
    )


# ── API: shared filter options ────────────────────────────────────────────────

@cgeo_bp.route("/api/filtros")
@jwt_required()
def cgeo_api_filtros():
    conn = _get_conn()
    if not conn:
        return jsonify({"error": "DB no disponible"}), 500
    try:
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)
        company_id = _get_user_company_id(cur, get_jwt_identity())
        query = """
            SELECT p.id_propiedad AS id, p.nombre AS name
            FROM propiedades p
            LEFT JOIN customer_companies cc ON p.customer_company_id = cc.id
            WHERE p.activa = TRUE OR p.activa IS NULL
        """
        params = []
        if company_id is not None:
            query += " AND cc.company_id = %s"
            params.append(company_id)
            
        query += " ORDER BY p.nombre"
        
        cur.execute(query, tuple(params))
        clientes = [{"id": r["id"], "name": r["name"]} for r in cur.fetchall()]

        # Distinct supervisors from incident reports, scoped by company
        sup_query = """
            SELECT DISTINCT TRIM(nombre_responsable) AS name
            FROM reportes_incidentes ri
            LEFT JOIN propiedades p ON ri.id_propiedad = p.id_propiedad
            LEFT JOIN customer_companies cc ON p.customer_company_id = cc.id
            WHERE TRIM(COALESCE(nombre_responsable,'')) <> ''
        """
        sup_params = []
        if company_id is not None:
            sup_query += " AND cc.company_id = %s"
            sup_params.append(company_id)
        sup_query += " ORDER BY name"
        cur.execute(sup_query, tuple(sup_params))
        supervisores = [r["name"] for r in cur.fetchall()]

        return jsonify({"clientes": clientes, "supervisores": supervisores})
    except Exception as e:
        app_logger.error(f"cgeo_api_filtros error: {e}", exc_info=True)
        return jsonify({"error": "Error interno"}), 500
    finally:
        conn.close()


# ── API: Recursos y Confiabilidad ─────────────────────────────────────────────

@cgeo_bp.route("/api/recursos-data")
@jwt_required()
@_admin_required
def cgeo_api_recursos_data():
    cliente = request.args.get("cliente")
    if cliente in ('Todos', ''):
        cliente = None
    start_date = request.args.get("start_date") or None
    end_date = request.args.get("end_date") or None

    conn = _get_conn()
    if not conn:
        return jsonify({"error": "DB no disponible"}), 500
    try:
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)
        today = date.today()

        # ── Equipos ──────────────────────────────────────────────────────────
        eq_conds, eq_params = [], []
        _add_cliente(eq_conds, eq_params, cliente, alias='c.')
        if start_date:
            eq_conds.append("c.fecha >= %s")
            eq_params.append(start_date)
        if end_date:
            eq_conds.append("c.fecha <= %s")
            eq_params.append(end_date)
        eq_where = _where(eq_conds)
        eq_lateral = f"""
            FROM confiabilidad_equipos c,
                 LATERAL jsonb_array_elements(c.inventario) AS elem
            {eq_where}
        """
        cur.execute(f"""
            SELECT
                SUM({_EQ_TOTAL_SQL}) AS total,
                SUM({_EQ_FUNC_SQL})  AS operativos
            {eq_lateral}
        """, tuple(eq_params))
        eq_row = cur.fetchone() or {}
        eq_total = int(eq_row.get("total") or 0)
        eq_op = int(eq_row.get("operativos") or 0)
        eq_no_op = eq_total - eq_op
        eq_pct = round(eq_op / eq_total * 100, 1) if eq_total else None

        # Tendencia mensual equipos (últimos 6 meses)
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', c.fecha), 'YYYY-MM') AS label,
                ROUND(SUM({_EQ_FUNC_SQL})::numeric
                    / NULLIF(SUM({_EQ_TOTAL_SQL}), 0) * 100, 1) AS pct
            {eq_lateral}
            GROUP BY DATE_TRUNC('month', c.fecha)
            ORDER BY DATE_TRUNC('month', c.fecha)
            LIMIT 8
        """, tuple(eq_params))
        eq_trend = [{"label": r["label"], "pct": float(r["pct"] or 0)} for r in cur.fetchall()]

        # Distribución por tipo de equipo (radio vs arma vs otro) from inventario JSON
        cur.execute(f"""
            SELECT
                LOWER(TRIM(elem->>'tipo_equipo')) AS tipo,
                SUM({_EQ_TOTAL_SQL}) AS total,
                SUM({_EQ_FUNC_SQL})  AS operativos
            {eq_lateral}
            GROUP BY LOWER(TRIM(elem->>'tipo_equipo'))
            ORDER BY total DESC
            LIMIT 10
        """, tuple(eq_params))
        eq_por_tipo = [
            {
                "tipo": r["tipo"] or "otro",
                "total": int(r["total"] or 0),
                "operativos": int(r["operativos"] or 0),
            }
            for r in cur.fetchall()
        ]

        # ── Vehículos ─────────────────────────────────────────────────────────
        veh_date = _veh_date()
        veh_conds, veh_params = [], []
        _add_cliente(veh_conds, veh_params, cliente)
        if start_date:
            veh_conds.append(f"{veh_date} >= %s")
            veh_params.append(start_date)
        if end_date:
            veh_conds.append(f"{veh_date} <= %s")
            veh_params.append(end_date)
        veh_where = _where(veh_conds)
        cur.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN NOT ({_VEH_FAULT_EXPR}) THEN 1 ELSE 0 END) AS aptos,
                SUM(CASE WHEN {_VEH_FAULT_EXPR} THEN 1 ELSE 0 END) AS no_aptos
            FROM planilla_vehicular
            {veh_where}
        """, tuple(veh_params))
        veh_row = cur.fetchone() or {}
        veh_total = int(veh_row.get("total") or 0)
        veh_aptos = int(veh_row.get("aptos") or 0)
        veh_no_aptos = int(veh_row.get("no_aptos") or 0)
        veh_mant = veh_total - veh_aptos - veh_no_aptos
        veh_pct = round(veh_aptos / veh_total * 100, 1) if veh_total else None

        # ── Certificaciones / Cumplimiento ────────────────────────────────────
        cum_conds, cum_params = [], []
        _add_cliente(cum_conds, cum_params, cliente)
        if start_date:
            cum_conds.append("fecha_hora >= %s")
            cum_params.append(start_date)
        if end_date:
            cum_conds.append("fecha_hora <= %s")
            cum_params.append(end_date)
        cum_where = _where(cum_conds)
        cur.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN LOWER(TRIM(nivel_cumplimiento)) = 'cumple' THEN 1 ELSE 0 END) AS vigentes,
                SUM(CASE WHEN vigencia_hasta IS NOT NULL AND vigencia_hasta < CURRENT_DATE THEN 1 ELSE 0 END) AS vencidas,
                SUM(CASE WHEN vigencia_hasta IS NOT NULL
                         AND vigencia_hasta >= CURRENT_DATE
                         AND vigencia_hasta <= CURRENT_DATE + INTERVAL '30 days' THEN 1 ELSE 0 END) AS proximas
            FROM checklist_cumplimiento
            {cum_where}
        """, tuple(cum_params))
        cum_row = cur.fetchone() or {}
        cum_total = int(cum_row.get("total") or 0)
        cum_vigentes = int(cum_row.get("vigentes") or 0)
        cum_vencidas = int(cum_row.get("vencidas") or 0)
        cum_proximas = int(cum_row.get("proximas") or 0)
        cum_pct = round(cum_vigentes / cum_total * 100, 1) if cum_total else None

        # ── Confiabilidad general (weighted avg of eq + veh + cum) ────────────
        scores = [s for s in [eq_pct, veh_pct, cum_pct] if s is not None]
        conf_general = round(sum(scores) / len(scores), 1) if scores else None

        # ── Semáforo ──────────────────────────────────────────────────────────
        def semaforo(pct):
            if pct is None:
                return "gris"
            if pct >= 85:
                return "verde"
            if pct >= 70:
                return "amarillo"
            return "rojo"

        semaforo_recursos = {
            "equipos":      semaforo(eq_pct),
            "vehiculos":    semaforo(veh_pct),
            "cumplimiento": semaforo(cum_pct),
            "general":      semaforo(conf_general),
        }

        # ── Listado de alertas (resumen operativo) ────────────────────────────
        alertas_listado = []
        # Certificaciones vencidas
        cert_conds2 = list(cum_conds) + ["vigencia_hasta IS NOT NULL", "vigencia_hasta < CURRENT_DATE"]
        cur.execute(f"""
            SELECT
                'Certificación' AS tipo,
                COALESCE(NULLIF(TRIM(curso_certificacion), ''), 'Certificación #' || id::text) AS elemento,
                cliente_instalacion AS cliente,
                'Vencida' AS estado,
                vigencia_hasta AS vencimiento,
                (CURRENT_DATE - vigencia_hasta) AS dias_restantes
            FROM checklist_cumplimiento
            {_where(cert_conds2)}
            ORDER BY vigencia_hasta ASC
            LIMIT 10
        """, tuple(cum_params))
        for r in cur.fetchall():
            alertas_listado.append({
                "tipo": r["tipo"],
                "elemento": r["elemento"],
                "cliente": r["cliente"],
                "estado": r["estado"],
                "vencimiento": r["vencimiento"].isoformat() if r["vencimiento"] else None,
                "dias_restantes": -int(r["dias_restantes"]) if r["dias_restantes"] is not None else None,
            })

        # Vehículos no aptos
        veh_conds2 = list(veh_conds) + [f"({_VEH_FAULT_EXPR})"]
        cur.execute(f"""
            SELECT
                'Vehículo' AS tipo,
                COALESCE(NULLIF(TRIM(placa_vehiculo), ''), 'Vehículo #' || id_planilla_vehicular::text) AS elemento,
                cliente_instalacion AS cliente,
                'No apto' AS estado,
                NULL::date AS vencimiento
            FROM planilla_vehicular
            {_where(veh_conds2)}
            ORDER BY creado_en DESC
            LIMIT 10
        """, tuple(veh_params))
        for r in cur.fetchall():
            alertas_listado.append({
                "tipo": r["tipo"],
                "elemento": r["elemento"],
                "cliente": r["cliente"],
                "estado": r["estado"],
                "vencimiento": None,
                "dias_restantes": None,
            })

        # Equipos no operativos (registros con equipos_operativos < total)
        eq_conds2 = list(eq_conds) + [
            f"({_EQ_FUNC_SQL}) < ({_EQ_TOTAL_SQL})",
            f"({_EQ_TOTAL_SQL}) > 0",
        ]
        eq_where2 = _where(eq_conds2)
        cur.execute(f"""
            SELECT
                'Equipo' AS tipo,
                COALESCE(NULLIF(TRIM(elem->>'nombre_equipo'), ''),
                         NULLIF(TRIM(elem->>'tipo_equipo'), ''),
                         'Equipo') AS elemento,
                c.cliente_instalacion AS cliente,
                'Fuera de servicio' AS estado
            FROM confiabilidad_equipos c,
                 LATERAL jsonb_array_elements(c.inventario) AS elem
            {eq_where2}
            ORDER BY c.fecha DESC
            LIMIT 10
        """, tuple(eq_params))
        for r in cur.fetchall():
            alertas_listado.append({
                "tipo": r["tipo"],
                "elemento": r["elemento"],
                "cliente": r["cliente"],
                "estado": r["estado"],
                "vencimiento": None,
                "dias_restantes": None,
            })

        total_alertas = len(alertas_listado)

        # ── Acciones recomendadas ─────────────────────────────────────────────
        acciones = []
        if cum_vencidas:
            acciones.append(f"Renovar {cum_vencidas} certificaciones vencidas.")
        if veh_no_aptos:
            acciones.append(f"Revisar {veh_no_aptos} vehículos no aptos.")
        if eq_no_op:
            acciones.append(f"Gestionar reparación de {eq_no_op} equipos fuera de servicio.")
        if cum_proximas:
            acciones.append(f"Renovar {cum_proximas} certificaciones que vencen en los próximos 30 días.")

        return jsonify({
            "confiabilidad_general": conf_general,
            "equipos": {
                "pct": eq_pct,
                "total": eq_total,
                "operativos": eq_op,
                "no_operativos": eq_no_op,
            },
            "equipos_por_tipo": eq_por_tipo,
            "vehiculos": {
                "pct": veh_pct,
                "total": veh_total,
                "aptos": veh_aptos,
                "no_aptos": veh_no_aptos,
                "mantenimiento": max(veh_mant, 0),
            },
            "certificaciones": {
                "pct": cum_pct,
                "total": cum_total,
                "vigentes": cum_vigentes,
                "vencidas": cum_vencidas,
                "proximas_vencer": cum_proximas,
            },
            "semaforo": semaforo_recursos,
            "tendencia_eq": eq_trend,
            "alertas": {
                "total": total_alertas,
                "certificaciones_vencidas": cum_vencidas,
                "proximas_vencer": cum_proximas,
                "vehiculos_no_aptos": veh_no_aptos,
                "equipos_no_op": eq_no_op,
                "listado": alertas_listado[:20],
            },
            "acciones": acciones,
            "ultima_actualizacion": today.isoformat(),
        })
    except Exception as e:
        app_logger.error(f"cgeo_api_recursos_data error: {e}", exc_info=True)
        return jsonify({"error": "Error interno"}), 500
    finally:
        conn.close()


# ── API: Motor de Alertas Accionables ────────────────────────────────────────

@cgeo_bp.route("/api/alertas")
@jwt_required()
@_admin_required
def cgeo_api_alertas():
    """
    Evalúa 8 reglas de negocio y devuelve alertas priorizadas.
    Orden: ROJO primero (reglas 1-3), luego AMARILLO (4-8); dentro de cada
    color, las más antiguas primero (mayor urgencia).
    """
    cliente = request.args.get("cliente")
    if cliente in ('Todos', ''):
        cliente = None

    conn = _get_conn()
    if not conn:
        return jsonify({"error": "DB no disponible"}), 500

    try:
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)
        alertas = []

        def _cp(col="id_propiedad"):
            cond, val = _cliente_cond(cliente)
            return ([cond], [val]) if cond else ([], [])

        # ── REGLA 1: Puesto sin supervisión > 48 h ────────────────────────────
        r1_conds, r1_params = _cp("id_propiedad")
        cur.execute(f"""
            SELECT
                TRIM(cliente_instalacion) AS puesto,
                MAX(fecha_hora) AS ultima_sup,
                EXTRACT(EPOCH FROM (NOW() - MAX(fecha_hora))) / 3600 AS horas,
                MAX(id_supervision) AS last_id
            FROM supervision_puesto
            {_where(r1_conds)}
            GROUP BY TRIM(cliente_instalacion)
            HAVING MAX(fecha_hora) < NOW() - INTERVAL '48 hours'
            ORDER BY MAX(fecha_hora) ASC
            LIMIT 5
        """, tuple(r1_params))
        for r in cur.fetchall():
            h = float(r["horas"] or 0)
            dias = round(h / 24, 1)
            texto = (f'"{r["puesto"]}" sin supervisión hace {int(h)}h'
                     if h < 48 else
                     f'"{r["puesto"]}" sin supervisión hace {dias} días')
            alertas.append({
                "id": f"r1_{r['puesto']}",
                "regla": 1,
                "texto": texto,
                "accion": "Ver última supervisión",
                "ruta_navegacion": f"/dashboard/supervision/?id={r['last_id']}",
                "record_id": r["last_id"],
                "form_type": "supervision_puesto",
                "color_semaforo": "rojo",
                "timestamp": r["ultima_sup"].isoformat() if r["ultima_sup"] else None,
                "horas": round(h, 1),
            })

        # ── REGLA 2: Incidente abierto sin gestión > 24 h ────────────────────
        r2_conds, r2_params = _cp()
        r2_conds += [
            "LOWER(TRIM(COALESCE(estado,''))) NOT IN ('cerrado','closed','resuelto','resolved')",
            "COALESCE(fecha_hora AT TIME ZONE 'UTC', creado_en) < NOW() - INTERVAL '24 hours'",
        ]
        cur.execute(f"""
            SELECT
                id_reporte_incidente AS id,
                COALESCE(NULLIF(TRIM(tipo_incidente),''), 'Incidente') AS tipo,
                EXTRACT(EPOCH FROM (NOW() - COALESCE(fecha_hora AT TIME ZONE 'UTC', creado_en))) / 3600 AS horas,
                COALESCE(fecha_hora AT TIME ZONE 'UTC', creado_en) AS ts,
                COALESCE(NULLIF(TRIM(estado),''), 'Abierto') AS estado,
                NULLIF(TRIM(COALESCE(responsable_asignado,'')), '') AS responsable_asignado
            FROM reportes_incidentes
            {_where(r2_conds)}
            ORDER BY COALESCE(fecha_hora AT TIME ZONE 'UTC', creado_en) ASC
            LIMIT 5
        """, tuple(r2_params))
        for r in cur.fetchall():
            h = float(r["horas"] or 0)
            alertas.append({
                "id": f"r2_{r['id']}",
                "regla": 2,
                "texto": f"Incidente #{r['id']} abierto hace {int(h)}h — {r['tipo']}",
                "accion": "Ver incidente",
                "ruta_navegacion": f"/dashboard/incidentes/?id={r['id']}",
                "record_id": r["id"],
                "form_type": "reporte_incidente",
                "color_semaforo": "rojo",
                "timestamp": r["ts"].isoformat() if r["ts"] else None,
                "horas": round(h, 1),
                "estado": r["estado"],
                "responsable_asignado": r["responsable_asignado"],
            })

        # ── REGLA 3: Cliente con historial reciente pero sin supervisión hoy ──
        # Proxy: clientes supervisados en los últimos 7 días pero NO hoy.
        r3_conds, r3_params = _cp("id_propiedad")
        r3_conds_hist = r3_conds + ["fecha_hora >= NOW() - INTERVAL '7 days'"]
        r3_conds_hoy  = r3_conds + ["fecha_hora::date = CURRENT_DATE"]
        cur.execute(f"""
            SELECT
                TRIM(cliente_instalacion) AS puesto,
                MAX(fecha_hora) AS ultima_sup
            FROM supervision_puesto
            {_where(r3_conds_hist)}
            GROUP BY TRIM(cliente_instalacion)
            HAVING TRIM(cliente_instalacion) NOT IN (
                SELECT TRIM(cliente_instalacion)
                FROM supervision_puesto
                {_where(r3_conds_hoy)}
            )
            ORDER BY MAX(fecha_hora) ASC
            LIMIT 5
        """, tuple(r3_params + r3_params))
        for r in cur.fetchall():
            puesto = r['puesto'] or ''
            alertas.append({
                "id": f"r3_{puesto}",
                "regla": 3,
                "texto": f"Puesto \"{puesto}\" sin supervisión registrada hoy",
                "accion": "Ver supervisiones",
                "ruta_navegacion": "/dashboard/supervision/",
                "color_semaforo": "rojo",
                "timestamp": r["ultima_sup"].isoformat() if r["ultima_sup"] else None,
                "horas": None,
            })

        # ── REGLA 4: Certificación próxima a vencer (≤ 30 días) ──────────────
        r4_conds, r4_params = _cp()
        r4_conds += [
            "vigencia_hasta IS NOT NULL",
            "vigencia_hasta >= CURRENT_DATE",
            "vigencia_hasta <= CURRENT_DATE + INTERVAL '30 days'",
        ]
        cur.execute(f"""
            SELECT
                id,
                COALESCE(NULLIF(TRIM(curso_certificacion),''), 'Certificación #' || id::text) AS cert,
                cliente_instalacion AS cliente,
                vigencia_hasta,
                (vigencia_hasta - CURRENT_DATE) AS dias_restantes
            FROM checklist_cumplimiento
            {_where(r4_conds)}
            ORDER BY vigencia_hasta ASC
            LIMIT 5
        """, tuple(r4_params))
        for r in cur.fetchall():
            d = int(r["dias_restantes"] or 0)
            alertas.append({
                "id": f"r4_{r['id']}",
                "regla": 4,
                "texto": f"Certificación \"{r['cert']}\" en {r['cliente']} vence en {d} días",
                "accion": "Ver certificación",
                "ruta_navegacion": f"/dashboard/cumplimiento/?id={r['id']}",
                "record_id": r["id"],
                "form_type": "checklist_cumplimiento",
                "color_semaforo": "amarillo",
                "timestamp": r["vigencia_hasta"].isoformat() if r["vigencia_hasta"] else None,
                "horas": d * 24,
            })

        # ── REGLA 5: Equipo sin registro de confiabilidad > 45 días ──────────
        r5_conds, r5_params = _cp()
        cur.execute(f"""
            SELECT
                cliente_instalacion AS instalacion,
                MAX(fecha) AS ultimo_reg,
                (CURRENT_DATE - MAX(fecha)) AS dias
            FROM confiabilidad_equipos
            {_where(r5_conds)}
            GROUP BY cliente_instalacion
            HAVING MAX(fecha) < CURRENT_DATE - INTERVAL '45 days'
            ORDER BY MAX(fecha) ASC
            LIMIT 5
        """, tuple(r5_params))
        for r in cur.fetchall():
            d = int(r["dias"] or 0)
            alertas.append({
                "id": f"r5_{r['instalacion']}",
                "regla": 5,
                "texto": f"Equipos en \"{r['instalacion']}\" sin reporte de confiabilidad hace {d} días",
                "accion": "Reportar estado",
                "ruta_navegacion": f"/dashboard/equipos/?cliente={r['instalacion']}",
                "color_semaforo": "amarillo",
                "timestamp": r["ultimo_reg"].isoformat() if r["ultimo_reg"] else None,
                "horas": d * 24,
            })

        # ── REGLA 6: Vehículo sin pre-operacional > 24 h ─────────────────────
        r6_conds, r6_params = _cp()
        r6_conds += ["placa_vehiculo IS NOT NULL", "TRIM(placa_vehiculo) != ''"]
        cur.execute(f"""
            SELECT
                TRIM(placa_vehiculo) AS placa,
                cliente_instalacion AS cliente,
                MAX(COALESCE(fecha_hora, creado_en)) AS ultimo_preop,
                EXTRACT(EPOCH FROM (NOW() - MAX(COALESCE(fecha_hora, creado_en)))) / 3600 AS horas
            FROM planilla_vehicular
            {_where(r6_conds)}
            GROUP BY TRIM(placa_vehiculo), cliente_instalacion
            HAVING MAX(COALESCE(fecha_hora, creado_en)) < NOW() - INTERVAL '24 hours'
            ORDER BY MAX(COALESCE(fecha_hora, creado_en)) ASC
            LIMIT 5
        """, tuple(r6_params))
        for r in cur.fetchall():
            h = float(r["horas"] or 0)
            alertas.append({
                "id": f"r6_{r['placa']}",
                "regla": 6,
                "texto": f"Vehículo {r['placa']} ({r['cliente']}) sin pre-operacional hace {int(h)}h",
                "accion": "Registrar pre-op",
                "ruta_navegacion": f"/dashboard/vehiculos/?placa={r['placa']}&cliente={r['cliente']}",
                "color_semaforo": "amarillo",
                "timestamp": r["ultimo_preop"].isoformat() if r["ultimo_preop"] else None,
                "horas": round(h, 1),
            })

        # ── REGLA 7: Checklist SST / cumplimiento vencido ─────────────────────
        r7_conds, r7_params = _cp()
        r7_conds += [
            "vigencia_hasta IS NOT NULL",
            "vigencia_hasta < CURRENT_DATE",
        ]
        cur.execute(f"""
            SELECT
                id,
                cliente_instalacion AS instalacion,
                vigencia_hasta,
                (CURRENT_DATE - vigencia_hasta) AS dias_vencido,
                COALESCE(NULLIF(TRIM(curso_certificacion),''), 'Checklist #' || id::text) AS nombre
            FROM checklist_cumplimiento
            {_where(r7_conds)}
            ORDER BY vigencia_hasta ASC
            LIMIT 5
        """, tuple(r7_params))
        for r in cur.fetchall():
            d = int(r["dias_vencido"] or 0)
            alertas.append({
                "id": f"r7_{r['id']}",
                "regla": 7,
                "texto": f"Checklist \"{r['nombre']}\" en {r['instalacion']} vencido hace {d} días",
                "accion": "Renovar checklist",
                "ruta_navegacion": f"/dashboard/cumplimiento/?id={r['id']}",
                "record_id": r["id"],
                "form_type": "checklist_cumplimiento",
                "color_semaforo": "amarillo",
                "timestamp": r["vigencia_hasta"].isoformat() if r["vigencia_hasta"] else None,
                "horas": d * 24,
            })

        # ── REGLA 8: NPS cliente bajo (< 3.0/5) en últimos 30 días ───────────
        # La escala almacenada es 0-40; 3.0/5 equivale a 24 en esa escala.
        r8_conds, r8_params = _cp()
        r8_conds += ["fecha_hora >= NOW() - INTERVAL '30 days'"]
        cur.execute(f"""
            SELECT
                TRIM(cliente_instalacion) AS cliente,
                ROUND(AVG(calificacion_global_nps), 2) AS avg_raw,
                ROUND(AVG(calificacion_global_nps) / 40 * 5, 2) AS avg_5,
                COUNT(*) AS encuestas,
                MAX(fecha_hora) AS ultima,
                MAX(id_encuesta) AS last_encuesta_id
            FROM medicion_experiencia_cliente
            {_where(r8_conds)}
            GROUP BY TRIM(cliente_instalacion)
            HAVING COUNT(*) > 0
               AND AVG(calificacion_global_nps) < 24
            ORDER BY AVG(calificacion_global_nps) ASC
            LIMIT 5
        """, tuple(r8_params))
        for r in cur.fetchall():
            score = float(r["avg_5"] or 0)
            alertas.append({
                "id": f"r8_{r['cliente']}",
                "regla": 8,
                "texto": f"Satisfacción baja en \"{r['cliente']}\": {score}/5 promedio ({r['encuestas']} encuestas)",
                "accion": "Ver encuesta",
                "record_id": r["last_encuesta_id"],
                "form_type": "medicion_experiencia_cliente",
                "ruta_navegacion": f"/dashboard/satisfaccion/",
                "color_semaforo": "amarillo",
                "timestamp": r["ultima"].isoformat() if r["ultima"] else None,
                "horas": None,
            })

        # ── Ordenar: ROJO primero, luego AMARILLO; dentro de cada color ───────
        # por timestamp ascendente (más antiguo = más urgente).
        prioridad = {"rojo": 0, "amarillo": 1}
        alertas.sort(key=lambda a: (
            prioridad.get(a["color_semaforo"], 9),
            a["timestamp"] or "9999",
        ))

        return jsonify({
            "alertas": alertas,
            "total": len(alertas),
            "rojas": sum(1 for a in alertas if a["color_semaforo"] == "rojo"),
            "amarillas": sum(1 for a in alertas if a["color_semaforo"] == "amarillo"),
            "timestamp": date.today().isoformat(),
        })

    except Exception as e:
        app_logger.error(f"cgeo_api_alertas error: {e}", exc_info=True)
        return jsonify({"error": "Error interno"}), 500
    finally:
        conn.close()


# ── API: Semáforo Global ─────────────────────────────────────────────────────

@cgeo_bp.route("/api/semaforo-global")
@jwt_required()
def cgeo_api_semaforo_global():
    """
    Retorna los KPIs necesarios para calcular el semáforo global de la operación.
    Diseñado para ser llamado junto con /api/alertas desde el Morning Briefing.
    """
    cliente = request.args.get("cliente")
    if cliente in ('Todos', ''):
        cliente = None

    conn = _get_conn()
    if not conn:
        return jsonify({"error": "DB no disponible"}), 500
    try:
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)

        def _cp(col="id_propiedad"):
            cond, val = _cliente_cond(cliente)
            return ([cond], [val]) if cond else ([], [])

        # Incidentes abiertos
        inc_conds, inc_params = _cp()
        inc_conds += [
            "LOWER(TRIM(COALESCE(estado,''))) NOT IN ('cerrado','closed','resuelto','resolved')"
        ]
        cur.execute(f"""
            SELECT COUNT(*) AS total FROM reportes_incidentes {_where(inc_conds)}
        """, tuple(inc_params))
        inc_abiertos = int((cur.fetchone() or {}).get("total") or 0)

        # Supervisiones: programadas hoy vs completadas hoy
        # Usamos puestos activos (supervisados en los últimos 30 días) como proxy de programadas
        sup_conds_hist, sup_params_hist = _cp("id_propiedad")
        sup_conds_hist_full = sup_conds_hist + ["fecha_hora >= NOW() - INTERVAL '30 days'"]
        cur.execute(f"""
            SELECT COUNT(DISTINCT TRIM(cliente_instalacion)) AS programadas
            FROM supervision_puesto {_where(sup_conds_hist_full)}
        """, sup_params_hist)
        sup_programadas = int((cur.fetchone() or {}).get("programadas") or 0)

        sup_conds_hoy, sup_params_hoy = _cp("id_propiedad")
        sup_conds_hoy_full = sup_conds_hoy + ["fecha_hora::date = CURRENT_DATE"]
        cur.execute(f"""
            SELECT COUNT(DISTINCT TRIM(cliente_instalacion)) AS completadas
            FROM supervision_puesto {_where(sup_conds_hoy_full)}
        """, sup_params_hoy)
        sup_completadas = int((cur.fetchone() or {}).get("completadas") or 0)

        # Equipos no operativos vs flota total
        eq_conds, eq_params = _cp()
        cur.execute(f"""
            SELECT
                COALESCE(SUM({_EQ_TOTAL_SQL}), 0) AS total,
                COALESCE(SUM({_EQ_FUNC_SQL}), 0)  AS operativos
            FROM confiabilidad_equipos c,
                 LATERAL jsonb_array_elements(
                     CASE WHEN jsonb_typeof(c.inventario) = 'array' THEN c.inventario ELSE '[]'::jsonb END
                 ) AS elem
            {_where(eq_conds)}
        """, tuple(eq_params))
        eq_row = cur.fetchone() or {}
        eq_total = int(eq_row.get("total") or 0)
        eq_op    = int(eq_row.get("operativos") or 0)
        eq_no_op = max(0, eq_total - eq_op)

        # Certificaciones próximas a vencer (≤ 30 días)
        cert_conds, cert_params = _cp()
        cert_conds += [
            "vigencia_hasta IS NOT NULL",
            "vigencia_hasta >= CURRENT_DATE",
            "vigencia_hasta <= CURRENT_DATE + INTERVAL '30 days'",
        ]
        cur.execute(f"""
            SELECT COUNT(*) AS total FROM checklist_cumplimiento {_where(cert_conds)}
        """, tuple(cert_params))
        cert_proximas = int((cur.fetchone() or {}).get("total") or 0)

        return jsonify({
            "inc_abiertos": inc_abiertos,
            "sup_completadas": sup_completadas,
            "sup_programadas": sup_programadas,
            "eq_no_op": eq_no_op,
            "eq_total": eq_total,
            "cert_proximas": cert_proximas,
        })

    except Exception as e:
        app_logger.error(f"cgeo_api_semaforo_global error: {e}", exc_info=True)
        return jsonify({"error": "Error interno"}), 500
    finally:
        conn.close()


# ── API: Morning Briefing ─────────────────────────────────────────────────────

@cgeo_bp.route("/api/morning-briefing-data")
@jwt_required()
@_admin_required
def cgeo_api_morning_briefing_data():
    """
    Single endpoint that returns all data needed by the Morning Briefing screen:
    KPIs (incidentes, supervisiones, equipos, certificaciones), supervision
    trend for the last 7 days (completadas vs programadas), and greeting data.
    """
    from datetime import date as _date, timedelta
    from admin_bp import get_thresholds

    conn = _get_conn()
    if not conn:
        return jsonify({"error": "DB no disponible"}), 500
    try:
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)

        # Fecha de inicio de operación configurada por el Administrador
        thresholds = get_thresholds()
        fecha_inicio_raw = thresholds.get('fecha_inicio_operacion')
        try:
            from datetime import date as _date2
            fecha_inicio = _date2.fromisoformat(fecha_inicio_raw) if fecha_inicio_raw else None
        except (ValueError, TypeError):
            fecha_inicio = None

        # ── Incidentes abiertos ───────────────────────────────────────────────
        if fecha_inicio:
            cur.execute("""
                SELECT
                    COUNT(*) AS total_abiertos,
                    SUM(CASE WHEN LOWER(TRIM(nivel_severidad)) IN ('crítico','critico') THEN 1 ELSE 0 END) AS criticos,
                    SUM(CASE WHEN COALESCE(fecha_hora AT TIME ZONE 'UTC', creado_en) < NOW() - INTERVAL '24 hours' THEN 1 ELSE 0 END) AS mas_24h
                FROM reportes_incidentes
                WHERE LOWER(TRIM(COALESCE(estado,'')))
                      NOT IN ('cerrado','closed','resuelto','resolved')
                  AND COALESCE(fecha_hora::date, creado_en::date) >= %s
            """, (fecha_inicio,))
        else:
            cur.execute("""
                SELECT
                    COUNT(*) AS total_abiertos,
                    SUM(CASE WHEN LOWER(TRIM(nivel_severidad)) IN ('crítico','critico') THEN 1 ELSE 0 END) AS criticos,
                    SUM(CASE WHEN COALESCE(fecha_hora AT TIME ZONE 'UTC', creado_en) < NOW() - INTERVAL '24 hours' THEN 1 ELSE 0 END) AS mas_24h
                FROM reportes_incidentes
                WHERE LOWER(TRIM(COALESCE(estado,'')))
                      NOT IN ('cerrado','closed','resuelto','resolved')
            """)
        inc_row = cur.fetchone() or {}
        inc_abiertos  = int(inc_row.get("total_abiertos") or 0)
        inc_criticos  = int(inc_row.get("criticos") or 0)
        inc_mas_24h   = int(inc_row.get("mas_24h") or 0)

        # ── Supervisiones hoy vs puestos activos (programadas proxy) ─────────
        sup_inicio = fecha_inicio if fecha_inicio else (_date.today() - timedelta(days=30))
        cur.execute("""
            SELECT COUNT(DISTINCT TRIM(cliente_instalacion)) AS programadas
            FROM supervision_puesto
            WHERE fecha_hora >= %s
        """, (sup_inicio,))
        sup_programadas = int((cur.fetchone() or {}).get("programadas") or 0)

        cur.execute("""
            SELECT COUNT(DISTINCT TRIM(cliente_instalacion)) AS completadas
            FROM supervision_puesto
            WHERE fecha_hora::date = CURRENT_DATE
        """)
        sup_completadas = int((cur.fetchone() or {}).get("completadas") or 0)

        # ── Equipos no operativos ─────────────────────────────────────────────
        cur.execute(f"""
            SELECT
                COALESCE(SUM({_EQ_TOTAL_SQL}), 0) AS total,
                COALESCE(SUM({_EQ_FUNC_SQL}), 0)  AS operativos
            FROM confiabilidad_equipos c,
                 LATERAL jsonb_array_elements(
                     CASE WHEN jsonb_typeof(c.inventario) = 'array' THEN c.inventario ELSE '[]'::jsonb END
                 ) AS elem
        """)
        eq_row = cur.fetchone() or {}
        eq_total = int(eq_row.get("total") or 0)
        eq_op    = int(eq_row.get("operativos") or 0)
        eq_no_op = max(0, eq_total - eq_op)
        eq_pct   = round(eq_op / eq_total * 100, 1) if eq_total else None

        # ── Equipos por tipo (radios, armas, motos) ───────────────────────────
        cur.execute(f"""
            SELECT
                LOWER(TRIM(elem->>'tipo_equipo')) AS tipo,
                COALESCE(SUM({_EQ_TOTAL_SQL}), 0) AS total,
                COALESCE(SUM({_EQ_FUNC_SQL}), 0)  AS operativos
            FROM confiabilidad_equipos c,
                 LATERAL jsonb_array_elements(
                     CASE WHEN jsonb_typeof(c.inventario) = 'array' THEN c.inventario ELSE '[]'::jsonb END
                 ) AS elem
            WHERE elem->>'tipo_equipo' IS NOT NULL
            GROUP BY LOWER(TRIM(elem->>'tipo_equipo'))
        """)
        eq_por_tipo = {
            r["tipo"]: {"total": int(r["total"] or 0), "operativos": int(r["operativos"] or 0)}
            for r in cur.fetchall() if r["tipo"]
        }

        # ── Certificaciones próximas a vencer (≤ 30 días) por nivel ─────────
        cur.execute("""
            SELECT
                COALESCE(NULLIF(TRIM(nivel_cumplimiento), ''), 'Sin categoría') AS nivel,
                COUNT(*) AS total
            FROM checklist_cumplimiento
            WHERE vigencia_hasta IS NOT NULL
              AND vigencia_hasta >= CURRENT_DATE
              AND vigencia_hasta <= CURRENT_DATE + INTERVAL '30 days'
            GROUP BY TRIM(nivel_cumplimiento)
        """)
        cert_por_nivel = {r["nivel"]: int(r["total"] or 0) for r in cur.fetchall()}
        cert_proximas = sum(cert_por_nivel.values())

        # ── Tendencia supervisiones — últimos 7 días ──────────────────────────
        today = _date.today()
        days7 = [today - timedelta(days=i) for i in range(6, -1, -1)]
        # Si fecha_inicio es posterior al inicio de la ventana de 7 días, recortamos
        trend_start = max(days7[0], fecha_inicio) if fecha_inicio else days7[0]

        # Completadas por día (últimos 7 días, conteo de puestos únicos supervisados)
        cur.execute("""
            SELECT
                fecha_hora::date AS dia,
                COUNT(DISTINCT TRIM(cliente_instalacion)) AS completadas
            FROM supervision_puesto
            WHERE fecha_hora::date >= %s
            GROUP BY fecha_hora::date
        """, (trend_start,))
        comp_by_day = {r["dia"]: int(r["completadas"]) for r in cur.fetchall()}

        tendencia = [
            {
                "fecha": d.isoformat(),
                "label": str(d.day) + " " + d.strftime("%b"),
                "completadas": comp_by_day.get(d, 0) if (not fecha_inicio or d >= fecha_inicio) else None,
                "programadas": sup_programadas if (not fecha_inicio or d >= fecha_inicio) else None,
            }
            for d in days7
        ]

        return jsonify({
            "kpis": {
                "inc_abiertos":    inc_abiertos,
                "inc_criticos":    inc_criticos,
                "inc_mas_24h":     inc_mas_24h,
                "sup_completadas": sup_completadas,
                "sup_programadas": sup_programadas,
                "eq_total":        eq_total,
                "eq_op":           eq_op,
                "eq_no_op":        eq_no_op,
                "eq_pct":          eq_pct,
                "eq_por_tipo":     eq_por_tipo,
                "cert_proximas":   cert_proximas,
                "cert_por_nivel":  cert_por_nivel,
            },
            "thresholds": {
                "eq_verde_max":    float(thresholds.get('equipos_verde_max', 5)),
                "eq_amarillo_max": float(thresholds.get('equipos_amarillo_max', 15)),
            },
            "tendencia_semana": tendencia,
        })

    except Exception as e:
        app_logger.error(f"cgeo_api_morning_briefing_data error: {e}", exc_info=True)
        return jsonify({"error": "Error interno"}), 500
    finally:
        conn.close()


# ── API: Operación e Incidentes ────────────────────────────────────────────────

@cgeo_bp.route("/api/operacion-data")
@jwt_required()
@_admin_required
def cgeo_api_operacion_data():
    cliente = request.args.get("cliente")
    if cliente in ('Todos', ''):
        cliente = None
    supervisor  = request.args.get("supervisor") or None
    start_date = request.args.get("start_date") or None
    end_date = request.args.get("end_date") or None

    conn = _get_conn()
    if not conn:
        return jsonify({"error": "DB no disponible"}), 500
    try:
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)

        def _date_conds(date_col, conds, params):
            if start_date:
                conds.append(f"{date_col} >= %s")
                params.append(start_date)
            if end_date:
                conds.append(f"{date_col} <= %s")
                params.append(end_date)

        # ── Incidentes ────────────────────────────────────────────────────────
        inc_conds, inc_params = [], []
        _add_cliente(inc_conds, inc_params, cliente)
        _date_conds("fecha_hora", inc_conds, inc_params)
        inc_where = _where(inc_conds)
        cur.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN LOWER(TRIM(nivel_severidad)) IN ('crítico','critico') THEN 1 ELSE 0 END) AS criticos,
                SUM(CASE WHEN LOWER(TRIM(nivel_severidad)) = 'alto' THEN 1 ELSE 0 END) AS altos,
                SUM(CASE WHEN LOWER(TRIM(nivel_severidad)) IN ('medio','moderado') THEN 1 ELSE 0 END) AS medios,
                SUM(CASE WHEN LOWER(TRIM(nivel_severidad)) = 'bajo' THEN 1 ELSE 0 END) AS bajos
            FROM reportes_incidentes
            {inc_where}
        """, tuple(inc_params))
        inc_row = cur.fetchone() or {}
        inc_total   = int(inc_row.get("total") or 0)
        inc_criticos = int(inc_row.get("criticos") or 0)
        inc_altos   = int(inc_row.get("altos") or 0)
        inc_medios  = int(inc_row.get("medios") or 0)
        inc_bajos   = int(inc_row.get("bajos") or 0)

        # Incidentes abiertos (resumen operativo)
        inc_ab_conds = list(inc_conds) + [
            "LOWER(TRIM(COALESCE(estado,''))) NOT IN ('cerrado','closed','resuelto','resolved')"
        ]
        cur.execute(f"""
            SELECT
                CAST(COALESCE(fecha_hora, creado_en) AS date) AS fecha,
                COALESCE(NULLIF(TRIM(descripcion_incidente),''), tipo_incidente, 'Incidente') AS incidente,
                cliente_instalacion AS cliente,
                nivel_severidad AS severidad,
                COALESCE(estado, 'Abierto') AS estado,
                (CURRENT_DATE - CAST(COALESCE(fecha_hora, creado_en) AS date)) AS dias_abierto
            FROM reportes_incidentes
            {_where(inc_ab_conds)}
            ORDER BY COALESCE(fecha_hora, creado_en) DESC
            LIMIT 10
        """, tuple(inc_params))
        inc_abiertos = [
            {
                "fecha": r["fecha"].isoformat() if r["fecha"] else None,
                "incidente": r["incidente"],
                "cliente": r["cliente"],
                "severidad": r["severidad"],
                "estado": r["estado"],
                "dias_abierto": int(r["dias_abierto"]) if r["dias_abierto"] is not None else 0,
            }
            for r in cur.fetchall()
        ]
        inc_criticos_abiertos = sum(1 for i in inc_abiertos if i["severidad"] and "crít" in i["severidad"].lower())

        # Total de abiertos sin límite (para KPI contextual)
        cur.execute(f"""
            SELECT
                COUNT(*) AS total_abiertos,
                SUM(CASE WHEN (CURRENT_DATE - CAST(COALESCE(fecha_hora, creado_en) AS date)) > 0 THEN 1 ELSE 0 END) AS mas_24h
            FROM reportes_incidentes
            {_where(inc_ab_conds)}
        """, tuple(inc_params))
        inc_ab_row = cur.fetchone() or {}
        inc_abiertos_total = int(inc_ab_row.get("total_abiertos") or 0)
        inc_mas_24h = int(inc_ab_row.get("mas_24h") or 0)

        # Tendencia mensual incidentes — separate condition set so supervisor
        # filter scopes only this chart without touching KPIs above.
        trend_conds  = list(inc_conds)
        trend_params = list(inc_params)
        if supervisor:
            trend_conds.append("TRIM(COALESCE(nombre_responsable,'')) = %s")
            trend_params.append(supervisor.strip())
        trend_where = _where(trend_conds)
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', COALESCE(fecha_hora, creado_en)), 'YYYY-MM') AS label,
                COUNT(*) AS total
            FROM reportes_incidentes
            {trend_where}
            GROUP BY DATE_TRUNC('month', COALESCE(fecha_hora, creado_en))
            ORDER BY DATE_TRUNC('month', COALESCE(fecha_hora, creado_en))
            LIMIT 8
        """, tuple(trend_params))
        inc_trend = {r["label"]: int(r["total"]) for r in cur.fetchall()}

        # ── Satisfacción ──────────────────────────────────────────────────────
        sat_conds, sat_params = [], []
        _add_cliente(sat_conds, sat_params, cliente)
        _date_conds("fecha_hora", sat_conds, sat_params)
        sat_where = _where(sat_conds)
        cur.execute(f"""
            SELECT
                AVG(calificacion_global_nps) AS avg_nps,
                COUNT(*) AS total,
                SUM(CASE WHEN LOWER(COALESCE(recomendaria_servicio::TEXT,'')) IN ('sí','si','yes','s') THEN 1 ELSE 0 END) AS recomienda
            FROM medicion_experiencia_cliente
            {sat_where}
        """, tuple(sat_params))
        sat_row = cur.fetchone() or {}
        sat_avg = float(sat_row.get("avg_nps") or 0)
        sat_total = int(sat_row.get("total") or 0)
        sat_rec = int(sat_row.get("recomienda") or 0)
        sat_pct = round(min(sat_avg / 40 * 100, 100), 1) if sat_avg else None
        sat_insatisfechos = max(sat_total - sat_rec, 0)

        # Ranking clientes por satisfacción
        cur.execute(f"""
            SELECT
                TRIM(cliente_instalacion) AS cliente,
                ROUND(AVG(calificacion_global_nps) / 40 * 100, 1) AS pct
            FROM medicion_experiencia_cliente
            {sat_where}
            GROUP BY TRIM(cliente_instalacion)
            HAVING COUNT(*) > 0
            ORDER BY pct DESC
            LIMIT 8
        """, tuple(sat_params))
        ranking = [
            {"cliente": r["cliente"], "pct": float(r["pct"] or 0)}
            for r in cur.fetchall()
        ]

        # Tendencia mensual satisfacción
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', fecha_hora), 'YYYY-MM') AS label,
                ROUND(AVG(calificacion_global_nps) / 40 * 100, 1) AS pct
            FROM medicion_experiencia_cliente
            {sat_where}
            GROUP BY DATE_TRUNC('month', fecha_hora)
            ORDER BY DATE_TRUNC('month', fecha_hora)
            LIMIT 8
        """, tuple(sat_params))
        sat_trend = {r["label"]: float(r["pct"] or 0) for r in cur.fetchall()}

        # ── Supervisión ───────────────────────────────────────────────────────
        sup_conds, sup_params = [], []
        _add_cliente(sup_conds, sup_params, cliente)
        _date_conds("fecha_hora", sup_conds, sup_params)
        sup_where = _where(sup_conds)
        _sup_score = " + ".join(
            f"COALESCE(CASE WHEN {col}::TEXT ~ '^[0-9.]+$' THEN {col}::NUMERIC ELSE 0 END, 0)"
            for col in ["asistencia_puntualidad", "presentacion_uniforme",
                        "estado_limpieza_puesto", "equipamiento_completo", "estado_bitacora"]
        )
        cur.execute(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN ({_sup_score}) >= 21 THEN 1 ELSE 0 END) AS excelente,
                SUM(CASE WHEN ({_sup_score}) >= 16 AND ({_sup_score}) < 21 THEN 1 ELSE 0 END) AS seguimiento,
                SUM(CASE WHEN ({_sup_score}) > 0  AND ({_sup_score}) < 16 THEN 1 ELSE 0 END) AS critico
            FROM supervision_puesto {sup_where}
        """, tuple(sup_params))
        sup_row = cur.fetchone() or {}
        sup_total      = int(sup_row.get("total")      or 0)
        sup_excelente  = int(sup_row.get("excelente")  or 0)
        sup_seguimiento = int(sup_row.get("seguimiento") or 0)
        sup_critico    = int(sup_row.get("critico")    or 0)

        # Tendencia mensual supervisión
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', fecha_hora), 'YYYY-MM') AS label,
                COUNT(*) AS total
            FROM supervision_puesto
            {sup_where}
            GROUP BY DATE_TRUNC('month', fecha_hora)
            ORDER BY DATE_TRUNC('month', fecha_hora)
            LIMIT 8
        """, tuple(sup_params))
        sup_trend = {r["label"]: int(r["total"]) for r in cur.fetchall()}

        # Supervisiones completadas hoy
        sup_hoy_conds = list(sup_conds) + ["fecha_hora::date = CURRENT_DATE"]
        cur.execute(f"""
            SELECT COUNT(*) AS hoy FROM supervision_puesto {_where(sup_hoy_conds)}
        """, tuple(sup_params))
        sup_hoy = int((cur.fetchone() or {}).get("hoy") or 0)

        # ── Capacitaciones ────────────────────────────────────────────────────
        cap_date = _capac_date()
        cap_safe = _capac_safe_len()
        cap_conds, cap_params = [], []
        _add_cliente(cap_conds, cap_params, cliente)
        if start_date:
            cap_conds.append(f"{cap_date} >= %s")
            cap_params.append(start_date)
        if end_date:
            cap_conds.append(f"{cap_date} <= %s")
            cap_params.append(end_date)
        cap_where = _where(cap_conds)
        cur.execute(f"""
            SELECT COUNT(*) AS total, COALESCE(SUM({cap_safe}), 0) AS asistentes
            FROM registro_de_capacitaciones {cap_where}
        """, tuple(cap_params))
        cap_row = cur.fetchone() or {}
        cap_total = int(cap_row.get("total") or 0)
        cap_asist = int(cap_row.get("asistentes") or 0)

        # Tendencia mensual capacitaciones
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', {cap_date}), 'YYYY-MM') AS label,
                COUNT(*) AS total
            FROM registro_de_capacitaciones
            {cap_where}
            GROUP BY DATE_TRUNC('month', {cap_date})
            ORDER BY DATE_TRUNC('month', {cap_date})
            LIMIT 8
        """, tuple(cap_params))
        cap_trend = {r["label"]: int(r["total"]) for r in cur.fetchall()}

        # ── Disciplina ────────────────────────────────────────────────────────
        disc_conds, disc_params = [], []
        _add_cliente(disc_conds, disc_params, cliente)
        _date_conds("fecha_hora", disc_conds, disc_params)
        disc_where = _where(disc_conds)
        cur.execute(f"""
            SELECT COUNT(*) AS total FROM informe_novedades_disciplinario {disc_where}
        """, tuple(disc_params))
        disc_row = cur.fetchone() or {}
        disc_total = int(disc_row.get("total") or 0)

        # ── Compromisos / visitas (resumen operativo) ─────────────────────────
        vis_conds, vis_params = [], []
        _add_cliente(vis_conds, vis_params, cliente)
        _date_conds("fecha_hora", vis_conds, vis_params)
        vis_where = _where(vis_conds)
        compromisos_pend = []
        vis_total = vis_cumplidos = vis_pendientes = vis_vencidos = 0
        try:
            # Aggregate counts for visitas KPI card
            cur.execute(f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN LOWER(TRIM(COALESCE(estado,'')))
                        IN ('cumplido','completado','cumplida','completada') THEN 1 ELSE 0 END) AS cumplidos,
                    SUM(CASE WHEN LOWER(TRIM(COALESCE(estado,'')))
                        IN ('vencido','vencida','expirado','expirada') THEN 1 ELSE 0 END) AS vencidos,
                    SUM(CASE WHEN LOWER(TRIM(COALESCE(estado,'')))
                        NOT IN ('cumplido','completado','cumplida','completada',
                                'vencido','vencida','expirado','expirada') THEN 1 ELSE 0 END) AS pendientes
                FROM registro_y_acta_de_visita
                {vis_where}
            """, tuple(vis_params))
            vis_row = cur.fetchone() or {}
            vis_total      = int(vis_row.get("total")      or 0)
            vis_cumplidos  = int(vis_row.get("cumplidos")  or 0)
            vis_vencidos   = int(vis_row.get("vencidos")   or 0)
            vis_pendientes = int(vis_row.get("pendientes") or 0)

            cur.execute(f"""
                SELECT
                    CAST(COALESCE(fecha_hora, creado_en) AS date) AS fecha_compromiso,
                    COALESCE(NULLIF(TRIM(motivo_visita),''), 'Visita') AS compromiso,
                    cliente_instalacion AS cliente,
                    COALESCE(estado, 'Pendiente') AS estado,
                    (CURRENT_DATE - CAST(COALESCE(fecha_hora, creado_en) AS date)) AS dias_retraso
                FROM registro_y_acta_de_visita
                {vis_where}
                ORDER BY COALESCE(fecha_hora, creado_en) DESC
                LIMIT 10
            """, tuple(vis_params))
            compromisos_pend = [
                {
                    "fecha": r["fecha_compromiso"].isoformat() if r["fecha_compromiso"] else None,
                    "compromiso": r["compromiso"],
                    "cliente": r["cliente"],
                    "estado": r["estado"],
                    "dias_retraso": int(r["dias_retraso"]) if r["dias_retraso"] is not None else 0,
                }
                for r in cur.fetchall()
            ]
        except Exception:
            pass

        # ── Unified trend labels ──────────────────────────────────────────────
        all_labels = sorted(set(list(inc_trend) + list(sat_trend) + list(sup_trend) + list(cap_trend)))

        # ── Acciones recomendadas (resumen operativo) ─────────────────────────
        acciones = []
        if inc_criticos_abiertos:
            acciones.append(f"Cerrar {inc_criticos_abiertos} incidentes críticos abiertos.")
        if len(compromisos_pend):
            acciones.append(f"Gestionar {len(compromisos_pend)} compromisos pendientes.")
        if sat_insatisfechos:
            acciones.append(f"Dar seguimiento a {sat_insatisfechos} clientes insatisfechos.")
        if cap_total == 0:
            acciones.append("Programar capacitaciones para el período seleccionado.")
        if disc_total:
            acciones.append(f"Dar cierre a {disc_total} reportes disciplinarios.")

        # ── Novedades destacadas ──────────────────────────────────────────────
        novedades = []
        if inc_criticos:
            novedades.append({"label": "Incidentes críticos abiertos", "value": inc_criticos, "color": "red"})
        if sat_insatisfechos:
            novedades.append({"label": "Clientes insatisfechos", "value": sat_insatisfechos, "color": "red"})
        if len(compromisos_pend):
            vencidos = sum(1 for c in compromisos_pend if c["dias_retraso"] > 0)
            pend = sum(1 for c in compromisos_pend if c["dias_retraso"] <= 0)
            if vencidos:
                novedades.append({"label": "Compromisos vencidos", "value": vencidos, "color": "red"})
            if pend:
                novedades.append({"label": "Compromisos pendientes", "value": pend, "color": "yellow"})
        if cap_total:
            novedades.append({"label": "Capacitaciones realizadas", "value": cap_total, "color": "green"})

        return jsonify({
            "incidentes": {
                "total": inc_total,
                "criticos": inc_criticos,
                "altos": inc_altos,
                "medios": inc_medios,
                "bajos": inc_bajos,
                "criticos_abiertos": inc_criticos_abiertos,
                "abiertos": inc_abiertos,
                "abiertos_total": inc_abiertos_total,
                "mas_24h": inc_mas_24h,
            },
            "satisfaccion": {
                "pct": sat_pct,
                "avg_nps": round(sat_avg, 1),
                "total": sat_total,
                "recomienda": sat_rec,
                "insatisfechos": sat_insatisfechos,
            },
            "supervisiones": {
                "total": sup_total,
                "hoy": sup_hoy,
                "excelente": sup_excelente,
                "seguimiento": sup_seguimiento,
                "critico": sup_critico,
            },
            "capacitaciones": {
                "total": cap_total,
                "asistentes": cap_asist,
            },
            "disciplina": {
                "total": disc_total,
            },
            "visitas": {
                "total": vis_total,
                "cumplidos": vis_cumplidos,
                "pendientes": vis_pendientes,
                "vencidos": vis_vencidos,
            },
            "ranking_satisfaccion": ranking,
            "novedades": novedades,
            "compromisos_pendientes": compromisos_pend,
            "acciones": acciones,
            "tendencia": {
                "labels": all_labels,
                "incidentes": [inc_trend.get(l, 0) for l in all_labels],
                "supervisiones": [sup_trend.get(l, 0) for l in all_labels],
                "capacitaciones": [cap_trend.get(l, 0) for l in all_labels],
                "satisfaccion": [sat_trend.get(l) for l in all_labels],
            },
            "ultima_actualizacion": date.today().isoformat(),
        })
    except Exception as e:
        app_logger.error(f"cgeo_api_operacion_data error: {e}", exc_info=True)
        return jsonify({"error": "Error interno"}), 500
    finally:
        conn.close()


# ── Morning Briefing PDF ─────────────────────────────────────────────────────

def _briefing_logo_data_url():
    try:
        path = os.path.join(os.path.dirname(__file__), 'static', 'logo_full.png')
        with open(path, 'rb') as f:
            return f"data:image/png;base64,{base64.b64encode(f.read()).decode()}"
    except Exception:
        return None


def _build_briefing_html(payload: dict) -> str:
    kpis       = payload.get('kpis') or {}
    alertas    = payload.get('alertas') or []
    semaforo   = payload.get('semaforo') or {}
    tendencia  = payload.get('tendencia') or {}
    chart_img  = payload.get('chart_image')   # base64 data URL from canvas
    cliente    = payload.get('cliente') or 'Todos los clientes'
    generated  = datetime.now().strftime('%d/%m/%Y %H:%M')

    nivel      = semaforo.get('nivel', 'verde')
    condiciones = semaforo.get('condiciones') or []

    nivel_color = {'verde': '#16a34a', 'amarillo': '#d97706', 'rojo': '#dc2626'}.get(nivel, '#64748b')
    nivel_label = {'verde': 'OPERACIÓN NORMAL', 'amarillo': 'ATENCIÓN REQUERIDA', 'rojo': 'ALERTA CRÍTICA'}.get(nivel, nivel.upper())
    nivel_emoji = {'verde': '🟢', 'amarillo': '🟡', 'rojo': '🔴'}.get(nivel, '⚪')

    logo_html = ''
    logo_url = _briefing_logo_data_url()
    if logo_url:
        logo_html = f'<img src="{logo_url}" style="height:36px;object-fit:contain" alt="SEKapp">'

    # KPI rows
    inc_ab  = kpis.get('inc_abiertos', 0)
    inc_cr  = kpis.get('inc_criticos', 0)
    sup_c   = kpis.get('sup_completadas', 0)
    sup_p   = kpis.get('sup_programadas', 0)
    sup_pct = round(sup_c / sup_p * 100) if sup_p else 0
    eq_op   = kpis.get('eq_op', 0)
    eq_tot  = kpis.get('eq_total', 0)
    eq_pct  = kpis.get('eq_pct', 0)
    cert    = kpis.get('cert_proximas', 0)

    kpi_rows = f"""
    <tr>
      <td class="kpi-name">Incidentes abiertos</td>
      <td class="kpi-val" style="color:{'#dc2626' if inc_ab > 0 else '#16a34a'}">{inc_ab}</td>
      <td class="kpi-sub">{inc_cr} crítico{'s' if inc_cr != 1 else ''}</td>
    </tr>
    <tr>
      <td class="kpi-name">Supervisiones del día</td>
      <td class="kpi-val" style="color:{'#16a34a' if sup_pct >= 80 else '#d97706' if sup_pct >= 50 else '#dc2626'}">{sup_c}/{sup_p}</td>
      <td class="kpi-sub">{sup_pct}% completado</td>
    </tr>
    <tr>
      <td class="kpi-name">Equipos operativos</td>
      <td class="kpi-val" style="color:{'#16a34a' if eq_pct >= 85 else '#d97706'}">{eq_op}/{eq_tot}</td>
      <td class="kpi-sub">{eq_pct}% operativo</td>
    </tr>
    <tr>
      <td class="kpi-name">Certificaciones próximas a vencer</td>
      <td class="kpi-val" style="color:{'#d97706' if cert > 0 else '#16a34a'}">{cert}</td>
      <td class="kpi-sub">próximos 30 días</td>
    </tr>
    """

    # Alert rows (max 5)
    rojas    = [a for a in alertas if a.get('color_semaforo') == 'rojo']
    amarillas = [a for a in alertas if a.get('color_semaforo') == 'amarillo']
    visible  = (rojas + amarillas)[:5]
    ocultas  = max(0, len(alertas) - 5)

    alerta_rows = ''
    if not visible:
        alerta_rows = '<tr><td colspan="2" style="color:#16a34a;padding:.5rem 0">✅ Sin alertas activas — operación en orden</td></tr>'
    else:
        for a in visible:
            dot_color = '#dc2626' if a.get('color_semaforo') == 'rojo' else '#d97706'
            alerta_rows += f"""
            <tr>
              <td style="width:12px;padding-right:.5rem">
                <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{dot_color}"></span>
              </td>
              <td style="font-size:.82rem;color:#334155;padding:.25rem 0">{a.get('texto','')}</td>
            </tr>"""
        if ocultas > 0:
            alerta_rows += f'<tr><td></td><td style="font-size:.78rem;color:#64748b">+{ocultas} alerta{"s" if ocultas != 1 else ""} adicional{"es" if ocultas != 1 else ""}</td></tr>'

    # Tendencia section
    chart_section = ''
    if chart_img and chart_img.startswith('data:image'):
        chart_section = f'''
        <div class="section-title">Tendencia — Últimos 7 días</div>
        <img src="{chart_img}" style="width:100%;max-height:200px;object-fit:contain;border-radius:8px;border:1px solid #e2e8f0" alt="Tendencia">
        '''
    elif tendencia.get('labels'):
        labels = tendencia['labels']
        sups   = tendencia.get('supervisiones', [0]*len(labels))
        progs  = tendencia.get('programadas', [0]*len(labels))
        rows = ''.join(
            f'<tr><td style="padding:.3rem .5rem;font-size:.78rem;color:#64748b">{l}</td>'
            f'<td style="text-align:center;padding:.3rem .5rem;font-size:.78rem">{sups[i] if i < len(sups) else 0}</td>'
            f'<td style="text-align:center;padding:.3rem .5rem;font-size:.78rem">{progs[i] if i < len(progs) else 0}</td></tr>'
            for i, l in enumerate(labels)
        )
        chart_section = f'''
        <div class="section-title">Tendencia — Últimos 7 días</div>
        <table style="width:100%;border-collapse:collapse">
          <tr>
            <th style="text-align:left;padding:.3rem .5rem;font-size:.78rem;color:#94a3b8;border-bottom:1px solid #e2e8f0">Fecha</th>
            <th style="text-align:center;padding:.3rem .5rem;font-size:.78rem;color:#94a3b8;border-bottom:1px solid #e2e8f0">Supervisiones completadas</th>
            <th style="text-align:center;padding:.3rem .5rem;font-size:.78rem;color:#94a3b8;border-bottom:1px solid #e2e8f0">Programadas</th>
          </tr>
          {rows}
        </table>'''

    cond_text = ' · '.join(c.get('texto', '') for c in condiciones) if condiciones else 'Sin condiciones de alerta'

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: 'Helvetica Neue', Arial, sans-serif; font-size:.875rem; color:#1e293b; background:#fff; padding:2rem; }}
  .header {{ display:flex; justify-content:space-between; align-items:center; border-bottom:2px solid #1e293b; padding-bottom:1rem; margin-bottom:1.5rem; }}
  .header-right {{ text-align:right; font-size:.78rem; color:#64748b; }}
  .semaforo-bar {{ background:{nivel_color}18; border:1.5px solid {nivel_color}; border-radius:8px; padding:.75rem 1rem; margin-bottom:1.5rem; display:flex; align-items:center; gap:.75rem; }}
  .semaforo-nivel {{ font-size:1rem; font-weight:700; color:{nivel_color}; }}
  .semaforo-cond {{ font-size:.8rem; color:#475569; }}
  .section-title {{ font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.08em; color:#94a3b8; margin:1.25rem 0 .6rem; border-bottom:1px solid #e2e8f0; padding-bottom:.3rem; }}
  table.kpi-table {{ width:100%; border-collapse:collapse; }}
  .kpi-name {{ font-size:.82rem; color:#475569; padding:.35rem 0; }}
  .kpi-val {{ font-size:1.1rem; font-weight:700; padding:.35rem .75rem; white-space:nowrap; }}
  .kpi-sub {{ font-size:.75rem; color:#94a3b8; padding:.35rem 0; }}
  .footer {{ margin-top:2rem; padding-top:.75rem; border-top:1px solid #e2e8f0; font-size:.72rem; color:#94a3b8; text-align:center; }}
  @page {{ margin:1.5cm; }}
</style>
</head>
<body>
  <div class="header">
    <div>{logo_html}</div>
    <div class="header-right">
      Morning Briefing Ejecutivo<br>
      <strong>{cliente}</strong><br>
      {generated}
    </div>
  </div>

  <div class="semaforo-bar">
    <span style="font-size:1.4rem">{nivel_emoji}</span>
    <div>
      <div class="semaforo-nivel">{nivel_label}</div>
      <div class="semaforo-cond">{cond_text}</div>
    </div>
  </div>

  <div class="section-title">KPIs del día</div>
  <table class="kpi-table">
    {kpi_rows}
  </table>

  <div class="section-title">Alertas activas</div>
  <table style="width:100%;border-collapse:collapse">
    {alerta_rows}
  </table>

  {chart_section}

  <div class="footer">Generado automáticamente por SEKapp — CONFIDENCIAL · {generated}</div>
</body>
</html>"""


@cgeo_bp.route('/api/morning-briefing-pdf', methods=['POST'])
@jwt_required()
def cgeo_morning_briefing_pdf():
    if not _WEASYPRINT_AVAILABLE:
        return jsonify({"error": "PDF generation not available in this environment"}), 503
    payload = request.get_json() or {}
    try:
        html = _build_briefing_html(payload)
        buf = BytesIO()
        _WeasyprintHTML(string=html).write_pdf(buf)
        buf.seek(0)
        filename = f"briefing_{date.today().isoformat()}.pdf"
        return send_file(buf, as_attachment=True, download_name=filename, mimetype='application/pdf')
    except Exception as e:
        app_logger.error(f"cgeo_morning_briefing_pdf error: {e}", exc_info=True)
        return jsonify({"error": "Error generando PDF"}), 500


def _fetch_record_details(form_type: str, record_id: int) -> dict:
    """Return a dict of display fields for the given form_type record, or {} on failure."""
    conn = _get_conn()
    if not conn:
        return {}
    try:
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            if form_type == 'reporte_incidente':
                cur.execute(
                    """
                    SELECT cliente_instalacion  AS cliente,
                           puesto_area_especifica AS ubicacion,
                           fecha_hora,
                           categoria,
                           tipo_incidente        AS subtipo,
                           descripcion_incidente AS descripcion,
                           estado,
                           responsable_asignado
                      FROM reportes_incidentes
                     WHERE id_reporte_incidente = %s
                    """,
                    (record_id,)
                )
                row = cur.fetchone()
                if not row:
                    return {}
                return {
                    'tipo_label': 'Incidente',
                    'consecutivo': f"#{record_id}",
                    'cliente': row['cliente'] or '—',
                    'fecha_evento': row['fecha_hora'],
                    'categoria': row['categoria'] or '—',
                    'subtipo': row['subtipo'] or '',
                    'descripcion': row['descripcion'] or '—',
                    'ubicacion': row['ubicacion'] or '—',
                    'estado': row['estado'] or '—',
                    'url_path': f"/dashboard/incidentes/?id={record_id}",
                }

            elif form_type == 'supervision_puesto':
                cur.execute(
                    """
                    SELECT supervisor,
                           nombre_guardia,
                           fecha_hora,
                           observaciones_novedades AS descripcion,
                           submitted_by_email
                      FROM supervision_puesto
                     WHERE id_supervision = %s
                    """,
                    (record_id,)
                )
                row = cur.fetchone()
                if not row:
                    return {}
                return {
                    'tipo_label': 'Supervisión de puesto',
                    'consecutivo': f"#{record_id}",
                    'cliente': row['supervisor'] or '—',
                    'fecha_evento': row['fecha_hora'],
                    'categoria': '—',
                    'subtipo': '',
                    'descripcion': row['descripcion'] or '—',
                    'ubicacion': '—',
                    'estado': '—',
                    'url_path': f"/dashboard/supervision/?id={record_id}",
                }

            elif form_type in ('visita', 'registro_y_acta_de_visita'):
                cur.execute(
                    """
                    SELECT cliente_instalacion  AS cliente,
                           puesto_area_especifica AS ubicacion,
                           fecha_hora,
                           motivo_visita         AS categoria,
                           actividades_realizadas AS descripcion,
                           visita_realizada_por
                      FROM registro_y_acta_de_visita
                     WHERE id_visita = %s
                    """,
                    (record_id,)
                )
                row = cur.fetchone()
                if not row:
                    return {}
                return {
                    'tipo_label': 'Visita',
                    'consecutivo': f"#{record_id}",
                    'cliente': row['cliente'] or '—',
                    'fecha_evento': row['fecha_hora'],
                    'categoria': row['categoria'] or '—',
                    'subtipo': '',
                    'descripcion': row['descripcion'] or '—',
                    'ubicacion': row['ubicacion'] or '—',
                    'estado': '—',
                    'url_path': f"/dashboard/visitas/?id={record_id}",
                }

            elif form_type in ('equipo', 'confiabilidad_equipos'):
                cur.execute(
                    """
                    SELECT cliente_instalacion AS cliente,
                           sitio              AS ubicacion,
                           fecha,
                           tecnico_mantenimiento
                      FROM confiabilidad_equipos
                     WHERE id = %s
                    """,
                    (record_id,)
                )
                row = cur.fetchone()
                if not row:
                    return {}
                return {
                    'tipo_label': 'Equipo',
                    'consecutivo': f"#{record_id}",
                    'cliente': row['cliente'] or '—',
                    'fecha_evento': row['fecha'],
                    'categoria': '—',
                    'subtipo': '',
                    'descripcion': f"Técnico: {row['tecnico_mantenimiento'] or '—'}",
                    'ubicacion': row['ubicacion'] or '—',
                    'estado': '—',
                    'url_path': f"/dashboard/equipos/?id={record_id}",
                }

            else:
                return {
                    'tipo_label': form_type.replace('_', ' ').title(),
                    'consecutivo': f"#{record_id}",
                    'url_path': '/dashboard/',
                }
    except Exception as e:
        app_logger.warning(f"_fetch_record_details({form_type}, {record_id}): {e}")
        return {}
    finally:
        conn.close()


def _format_fecha(value) -> str:
    """Format a date/datetime value for display in email."""
    if value is None:
        return '—'
    try:
        from datetime import datetime, date
        if isinstance(value, datetime):
            return value.strftime('%d/%m/%Y %H:%M')
        if isinstance(value, date):
            return value.strftime('%d/%m/%Y')
        return str(value)
    except Exception:
        return str(value)


def _send_hallazgo_assignment_email(*, assignee, asignado_por, form_type, record_id,
                                    fecha_limite, nota):
    """Build and send the enriched assignment notification email."""
    details = _fetch_record_details(form_type, record_id)

    tipo_label  = details.get('tipo_label', form_type.replace('_', ' ').title())
    consecutivo = details.get('consecutivo', f"#{record_id}")
    subject = f"[SEKApp] Hallazgo asignado – {tipo_label} {consecutivo}"
    if details.get('categoria') and details['categoria'] != '—':
        subject += f" – {details['categoria']}"

    # Resolve assigner display name
    try:
        conn2 = _get_conn()
        assigner_name = asignado_por
        if conn2:
            with conn2.cursor(cursor_factory=extras.RealDictCursor) as cur2:
                cur2.execute("SELECT name FROM users WHERE email = %s", (asignado_por,))
                row2 = cur2.fetchone()
                if row2:
                    assigner_name = row2['name']
            conn2.close()
    except Exception:
        assigner_name = asignado_por

    fecha_limite_str = _format_fecha(fecha_limite) if fecha_limite else '—'
    fecha_evento_str = _format_fecha(details.get('fecha_evento')) if details.get('fecha_evento') else '—'

    # Build record URL (absolute when possible via url_for, else relative)
    try:
        from flask import url_for as _url_for
        base_url = _url_for('cgeo_bp.cgeo_morning_briefing', _external=True).rsplit('/morning', 1)[0]
        record_url = base_url + details.get('url_path', '/dashboard/')
    except Exception:
        record_url = details.get('url_path', '/dashboard/')

    def row(label, value):
        if not value or value == '—':
            return ''
        return (
            f'<tr>'
            f'<td style="padding:6px 12px 6px 0;color:#6b7280;font-size:13px;white-space:nowrap;'
            f'vertical-align:top;">{escape(label)}</td>'
            f'<td style="padding:6px 0;font-size:13px;color:#111827;">{escape(str(value))}</td>'
            f'</tr>'
        )

    rows = ''.join([
        row('Tipo de registro', tipo_label),
        row('Consecutivo', consecutivo),
        row('Cliente / Instalación', details.get('cliente', '—')),
        row('Fecha del evento', fecha_evento_str),
        row('Categoría', details.get('categoria', '')),
        row('Descripción', details.get('descripcion', '')),
        row('Ubicación', details.get('ubicacion', '')),
        row('Estado actual', details.get('estado', '')),
        row('Responsable asignado', assignee['name']),
        row('Fecha límite', fecha_limite_str),
        row('Asignado por', assigner_name),
    ])

    note_block = ''
    if nota:
        note_block = (
            f'<p style="margin:16px 0 4px;font-weight:600;font-size:13px;color:#374151;">'
            f'Observaciones del asignador:</p>'
            f'<p style="margin:0;padding:10px 14px;background:#f9fafb;border-left:3px solid #d1d5db;'
            f'font-size:13px;color:#374151;">{escape(nota)}</p>'
        )

    body = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#111827;">
      <div style="background:#1e3a5f;padding:20px 24px;border-radius:6px 6px 0 0;">
        <p style="margin:0;font-size:18px;font-weight:700;color:#ffffff;">SEKApp</p>
        <p style="margin:4px 0 0;font-size:13px;color:#93c5fd;">Notificación de hallazgo asignado</p>
      </div>
      <div style="background:#ffffff;padding:24px;border:1px solid #e5e7eb;border-top:none;
                  border-radius:0 0 6px 6px;">
        <p style="margin:0 0 16px;font-size:15px;">
          Hola <strong>{escape(assignee['name'])}</strong>,
        </p>
        <p style="margin:0 0 20px;font-size:14px;color:#374151;">
          Se te ha asignado un hallazgo que requiere seguimiento:
        </p>
        <table style="border-collapse:collapse;width:100%;margin-bottom:16px;">
          {rows}
        </table>
        {note_block}
        <div style="margin-top:28px;text-align:center;">
          <a href="{record_url}"
             style="display:inline-block;padding:11px 28px;background:#1e3a5f;color:#ffffff;
                    text-decoration:none;border-radius:5px;font-size:14px;font-weight:600;">
            Ver hallazgo
          </a>
        </div>
        <p style="margin:24px 0 0;font-size:11px;color:#9ca3af;text-align:center;">
          Kanan · SEKApp — este correo fue generado automáticamente.
        </p>
      </div>
    </div>
    """

    send_email(
        to_emails=assignee['email'],
        subject=subject,
        body=body,
        is_html=True,
    )


def _ensure_asignaciones_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS asignaciones_hallazgo (
                id            SERIAL PRIMARY KEY,
                form_type     TEXT        NOT NULL,
                record_id     INTEGER     NOT NULL,
                asignado_a    INTEGER     REFERENCES users(id),
                asignado_por  TEXT,
                fecha_limite  DATE,
                nota          TEXT,
                estado        TEXT        NOT NULL DEFAULT 'Asignado',
                company_id    INTEGER,
                creado_en     TIMESTAMP   NOT NULL DEFAULT NOW()
            )
        """)
    conn.commit()


@cgeo_bp.route('/api/usuarios-asignables', methods=['GET'])
@jwt_required()
def usuarios_asignables():
    conn = _get_conn()
    if not conn:
        return jsonify({"error": "DB no disponible"}), 500
    try:
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            company_id = _get_user_company_id(cur, get_jwt_identity())
            if company_id is not None:
                cur.execute(
                    "SELECT id, name, email FROM users "
                    "WHERE company_id = %s AND is_active = TRUE ORDER BY name",
                    (company_id,)
                )
            else:
                cur.execute(
                    "SELECT id, name, email FROM users "
                    "WHERE is_active = TRUE ORDER BY name"
                )
            usuarios = cur.fetchall()
            return jsonify({"usuarios": [dict(u) for u in usuarios]})
    except Exception as e:
        app_logger.error(f"usuarios_asignables error: {e}", exc_info=True)
        return jsonify({"error": "Error interno"}), 500
    finally:
        conn.close()


@cgeo_bp.route('/api/asignar-hallazgo', methods=['POST'])
@jwt_required()
def asignar_hallazgo():
    payload = request.get_json() or {}
    form_type   = payload.get('form_type')
    record_id   = payload.get('record_id')
    asignado_a  = payload.get('asignado_a')   # user id (int)
    fecha_limite = payload.get('fecha_limite') # ISO date string or None
    nota        = payload.get('nota', '')

    if not all([form_type, record_id, asignado_a]):
        return jsonify({"error": "Faltan campos requeridos: form_type, record_id, asignado_a"}), 400

    asignado_por = get_jwt_identity()
    conn = _get_conn()
    if not conn:
        return jsonify({"error": "DB no disponible"}), 500
    try:
        _ensure_asignaciones_table(conn)
        with conn.cursor(cursor_factory=extras.RealDictCursor) as cur:
            company_id = _get_user_company_id(cur, asignado_por)

            # Fetch assignee name and email for response and notification
            cur.execute("SELECT name, email FROM users WHERE id = %s", (asignado_a,))
            assignee = cur.fetchone()
            if not assignee:
                return jsonify({"error": "Usuario asignado no encontrado"}), 404

            # Insert assignment record
            cur.execute(
                """
                INSERT INTO asignaciones_hallazgo
                    (form_type, record_id, asignado_a, asignado_por, fecha_limite, nota, estado, company_id)
                VALUES (%s, %s, %s, %s, %s, %s, 'Asignado', %s)
                RETURNING id
                """,
                (form_type, record_id, asignado_a, asignado_por,
                 fecha_limite or None, nota or None, company_id)
            )
            assignment_id = cur.fetchone()['id']

            # Keep reportes_incidentes in sync for incident reports
            if form_type == 'reporte_incidente':
                cur.execute(
                    """
                    UPDATE reportes_incidentes
                       SET responsable_asignado = %s, estado = 'Asignado'
                     WHERE id_reporte_incidente = %s
                    """,
                    (assignee['name'], record_id)
                )

        conn.commit()

        # Notify assignee by email (non-blocking — failure does not abort the response)
        try:
            _send_hallazgo_assignment_email(
                assignee=assignee,
                asignado_por=asignado_por,
                form_type=form_type,
                record_id=record_id,
                fecha_limite=fecha_limite,
                nota=nota,
            )
        except Exception as mail_err:
            app_logger.warning(f"asignar_hallazgo: email notification failed: {mail_err}")

        return jsonify({
            "success": True,
            "assignment_id": assignment_id,
            "estado": "Asignado",
            "responsable": assignee['name'],
            "responsable_email": assignee['email'],
        })

    except Exception as e:
        conn.rollback()
        app_logger.error(f"asignar_hallazgo error: {e}", exc_info=True)
        return jsonify({"error": "Error interno"}), 500
    finally:
        conn.close()
