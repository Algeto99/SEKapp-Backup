"""
Matrices Hub — Consultar Matrices
Central hub for operational matrices: incidents, visits, supervision, discipline, etc.
"""

import calendar
import logging
import os
from datetime import date, timedelta

import psycopg2
from psycopg2 import extras

from flask import Blueprint, render_template, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt

from db import get_db_connection

matrices_bp = Blueprint("matrices_bp", __name__)
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
    return user_name, is_admin


@matrices_bp.route("/")
@jwt_required()
def matrices_hub():
    user_email = get_jwt_identity()
    user_name, is_admin = _get_user_info(user_email)
    return render_template(
        "matrices_hub.html",
        current_user=user_email,
        user_name=user_name,
        is_admin=is_admin,
    )


@matrices_bp.route("/api/stats")
@jwt_required()
def matrices_api_stats():
    conn = _get_conn()
    if not conn:
        return jsonify({"error": "DB no disponible"}), 500
    try:
        cur = conn.cursor(cursor_factory=extras.RealDictCursor)
        
        month_arg = request.args.get("month")
        if month_arg:
            try:
                year, month = map(int, month_arg.split('-'))
                selected_date = date(year, month, 1)
            except Exception:
                selected_date = date.today()
        else:
            selected_date = date.today()

        month_start = selected_date.replace(day=1)
        last_day = calendar.monthrange(selected_date.year, selected_date.month)[1]
        month_end = date(selected_date.year, selected_date.month, last_day)

        user_email = get_jwt_identity()
        company_id = _get_user_company_id(cur, user_email)
        cid_cond = "AND company_id = %s" if company_id is not None else ""
        date_end = month_end + timedelta(days=1)

        stats = {}

        # ── Incidentes abiertos ──────────────────────────────────────────────
        try:
            params = [month_start, date_end] + ([company_id] if company_id is not None else [])
            cur.execute(f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN LOWER(TRIM(COALESCE(estado,''))) NOT IN
                        ('cerrado','closed','resuelto','resolved') THEN 1 ELSE 0 END) AS abiertos,
                    SUM(CASE WHEN LOWER(TRIM(nivel_severidad)) IN ('crítico','critico') THEN 1 ELSE 0 END) AS criticos
                FROM reportes_incidentes
                WHERE COALESCE(fecha_hora AT TIME ZONE 'UTC', creado_en) >= %s
                  AND COALESCE(fecha_hora AT TIME ZONE 'UTC', creado_en) < %s
                  {cid_cond}
            """, params)
            r = cur.fetchone() or {}
            stats["incidentes"] = {
                "total": int(r.get("total") or 0),
                "abiertos": int(r.get("abiertos") or 0),
                "criticos": int(r.get("criticos") or 0),
            }
        except Exception:
            stats["incidentes"] = {"total": 0, "abiertos": 0, "criticos": 0}

        # ── Visitas / compromisos pendientes ─────────────────────────────────
        try:
            params = [month_start, date_end] + ([company_id] if company_id is not None else [])
            cur.execute(f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN LOWER(TRIM(COALESCE(estado,''))) IN
                        ('pendiente','','abierto') OR estado IS NULL THEN 1 ELSE 0 END) AS pendientes
                FROM registro_y_acta_de_visita
                WHERE COALESCE(fecha_hora, creado_en) >= %s
                  AND COALESCE(fecha_hora, creado_en) < %s
                  {cid_cond}
            """, params)
            r = cur.fetchone() or {}
            stats["visitas"] = {
                "total": int(r.get("total") or 0),
                "pendientes": int(r.get("pendientes") or 0),
            }
        except Exception:
            stats["visitas"] = {"total": 0, "pendientes": 0}

        # ── Supervisiones del mes ────────────────────────────────────────────
        try:
            params = [month_start, date_end] + ([company_id] if company_id is not None else [])
            cur.execute(f"""
                SELECT COUNT(*) AS total FROM supervision_puesto
                WHERE fecha_hora >= %s AND fecha_hora < %s {cid_cond}
            """, params)
            r = cur.fetchone() or {}
            stats["supervision"] = {"total": int(r.get("total") or 0)}
        except Exception:
            stats["supervision"] = {"total": 0}

        # ── Disciplina ───────────────────────────────────────────────────────
        try:
            params = [month_start, date_end] + ([company_id] if company_id is not None else [])
            cur.execute(f"""
                SELECT COUNT(*) AS total FROM informe_novedades_disciplinario
                WHERE fecha_hora >= %s AND fecha_hora < %s {cid_cond}
            """, params)
            r = cur.fetchone() or {}
            stats["disciplina"] = {"total": int(r.get("total") or 0)}
        except Exception:
            stats["disciplina"] = {"total": 0}

        # ── Capacitaciones del mes ───────────────────────────────────────────
        try:
            params = [month_start, date_end] + ([company_id] if company_id is not None else [])
            cur.execute(f"""
                SELECT COUNT(*) AS total FROM registro_de_capacitaciones
                WHERE COALESCE(fecha_hora, creado_en::timestamp) >= %s
                  AND COALESCE(fecha_hora, creado_en::timestamp) < %s
                  {cid_cond}
            """, params)
            r = cur.fetchone() or {}
            stats["capacitaciones"] = {"total": int(r.get("total") or 0)}
        except Exception:
            stats["capacitaciones"] = {"total": 0}

        # ── Certificaciones vencidas ─────────────────────────────────────────
        try:
            params = [company_id] if company_id is not None else []
            where = f"WHERE company_id = %s" if company_id is not None else ""
            cur.execute(f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN vigencia_hasta IS NOT NULL AND vigencia_hasta < CURRENT_DATE THEN 1 ELSE 0 END) AS vencidas,
                    SUM(CASE WHEN vigencia_hasta IS NOT NULL
                             AND vigencia_hasta >= CURRENT_DATE
                             AND vigencia_hasta <= CURRENT_DATE + INTERVAL '15 days' THEN 1 ELSE 0 END) AS proximas
                FROM checklist_cumplimiento {where}
            """, params)
            r = cur.fetchone() or {}
            stats["cumplimiento"] = {
                "total": int(r.get("total") or 0),
                "vencidas": int(r.get("vencidas") or 0),
                "proximas": int(r.get("proximas") or 0),
            }
        except Exception:
            stats["cumplimiento"] = {"total": 0, "vencidas": 0, "proximas": 0}

        stats["mes"] = month_start.strftime("%B %Y")
        stats["mes_iso"] = month_start.strftime("%Y-%m")
        return jsonify(stats)
    except Exception as e:
        app_logger.error(f"matrices_api_stats error: {e}", exc_info=True)
        return jsonify({"error": "Error interno"}), 500
    finally:
        conn.close()
