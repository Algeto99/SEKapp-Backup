"""
Centro de Gestión Ejecutiva y Operativa (CGEO)
Blueprint with two sub-modules:
  - Gestión de Recursos y Confiabilidad  (/cgeo/recursos/)
  - Gestión de Operación y Novedades     (/cgeo/operacion/)
Each sub-module exposes an Informe Ejecutivo and a Resumen Operativo tab.
"""

import logging
import os
from datetime import date, timedelta
from functools import wraps

import psycopg2
from psycopg2 import extras
from flask import Blueprint, render_template, jsonify, request, redirect
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt

from db import get_db_connection

cgeo_bp = Blueprint("cgeo_bp", __name__)
app_logger = logging.getLogger(__name__)


def _get_conn():
    return get_db_connection()


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
        try:
            claims = get_jwt()
            if not claims.get("is_admin", False):
                if request.path.startswith("/cgeo/api/"):
                    return jsonify({"error": "Acceso denegado"}), 403
                return redirect("/landing/")
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return f(*args, **kwargs)
    return decorated


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
        f"CASE WHEN {col} IS NOT NULL AND {col} NOT IN ('','[]','null') "
        f"THEN json_array_length({col}::json) ELSE 0 END"
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


# ── API: shared filter options ────────────────────────────────────────────────

@cgeo_bp.route("/api/filtros")
@jwt_required()
def cgeo_api_filtros():
    conn = _get_conn()
    if not conn:
        return jsonify({"error": "DB no disponible"}), 500
    try:
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)
        clientes = set()
        for tbl, col in [
            ("confiabilidad_equipos", "cliente_instalacion"),
            ("planilla_vehicular",    "cliente_instalacion"),
            ("checklist_cumplimiento", "cliente_instalacion"),
            ("reportes_incidentes",   "cliente_instalacion"),
            ("supervision_puesto",    "cliente_instalacion"),
        ]:
            try:
                cur.execute(f"SELECT DISTINCT TRIM({col}) AS c FROM {tbl} WHERE {col} IS NOT NULL AND TRIM({col}) <> '' ORDER BY c")
                clientes.update(r["c"] for r in cur.fetchall())
            except Exception:
                pass
        return jsonify({"clientes": sorted(clientes)})
    except Exception as e:
        app_logger.error(f"cgeo_api_filtros error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ── API: Recursos y Confiabilidad ─────────────────────────────────────────────

@cgeo_bp.route("/api/recursos-data")
@jwt_required()
@_admin_required
def cgeo_api_recursos_data():
    cliente = request.args.get("cliente") or None
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
        if cliente:
            eq_conds.append("c.cliente_instalacion = %s")
            eq_params.append(cliente)
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
        """, eq_params)
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
        """, eq_params)
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
        """, eq_params)
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
        if cliente:
            veh_conds.append("cliente_instalacion = %s")
            veh_params.append(cliente)
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
        """, veh_params)
        veh_row = cur.fetchone() or {}
        veh_total = int(veh_row.get("total") or 0)
        veh_aptos = int(veh_row.get("aptos") or 0)
        veh_no_aptos = int(veh_row.get("no_aptos") or 0)
        veh_mant = veh_total - veh_aptos - veh_no_aptos
        veh_pct = round(veh_aptos / veh_total * 100, 1) if veh_total else None

        # ── Certificaciones / Cumplimiento ────────────────────────────────────
        cum_conds, cum_params = [], []
        if cliente:
            cum_conds.append("cliente_instalacion = %s")
            cum_params.append(cliente)
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
        """, cum_params)
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
        """, cum_params)
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
        """, veh_params)
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
        """, eq_params)
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
        return jsonify({"error": str(e)}), 500
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
    cliente = request.args.get("cliente") or None

    conn = _get_conn()
    if not conn:
        return jsonify({"error": "DB no disponible"}), 500

    try:
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)
        alertas = []

        def _cp(col="cliente_instalacion"):
            """Condición de filtro por cliente y sus parámetros."""
            return ([f"{col} = %s"], [cliente]) if cliente else ([], [])

        # ── REGLA 1: Puesto sin supervisión > 48 h ────────────────────────────
        r1_conds, r1_params = _cp("cliente")
        cur.execute(f"""
            SELECT
                TRIM(cliente) AS puesto,
                MAX(fecha_hora) AS ultima_sup,
                EXTRACT(EPOCH FROM (NOW() - MAX(fecha_hora))) / 3600 AS horas
            FROM supervision_puesto
            {_where(r1_conds)}
            GROUP BY TRIM(cliente)
            HAVING MAX(fecha_hora) < NOW() - INTERVAL '48 hours'
            ORDER BY MAX(fecha_hora) ASC
            LIMIT 5
        """, r1_params)
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
                "accion": "Ver puesto",
                "ruta_navegacion": f"/cgeo/operacion/?cliente={r['puesto']}",
                "color_semaforo": "rojo",
                "timestamp": r["ultima_sup"].isoformat() if r["ultima_sup"] else None,
                "horas": round(h, 1),
            })

        # ── REGLA 2: Incidente abierto sin gestión > 24 h ────────────────────
        r2_conds, r2_params = _cp()
        r2_conds += [
            "LOWER(TRIM(COALESCE(estado,''))) NOT IN ('cerrado','closed','resuelto','resolved')",
            "COALESCE(fecha_hora, creado_en) < NOW() - INTERVAL '24 hours'",
        ]
        cur.execute(f"""
            SELECT
                id,
                COALESCE(NULLIF(TRIM(tipo_incidente),''), 'Incidente') AS tipo,
                EXTRACT(EPOCH FROM (NOW() - COALESCE(fecha_hora, creado_en))) / 3600 AS horas,
                COALESCE(fecha_hora, creado_en) AS ts
            FROM reportes_incidentes
            {_where(r2_conds)}
            ORDER BY COALESCE(fecha_hora, creado_en) ASC
            LIMIT 5
        """, r2_params)
        for r in cur.fetchall():
            h = float(r["horas"] or 0)
            alertas.append({
                "id": f"r2_{r['id']}",
                "regla": 2,
                "texto": f"Incidente #{r['id']} abierto hace {int(h)}h — {r['tipo']}",
                "accion": "Ver incidente",
                "ruta_navegacion": f"/dashboard/incidentes/?id={r['id']}",
                "color_semaforo": "rojo",
                "timestamp": r["ts"].isoformat() if r["ts"] else None,
                "horas": round(h, 1),
            })

        # ── REGLA 3: Cliente con historial reciente pero sin supervisión hoy ──
        # Proxy: clientes supervisados en los últimos 7 días pero NO hoy.
        r3_conds, r3_params = _cp("cliente")
        r3_conds_hist = r3_conds + ["fecha_hora >= NOW() - INTERVAL '7 days'"]
        r3_conds_hoy  = r3_conds + ["fecha_hora::date = CURRENT_DATE"]
        cur.execute(f"""
            SELECT
                TRIM(cliente) AS puesto,
                MAX(fecha_hora) AS ultima_sup
            FROM supervision_puesto
            {_where(r3_conds_hist)}
            GROUP BY TRIM(cliente)
            HAVING TRIM(cliente) NOT IN (
                SELECT TRIM(cliente)
                FROM supervision_puesto
                {_where(r3_conds_hoy)}
            )
            ORDER BY MAX(fecha_hora) ASC
            LIMIT 5
        """, r3_params + r3_params)
        for r in cur.fetchall():
            alertas.append({
                "id": f"r3_{r['puesto']}",
                "regla": 3,
                "texto": f"Puesto \"{r['puesto']}\" sin supervisión registrada hoy",
                "accion": "Ir a supervisión",
                "ruta_navegacion": f"/forms/supervision_puesto?cliente={r['puesto']}",
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
        """, r4_params)
        for r in cur.fetchall():
            d = int(r["dias_restantes"] or 0)
            alertas.append({
                "id": f"r4_{r['id']}",
                "regla": 4,
                "texto": f"Certificación \"{r['cert']}\" en {r['cliente']} vence en {d} días",
                "accion": "Ver certificación",
                "ruta_navegacion": f"/dashboard/cumplimiento/?id={r['id']}",
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
        """, r5_params)
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
        """, r6_params)
        for r in cur.fetchall():
            h = float(r["horas"] or 0)
            alertas.append({
                "id": f"r6_{r['placa']}",
                "regla": 6,
                "texto": f"Vehículo {r['placa']} ({r['cliente']}) sin pre-operacional hace {int(h)}h",
                "accion": "Registrar pre-op",
                "ruta_navegacion": f"/forms/planilla_vehicular?cliente={r['cliente']}",
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
        """, r7_params)
        for r in cur.fetchall():
            d = int(r["dias_vencido"] or 0)
            alertas.append({
                "id": f"r7_{r['id']}",
                "regla": 7,
                "texto": f"Checklist \"{r['nombre']}\" en {r['instalacion']} vencido hace {d} días",
                "accion": "Renovar checklist",
                "ruta_navegacion": f"/dashboard/cumplimiento/?id={r['id']}",
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
                ROUND(AVG(NULLIF(calificacion_global_nps::TEXT,'')::NUMERIC), 2) AS avg_raw,
                ROUND(AVG(NULLIF(calificacion_global_nps::TEXT,'')::NUMERIC) / 40 * 5, 2) AS avg_5,
                COUNT(*) AS encuestas,
                MAX(fecha_hora) AS ultima
            FROM medicion_experiencia_cliente
            {_where(r8_conds)}
            GROUP BY TRIM(cliente_instalacion)
            HAVING COUNT(*) > 0
               AND AVG(NULLIF(calificacion_global_nps::TEXT,'')::NUMERIC) < 24
            ORDER BY AVG(NULLIF(calificacion_global_nps::TEXT,'')::NUMERIC) ASC
            LIMIT 5
        """, r8_params)
        for r in cur.fetchall():
            score = float(r["avg_5"] or 0)
            alertas.append({
                "id": f"r8_{r['cliente']}",
                "regla": 8,
                "texto": f"Satisfacción baja en \"{r['cliente']}\": {score}/5 promedio ({r['encuestas']} encuestas)",
                "accion": "Ver encuestas",
                "ruta_navegacion": f"/dashboard/satisfaccion/?cliente={r['cliente']}",
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
        return jsonify({"error": str(e)}), 500
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
    cliente = request.args.get("cliente") or None

    conn = _get_conn()
    if not conn:
        return jsonify({"error": "DB no disponible"}), 500
    try:
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)

        def _cp(col="cliente_instalacion"):
            return ([f"{col} = %s"], [cliente]) if cliente else ([], [])

        # Incidentes abiertos
        inc_conds, inc_params = _cp()
        inc_conds += [
            "LOWER(TRIM(COALESCE(estado,''))) NOT IN ('cerrado','closed','resuelto','resolved')"
        ]
        cur.execute(f"""
            SELECT COUNT(*) AS total FROM reportes_incidentes {_where(inc_conds)}
        """, inc_params)
        inc_abiertos = int((cur.fetchone() or {}).get("total") or 0)

        # Supervisiones: programadas hoy vs completadas hoy
        # Usamos puestos activos (supervisados en los últimos 30 días) como proxy de programadas
        sup_conds_hist, sup_params_hist = _cp("cliente")
        sup_conds_hist_full = sup_conds_hist + ["fecha_hora >= NOW() - INTERVAL '30 days'"]
        cur.execute(f"""
            SELECT COUNT(DISTINCT TRIM(cliente)) AS programadas
            FROM supervision_puesto {_where(sup_conds_hist_full)}
        """, sup_params_hist)
        sup_programadas = int((cur.fetchone() or {}).get("programadas") or 0)

        sup_conds_hoy, sup_params_hoy = _cp("cliente")
        sup_conds_hoy_full = sup_conds_hoy + ["fecha_hora::date = CURRENT_DATE"]
        cur.execute(f"""
            SELECT COUNT(DISTINCT TRIM(cliente)) AS completadas
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
                 LATERAL jsonb_array_elements(c.inventario) AS elem
            {_where(eq_conds)}
        """, eq_params)
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
        """, cert_params)
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
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ── API: Operación y Novedades ────────────────────────────────────────────────

@cgeo_bp.route("/api/operacion-data")
@jwt_required()
@_admin_required
def cgeo_api_operacion_data():
    cliente = request.args.get("cliente") or None
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
        if cliente:
            inc_conds.append("cliente_instalacion = %s")
            inc_params.append(cliente)
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
        """, inc_params)
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
        """, inc_params)
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
        """, inc_params)
        inc_ab_row = cur.fetchone() or {}
        inc_abiertos_total = int(inc_ab_row.get("total_abiertos") or 0)
        inc_mas_24h = int(inc_ab_row.get("mas_24h") or 0)

        # Tendencia mensual incidentes
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', COALESCE(fecha_hora, creado_en)), 'YYYY-MM') AS label,
                COUNT(*) AS total
            FROM reportes_incidentes
            {inc_where}
            GROUP BY DATE_TRUNC('month', COALESCE(fecha_hora, creado_en))
            ORDER BY DATE_TRUNC('month', COALESCE(fecha_hora, creado_en))
            LIMIT 8
        """, inc_params)
        inc_trend = {r["label"]: int(r["total"]) for r in cur.fetchall()}

        # ── Satisfacción ──────────────────────────────────────────────────────
        sat_conds, sat_params = [], []
        if cliente:
            sat_conds.append("cliente_instalacion = %s")
            sat_params.append(cliente)
        _date_conds("fecha_hora", sat_conds, sat_params)
        sat_where = _where(sat_conds)
        cur.execute(f"""
            SELECT
                AVG(NULLIF(calificacion_global_nps::TEXT,'')::NUMERIC) AS avg_nps,
                COUNT(*) AS total,
                SUM(CASE WHEN LOWER(COALESCE(recomendaria_servicio::TEXT,'')) IN ('sí','si','yes','s') THEN 1 ELSE 0 END) AS recomienda
            FROM medicion_experiencia_cliente
            {sat_where}
        """, sat_params)
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
                ROUND(AVG(NULLIF(calificacion_global_nps::TEXT,'')::NUMERIC) / 40 * 100, 1) AS pct
            FROM medicion_experiencia_cliente
            {sat_where}
            GROUP BY TRIM(cliente_instalacion)
            HAVING COUNT(*) > 0
            ORDER BY pct DESC
            LIMIT 8
        """, sat_params)
        ranking = [
            {"cliente": r["cliente"], "pct": float(r["pct"] or 0)}
            for r in cur.fetchall()
        ]

        # Tendencia mensual satisfacción
        cur.execute(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('month', fecha_hora), 'YYYY-MM') AS label,
                ROUND(AVG(NULLIF(calificacion_global_nps::TEXT,'')::NUMERIC) / 40 * 100, 1) AS pct
            FROM medicion_experiencia_cliente
            {sat_where}
            GROUP BY DATE_TRUNC('month', fecha_hora)
            ORDER BY DATE_TRUNC('month', fecha_hora)
            LIMIT 8
        """, sat_params)
        sat_trend = {r["label"]: float(r["pct"] or 0) for r in cur.fetchall()}

        # ── Supervisión ───────────────────────────────────────────────────────
        sup_conds, sup_params = [], []
        if cliente:
            sup_conds.append("cliente = %s")
            sup_params.append(cliente)
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
        """, sup_params)
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
        """, sup_params)
        sup_trend = {r["label"]: int(r["total"]) for r in cur.fetchall()}

        # Supervisiones completadas hoy
        sup_hoy_conds = list(sup_conds) + ["fecha_hora::date = CURRENT_DATE"]
        cur.execute(f"""
            SELECT COUNT(*) AS hoy FROM supervision_puesto {_where(sup_hoy_conds)}
        """, sup_params)
        sup_hoy = int((cur.fetchone() or {}).get("hoy") or 0)

        # ── Capacitaciones ────────────────────────────────────────────────────
        cap_date = _capac_date()
        cap_safe = _capac_safe_len()
        cap_conds, cap_params = [], []
        if cliente:
            cap_conds.append("cliente_instalacion = %s")
            cap_params.append(cliente)
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
        """, cap_params)
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
        """, cap_params)
        cap_trend = {r["label"]: int(r["total"]) for r in cur.fetchall()}

        # ── Disciplina ────────────────────────────────────────────────────────
        disc_conds, disc_params = [], []
        if cliente:
            disc_conds.append("cliente_instalacion = %s")
            disc_params.append(cliente)
        _date_conds("fecha_hora", disc_conds, disc_params)
        disc_where = _where(disc_conds)
        cur.execute(f"""
            SELECT COUNT(*) AS total FROM informe_novedades_disciplinario {disc_where}
        """, disc_params)
        disc_row = cur.fetchone() or {}
        disc_total = int(disc_row.get("total") or 0)

        # ── Compromisos / visitas (resumen operativo) ─────────────────────────
        vis_conds, vis_params = [], []
        if cliente:
            vis_conds.append("cliente_instalacion = %s")
            vis_params.append(cliente)
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
            """, vis_params)
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
            """, vis_params)
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
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
