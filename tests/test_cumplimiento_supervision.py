"""Cumplimiento de la programación de supervisiones por cliente (dashboard_bp).

Pedido de KANAN (2026-10-05): filtro "Período" en el Dashboard de Supervisión,
tabla de cumplimiento por cliente contra la programación de Umbrales KPI y envío
a los Coordinadores acotado al ámbito de cada uno. Reproduce el ejemplo del
pedido: A 20/18, B 5/2, C 10/12 → total 35 / 32 / 30 contadas = 86 %.

Usa el arranque común de tests/sekapp_testing.py (Postgres desechable, esquema
recreado desde sql/schema.sql). Correr con:
    DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib monolith/venv/bin/python tests/test_cumplimiento_supervision.py
(el prefijo sólo hace falta en el Mac para que WeasyPrint cargue y se pruebe el PDF).
"""
import unittest
import zoneinfo
from datetime import datetime, timedelta
from unittest import mock

from sekapp_testing import (A, sql, eventos, configurar_app, recrear_base, crear_empresa,
                            crear_usuario, login)

ADMIN = 'admin@pruebas.sekapp'
COORD = 'coordinador@pruebas.sekapp'       # ámbito: Cliente A
COORD2 = 'coordinador2@pruebas.sekapp'     # sin ámbito
SUP = 'supervisor@pruebas.sekapp'
D = {}

# Programación semanal y realizadas en la semana anterior, como en el pedido.
PROGRAMACION = {'A': 20, 'B': 5, 'C': 10}
REALIZADAS = {'A': 18, 'B': 2, 'C': 12}


def setUpModule():
    configurar_app()
    recrear_base()
    cid = crear_empresa()
    D['admin'] = crear_usuario(ADMIN, 'Admin Pruebas', cid, is_admin=True, is_super_admin=True)
    D['coord'] = crear_usuario(COORD, 'Coordinador Pruebas', cid, is_coordinador=True)
    D['coord2'] = crear_usuario(COORD2, 'Coordinador Sin Ámbito', cid, is_coordinador=True)
    D['sup'] = crear_usuario(SUP, 'Supervisor Pruebas', cid)

    # "Hoy" de la operación: Umbrales KPI arranca con America/Bogota por defecto.
    hoy = datetime.now(zoneinfo.ZoneInfo('America/Bogota')).date()
    D['hoy'] = hoy
    D['lunes'] = hoy - timedelta(days=hoy.weekday()) - timedelta(days=7)
    D['domingo'] = D['lunes'] + timedelta(days=6)

    for letra in ('A', 'B', 'C'):
        c = sql("INSERT INTO customer_companies (company_id, name, code, is_active) "
                "VALUES (%s, %s, %s, TRUE) RETURNING id", [cid, f'Cliente {letra}', letra], uno=True)['id']
        p = sql("INSERT INTO propiedades (nombre, activa, customer_company_id) "
                "VALUES (%s, TRUE, %s) RETURNING id_propiedad", [f'Instalación {letra}1', c], uno=True)['id_propiedad']
        D[f'cli{letra}'], D[f'prop{letra}'] = c, p
        sql("INSERT INTO supervision_programacion (customer_company_id, periodicidad, meta) VALUES (%s, 'semanal', %s)",
            [c, PROGRAMACION[letra]])
        # Realizadas repartidas en los 7 días de la semana anterior (reloj de pared).
        for i in range(REALIZADAS[letra]):
            dia = D['lunes'] + timedelta(days=i % 7)
            sql("INSERT INTO supervision_puesto (cliente_instalacion, id_propiedad, customer_company_id, company_id, "
                "supervisor, submitted_by_email, fecha_hora, creado_en) VALUES (%s, %s, %s, %s, 'Sup Prueba', %s, %s, NOW())",
                [f'Instalación {letra}1', p, c, cid, SUP, f"{dia.isoformat()} {8 + i // 7:02d}:00:00"])
    # Una supervisión de esta semana, fuera de "semana anterior" (hoy a las 06:00).
    sql("INSERT INTO supervision_puesto (cliente_instalacion, id_propiedad, customer_company_id, company_id, "
        "supervisor, submitted_by_email, fecha_hora, creado_en) VALUES (%s, %s, %s, %s, 'Sup Prueba', %s, %s, NOW())",
        ['Instalación A1', D['propA'], D['cliA'], cid, SUP, f"{hoy.isoformat()} 06:00:00"])

    sql("INSERT INTO coordinador_ambito (user_id, customer_company_id, creado_por) VALUES (%s, %s, %s)",
        [D['coord'], D['cliA'], ADMIN])


class CumplimientoTests(unittest.TestCase):
    admin = coord = coord2 = None

    @classmethod
    def setUpClass(cls):
        cls.admin, cls.coord, cls.coord2 = (A.app.test_client() for _ in range(3))
        for cliente, email in ((cls.admin, ADMIN), (cls.coord, COORD), (cls.coord2, COORD2)):
            r = login(cliente, email)
            assert r.status_code == 302, (email, r.status_code)

    def tabla(self, cliente, query=''):
        r = cliente.get('/dashboard/api/supervision/cumplimiento' + query)
        self.assertEqual(r.status_code, 200, r.data[:300])
        return r.get_json()

    @staticmethod
    def fila(datos, nombre):
        return next(f for f in datos['filas'] if f['cliente'] == nombre)

    # ------------------------------------------------------------------
    def test_01_semana_anterior_ejemplo_del_pedido(self):
        d = self.tabla(self.admin, '?periodo=semana_anterior')
        self.assertEqual((d['desde'], d['hasta']), (D['lunes'].isoformat(), D['domingo'].isoformat()))
        self.assertEqual(d['dias'], 7)
        self.assertEqual(d['origen'], 'semana_anterior')
        self.assertTrue(d['hay_programacion'])
        # Orden de menor a mayor cumplimiento.
        self.assertEqual([f['cliente'] for f in d['filas']], ['Cliente B', 'Cliente A', 'Cliente C'])
        b, a, c = d['filas']
        self.assertEqual((b['programadas'], b['realizadas'], b['contadas'], b['pct'], b['tono']), (5, 2, 2, 40, 'rojo'))
        self.assertEqual((a['programadas'], a['realizadas'], a['contadas'], a['pct'], a['tono']), (20, 18, 18, 90, 'verde'))
        # El exceso de C se cuenta sólo hasta lo programado.
        self.assertEqual((c['programadas'], c['realizadas'], c['contadas'], c['pct'], c['tono']), (10, 12, 10, 100, 'verde'))
        # Total sobre cantidades acumuladas, no promedio de porcentajes (que daría 77).
        t = d['total']
        self.assertEqual((t['programadas'], t['realizadas'], t['contadas'], t['pct'], t['tono']), (35, 32, 30, 86, 'amarillo'))
        self.assertEqual(d['umbrales'], {'verde_min': 90.0, 'amarillo_min': 70.0})

    def test_02_sin_periodo_usa_mes_actual_y_anio_mes_manda(self):
        d = self.tabla(self.admin)
        self.assertEqual(d['origen'], 'mes_actual')
        self.assertEqual((d['desde'], d['hasta']), (D['hoy'].replace(day=1).isoformat(), D['hoy'].isoformat()))
        # Año / Mes elegidos sin Período: el lapso sale de ellos, recortado a hoy.
        lunes = D['lunes']
        d = self.tabla(self.admin, f"?year={lunes.year}&month={lunes.month}")
        self.assertEqual(d['origen'], 'anio_mes')
        self.assertEqual(d['desde'], lunes.replace(day=1).isoformat())
        self.assertLessEqual(d['hasta'], D['hoy'].isoformat())
        # Un Período mal escrito o un `hasta` roto no tumban la consulta.
        self.assertEqual(self.tabla(self.admin, '?periodo=ayer')['origen'], 'mes_actual')
        self.assertEqual(self.tabla(self.admin, '?periodo=personalizado&desde=2026-01-01&hasta=ayer')['origen'], 'mes_actual')

    def test_03_personalizado_prorratea_la_meta(self):
        lunes = D['lunes']
        # Un solo día: meta semanal 20 → 20/7 = 2.86 → 3; 5 → 0.71 → 1; 10 → 1.43 → 1.
        d = self.tabla(self.admin, f"?periodo=personalizado&desde={lunes}&hasta={lunes}")
        self.assertEqual(d['dias'], 1)
        self.assertEqual(self.fila(d, 'Cliente A')['programadas'], 3)
        self.assertEqual(self.fila(d, 'Cliente B')['programadas'], 1)
        self.assertEqual(self.fila(d, 'Cliente C')['programadas'], 1)
        # El lunes tiene las supervisiones con i % 7 == 0: A 3 (0, 7, 14), B 1, C 2.
        self.assertEqual(self.fila(d, 'Cliente A')['realizadas'], 3)
        self.assertEqual(self.fila(d, 'Cliente B')['realizadas'], 1)
        self.assertEqual(self.fila(d, 'Cliente C')['realizadas'], 2)
        # `hasta` es inclusivo: lunes a martes trae también los del martes.
        d = self.tabla(self.admin, f"?periodo=personalizado&desde={lunes}&hasta={lunes + timedelta(days=1)}")
        self.assertEqual(self.fila(d, 'Cliente A')['realizadas'], 6)
        # Filtro Cliente: sólo ese cliente.
        d = self.tabla(self.admin, f"?periodo=semana_anterior&cliente={D['cliB']}")
        self.assertEqual([f['cliente'] for f in d['filas']], ['Cliente B'])
        self.assertEqual(d['total']['pct'], 40)

    def test_04_coordinador_acotado_a_su_ambito(self):
        d = self.tabla(self.coord, '?periodo=semana_anterior')
        self.assertEqual([f['cliente'] for f in d['filas']], ['Cliente A'])
        self.assertEqual(d['total']['pct'], 90)
        self.assertEqual(self.tabla(self.coord2, '?periodo=semana_anterior')['filas'], [], 'sin ámbito no ve nada')

    def test_05_periodo_en_datos_y_detalles(self):
        r = self.admin.get('/dashboard/api/supervision/data?periodo=semana_anterior')
        self.assertEqual(r.status_code, 200, r.data[:300])
        d = r.get_json()
        self.assertEqual(d['kpi']['total'], 32)
        self.assertEqual(d['rango'], {'periodo': 'semana_anterior', 'desde': D['lunes'].isoformat(),
                                      'hasta': D['domingo'].isoformat()})
        # Con Período, Año / Mes / Día se ignoran (son excluyentes en la barra).
        r = self.admin.get('/dashboard/api/supervision/data?periodo=semana_anterior&year=2000&month=1')
        self.assertEqual(r.get_json()['kpi']['total'], 32)
        # Mes actual incluye la supervisión de hoy y ninguna de la semana anterior si
        # ésta cayó en el mes pasado; en todo caso cuenta la de hoy.
        r = self.admin.get('/dashboard/api/supervision/data?periodo=mes_actual')
        self.assertGreaterEqual(r.get_json()['kpi']['total'], 1)
        r = self.admin.get('/dashboard/api/supervision/detalles?periodo=semana_anterior')
        self.assertEqual(len(r.get_json()['detalles']), 32)
        r = self.coord.get('/dashboard/api/supervision/detalles?periodo=semana_anterior')
        self.assertEqual(len(r.get_json()['detalles']), 18)

    def test_06_pdf(self):
        import dashboard_bp
        r = self.admin.post('/dashboard/api/supervision/cumplimiento/pdf',
                            json={'periodo': 'semana_anterior'}, headers={'Accept': 'application/pdf, application/json'})
        if dashboard_bp._WEASYPRINT_AVAILABLE:
            self.assertEqual(r.status_code, 200, r.data[:300])
            self.assertEqual(r.mimetype, 'application/pdf')
            self.assertTrue(r.data.startswith(b'%PDF'))
            self.assertIn(f"cumplimiento_supervisiones_{D['lunes']}_{D['domingo']}.pdf",
                          r.headers.get('Content-Disposition', ''))
            self.assertTrue(eventos(accion='Generación de PDF de cumplimiento'), 'queda en Auditoría')
        else:
            print('\n[aviso] WeasyPrint no disponible en este proceso: el PDF responde 503 (correr con DYLD_FALLBACK_LIBRARY_PATH).')
            self.assertEqual(r.status_code, 503)
        # El HTML del PDF lleva la tabla completa.
        from flask import g
        with A.app.test_request_context():
            conn = dashboard_bp.get_db_connection()
            cur = conn.cursor(cursor_factory=dashboard_bp.psycopg2.extras.DictCursor)
            datos = dashboard_bp._cumplimiento_programacion(cur, D['lunes'], D['domingo'])
            html = dashboard_bp._cumplimiento_html(datos)
            conn.close()
        for esperado in ('Cliente A', 'Cliente B', 'Cliente C', '>86 %<', '>40 %<', '>100 %<', 'Total'):
            self.assertIn(esperado, html)
        # Sólo Administrador.
        r = self.coord.post('/dashboard/api/supervision/cumplimiento/pdf', json={'periodo': 'semana_anterior'},
                            headers={'Accept': 'application/json'})
        self.assertEqual(r.status_code, 403)

    def test_07_coordinadores_y_envio_por_correo(self):
        r = self.admin.get('/dashboard/api/supervision/coordinadores')
        self.assertEqual(r.status_code, 200, r.data[:300])
        coords = {c['email']: c for c in r.get_json()['coordinadores']}
        self.assertEqual(set(coords), {COORD, COORD2})
        self.assertEqual(coords[COORD]['clientes'], [D['cliA']])
        self.assertEqual(coords[COORD]['ambito'], ['Cliente A'])
        self.assertTrue(coords[COORD2]['sin_ambito'])
        self.assertEqual(self.coord.get('/dashboard/api/supervision/coordinadores',
                                        headers={'Accept': 'application/json'}).status_code, 403)

        enviados = []

        def falso_envio(to, asunto, cuerpo, is_html=False, cc_emails=None):
            enviados.append((to, asunto, cuerpo, is_html))
            return True

        with mock.patch('dashboard_bp.send_email', side_effect=falso_envio):
            r = self.admin.post('/dashboard/api/supervision/cumplimiento/email',
                                json={'periodo': 'semana_anterior', 'coordinadores': [D['coord'], D['coord2']],
                                      'mensaje': 'Favor revisar <B>'})
        self.assertEqual(r.status_code, 200, r.data[:300])
        d = r.get_json()
        self.assertEqual(d['enviados'], 1)
        estados = {x['email']: x for x in d['resultados']}
        self.assertEqual(estados[COORD]['estado'], 'enviado')
        self.assertEqual((estados[COORD]['clientes'], estados[COORD]['pct']), (1, 90))
        self.assertEqual(estados[COORD2]['estado'], 'omitido')
        self.assertEqual(len(enviados), 1)
        to, asunto, cuerpo, is_html = enviados[0]
        self.assertEqual(to, COORD)
        self.assertTrue(is_html)
        self.assertIn('Cumplimiento de supervisiones', asunto)
        # Sólo su cliente, el saludo con su nombre y el mensaje escapado.
        self.assertIn('Cliente A', cuerpo)
        self.assertNotIn('Cliente B', cuerpo)
        self.assertNotIn('Cliente C', cuerpo)
        self.assertIn('Coordinador Pruebas', cuerpo)
        self.assertIn('Favor revisar &lt;B&gt;', cuerpo)
        self.assertIn('>90 %<', cuerpo)
        ev = eventos(accion='Envío de cumplimiento a Coordinadores')
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]['detalle'].get('enviados'), [COORD])

        # Sin destinatarios → 400; correo caído → 502; Coordinador → 403.
        self.assertEqual(self.admin.post('/dashboard/api/supervision/cumplimiento/email',
                                         json={'periodo': 'semana_anterior', 'coordinadores': []}).status_code, 400)
        with mock.patch('dashboard_bp.send_email', return_value=False):
            r = self.admin.post('/dashboard/api/supervision/cumplimiento/email',
                                json={'periodo': 'semana_anterior', 'coordinadores': [D['coord']]})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json()['resultados'][0]['estado'], 'fallido')
        self.assertEqual(self.coord.post('/dashboard/api/supervision/cumplimiento/email',
                                         json={'coordinadores': [D['coord']]},
                                         headers={'Accept': 'application/json'}).status_code, 403)

    def test_08_ambito_por_instalacion(self):
        # Coordinador con la instalación B1 (no el cliente B): ve la fila de B con las
        # realizadas de su instalación y las programadas del cliente, que es la única
        # granularidad de la programación.
        sql("INSERT INTO coordinador_ambito (user_id, id_propiedad, creado_por) VALUES (%s, %s, %s)",
            [D['coord2'], D['propB'], ADMIN])
        try:
            d = self.tabla(self.coord2, '?periodo=semana_anterior')
            self.assertEqual([f['cliente'] for f in d['filas']], ['Cliente B'])
            self.assertEqual(self.fila(d, 'Cliente B')['realizadas'], 2)
        finally:
            sql("DELETE FROM coordinador_ambito WHERE user_id = %s", [D['coord2']])

    def test_09_sin_programacion_no_inventa_meta(self):
        sql("DELETE FROM supervision_programacion")
        try:
            d = self.tabla(self.admin, '?periodo=semana_anterior')
            self.assertFalse(d['hay_programacion'])
            self.assertEqual(sorted(f['cliente'] for f in d['filas']), ['Cliente A', 'Cliente B', 'Cliente C'])
            for f in d['filas']:
                self.assertEqual((f['programadas'], f['contadas'], f['pct'], f['tono']), (0, 0, None, 'gris'))
                self.assertEqual(f['realizadas'], REALIZADAS[f['cliente'][-1]])
            self.assertEqual((d['total']['programadas'], d['total']['realizadas'], d['total']['pct']), (0, 32, None))
        finally:
            for letra in ('A', 'B', 'C'):
                sql("INSERT INTO supervision_programacion (customer_company_id, periodicidad, meta) VALUES (%s, 'semanal', %s)",
                    [D[f'cli{letra}'], PROGRAMACION[letra]])

    def test_10_prorrateo_mensual_y_diario(self):
        import dashboard_bp as dbp
        from datetime import date
        # Mes calendario completo con meta mensual → exactamente la meta, tenga 28, 30 o 31 días.
        self.assertEqual(dbp._programadas_en_lapso(10, 'mensual', date(2026, 2, 1), date(2026, 2, 28)), 10)
        self.assertEqual(dbp._programadas_en_lapso(10, 'mensual', date(2026, 9, 1), date(2026, 9, 30)), 10)
        self.assertEqual(dbp._programadas_en_lapso(20, 'mensual', date(2026, 10, 1), date(2026, 10, 31)), 20)
        # Mes actual al día 5 con meta mensual 31 en octubre → 5.
        self.assertEqual(dbp._programadas_en_lapso(31, 'mensual', date(2026, 10, 1), date(2026, 10, 5)), 5)
        # Dos meses enteros → el doble.
        self.assertEqual(dbp._programadas_en_lapso(10, 'mensual', date(2026, 9, 1), date(2026, 10, 31)), 20)
        # Semanal: 5 en 30 días → 21.4 → 21; diario: meta por cada día.
        self.assertEqual(dbp._programadas_en_lapso(5, 'semanal', date(2026, 9, 1), date(2026, 9, 30)), 21)
        self.assertEqual(dbp._programadas_en_lapso(3, 'diario', date(2026, 9, 1), date(2026, 9, 7)), 21)
        self.assertEqual(dbp._programadas_en_lapso(0, 'semanal', date(2026, 9, 1), date(2026, 9, 7)), 0)
        # Semana anterior = lunes a domingo; mes anterior = mes calendario completo.
        with A.app.test_request_context():
            d, h = dbp._rango_periodo('semana_anterior')
            self.assertEqual((d.weekday(), h.weekday(), (h - d).days), (0, 6, 6))
            self.assertLess(h, D['hoy'])
            d, h = dbp._rango_periodo('mes_anterior')
            self.assertEqual((d.day, h, (h + timedelta(days=1)).day), (1, D['hoy'].replace(day=1) - timedelta(days=1), 1))
            self.assertEqual(dbp._rango_periodo('personalizado', '2026-02-10', '2026-02-01'), (None, None), 'desde > hasta')
            self.assertEqual(dbp._rango_periodo(None, '2026-02-01', '2026-02-10'), (None, None), 'sin período no hay rango')


if __name__ == '__main__':
    unittest.main(verbosity=2)
