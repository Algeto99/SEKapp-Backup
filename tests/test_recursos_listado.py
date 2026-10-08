"""Recursos y Confiabilidad → Resumen Operativo → "Elementos que Requieren Atención".

Pedido del cliente (2026-10-07): el listado repetía la misma placa tantas veces
como planillas no aptas tuviera, y los mismos radios "Fuera de servicio" tantas
veces como reportes de Confiabilidad de Equipos hubiera. Cada elemento debe
salir una sola vez, con un único registro (el más reciente), sin tocar los
datos históricos ni los contadores de las tarjetas, que siguen contando
registros y unidades.

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
    # en falla (3 y 2 unidades) y cámaras sanas; otra instalación con 2 radios
    # en falla. En el listado: una fila de radios por instalación, ninguna de cámaras.
    def inventario(radios_total, radios_op):
        return json.dumps([
            {"tipo_equipo": "Radios",  "total_equipos": str(radios_total), "equipos_operativos": str(radios_op)},
            {"tipo_equipo": "Cámaras", "total_equipos": "10", "equipos_operativos": "10"},
        ])
    reportes = [
        ('P.H. LOS OLIVOS', 2, inventario(5, 2)),
        ('P.H. LOS OLIVOS', 1, inventario(5, 3)),
        ('P.H. OTRO',       0, inventario(4, 2)),
    ]
    for cliente, dias, inv in reportes:
        sql("INSERT INTO confiabilidad_equipos (cliente_instalacion, fecha, inventario) "
            "VALUES (%s, CURRENT_DATE - %s, %s::jsonb)", [cliente, dias, inv])


class RecursosListadoTests(unittest.TestCase):
    admin = None

    @classmethod
    def setUpClass(cls):
        cls.admin = A.app.test_client()
        assert login(cls.admin, ADMIN).status_code == 302

    def _alertas(self):
        r = self.admin.get('/cgeo/api/recursos-data')
        self.assertEqual(r.status_code, 200, r.data[:300])
        return r.get_json()['alertas']

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

    def test_04_los_contadores_siguen_contando_registros(self):
        a = self._alertas()
        self.assertEqual(a['vehiculos_no_aptos'], 6, '3 + 2 + 1 planillas no aptas')
        self.assertEqual(a['equipos_no_op'], 7, '3 + 2 + 2 radios en falla')
        self.assertEqual(a['total'], 5, 'el listado consolidado: 3 placas + 2 instalaciones')


if __name__ == '__main__':
    unittest.main(verbosity=2)
