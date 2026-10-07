#!/usr/bin/env python3
"""Validación con datos reales de la tarjeta "Supervisiones" del Morning Briefing.

Compara, para las últimas N semanas cerradas (lunes a domingo) y para la ventana
vigente, el cálculo ANTERIOR de la tarjeta con el NUEVO, unificado con la tabla y
el PDF del Dashboard de Supervisión (validado por KANAN el 2026-10-07):

  anterior: registros de supervision_puesto contra la meta completa, sin tope.
  nuevo:    una supervisión por instalación y día, meta repartida por días
            (mensual por días calendario), tope por cliente y, en la ventana
            vigente, cierre en ayer con el día de hoy aparte ("En curso").

Desglosa cada diferencia en sus dos causas: instalaciones-día repetidas (varios
puestos o varias visitas a la misma instalación el mismo día) y tope por cliente.

Sólo lee. (La inicialización estándar de kpi_thresholds que hace la app al leer
los umbrales también corre aquí; no toca datos de supervisiones.)

Uso, desde la raíz del repo:
    DATABASE_URL=postgresql://usuario:clave@host:5432/base \\
        monolith/venv/bin/python scripts/validar_cumplimiento_tarjeta.py [--semanas 4]
Para Cloud SQL, abrir antes el proxy (cloud-sql-proxy) y apuntar la URL a 127.0.0.1.
También acepta --url en lugar de la variable de entorno.
"""
import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / 'monolith'))


def _n(fila):
    return int((fila['n'] if hasattr(fila, 'keys') else fila[0]) or 0)


def registros_cliente(cur, cliente_id, desde, hasta, dbp):
    """Cuenta ANTERIOR: registros del cliente en el lapso (sin deduplicar por instalación)."""
    conds, params = [], []
    dbp._add_scope_filters(conds, params, cliente=str(cliente_id) if cliente_id is not None else None, col_puesto=None)
    dbp._gestion_add_desde(conds, params, "fecha_hora", desde.isoformat())
    dbp._gestion_add_hasta(conds, params, "fecha_hora", hasta.isoformat())
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    cur.execute(f"SELECT COUNT(*) AS n FROM supervision_puesto {where}", params)
    return _n(cur.fetchone())


def pct(a, b):
    return f"{int(a / b * 100 + 0.5):>3} %" if b else "  — "


def imprimir_lapso(cur, dbp, titulo, desde, hasta, vigente=None):
    """Una tabla por lapso. Con `vigente` imprime la ventana de la tarjeta (cada
    cliente en la suya, cerrada en ayer); si no, el lapso cerrado [desde, hasta]."""
    nuevo = vigente or dbp._cumplimiento_programacion(cur, desde, hasta)
    filas = vigente['por_cliente'] if vigente else nuevo['filas']
    hoy = date.fromisoformat(nuevo['hoy'])
    print(f"\n{titulo}")
    print(f"{'Cliente':<28} {'Meta':>10} | {'Registros':>9} {'% ant.':>7} | {'Inst-día':>8} {'Contadas':>8} {'Program.':>8} {'% nuevo':>7} | {'Repet.':>6} {'Tope':>5}")
    print('-' * 118)
    t_reg = t_meta = 0
    for f in filas:
        if f.get('estado') == 'sin_programacion':
            continue
        meta, per = int(f.get('meta') or 0), (f.get('periodicidad') or '')[:3]
        # Lo anterior medía la ventana completa, con hoy, contra la meta entera y sin tope.
        d_ant = desde if not vigente else date.fromisoformat(f['desde'])
        h_ant = hasta if not vigente else hoy
        reg = registros_cliente(cur, f['cliente_id'], d_ant, h_ant, dbp)
        real, cont, prog = f['realizadas'], f.get('contadas') or 0, f.get('programadas') or 0
        repet = max(0, reg - real - (f.get('en_curso') or 0))
        print(f"{f['cliente'][:28]:<28} {f'{meta} {per}':>10} | {reg:>9} {pct(reg, meta):>7} | {real:>8} {cont:>8} {prog:>8} {pct(cont, prog):>7} | {repet:>6} {max(0, real - cont):>5}")
        t_reg += reg
        t_meta += meta
    tot = nuevo['total'] if not vigente else nuevo
    t_real, t_cont, t_prog = tot['realizadas'], tot.get('contadas') or 0, tot.get('programadas') or 0
    print('-' * 118)
    print(f"{'TOTAL':<28} {t_meta:>10} | {t_reg:>9} {pct(t_reg, t_meta):>7} | {t_real:>8} {t_cont:>8} {t_prog:>8} {pct(t_cont, t_prog):>7} | {max(0, t_reg - t_real - (tot.get('en_curso') or 0)):>6} {max(0, t_real - t_cont):>5}")
    if vigente:
        print(f"Hoy en curso (no entra al porcentaje): {vigente['en_curso']} instalaciones-día"
              + (" · la ventana no tiene todavía ningún día cerrado" if vigente['sin_dia_cerrado'] else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--semanas', type=int, default=4, help='semanas cerradas a comparar (por defecto 4)')
    ap.add_argument('--url', help='URL de la base (si no se da, DATABASE_URL)')
    args = ap.parse_args()
    if args.url:
        os.environ['DATABASE_URL'] = args.url
    if not os.environ.get('DATABASE_URL'):
        sys.exit('Falta DATABASE_URL (o --url).')
    os.environ.setdefault('FLASK_SECRET_KEY', 'validacion-local-sin-servidor-0000000000')
    os.environ.setdefault('JWT_SECRET_KEY', 'validacion-local-sin-servidor-0000000000')
    logging.disable(logging.WARNING)

    from psycopg2 import extras
    from db import get_db_connection
    import dashboard_bp as dbp
    from admin_bp import hoy_operacion, get_supervision_programacion, get_thresholds, calcular_supervisiones

    conn = get_db_connection()
    if not conn:
        sys.exit('No se pudo conectar a la base.')
    cur = conn.cursor(cursor_factory=extras.DictCursor)
    try:
        t = get_thresholds()
        hoy = hoy_operacion()
        programacion = [p for p in get_supervision_programacion(cur) if int(p.get('meta') or 0) > 0]
        print(f"Base: {os.environ['DATABASE_URL'].split('@')[-1]}")
        print(f"Hoy (zona {t.get('zona_horaria') or 'America/Bogota por defecto'}): {hoy} · meta general {int(t.get('supervision_meta') or 0)} {t.get('supervision_periodicidad')}"
              f" · clientes con programación propia: {len(programacion)}")
        for p in programacion:
            print(f"   {p['name']}: {p['meta']} {p['periodicidad']}")
        if not programacion:
            print("   (ninguno: rige la meta general)")

        lunes_actual = hoy - timedelta(days=hoy.weekday())
        for k in range(args.semanas, 0, -1):
            d = lunes_actual - timedelta(days=7 * k)
            h = d + timedelta(days=6)
            imprimir_lapso(cur, dbp, f"Semana cerrada {d} – {h}", d, h)

        vig = calcular_supervisiones(cur)
        imprimir_lapso(cur, dbp, f"Ventana vigente de la tarjeta (cerrada en ayer; desde {vig.get('desde')} hasta {vig.get('hasta_cerrado') or '—'})",
                       None, None, vigente=vig)
        print("\nLectura: 'Repet.' son instalaciones-día repetidas (varios puestos o visitas a la misma instalación el mismo día);"
              " 'Tope' es lo que excede la programación de cada cliente y ya no compensa a otros.")
        conn.rollback()
    finally:
        cur.close()
        conn.close()


if __name__ == '__main__':
    main()
