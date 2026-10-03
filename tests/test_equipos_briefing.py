"""Tarjeta "Equipos" del Morning Briefing: cada cifra sale de su propia tabla.

Pedido de KANAN (2026-10-02): la tarjeta se llamaba "Radios" pero su detalle
hablaba de radios, armas y motos, y la cifra grande no decía qué contaba. Esta
prueba deja por escrito de dónde sale cada número y que cuadra con la pantalla
que lo origina:
- radios y armas registrados → supervision_puesto, contados como Bases de Datos
  (mismo identificador normalizado: "EG-4251" y "eg 4251" son el mismo radio);
- radios y armas operativos → inventario de Confiabilidad de Equipos;
- motos aptas → planilla de motocicletas, igual que Recursos.

Usa el arranque común de tests/sekapp_testing.py (Postgres desechable). Correr con:
    monolith/venv/bin/python tests/test_equipos_briefing.py
"""
import json
import unittest

from sekapp_testing import A, sql, configurar_app, recrear_base, crear_empresa, crear_usuario, login

ADMIN = 'admin@pruebas.sekapp'
D = {}


def setUpModule():
    configurar_app()
    recrear_base()
    cid = crear_empresa()
    D['admin'] = crear_usuario(ADMIN, 'Admin Pruebas', cid, is_admin=True, is_super_admin=True)
    c = sql("INSERT INTO customer_companies (company_id, name, code, is_active) "
            "VALUES (%s, 'Cliente E', 'E', TRUE) RETURNING id", [cid], uno=True)['id']
    p = sql("INSERT INTO propiedades (nombre, activa, customer_company_id) "
            "VALUES ('Instalación E1', TRUE, %s) RETURNING id_propiedad", [c], uno=True)['id_propiedad']

    # Supervisiones: el mismo radio escrito de dos formas, otro radio, y un arma
    # sin radio (equipamiento_completo vacío para que no cuente como radio sin serial).
    filas = [
        dict(radio='EG-4251', equipo='5', porta=None, arma=None),
        dict(radio='eg 4251', equipo='4', porta=None, arma=None),
        dict(radio='EG-9000', equipo='3', porta=None, arma=None),
        dict(radio=None,      equipo=None, porta='Si', arma='ARM-001'),
    ]
    for f in filas:
        sql("INSERT INTO supervision_puesto (cliente_instalacion, id_propiedad, customer_company_id, company_id, "
            "supervisor, submitted_by_email, fecha_hora, radio_asignado_serial, equipamiento_completo, porta_arma, serie_arma) "
            "VALUES ('Instalación E1', %s, %s, %s, 'Sup Prueba', %s, NOW() - INTERVAL '2 hours', %s, %s, %s, %s)",
            [p, c, cid, ADMIN, f['radio'], f['equipo'], f['porta'], f['arma']])

    # Inventario de Confiabilidad de Equipos: 3 radios (2 operativos) y 10 cámaras
    # que NO deben colarse bajo la etiqueta de radios.
    inventario = [
        {"tipo_equipo": "Radios",  "total_equipos": "3",  "equipos_operativos": "2"},
        {"tipo_equipo": "Cámaras", "total_equipos": "10", "equipos_operativos": "10"},
    ]
    sql("INSERT INTO confiabilidad_equipos (cliente_instalacion, inventario, company_id, customer_company_id) "
        "VALUES ('Instalación E1', %s::jsonb, %s, %s)", [json.dumps(inventario), cid, c])

    # Planilla de motocicletas: una apta y una con neumáticos en falla.
    from dashboard_bp import _FLEET_FAULT_VALUES
    sql("INSERT INTO planilla_motocicletas (cliente_instalacion, fecha_hora, placa_motocicleta) "
        "VALUES ('Instalación E1', NOW() - INTERVAL '1 day', 'MOTO-1')")
    sql("INSERT INTO planilla_motocicletas (cliente_instalacion, fecha_hora, placa_motocicleta, estado_neumaticos) "
        "VALUES ('Instalación E1', NOW() - INTERVAL '1 day', 'MOTO-2', %s)", [_FLEET_FAULT_VALUES[0]])


class EquiposBriefingTests(unittest.TestCase):
    admin = None

    @classmethod
    def setUpClass(cls):
        cls.admin = A.app.test_client()
        assert login(cls.admin, ADMIN).status_code == 302

    def _json(self, ruta):
        r = self.admin.get(ruta)
        self.assertEqual(r.status_code, 200, (ruta, r.data[:300]))
        return r.get_json()

    def test_01_briefing_trae_cada_cifra_de_su_tabla(self):
        k = self._json('/cgeo/api/morning-briefing-data')['kpis']
        self.assertEqual(k['radios_registrados'], 2, 'EG-4251 y eg 4251 son el mismo radio; más EG-9000')
        self.assertEqual(k['armas_registrados'], 1)
        self.assertEqual(k['eq_por_tipo']['radios'], {'total': 3, 'operativos': 2})
        self.assertNotIn('armas', k['eq_por_tipo'], 'sin inventario de armas: la tarjeta sólo puede decir cuántas hay')
        self.assertEqual((k['moto_total'], k['moto_aptas']), (2, 1))

    def test_02_cuadra_con_bases_de_datos(self):
        k = self._json('/cgeo/api/morning-briefing-data')['kpis']
        self.assertEqual(self._json('/dashboard/api/bases_de_datos/radios')['total'], k['radios_registrados'])
        self.assertEqual(self._json('/dashboard/api/bases_de_datos/armas')['total'], k['armas_registrados'])

    def test_03_cuadra_con_recursos(self):
        d = self._json('/cgeo/api/recursos-data')
        k = self._json('/cgeo/api/morning-briefing-data')['kpis']
        self.assertEqual(d['radios']['registrados'], k['radios_registrados'])
        self.assertEqual((d['radios']['total'], d['radios']['operativos']), (3, 2))
        self.assertEqual(d['armas']['registrados'], k['armas_registrados'])
        self.assertEqual(d['vehiculos_motos']['total'], k['moto_total'])
        self.assertEqual(d['vehiculos_motos']['pct'], 50.0)

    def test_04_la_tarjeta_se_llama_equipos_y_nombra_cada_valor(self):
        html = self.admin.get('/cgeo/morning-briefing/').get_data(as_text=True)
        self.assertIn("lbl: 'Equipos'", html)
        self.assertNotIn("lbl: 'Radios'", html)
        for texto in ('registrados en Bases de Datos', 'Radios operativos:', 'Armas registradas:', 'Motos aptas:'):
            self.assertIn(texto, html, texto)


if __name__ == '__main__':
    unittest.main(verbosity=2)
