"""Hallazgo asignado: "Cliente / Instalación" es la del registro de origen.

Pedido del cliente (2026-10-08): el correo de hallazgo asignado sobre una
supervisión de puesto mostraba bajo "Cliente / Instalación" el nombre del
supervisor que hizo el reporte (cgeo_bp._fetch_record_details devolvía
`supervisor` como cliente). Debe ser la instalación del registro y, si no tiene
nombre, la propiedad asociada. La vista web del hallazgo usa el mismo helper.

Usa el arranque común de tests/sekapp_testing.py (Postgres desechable). Correr con:
    monolith/venv/bin/python tests/test_hallazgo_instalacion.py
"""
import unittest

from sekapp_testing import A, sql, configurar_app, recrear_base, crear_empresa, crear_usuario, login

ADMIN = 'admin@pruebas.sekapp'
SUP = 'sup@pruebas.sekapp'
D = {}


def setUpModule():
    configurar_app()
    recrear_base()
    cid = crear_empresa()
    crear_usuario(ADMIN, 'Admin Pruebas', cid, is_admin=True, is_super_admin=True)
    D['sup_id'] = crear_usuario(SUP, 'Supervisor Pruebas', cid)
    p = sql("INSERT INTO propiedades (nombre, activa) VALUES ('Propiedad 1', TRUE) RETURNING id_propiedad",
            uno=True)['id_propiedad']
    D['con_nombre'] = sql(
        "INSERT INTO supervision_puesto (supervisor, submitted_by_email, cliente_instalacion, id_propiedad, "
        "fecha_hora, creado_en, observaciones_novedades) "
        "VALUES ('LINZ DORIA', %s, 'P.H. LOS OLIVOS', %s, NOW(), NOW(), 'El guarda no es puntual') "
        "RETURNING id_supervision", [ADMIN, p], uno=True)['id_supervision']
    D['sin_nombre'] = sql(
        "INSERT INTO supervision_puesto (supervisor, submitted_by_email, cliente_instalacion, id_propiedad, "
        "fecha_hora, creado_en) VALUES ('LINZ DORIA', %s, '  ', %s, NOW(), NOW()) RETURNING id_supervision",
        [ADMIN, p], uno=True)['id_supervision']


class HallazgoInstalacionTests(unittest.TestCase):
    admin = sup = None

    @classmethod
    def setUpClass(cls):
        cls.admin = A.app.test_client()
        assert login(cls.admin, ADMIN).status_code == 302
        cls.sup = A.app.test_client()
        assert login(cls.sup, SUP).status_code == 302

    def test_01_el_detalle_toma_la_instalacion_del_registro(self):
        import cgeo_bp as C
        with A.app.test_request_context():
            d = C._fetch_record_details('supervision_puesto', D['con_nombre'])
            self.assertEqual(d['cliente'], 'P.H. LOS OLIVOS')
            self.assertEqual(d['descripcion'], 'El guarda no es puntual')
            self.assertNotIn('LINZ DORIA', str(d), 'el supervisor no es el cliente')
            d = C._fetch_record_details('supervision_puesto', D['sin_nombre'])
            self.assertEqual(d['cliente'], 'Propiedad 1', 'sin nombre de instalación, el de la propiedad')

    def test_02_el_correo_y_la_vista_muestran_la_instalacion(self):
        import cgeo_bp as C
        import email_utils as E
        enviados = []

        def captura(*a, **k):
            enviados.append((a, k))
            return True
        tenia = hasattr(C, 'send_email')
        orig_c, orig_e = getattr(C, 'send_email', None), E.send_email
        C.send_email = captura
        E.send_email = captura
        try:
            r = self.admin.post('/cgeo/api/asignar-hallazgo', json={
                'form_type': 'supervision_puesto', 'record_id': D['con_nombre'], 'asignado_a': D['sup_id'],
                'fecha_limite': '2026-10-31', 'nota': '',
                'hallazgo_titulo': 'Arma AR-003 (Revolver) en "Propiedad 1": mantenimiento vencido hace 62 días'})
        finally:
            E.send_email = orig_e
            if tenia:
                C.send_email = orig_c
            else:
                del C.send_email
        self.assertEqual(r.status_code, 200, r.data[:300])
        self.assertEqual(len(enviados), 1, 'un correo al responsable')
        cuerpo = ' '.join(str(x) for x in enviados[0][0]) + ' ' + ' '.join(str(v) for v in enviados[0][1].values())
        self.assertIn('Cliente / Instalaci', cuerpo)
        self.assertIn('P.H. LOS OLIVOS', cuerpo)
        self.assertNotIn('LINZ DORIA', cuerpo, 'el supervisor ya no aparece como cliente')

        asig = r.get_json()['assignment_id']
        html = self.sup.get(f'/cgeo/hallazgo/{asig}').get_data(as_text=True)
        self.assertIn('P.H. LOS OLIVOS', html)
        self.assertNotIn('LINZ DORIA', html)


if __name__ == '__main__':
    unittest.main(verbosity=2)
