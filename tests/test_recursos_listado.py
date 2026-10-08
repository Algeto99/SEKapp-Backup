"""Recursos y Confiabilidad → Resumen Operativo, y tarjeta "Equipos" del Briefing.

Pedidos del cliente del 2026-10-07 y 2026-10-08, todos con la misma causa: cada
registro (planilla pre-operacional, reporte de Confiabilidad de Equipos) es una
foto, y los indicadores sumaban fotos. Reglas que fija esta prueba:

- Vehículos y motos: la placa es la unidad y su última planilla del período
  decide si está apta (cgeo_bp._flota_vigente). Una placa con tres planillas no
  aptas es un vehículo; una que falló y luego pasó no cuenta ni sale en el
  listado. "Carros Aptos", el dónut y "Motos aptas" del Briefing siguen la
  misma regla.
- Equipos: último reporte por instalación y tipo
  (cgeo_bp._eq_inventario_vigente) en Recursos, Briefing y Semáforo Global; la
  tendencia mensual, el último de cada mes. El listado muestra sólo lo que ese
  último reporte deja en falla.
- Radios registrados: seriales distintos de supervision_puesto más las
  supervisiones sin serial, desglosados, y la tarjeta dice para cuántos hay
  estado capturado en Confiabilidad.
- La tarjeta "Equipos" del Briefing respeta el cliente pedido (y el ámbito del
  Coordinador, vía _add_scope).

Usa el arranque común de tests/sekapp_testing.py (Postgres desechable). Correr con:
    monolith/venv/bin/python tests/test_recursos_listado.py
"""
import json
import unittest

from sekapp_testing import A, sql, configurar_app, recrear_base, crear_empresa, crear_usuario, login

ADMIN = 'admin@pruebas.sekapp'
OLIVOS = '/cgeo/api/recursos-data?cliente=P.H.%20LOS%20OLIVOS'


def setUpModule():
    configurar_app()
    recrear_base()
    cid = crear_empresa()
    crear_usuario(ADMIN, 'Admin Pruebas', cid, is_admin=True, is_super_admin=True)

    # Planillas: EC2470 tres veces no apta (la más reciente en otra sede),
    # ET9541 dos, ET9660 una. EC2471 apta. ET9700 falló y luego pasó: su última
    # planilla la deja apta, así que ni cuenta ni sale en el listado.
    planillas = [
        ('EC2470', 1, 'P.H. LOS OLIVOS', 'No Funciona'),
        ('EC2470', 2, 'NO APLICA',       'No Funciona'),
        ('EC2470', 3, 'NO APLICA',       'No Funciona'),
        ('ET9541', 4, 'NO APLICA',       'No Funciona'),
        ('ET9541', 5, 'NO APLICA',       'No Funciona'),
        ('ET9660', 6, 'NO APLICA',       'No Funciona'),
        ('EC2471', 7, 'NO APLICA',       'Funciona'),
        ('ET9700', 8, 'NO APLICA',       'Funciona'),
        ('ET9700', 9, 'NO APLICA',       'No Funciona'),
    ]
    for placa, horas, cliente, luces in planillas:
        sql("INSERT INTO planilla_vehicular (cliente_instalacion, fecha_hora, placa_vehiculo, luces_delanteras) "
            "VALUES (%s, NOW() - (%s || ' hours')::interval, %s, %s)", [cliente, horas, placa, luces])

    # Motos: MOTO-1 apta en LOS OLIVOS; MOTO-2 falló y luego pasó; MOTO-3 no apta.
    from dashboard_bp import _FLEET_FAULT_VALUES
    falla = _FLEET_FAULT_VALUES[0]
    motos = [
        ('MOTO-1', 24, 'P.H. LOS OLIVOS', None),
        ('MOTO-2', 48, 'NO APLICA',       falla),
        ('MOTO-2', 24, 'NO APLICA',       None),
        ('MOTO-3', 24, 'NO APLICA',       falla),
    ]
    for placa, horas, cliente, neumaticos in motos:
        sql("INSERT INTO planilla_motocicletas (cliente_instalacion, fecha_hora, placa_motocicleta, estado_neumaticos) "
            "VALUES (%s, NOW() - (%s || ' hours')::interval, %s, %s)", [cliente, horas, placa, neumaticos])

    # Confiabilidad de Equipos. LOS OLIVOS: dos reportes en marzo (3 y luego 2
    # radios en falla) con cámaras sanas. OTRO: un reporte en abril. SANO: falló
    # el 1 de marzo y el 15 ya estaba todo operativo. Fechas fijas para que la
    # tendencia mensual sea determinista.
    def inventario(radios_total, radios_op, camaras=True):
        filas = [{"tipo_equipo": "Radios", "total_equipos": str(radios_total), "equipos_operativos": str(radios_op)}]
        if camaras:
            filas.append({"tipo_equipo": "Cámaras", "total_equipos": "10", "equipos_operativos": "10"})
        return json.dumps(filas)
    reportes = [
        ('P.H. LOS OLIVOS', '2026-03-05', inventario(5, 2)),
        ('P.H. LOS OLIVOS', '2026-03-20', inventario(5, 3)),
        ('P.H. OTRO',       '2026-04-02', inventario(4, 2)),
        ('P.H. SANO',       '2026-03-01', inventario(2, 0, camaras=False)),
        ('P.H. SANO',       '2026-03-15', inventario(2, 2, camaras=False)),
    ]
    for cliente, fecha, inv in reportes:
        sql("INSERT INTO confiabilidad_equipos (cliente_instalacion, fecha, inventario) "
            "VALUES (%s, %s::date, %s::jsonb)", [cliente, fecha, inv])

    # Supervisiones de LOS OLIVOS: el mismo radio escrito de dos formas y una
    # supervisión sin serial, que Bases de Datos lista como una unidad más.
    for serial in ('R-100', 'r 100', None):
        sql("INSERT INTO supervision_puesto (cliente_instalacion, supervisor, submitted_by_email, fecha_hora, "
            "radio_asignado_serial, equipamiento_completo) "
            "VALUES ('P.H. LOS OLIVOS', 'Sup Prueba', %s, NOW() - INTERVAL '3 hours', %s, '5')", [ADMIN, serial])


class RecursosListadoTests(unittest.TestCase):
    admin = None

    @classmethod
    def setUpClass(cls):
        cls.admin = A.app.test_client()
        assert login(cls.admin, ADMIN).status_code == 302

    def _json(self, ruta='/cgeo/api/recursos-data'):
        r = self.admin.get(ruta)
        self.assertEqual(r.status_code, 200, (ruta, r.data[:300]))
        return r.get_json()

    def _alertas(self):
        return self._json()['alertas']

    # ── Vehículos ────────────────────────────────────────────────────────────
    def test_01_cada_placa_sale_una_sola_vez(self):
        vehiculos = [a for a in self._alertas()['listado'] if a['tipo'] == 'Vehículo']
        self.assertEqual(sorted(a['elemento'] for a in vehiculos), ['EC2470', 'ET9541', 'ET9660'],
                         'una fila por placa cuya última planilla es no apta; EC2471 y ET9700 están aptas')
        self.assertTrue(all(a['estado'] == 'No apto' for a in vehiculos))

    def test_02_se_conserva_la_planilla_mas_reciente(self):
        vehiculos = [a for a in self._alertas()['listado'] if a['tipo'] == 'Vehículo']
        ec2470 = next(a for a in vehiculos if a['elemento'] == 'EC2470')
        self.assertEqual(ec2470['cliente'], 'P.H. LOS OLIVOS',
                         'la fila de EC2470 es su planilla más reciente, no la primera')
        self.assertEqual([a['elemento'] for a in vehiculos], ['EC2470', 'ET9541', 'ET9660'],
                         'el listado sigue ordenado del más reciente al más antiguo')

    def test_03_la_tarjeta_y_el_porcentaje_cuentan_placas(self):
        d = self._json()
        self.assertEqual(d['alertas']['vehiculos_no_aptos'], 3,
                         'EC2470, ET9541 y ET9660 una vez cada uno, aunque sumen 6 planillas')
        self.assertIn('Revisar 3 vehículos no aptos.', d['acciones'])
        v = d['vehiculos']
        self.assertEqual((v['total'], v['aptos'], v['no_aptos'], v['mantenimiento']), (5, 2, 3, 0),
                         '5 placas; EC2471 y ET9700 aptas por su última planilla')
        self.assertEqual(d['vehiculos_carros']['pct'], 40.0)

    def test_04_las_motos_siguen_la_misma_regla(self):
        m = self._json()['vehiculos_motos']
        self.assertEqual((m['total'], m['aptos'], m['no_aptos']), (3, 2, 1),
                         'MOTO-2 falló y luego pasó: apta')
        self.assertEqual(self._json(OLIVOS)['vehiculos_motos']['total'], 1)

    # ── Equipos ──────────────────────────────────────────────────────────────
    def test_05_cada_radio_sale_una_vez_por_instalacion_y_solo_si_sigue_en_falla(self):
        equipos = [a for a in self._alertas()['listado'] if a['tipo'] == 'Equipo']
        self.assertEqual(sorted((a['elemento'], a['cliente']) for a in equipos),
                         [('Radios', 'P.H. LOS OLIVOS'), ('Radios', 'P.H. OTRO')],
                         'una fila por instalación y tipo; SANO ya está operativo y las cámaras sanas no salen')
        self.assertTrue(all(a['estado'] == 'Fuera de servicio' for a in equipos))
        self.assertEqual(self._alertas()['total'], 5, 'listado consolidado: 3 placas + 2 instalaciones')

    def test_06_los_equipos_cuentan_el_ultimo_reporte_por_instalacion(self):
        d = self._json()
        e = d['equipos']
        self.assertEqual((e['total'], e['operativos'], e['no_operativos']), (31, 27, 4),
                         'LOS OLIVOS por su último reporte (5 radios, 3 op, 10 cámaras), '
                         'OTRO (4, 2, 10) y SANO por el del 15 de marzo (2, 2)')
        self.assertEqual(d['alertas']['equipos_no_op'], 4)
        self.assertEqual((d['radios']['total'], d['radios']['operativos'], d['radios']['pct']), (11, 7, 63.6))
        por_tipo = {t['tipo']: (t['total'], t['operativos']) for t in d['equipos_por_tipo']}
        self.assertEqual(por_tipo, {'radios': (11, 7), 'cámaras': (20, 20)})
        self.assertEqual(d['tendencia_eq'],
                         [{'label': '2026-03', 'pct': 88.2}, {'label': '2026-04', 'pct': 85.7}],
                         'por mes también cuenta el último reporte de cada instalación')

    def test_07_el_periodo_y_el_cliente_acotan_el_ultimo_reporte(self):
        e = self._json(OLIVOS)['equipos']
        self.assertEqual((e['total'], e['no_operativos']), (15, 2))
        e = self._json('/cgeo/api/recursos-data?start_date=2026-03-01&end_date=2026-03-10')['equipos']
        self.assertEqual((e['total'], e['no_operativos']), (17, 5),
                         'dentro del período mandan los reportes del 5 (LOS OLIVOS) y del 1 (SANO) de marzo')

    # ── Radios registrados y Briefing ────────────────────────────────────────
    def test_08_registrados_desglosa_con_y_sin_serial(self):
        r = self._json()['radios']
        self.assertEqual((r['registrados'], r['registrados_con_serial'], r['registrados_sin_serial']), (2, 1, 1),
                         'R-100 y "r 100" son el mismo serial; la supervisión sin serial cuenta aparte')
        self.assertEqual(self._json(OLIVOS)['radios']['registrados'], 2)

    def test_09_briefing_y_semaforo_cuentan_igual_que_recursos(self):
        d = self._json()
        k = self._json('/cgeo/api/morning-briefing-data')['kpis']
        self.assertEqual(k['eq_por_tipo']['radios'], {'total': 11, 'operativos': 7})
        self.assertEqual((k['eq_total'], k['eq_no_op']), (d['equipos']['total'], d['equipos']['no_operativos']))
        self.assertEqual((k['moto_total'], k['moto_aptas']), (3, 2))
        self.assertEqual((k['radios_registrados'], k['radios_registrados_con_serial'],
                          k['radios_registrados_sin_serial']), (2, 1, 1))
        s = self._json('/cgeo/api/semaforo-global')
        self.assertEqual((s['eq_total'], s['eq_no_op']), (d['equipos']['total'], d['equipos']['no_operativos']))

    def test_10_la_tarjeta_equipos_del_briefing_respeta_el_cliente(self):
        k = self._json('/cgeo/api/morning-briefing-data?cliente=P.H.%20LOS%20OLIVOS')['kpis']
        self.assertEqual((k['eq_total'], k['eq_no_op']), (15, 2))
        self.assertEqual(k['eq_por_tipo']['radios'], {'total': 5, 'operativos': 3})
        self.assertEqual((k['moto_total'], k['moto_aptas']), (1, 1))
        self.assertEqual(k['radios_registrados'], 2)
        k = self._json('/cgeo/api/morning-briefing-data?cliente=P.H.%20OTRO')['kpis']
        self.assertEqual((k['eq_total'], k['moto_total'], k['radios_registrados']), (14, 0, 0))


if __name__ == '__main__':
    unittest.main(verbosity=2)
