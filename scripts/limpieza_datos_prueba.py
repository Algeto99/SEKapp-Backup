#!/usr/bin/env python3
"""
SEKapp - Script de Limpieza de Información de Prueba
====================================================
Elimina de forma segura y controlada todos los registros y formularios
generados durante pruebas y validaciones de la plataforma.

Tablas Operativas que se limpian:
  - reportes_incidentes_personas
  - capacitacion_asistencia
  - asignaciones_hallazgo
  - formulario_edicion_historial
  - reportes_incidentes
  - supervision_puesto
  - medicion_experiencia_cliente
  - log_de_patrullas
  - informe_novedades_disciplinario
  - registro_de_capacitaciones
  - registro_y_acta_de_visita
  - planilla_vehicular
  - planilla_motocicletas
  - checklist_cumplimiento
  - confiabilidad_equipos
  - auditoria_modificaciones (si existe)

Tablas Catálogo / Maestras Preservadas:
  - companies
  - customer_companies
  - propiedades
  - puestos
  - users
  - authorized_emails
  - kpi_thresholds
  - saved_filters

Uso:
  python3 scripts/limpieza_datos_prueba.py             # Modo Dry-Run (Solo conteo informativo)
  python3 scripts/limpieza_datos_prueba.py --execute   # Ejecución real de la limpieza
"""

import os
import sys
import argparse
import logging
import psycopg2
from psycopg2 import extras

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("limpieza_datos_prueba")

OPERATIONAL_TABLES = [
    "reportes_incidentes_personas",
    "capacitacion_asistencia",
    "asignaciones_hallazgo",
    "formulario_edicion_historial",
    "reportes_incidentes",
    "supervision_puesto",
    "medicion_experiencia_cliente",
    "log_de_patrullas",
    "informe_novedades_disciplinario",
    "registro_de_capacitaciones",
    "registro_y_acta_de_visita",
    "planilla_vehicular",
    "planilla_motocicletas",
    "checklist_cumplimiento",
    "confiabilidad_equipos",
]

CATALOG_TABLES = [
    "companies",
    "customer_companies",
    "propiedades",
    "puestos",
    "users",
    "authorized_emails",
    "kpi_thresholds",
    "saved_filters",
]


def get_connection():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        # Check parent or current directory for .env
        for env_path in [".env", "monolith/.env", "../.env"]:
            if os.path.exists(env_path):
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("DATABASE_URL="):
                            db_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                            break
            if db_url:
                break

    if not db_url:
        logger.error("No se encontró la variable de entorno DATABASE_URL.")
        sys.exit(1)

    return psycopg2.connect(db_url)


def get_table_counts(cur, tables):
    counts = {}
    for table in tables:
        try:
            cur.execute("SELECT to_regclass(%s)", (table,))
            exists = cur.fetchone()[0] is not None
            if exists:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                counts[table] = cur.fetchone()[0]
            else:
                counts[table] = None  # No existe en este esquema
        except Exception as e:
            counts[table] = f"Error: {e}"
    return counts


def main():
    parser = argparse.ArgumentParser(description="Limpieza de información de prueba de SEKapp")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Ejecutar el truncado real de las tablas operativas (por defecto es Dry-Run)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Omitir solicitud de confirmación en modo interactivo",
    )
    args = parser.parse_args()

    conn = get_connection()
    cur = conn.cursor()

    try:
        logger.info("=" * 60)
        logger.info("SEKapp - Verificación de Estado de Tablas")
        logger.info("=" * 60)

        # 1. Conteo de Tablas Maestras
        cat_counts = get_table_counts(cur, CATALOG_TABLES)
        logger.info("\n[CATÁLOGOS Y USUARIOS PRESERVADOS]")
        for tbl, cnt in cat_counts.items():
            status = f"{cnt} registros" if isinstance(cnt, int) else ("No existe" if cnt is None else cnt)
            logger.info(f"  • {tbl.ljust(35)}: {status} (PRESERVADO)")

        # 2. Conteo de Tablas Operativas
        op_counts = get_table_counts(cur, OPERATIONAL_TABLES)
        total_op_records = sum(c for c in op_counts.values() if isinstance(c, int))
        logger.info(f"\n[REGISTROS OPERATIVOS / FORMULARIOS ({total_op_records} registros totales)]")
        for tbl, cnt in op_counts.items():
            status = f"{cnt} registros" if isinstance(cnt, int) else ("No existe" if cnt is None else cnt)
            logger.info(f"  • {tbl.ljust(35)}: {status}")

        # Check optional table auditoria_modificaciones
        cur.execute("SELECT to_regclass('auditoria_modificaciones')")
        has_auditoria = cur.fetchone()[0] is not None

        if not args.execute:
            logger.info("\n" + "=" * 60)
            logger.info("MODO DRY-RUN FINALIZADO")
            logger.info("No se modificó ninguna tabla ni se eliminó ningún dato.")
            logger.info("Para ejecutar la limpieza real, ejecute:")
            logger.info("    python3 scripts/limpieza_datos_prueba.py --execute")
            logger.info("=" * 60)
            return

        # Confirmation if interactive and not forced
        if not args.force and sys.stdin.isatty():
            confirm = input("\n¿Está SEGURO de eliminar todos los datos operativos de prueba? [s/N]: ")
            if confirm.strip().lower() not in ("s", "si", "y", "yes"):
                logger.info("Operación cancelada por el usuario.")
                return

        logger.info("\nEjecutando limpieza en transacción...")

        existing_op_tables = [t for t, c in op_counts.items() if isinstance(c, int)]
        if existing_op_tables:
            truncate_query = f"TRUNCATE TABLE {', '.join(existing_op_tables)} RESTART IDENTITY CASCADE;"
            cur.execute(truncate_query)

        if has_auditoria:
            cur.execute("TRUNCATE TABLE auditoria_modificaciones RESTART IDENTITY CASCADE;")

        conn.commit()
        logger.info("Transacción confirmada con éxito.")

        # Post-verification
        post_counts = get_table_counts(cur, OPERATIONAL_TABLES)
        post_cat_counts = get_table_counts(cur, CATALOG_TABLES)

        logger.info("\n[VERIFICACIÓN POST-LIMPIEZA]")
        all_zero = True
        for tbl, cnt in post_counts.items():
            if isinstance(cnt, int) and cnt > 0:
                all_zero = False
                logger.error(f"  ✗ {tbl.ljust(35)}: {cnt} registros (NO LIMPIADO)")
            else:
                logger.info(f"  ✓ {tbl.ljust(35)}: 0 registros (Limpio)")

        logger.info("\n[VERIFICACIÓN DE CATÁLOGOS]")
        for tbl, cnt in post_cat_counts.items():
            logger.info(f"  ✓ {tbl.ljust(35)}: {cnt} registros intactos")

        if all_zero:
            logger.info("\n" + "=" * 60)
            logger.info("LIMPIEZA COMPLETADA CON ÉXITO: Todos los formularios y registros operativos quedaron en 0.")
            logger.info("=" * 60)
        else:
            logger.warning("\nAdvertencia: Algunas tablas operativas aún tienen registros.")

    except Exception as e:
        conn.rollback()
        logger.error(f"Error durante la ejecución: {e}", exc_info=True)
        sys.exit(1)
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
