"""
Matrices Hub — Consultar Matrices
Central hub for operational matrices: incidents, visits, supervision, discipline, etc.
"""

import os
import logging
import urllib.parse as urlparse
from datetime import date, timedelta

import psycopg2
from psycopg2 import extras

from flask import Blueprint, render_template, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt

matrices_bp = Blueprint("matrices_bp", __name__)
app_logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")


def _get_conn():
    if not DATABASE_URL:
        return None
    urlparse.uses_netloc.append("postgres")
    p = urlparse.urlparse(DATABASE_URL)
    q = dict(urlparse.parse_qsl(p.query))
    try:
        return psycopg2.connect(
            dbname=p.path[1:], user=p.username, password=p.password,
            host=q.get("host", p.hostname), port=q.get("port", p.port or "5432"),
            cursor_factory=extras.RealDictCursor,
        )
    except Exception as e:
        app_logger.error(f"Matrices DB connect error: {e}", exc_info=True)
        return None


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
        cur = conn.cursor()
        today = date.today()
        month_start = today.replace(day=1)

        stats = {}

        # ── Incidentes abiertos ──────────────────────────────────────────────
        try:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN LOWER(TRIM(COALESCE(estado,''))) NOT IN
                        ('cerrado','closed','resuelto','resolved') THEN 1 ELSE 0 END) AS abiertos,
                    SUM(CASE WHEN LOWER(TRIM(nivel_severidad)) IN ('crítico','critico') THEN 1 ELSE 0 END) AS criticos
                FROM reportes_incidentes
                WHERE COALESCE(fecha_hora, creado_en) >= %s
            """, (month_start,))
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
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN LOWER(TRIM(COALESCE(estado,''))) IN
                        ('pendiente','','abierto') OR estado IS NULL THEN 1 ELSE 0 END) AS pendientes
                FROM registro_y_acta_de_visita
                WHERE COALESCE(fecha_hora, creado_en) >= %s
            """, (month_start,))
            r = cur.fetchone() or {}
            stats["visitas"] = {
                "total": int(r.get("total") or 0),
                "pendientes": int(r.get("pendientes") or 0),
            }
        except Exception:
            stats["visitas"] = {"total": 0, "pendientes": 0}

        # ── Supervisiones del mes ────────────────────────────────────────────
        try:
            cur.execute("""
                SELECT COUNT(*) AS total FROM supervision_puesto
                WHERE fecha_hora >= %s
            """, (month_start,))
            r = cur.fetchone() or {}
            stats["supervision"] = {"total": int(r.get("total") or 0)}
        except Exception:
            stats["supervision"] = {"total": 0}

        # ── Disciplina ───────────────────────────────────────────────────────
        try:
            cur.execute("""
                SELECT COUNT(*) AS total FROM informe_novedades_disciplinario
                WHERE fecha_hora >= %s
            """, (month_start,))
            r = cur.fetchone() or {}
            stats["disciplina"] = {"total": int(r.get("total") or 0)}
        except Exception:
            stats["disciplina"] = {"total": 0}

        # ── Capacitaciones del mes ───────────────────────────────────────────
        try:
            cur.execute("""
                SELECT COUNT(*) AS total FROM registro_de_capacitaciones
                WHERE COALESCE(fecha_hora, creado_en::timestamp) >= %s
            """, (month_start,))
            r = cur.fetchone() or {}
            stats["capacitaciones"] = {"total": int(r.get("total") or 0)}
        except Exception:
            stats["capacitaciones"] = {"total": 0}

        # ── Certificaciones vencidas ─────────────────────────────────────────
        try:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN vigencia_hasta IS NOT NULL AND vigencia_hasta < CURRENT_DATE THEN 1 ELSE 0 END) AS vencidas,
                    SUM(CASE WHEN vigencia_hasta IS NOT NULL
                             AND vigencia_hasta >= CURRENT_DATE
                             AND vigencia_hasta <= CURRENT_DATE + INTERVAL '15 days' THEN 1 ELSE 0 END) AS proximas
                FROM checklist_cumplimiento
            """)
            r = cur.fetchone() or {}
            stats["cumplimiento"] = {
                "total": int(r.get("total") or 0),
                "vencidas": int(r.get("vencidas") or 0),
                "proximas": int(r.get("proximas") or 0),
            }
        except Exception:
            stats["cumplimiento"] = {"total": 0, "vencidas": 0, "proximas": 0}

        stats["mes"] = month_start.strftime("%B %Y")
        return jsonify(stats)
    except Exception as e:
        app_logger.error(f"matrices_api_stats error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
