"""Recursos y Confiabilidad → Resumen Operativo → "Elementos que Requieren Atención".

Pedido del cliente (2026-10-07): el listado repetía la misma placa tantas veces
como planillas no aptas tuviera, y los mismos radios "Fuera de servicio" tantas
veces como reportes de Confiabilidad de Equipos hubiera. Cada elemento debe
salir una sola vez, con un único registro (el más reciente), sin tocar los
datos históricos.

Segundo pedido (2026-10-08): la tarjeta "Vehículos No Aptos" marcaba 43 porque
sumaba planillas no aptas, una por día por el mismo vehículo. Ahora cuenta
vehículos distintos con alguna planilla no apta en el período, con la misma
identidad (placa) que el listado. El porcentaje "Carros Aptos", el dónut y el
Dashboard de Vehículos siguen midiendo inspecciones.

Tercer pedido (2026-10-08): "Equipos No Operativos" marcaba 6 radios en P.H.
Los Olivos, que tiene 3. Cada reporte de Confiabilidad de Equipos es una foto
del parque y se sumaban todos los reportes del período. Ahora Recursos, el
Morning Briefing y el Semáforo Global cuentan el último reporte por
instalación y tipo (cgeo_bp._eq_inventario_vigente); la tendencia mensual, el
último de cada mes.

Usa el arranque común de tests/sekapp_testing.py (Postgres desechable). Correr con:
    monolith/venv/bin/python tests/test_recursos_listado.py
"""
import json
import unittest

from sekapp_testing import A, sql, configurar_app, recrear_base, crear_empresa, crear_usuario, login

ADMIN = 'admin@pruebas.sekapp'


def setUpModule():
    configurar_app()
    recrear_base()
    cid = crear_empresa()
    crear_usuario(ADMIN, 'Admin Pruebas', cid, is_admin=True, is_super_admin=True)

    # Planillas no aptas: EC2470 tres veces (la más reciente en otra sede),
    # ET9541 dos veces, ET9660 una vez. EC2471 está apta y no debe salir.
    planillas = [
        ('EC2470', 1, 'P.H. LOS OLIVOS', 'No Funciona'),
        ('EC2470', 2, 'NO APLICA',       'No Funciona'),
        ('EC2470', 3, 'NO APLICA',       'No Funciona'),
        ('ET9541', 4, 'NO APLICA',       'No Funciona'),
        ('ET9541', 5, 'NO APLICA',       'No Funciona'),
        ('ET9660', 6, 'NO APLICA',       'No Funciona'),
        ('EC2471', 7, 'NO APLICA',       'Funciona'),
    ]
    for placa, horas, cliente, luces in planillas:
        sql("INSERT INTO planilla_vehicular (cliente_instalacion, fecha_hora, placa_vehiculo, luces_delanteras) "
            "VALUES (%s, NOW() - (%s || ' hours')::interval, %s, %s)", [cliente, horas, placa, luces])

    # Confiabilidad de Equipos: dos reportes de la misma instalación con radios
    # en falla (3 y luego 2 unidades) y cámaras sanas; otra instalación con 2
    # radios en falla. En el listado: una fila de radios por instalación,
    # ninguna de cámaras. Fechas fijas para que la tendencia mensual sea
    # determinista: marzo tiene dos reportes de LOS OLIVOS, abril uno de OTRO.
    def inventario(radios_total, radios_op):
        return json.dumps([
            {"tipo_equipo": "Radios",  "total_equipos": str(radios_total), "equipos_operativos": str(radios_op)},
            {"tipo_equipo": "Cámaras", "total_equipos": "10", "equipos_operativos": "10"},
        ])
    reportes = [
        ('P.H. LOS OLIVOS', '2026-03-05', inventario(5, 2)),
        ('P.H. LOS OLIVOS', '2026-03-20', inventario(5, 3)),
        ('P.H. OTRO',       '2026-04-02', inventario(4, 2)),
    ]
    for cliente, fecha, inv in reportes:
        sql("INSERT INTO confiabilidad_equipos (cliente_instalacion, fecha, inventario) "
            "VALUES (%s, %s::date, %s::jsonb)", [cliente, fecha, inv])


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

    def test_01_cada_placa_sale_una_sola_vez(self):
        vehiculos = [a for a in self._alertas()['listado'] if a['tipo'] == 'Vehículo']
        placas = [a['elemento'] for a in vehiculos]
        self.assertEqual(sorted(placas), ['EC2470', 'ET9541', 'ET9660'],
                         'una fila por placa no apta; EC2471 está apta y no sale')
        self.assertTrue(all(a['estado'] == 'No apto' for a in vehiculos))

    def test_02_se_conserva_la_planilla_mas_reciente(self):
        vehiculos = [a for a in self._alertas()['listado'] if a['tipo'] == 'Vehículo']
        ec2470 = next(a for a in vehiculos if a['elemento'] == 'EC2470')
        self.assertEqual(ec2470['cliente'], 'P.H. LOS OLIVOS',
                         'la fila de EC2470 es su planilla más reciente, no la primera')
        self.assertEqual([a['elemento'] for a in vehiculos], ['EC2470', 'ET9541', 'ET9660'],
                         'el listado sigue ordenado del más reciente al más antiguo')

    def test_03_cada_radio_sale_una_vez_por_instalacion(self):
        equipos = [a for a in self._alertas()['listado'] if a['tipo'] == 'Equipo']
        self.assertEqual(sorted((a['elemento'], a['cliente']) for a in equipos),
                         [('Radios', 'P.H. LOS OLIVOS'), ('Radios', 'P.H. OTRO')],
                         'una fila por instalación y tipo; las cámaras sanas no salen')
        self.assertTrue(all(a['estado'] == 'Fuera de servicio' for a in equipos))

    def test_04_la_tarjeta_cuenta_vehiculos_unicos(self):
        d = self._json()
        a = d['alertas']
        self.assertEqual(a['vehiculos_no_aptos'], 3,
                         'EC2470, ET9541 y ET9660 una vez cada uno, aunque sumen 6 planillas')
        self.assertIn('Revisar 3 vehículos no aptos.', d['acciones'])
        self.assertEqual(a['equipos_no_op'], 4, '2 radios del último reporte de LOS OLIVOS + 2 de OTRO')
        self.assertEqual(a['total'], 5, 'el listado consolidado: 3 placas + 2 instalaciones')

    def test_05_el_porcentaje_sigue_midiendo_planillas(self):
        v = self._json()['vehiculos']
        self.assertEqual((v['total'], v['aptos'], v['no_aptos']), (7, 1, 6),
                         'el dónut y "Carros Aptos" miden inspecciones, no vehículos')

    def test_06_los_equipos_cuentan_el_ultimo_reporte_por_instalacion(self):
        d = self._json()
        e = d['equipos']
        self.assertEqual((e['total'], e['operativos'], e['no_operativos']), (29, 25, 4),
                         'LOS OLIVOS vale por su último reporte (5 radios, 3 op, 10 cámaras), '
                         'no por la suma de los dos; más OTRO (4 radios, 2 op, 10 cámaras)')
        self.assertEqual((d['radios']['total'], d['radios']['operativos']), (9, 5))
        por_tipo = {t['tipo']: (t['total'], t['operativos']) for t in d['equipos_por_tipo']}
        self.assertEqual(por_tipo, {'radios': (9, 5), 'cámaras': (20, 20)})
        self.assertEqual(d['tendencia_eq'],
                         [{'label': '2026-03', 'pct': 86.7}, {'label': '2026-04', 'pct': 85.7}],
                         'por mes también cuenta el último reporte: marzo es sólo el del día 20')

    def test_07_el_periodo_y_el_cliente_acotan_el_ultimo_reporte(self):
        e = self._json('/cgeo/api/recursos-data?cliente=P.H.%20LOS%20OLIVOS')['equipos']
        self.assertEqual((e['total'], e['no_operativos']), (15, 2))
        e = self._json('/cgeo/api/recursos-data?start_date=2026-03-01&end_date=2026-03-10')['equipos']
        self.assertEqual((e['total'], e['no_operativos']), (15, 3),
                         'dentro del período el último reporte es el del 5 de marzo, con 3 radios en falla')

    def test_08_briefing_y_semaforo_cuentan_igual_que_recursos(self):
        d = self._json()
        k = self._json('/cgeo/api/morning-briefing-data')['kpis']
        self.assertEqual(k['eq_por_tipo']['radios'], {'total': 9, 'operativos': 5})
        self.assertEqual((k['eq_total'], k['eq_no_op']), (d['equipos']['total'], d['equipos']['no_operativos']))
        s = self._json('/cgeo/api/semaforo-global')
        self.assertEqual((s['eq_total'], s['eq_no_op']), (d['equipos']['total'], d['equipos']['no_operativos']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
