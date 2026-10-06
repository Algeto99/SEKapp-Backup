"""Cumplimiento de la programación de supervisiones por cliente (dashboard_bp).

Pedido de KANAN (2026-10-05): filtro "Período" en el Dashboard de Supervisión,
tabla de cumplimiento por cliente con la MISMA lógica que el gráfico
"Supervisiones — últimos 7 días" del Morning Briefing y que Umbrales KPI, y envío
a los Coordinadores acotado al ámbito de cada uno. Reproduce el ejemplo del
pedido: A 20/18, B 5/2, C 10/12 → total 35 / 32 / 30 contadas = 86 %.

Realizadas se cuentan como el gráfico: instalaciones distintas supervisadas por
día. Por eso la siembra reparte cada cliente en varias instalaciones y añade una
supervisión repetida (misma instalación, mismo día) que NO debe sumar.

Usa el arranque común de tests/sekapp_testing.py (Postgres desechable, esquema
recreado desde sql/schema.sql). Correr con:
    DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib monolith/venv/bin/python tests/test_cumplimiento_supervision.py
(el prefijo sólo hace falta en el Mac para que WeasyPrint cargue y se pruebe el PDF).
"""
import unittest
import zoneinfo
from datetime import date, datetime, timedelta
from unittest import mock

from sekapp_testing import (A, sql, eventos, configurar_app, recrear_base, crear_empresa,
                            crear_usuario, login)

ADMIN = 'admin@pruebas.sekapp'
COORD = 'coordinador@pruebas.sekapp'       # ámbito: Cliente A
COORD2 = 'coordinador2@pruebas.sekapp'     # sin ámbito
SUP = 'supervisor@pruebas.sekapp'
D = {}

# Programación semanal y realizadas (instalación-día) en la semana anterior.
PROGRAMACION = {'A': 20, 'B': 5, 'C': 10}
REALIZADAS = {'A': 18, 'B': 2, 'C': 12}


def sembrar_supervision(letra, instalacion, cuando):
    sql("INSERT INTO supervision_puesto (cliente_instalacion, id_propiedad, customer_company_id, company_id, "
        "supervisor, submitted_by_email, fecha_hora, creado_en) VALUES (%s, %s, %s, %s, 'Sup Prueba', %s, %s, NOW())",
        [f'Instalación {letra}{instalacion}', D[f'prop{letra}{instalacion}'], D[f'cli{letra}'], D['cid'], SUP, cuando])


def setUpModule():
    configurar_app()
    recrear_base()
    cid = crear_empresa()
    D['cid'] = cid
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
        D[f'cli{letra}'] = c
        # Tres instalaciones por cliente: la cuenta del gráfico es por instalación y día.
        for k in (1, 2, 3):
            D[f'prop{letra}{k}'] = sql("INSERT INTO propiedades (nombre, activa, customer_company_id) "
                                       "VALUES (%s, TRUE, %s) RETURNING id_propiedad",
                                       [f'Instalación {letra}{k}', c], uno=True)['id_propiedad']
        sql("INSERT INTO supervision_programacion (customer_company_id, periodicidad, meta) VALUES (%s, 'semanal', %s)",
            [c, PROGRAMACION[letra]])
        # Realizada i: día lunes + i % 7, instalación 1 + i // 7 → pares (día, instalación) distintos.
        for i in range(REALIZADAS[letra]):
            dia = D['lunes'] + timedelta(days=i % 7)
            sembrar_supervision(letra, 1 + i // 7, f"{dia.isoformat()} 08:00:00")
    # Repetida: Instalación A1 el lunes otra vez. Es un registro más, no una realizada más.
    sembrar_supervision('A', 1, f"{D['lunes'].isoformat()} 15:00:00")
    # Una supervisión de esta semana, fuera de "semana anterior" (hoy a las 06:00).
    sembrar_supervision('A', 1, f"{hoy.isoformat()} 06:00:00")

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

    @staticmethod
    def resumen(f):
        return (f['programadas'], f['realizadas'], f['contadas'], f['pct'], f['tono'])

    # ------------------------------------------------------------------
    def test_01_semana_anterior_ejemplo_del_pedido(self):
        d = self.tabla(self.admin, '?periodo=semana_anterior')
        self.assertEqual((d['desde'], d['hasta']), (D['lunes'].isoformat(), D['domingo'].isoformat()))
        self.assertEqual((d['dias'], d['origen']), (7, 'semana_anterior'))
        self.assertTrue(d['hay_programacion'])
        self.assertFalse(d['sin_datos'])
        self.assertIsNone(d['recortado_inicio'])
        # Orden de menor a mayor cumplimiento.
        self.assertEqual([f['cliente'] for f in d['filas']], ['Cliente B', 'Cliente A', 'Cliente C'])
        b, a, c = d['filas']
        self.assertEqual(self.resumen(b), (5, 2, 2, 40, 'rojo'))
        # A tiene 19 registros pero 18 instalaciones-día: la repetida no suma.
        self.assertEqual(self.resumen(a), (20, 18, 18, 90, 'verde'))
        # El exceso de C se cuenta sólo hasta lo programado.
        self.assertEqual(self.resumen(c), (10, 12, 10, 100, 'verde'))
        for f in d['filas']:
            self.assertEqual((f['estado'], f['prorrateado'], f['por_instalacion']), ('ok', False, False))
        # Total sobre cantidades acumuladas, no promedio de porcentajes (que daría 77).
        t = d['total']
        self.assertEqual(self.resumen(t), (35, 32, 30, 86, 'amarillo'))
        self.assertEqual((t['estado'], t['prorrateado'], t['meta_general']), ('ok', False, None))
        self.assertEqual(d['umbrales'], {'verde_min': 90.0, 'amarillo_min': 70.0})

    def test_02_sin_periodo_usa_mes_actual_y_anio_mes_manda(self):
        d = self.tabla(self.admin)
        self.assertEqual(d['origen'], 'mes_actual')
        self.assertEqual((d['desde'], d['hasta']), (D['hoy'].replace(day=1).isoformat(), D['hoy'].isoformat()))
        lunes = D['lunes']
        d = self.tabla(self.admin, f"?year={lunes.year}&month={lunes.month}")
        self.assertEqual(d['origen'], 'anio_mes')
        self.assertEqual(d['desde'], lunes.replace(day=1).isoformat())
        self.assertLessEqual(d['hasta'], D['hoy'].isoformat())
        self.assertEqual(self.tabla(self.admin, '?periodo=ayer')['origen'], 'mes_actual')
        self.assertEqual(self.tabla(self.admin, '?periodo=personalizado&desde=2026-01-01&hasta=ayer')['origen'], 'mes_actual')

    def test_03_personalizado_prorratea_y_filtra(self):
        lunes = D['lunes']
        # Un solo día: meta semanal 20 → 20/7 = 2.86 → 3; 5 → 0.71 → 1; 10 → 1.43 → 1. Todo prorrateado.
        d = self.tabla(self.admin, f"?periodo=personalizado&desde={lunes}&hasta={lunes}")
        self.assertEqual(d['dias'], 1)
        self.assertEqual([(f['cliente'], f['programadas'], f['prorrateado']) for f in sorted(d['filas'], key=lambda f: f['cliente'])],
                         [('Cliente A', 3, True), ('Cliente B', 1, True), ('Cliente C', 1, True)])
        self.assertTrue(d['total']['prorrateado'])
        # El lunes: A en A1, A2 y A3 (la repetida en A1 no suma) → 3; B 1; C en C1 y C2 → 2.
        self.assertEqual(self.fila(d, 'Cliente A')['realizadas'], 3)
        self.assertEqual(self.fila(d, 'Cliente B')['realizadas'], 1)
        self.assertEqual(self.fila(d, 'Cliente C')['realizadas'], 2)
        # `hasta` es inclusivo: lunes a martes trae también los del martes.
        d = self.tabla(self.admin, f"?periodo=personalizado&desde={lunes}&hasta={lunes + timedelta(days=1)}")
        self.assertEqual(self.fila(d, 'Cliente A')['realizadas'], 6)
        # Dos semanas exactas (las dos anteriores, para que `hasta` no pase de hoy y la
        # tabla no lo recorte): el doble de la meta sin prorrateo.
        d = self.tabla(self.admin, f"?periodo=personalizado&desde={lunes - timedelta(days=7)}&hasta={lunes + timedelta(days=6)}")
        self.assertEqual((self.fila(d, 'Cliente A')['programadas'], self.fila(d, 'Cliente A')['prorrateado']), (40, False))
        # Filtro Cliente: sólo ese cliente.
        d = self.tabla(self.admin, f"?periodo=semana_anterior&cliente={D['cliB']}")
        self.assertEqual([f['cliente'] for f in d['filas']], ['Cliente B'])
        self.assertEqual(d['total']['pct'], 40)

    def test_04_instalacion_filtrada_sobre_programacion_del_cliente(self):
        # A2 tiene 7 realizadas (días 7..13 de la siembra); la programación sigue siendo la de A.
        d = self.tabla(self.admin, f"?periodo=semana_anterior&propiedad={D['propA2']}")
        self.assertTrue(d['por_instalacion'])
        self.assertEqual([f['cliente'] for f in d['filas']], ['Cliente A'])
        a = self.fila(d, 'Cliente A')
        self.assertEqual(self.resumen(a), (20, 7, 7, 35, 'rojo'))
        self.assertTrue(a['por_instalacion'])
        # Con cliente e instalación a la vez, igual.
        d = self.tabla(self.admin, f"?periodo=semana_anterior&cliente={D['cliA']}&propiedad={D['propA2']}")
        self.assertEqual(self.fila(d, 'Cliente A')['realizadas'], 7)
        # Instalación de un cliente que no la tiene en su ámbito → nada.
        d = self.tabla(self.coord, f"?periodo=semana_anterior&propiedad={D['propB1']}")
        self.assertEqual(d['filas'], [])

    def test_05_coordinador_acotado_a_su_ambito(self):
        d = self.tabla(self.coord, '?periodo=semana_anterior')
        self.assertEqual([f['cliente'] for f in d['filas']], ['Cliente A'])
        self.assertEqual(d['total']['pct'], 90)
        self.assertEqual(self.tabla(self.coord2, '?periodo=semana_anterior')['filas'], [], 'sin ámbito no ve nada')

    def test_06_periodo_en_datos_y_detalles(self):
        # La tarjeta y el detalle siguen contando registros (19 de A + 2 + 12 = 33): no se tocan.
        r = self.admin.get('/dashboard/api/supervision/data?periodo=semana_anterior')
        self.assertEqual(r.status_code, 200, r.data[:300])
        d = r.get_json()
        self.assertEqual(d['kpi']['total'], 33)
        self.assertEqual(d['rango'], {'periodo': 'semana_anterior', 'desde': D['lunes'].isoformat(),
                                      'hasta': D['domingo'].isoformat()})
        r = self.admin.get('/dashboard/api/supervision/data?periodo=semana_anterior&year=2000&month=1')
        self.assertEqual(r.get_json()['kpi']['total'], 33, 'con Período, Año / Mes se ignoran')
        r = self.admin.get('/dashboard/api/supervision/data?periodo=mes_actual')
        self.assertGreaterEqual(r.get_json()['kpi']['total'], 1)
        r = self.admin.get('/dashboard/api/supervision/detalles?periodo=semana_anterior')
        self.assertEqual(len(r.get_json()['detalles']), 33)
        r = self.coord.get('/dashboard/api/supervision/detalles?periodo=semana_anterior')
        self.assertEqual(len(r.get_json()['detalles']), 19)

    def test_07_pdf(self):
        import dashboard_bp
        r = self.admin.post('/dashboard/api/supervision/cumplimiento/pdf',
                            json={'periodo': 'semana_anterior'}, headers={'Accept': 'application/pdf, application/json'})
        if dashboard_bp._WEASYPRINT_AVAILABLE:
            self.assertEqual(r.status_code, 200, r.data[:300])
            self.assertEqual(r.mimetype, 'application/pdf')
            self.assertTrue(r.data.startswith(b'%PDF'))
            self.assertIn(f"cumplimiento_supervisiones_{D['lunes']}_{D['domingo']}.pdf",
                          r.headers.get('Content-Disposition', ''))
            ev = eventos(accion='Generación de PDF de cumplimiento')
            self.assertTrue(ev, 'queda en Auditoría')
            self.assertEqual(ev[-1]['usuario_email'], ADMIN)
            self.assertEqual(ev[-1]['detalle']['periodo'],
                             {'clave': 'semana_anterior', 'etiqueta': 'Semana anterior', 'desde': D['lunes'].isoformat(),
                              'hasta': D['domingo'].isoformat(), 'recortado_inicio': None})
            self.assertEqual(ev[-1]['detalle']['filtros'], {'cliente': None, 'instalacion': None, 'texto': 'Todos los clientes'})
        else:
            print('\n[aviso] WeasyPrint no disponible en este proceso: el PDF responde 503 (correr con DYLD_FALLBACK_LIBRARY_PATH).')
            self.assertEqual(r.status_code, 503)
        with A.app.test_request_context():
            conn = dashboard_bp.get_db_connection()
            cur = conn.cursor(cursor_factory=dashboard_bp.psycopg2.extras.DictCursor)
            datos = dashboard_bp._cumplimiento_programacion(cur, D['lunes'], D['domingo'])
            html = dashboard_bp._cumplimiento_html(datos)
            un_dia = dashboard_bp._cumplimiento_programacion(cur, D['lunes'], D['lunes'], propiedad=str(D['propA2']))
            html_dia = dashboard_bp._cumplimiento_html(un_dia, filtros_txt=dashboard_bp._texto_filtros(cur, None, str(D['propA2'])))
            conn.close()
        for esperado in ('Cliente A', 'Cliente B', 'Cliente C', '>86 %<', '>40 %<', '>100 %<', 'Total'):
            self.assertIn(esperado, html)
        self.assertNotIn('prorrateado</span>', html, 'una semana exacta no se prorratea')
        for esperado in ('prorrateado', 'sobre programación del cliente', 'Instalación A2'):
            self.assertIn(esperado, html_dia)
        r = self.coord.post('/dashboard/api/supervision/cumplimiento/pdf', json={'periodo': 'semana_anterior'},
                            headers={'Accept': 'application/json'})
        self.assertEqual(r.status_code, 403, 'sólo Administrador')

    def test_08_coordinadores_y_envio_por_correo(self):
        import contextlib
        import dashboard_bp
        r = self.admin.get('/dashboard/api/supervision/coordinadores')
        self.assertEqual(r.status_code, 200, r.data[:300])
        coords = {c['email']: c for c in r.get_json()['coordinadores']}
        self.assertEqual(set(coords), {COORD, COORD2})
        self.assertEqual(coords[COORD]['clientes'], [D['cliA']])
        self.assertEqual(coords[COORD]['ambito'], ['Cliente A'])
        self.assertTrue(coords[COORD2]['sin_ambito'])
        self.assertEqual(self.coord.get('/dashboard/api/supervision/coordinadores',
                                        headers={'Accept': 'application/json'}).status_code, 403)

        # Sin WeasyPrint en este proceso se simula el PDF para probar igual el flujo.
        class _PdfFalso:
            def __init__(self, string=''):
                self.string = string

            def write_pdf(self, buf):
                buf.write(b'%PDF-1.4 simulado\n' + self.string.encode('utf-8'))

        def parches():
            pila = contextlib.ExitStack()
            if not dashboard_bp._WEASYPRINT_AVAILABLE:
                pila.enter_context(mock.patch.object(dashboard_bp, '_WEASYPRINT_AVAILABLE', True))
                pila.enter_context(mock.patch.object(dashboard_bp, '_WeasyprintHTML', _PdfFalso))
            return pila

        enviados = []

        def falso_envio(to, asunto, cuerpo, is_html=False, cc_emails=None, attachments=None):
            enviados.append((to, asunto, cuerpo, is_html, attachments))
            return True

        with parches(), mock.patch('dashboard_bp.send_email', side_effect=falso_envio):
            r = self.admin.post('/dashboard/api/supervision/cumplimiento/email',
                                json={'periodo': 'semana_anterior', 'coordinadores': [D['coord'], D['coord2']],
                                      'mensaje': 'Favor revisar <B>'})
        self.assertEqual(r.status_code, 200, r.data[:300])
        d = r.get_json()
        self.assertEqual(d['enviados'], 1)
        estados = {x['email']: x for x in d['resultados']}
        nombre_pdf = f"cumplimiento_supervisiones_{D['lunes']}_{D['domingo']}_coordinador-pruebas.pdf"
        self.assertEqual(estados[COORD]['estado'], 'enviado')
        self.assertEqual((estados[COORD]['clientes'], estados[COORD]['pct'], estados[COORD]['pdf']), (1, 90, nombre_pdf))
        # Sin clientes asignados: ni PDF ni correo, y se informa.
        self.assertEqual((estados[COORD2]['estado'], estados[COORD2]['motivo']), ('omitido', 'Sin clientes asignados'))
        self.assertEqual(len(enviados), 1)
        to, asunto, cuerpo, is_html, adjuntos = enviados[0]
        self.assertEqual(to, COORD)
        self.assertTrue(is_html)
        self.assertIn('Cumplimiento de supervisiones', asunto)
        # Un PDF independiente por Coordinador, adjunto.
        self.assertEqual(len(adjuntos), 1)
        nombre, contenido, mimetype = adjuntos[0]
        self.assertEqual((nombre, mimetype), (nombre_pdf, 'application/pdf'))
        self.assertTrue(contenido.startswith(b'%PDF'))
        # Cuerpo: sólo su cliente, saludo, mensaje escapado, clientes asignados y aviso del adjunto.
        for esperado in ('Cliente A', 'Coordinador Pruebas', 'Favor revisar &lt;B&gt;', '>90 %<',
                         'Clientes asignados:', 'Se adjunta el PDF', nombre_pdf):
            self.assertIn(esperado, cuerpo)
        self.assertNotIn('Cliente B', cuerpo)
        self.assertNotIn('Cliente C', cuerpo)
        # Sección 4: el log registra quién, a quién, período y filtros.
        ev = eventos(accion='Envío de cumplimiento a Coordinadores')
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]['usuario_email'], ev[0]['estado']), (ADMIN, 'Exitoso'))
        det = ev[0]['detalle']
        self.assertEqual(det['enviados'], [COORD])
        self.assertEqual(det['omitidos'], [COORD2])
        self.assertEqual(det['periodo'], {'clave': 'semana_anterior', 'etiqueta': 'Semana anterior',
                                          'desde': D['lunes'].isoformat(), 'hasta': D['domingo'].isoformat(),
                                          'recortado_inicio': None})
        self.assertEqual(det['filtros'], {'cliente': None, 'instalacion': None, 'texto': 'Todos los clientes'})
        dest = {x['email']: x for x in det['destinatarios']}
        self.assertEqual((dest[COORD]['nombre'], dest[COORD]['estado'], dest[COORD]['pdf'], dest[COORD]['pct']),
                         ('Coordinador Pruebas', 'enviado', nombre_pdf, 90))
        self.assertEqual((dest[COORD2]['estado'], dest[COORD2]['motivo']), ('omitido', 'Sin clientes asignados'))
        self.assertEqual(det['mensaje'], 'Favor revisar <B>')
        self.assertIn('coordinadores', det, 'los ids del cuerpo los captura el catálogo')

        # El HTML del PDF del Coordinador lleva período, generación, clientes asignados y su total.
        with A.app.test_request_context():
            conn = dashboard_bp.get_db_connection()
            cur = conn.cursor(cursor_factory=dashboard_bp.psycopg2.extras.DictCursor)
            from coordinador import cargar_ambito
            datos = dashboard_bp._cumplimiento_programacion(cur, D['lunes'], D['domingo'], ambito=cargar_ambito(conn, COORD))
            html = dashboard_bp._cumplimiento_html(datos, clientes_asignados=['Cliente A'], coordinador='Coordinador Pruebas')
            conn.close()
        for esperado in ('Período:', 'Generado el', 'Clientes asignados:</strong> Cliente A', 'Coordinador:</strong> Coordinador Pruebas',
                         'Total del Coordinador', '>90 %<'):
            self.assertIn(esperado, html)

        # Filtro de Cliente ajeno al ámbito: no se genera ni envía, se informa.
        enviados.clear()
        with parches(), mock.patch('dashboard_bp.send_email', side_effect=falso_envio):
            r = self.admin.post('/dashboard/api/supervision/cumplimiento/email',
                                json={'periodo': 'semana_anterior', 'cliente': D['cliB'], 'coordinadores': [D['coord']]})
        self.assertEqual(r.status_code, 502)
        self.assertIn('fuera del filtro', r.get_json()['resultados'][0]['motivo'])
        self.assertEqual(enviados, [])
        ev = eventos(accion='Envío de cumplimiento a Coordinadores')[-1]
        self.assertEqual(ev['estado'], 'Error')
        self.assertEqual(ev['detalle']['filtros']['cliente'], {'id': D['cliB'], 'nombre': 'Cliente B'})
        self.assertEqual(ev['detalle']['filtros']['texto'], 'Cliente B')
        self.assertIn('fuera del filtro', ev['detalle']['destinatarios'][0]['motivo'])
        self.assertEqual(ev['detalle']['enviados'], [])

        # Con clientes asignados pero sin datos en el lapso sí recibe su PDF ("Sin datos").
        sql("INSERT INTO coordinador_ambito (user_id, customer_company_id, creado_por) VALUES (%s, %s, %s)",
            [D['coord2'], D['cliC'], ADMIN])
        try:
            with parches(), mock.patch('dashboard_bp.send_email', side_effect=falso_envio):
                r = self.admin.post('/dashboard/api/supervision/cumplimiento/email',
                                    json={'periodo': 'personalizado', 'desde': '2020-01-06', 'hasta': '2020-01-12',
                                          'coordinadores': [D['coord2']]})
            self.assertEqual(r.status_code, 200, r.data[:300])
            self.assertEqual(r.get_json()['resultados'][0]['estado'], 'enviado')
            self.assertIn('Sin datos', enviados[-1][2])
            self.assertEqual(len(enviados[-1][4]), 1)
        finally:
            sql("DELETE FROM coordinador_ambito WHERE user_id = %s", [D['coord2']])

        # Sin destinatarios → 400; correo caído → 502; sin WeasyPrint → 503 y nada enviado; Coordinador → 403.
        self.assertEqual(self.admin.post('/dashboard/api/supervision/cumplimiento/email',
                                         json={'periodo': 'semana_anterior', 'coordinadores': []}).status_code, 400)
        with parches(), mock.patch('dashboard_bp.send_email', return_value=False):
            r = self.admin.post('/dashboard/api/supervision/cumplimiento/email',
                                json={'periodo': 'semana_anterior', 'coordinadores': [D['coord']]})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json()['resultados'][0]['estado'], 'fallido')
        enviados.clear()
        with mock.patch.object(dashboard_bp, '_WEASYPRINT_AVAILABLE', False), \
             mock.patch('dashboard_bp.send_email', side_effect=falso_envio):
            r = self.admin.post('/dashboard/api/supervision/cumplimiento/email',
                                json={'periodo': 'semana_anterior', 'coordinadores': [D['coord']]})
        self.assertEqual((r.status_code, enviados), (503, []))
        self.assertEqual(self.coord.post('/dashboard/api/supervision/cumplimiento/email',
                                         json={'coordinadores': [D['coord']]},
                                         headers={'Accept': 'application/json'}).status_code, 403)

    def test_09_ambito_por_instalacion(self):
        # Coordinador con la instalación B1 (no el cliente B): fila de B con las realizadas
        # de su instalación y las programadas del cliente.
        sql("INSERT INTO coordinador_ambito (user_id, id_propiedad, creado_por) VALUES (%s, %s, %s)",
            [D['coord2'], D['propB1'], ADMIN])
        try:
            d = self.tabla(self.coord2, '?periodo=semana_anterior')
            self.assertEqual([f['cliente'] for f in d['filas']], ['Cliente B'])
            self.assertEqual(self.resumen(self.fila(d, 'Cliente B')), (5, 2, 2, 40, 'rojo'))
        finally:
            sql("DELETE FROM coordinador_ambito WHERE user_id = %s", [D['coord2']])

    def test_10_sin_datos_y_fecha_de_inicio(self):
        # Lapso sin ningún registro: programadas sí, cumplimiento "Sin datos", nada en rojo.
        d = self.tabla(self.admin, '?periodo=personalizado&desde=2020-01-06&hasta=2020-01-12')
        self.assertTrue(d['sin_datos'])
        self.assertEqual(sorted(f['cliente'] for f in d['filas']), ['Cliente A', 'Cliente B', 'Cliente C'])
        for f in d['filas']:
            self.assertEqual((f['estado'], f['realizadas'], f['pct'], f['tono']), ('sin_datos', 0, None, 'gris'))
        self.assertEqual(self.fila(d, 'Cliente A')['programadas'], 20)
        self.assertEqual((d['total']['estado'], d['total']['pct'], d['total']['programadas']), ('sin_datos', None, 35))
        # Un cliente en cero mientras los demás sí tienen datos es 0 %, no "Sin datos":
        # el martes sólo A y C tienen supervisiones (B sembró lunes y martes... B sí tiene);
        # el miércoles B no tiene ninguna.
        miercoles = D['lunes'] + timedelta(days=2)
        d = self.tabla(self.admin, f"?periodo=personalizado&desde={miercoles}&hasta={miercoles}")
        self.assertFalse(d['sin_datos'])
        self.assertEqual((self.fila(d, 'Cliente B')['estado'], self.fila(d, 'Cliente B')['pct'], self.fila(d, 'Cliente B')['tono']), ('ok', 0, 'rojo'))
        # Fecha de inicio de operación (Umbrales KPI) dentro del lapso: se recorta, como el gráfico.
        inicio = D['lunes'] + timedelta(days=2)
        sql("INSERT INTO kpi_thresholds (key, value, text_value) VALUES ('fecha_inicio_operacion', 0, %s) "
            "ON CONFLICT (key) DO UPDATE SET text_value = EXCLUDED.text_value", [inicio.isoformat()])
        try:
            d = self.tabla(self.admin, '?periodo=semana_anterior')
            self.assertEqual((d['desde'], d['recortado_inicio'], d['dias']), (inicio.isoformat(), inicio.isoformat(), 5))
            a = self.fila(d, 'Cliente A')
            # 20 × 5 / 7 = 14.3 → 14, prorrateado; realizadas de miércoles a domingo: A1 5 + A2 5 + A3 2.
            self.assertEqual((a['programadas'], a['prorrateado'], a['realizadas']), (14, True, 12))
            # Lapso íntegro anterior al inicio → sin días, "Sin datos".
            d = self.tabla(self.admin, f"?periodo=personalizado&desde={D['lunes']}&hasta={D['lunes']}")
            self.assertEqual((d['dias'], d['sin_datos'], d['filas']), (0, True, []))
        finally:
            sql("DELETE FROM kpi_thresholds WHERE key = 'fecha_inicio_operacion'")

    def test_11_sin_programacion_usa_la_meta_general(self):
        import dashboard_bp
        sql("DELETE FROM supervision_programacion")
        try:
            d = self.tabla(self.admin, '?periodo=semana_anterior')
            self.assertFalse(d['hay_programacion'])
            self.assertEqual(sorted(f['cliente'] for f in d['filas']), ['Cliente A', 'Cliente B', 'Cliente C'])
            for f in d['filas']:
                self.assertEqual((f['estado'], f['programadas'], f['contadas'], f['pct'], f['tono']),
                                 ('sin_programacion', None, None, None, 'gris'))
                self.assertEqual(f['realizadas'], REALIZADAS[f['cliente'][-1]])
            # Total contra la meta general de Umbrales KPI (25 diarias → 175 en 7 días), como el briefing.
            t = d['total']
            self.assertEqual((t['estado'], t['programadas'], t['realizadas'], t['contadas'], t['pct'], t['tono']),
                             ('meta_general', 175, 32, 32, 18, 'rojo'))
            self.assertEqual(t['meta_general'], {'meta': 25, 'periodicidad': 'diario'})
            # Con un cliente filtrado la meta general no aplica: no hay porcentaje.
            d = self.tabla(self.admin, f"?periodo=semana_anterior&cliente={D['cliA']}")
            self.assertEqual((d['total']['estado'], d['total']['pct'], d['total']['programadas']), ('sin_programacion', None, None))
            # Al Coordinador tampoco se le aplica la meta general.
            d = self.tabla(self.coord, '?periodo=semana_anterior')
            self.assertEqual(d['total']['estado'], 'sin_programacion')
            with A.app.test_request_context():
                conn = dashboard_bp.get_db_connection()
                cur = conn.cursor(cursor_factory=dashboard_bp.psycopg2.extras.DictCursor)
                html = dashboard_bp._cumplimiento_html(dashboard_bp._cumplimiento_programacion(cur, D['lunes'], D['domingo']))
                conn.close()
            self.assertIn('Sin programación', html)
            self.assertIn('meta general 25 por día', html)
            # Cliente sin programación y con otros programados: aparece, "Sin programación", fuera del Total.
            sql("INSERT INTO supervision_programacion (customer_company_id, periodicidad, meta) VALUES (%s, 'semanal', %s)",
                [D['cliA'], PROGRAMACION['A']])
            d = self.tabla(self.admin, '?periodo=semana_anterior')
            self.assertEqual([(f['cliente'], f['estado']) for f in d['filas']],
                             [('Cliente A', 'ok'), ('Cliente B', 'sin_programacion'), ('Cliente C', 'sin_programacion')])
            self.assertEqual(self.resumen(d['total']), (20, 18, 18, 90, 'verde'))
        finally:
            sql("DELETE FROM supervision_programacion")
            for letra in ('A', 'B', 'C'):
                sql("INSERT INTO supervision_programacion (customer_company_id, periodicidad, meta) VALUES (%s, 'semanal', %s)",
                    [D[f'cli{letra}'], PROGRAMACION[letra]])

    def test_12_prorrateo_y_rangos(self):
        import dashboard_bp as dbp
        from admin_bp import _DIAS_PERIODO
        self.assertEqual(_DIAS_PERIODO, {'diario': 1, 'semanal': 7, 'mensual': 30}, 'misma base que el gráfico')
        # Mensual con base 30, como el gráfico: 30 días exactos, 31 y 28 prorrateados.
        self.assertEqual(dbp._programadas_en_lapso(10, 'mensual', 30), (10, False))
        self.assertEqual(dbp._programadas_en_lapso(20, 'mensual', 31), (21, True))
        self.assertEqual(dbp._programadas_en_lapso(10, 'mensual', 28), (9, True))
        self.assertEqual(dbp._programadas_en_lapso(31, 'mensual', 5), (5, True))
        # Semanal: 30 días → 21.4 → 21 prorrateado; 14 días → exacto.
        self.assertEqual(dbp._programadas_en_lapso(5, 'semanal', 30), (21, True))
        self.assertEqual(dbp._programadas_en_lapso(5, 'semanal', 14), (10, False))
        # Diario: nunca se prorratea.
        self.assertEqual(dbp._programadas_en_lapso(3, 'diario', 7), (21, False))
        self.assertEqual(dbp._programadas_en_lapso(0, 'semanal', 7), (0, False))
        self.assertEqual(dbp._programadas_en_lapso(5, 'semanal', 0), (0, False))
        with A.app.test_request_context():
            d, h = dbp._rango_periodo('semana_anterior')
            self.assertEqual((d.weekday(), h.weekday(), (h - d).days), (0, 6, 6))
            self.assertLess(h, D['hoy'])
            d, h = dbp._rango_periodo('mes_anterior')
            self.assertEqual((d.day, h, (h + timedelta(days=1)).day), (1, D['hoy'].replace(day=1) - timedelta(days=1), 1))
            self.assertEqual(dbp._rango_periodo('personalizado', '2026-02-10', '2026-02-01'), (None, None), 'desde > hasta')
            self.assertEqual(dbp._rango_periodo(None, '2026-02-01', '2026-02-10'), (None, None), 'sin período no hay rango')

    def test_13_send_email_con_adjunto(self):
        # email_utils.send_email arma un multipart con el PDF; sin adjuntos el mensaje no cambia.
        import email_utils
        capturados = []

        class SMTPFalso:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def ehlo(self): pass
            def starttls(self, context=None): pass
            def login(self, usuario, clave): pass
            def send_message(self, msg, to_addrs=None): capturados.append((msg, to_addrs))

        claves = ('SENDER_EMAIL', 'SMTP_SERVER', 'SMTP_PORT', 'EMAIL_PASSWORD')
        previo = {k: A.app.config.get(k) for k in claves}
        A.app.config.update(SENDER_EMAIL='sekapp@pruebas.sekapp', SMTP_SERVER='smtp.pruebas', SMTP_PORT=587,
                            EMAIL_PASSWORD='clave')
        try:
            with A.app.app_context(), mock.patch.object(email_utils.smtplib, 'SMTP', SMTPFalso):
                ok = email_utils.send_email('destino@pruebas.sekapp', 'Asunto', '<b>hola</b>', is_html=True,
                                            attachments=[('informe.pdf', b'%PDF-1.4 x', 'application/pdf')])
                ok2 = email_utils.send_email('destino@pruebas.sekapp', 'Asunto', 'texto plano')
        finally:
            for k, v in previo.items():
                if v is None:
                    A.app.config.pop(k, None)
                else:
                    A.app.config[k] = v
        self.assertTrue(ok and ok2)
        msg, destinos = capturados[0]
        partes = [p for p in msg.walk() if not p.is_multipart()]
        self.assertEqual([p.get_content_type() for p in partes], ['text/html', 'application/pdf'])
        self.assertEqual(partes[1].get_filename(), 'informe.pdf')
        self.assertEqual(partes[1].get_payload(decode=True), b'%PDF-1.4 x')
        self.assertEqual(destinos, ['destino@pruebas.sekapp'])
        msg2, _ = capturados[1]
        self.assertEqual([p.get_content_type() for p in msg2.walk() if not p.is_multipart()], ['text/plain'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
