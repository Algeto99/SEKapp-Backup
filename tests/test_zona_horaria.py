"""Zona horaria de la operación: "hoy" y los períodos salen de Umbrales KPI, no de UTC.

Reproduce el caso reportado por KANAN (1 de octubre de 2026): una supervisión
registrada a las 8:19 p. m. de Panamá no contaba en "Supervisiones hoy" ni en el
gráfico de 7 días, porque el servidor (date.today) y Postgres (CURRENT_DATE / NOW)
decidían "hoy" en UTC, donde ya era 2 de octubre. `fecha_hora` guarda el reloj de
pared de la operación, así que "hoy" tiene que ser el de esa misma zona.

Para que la prueba sea reproducible a cualquier hora, elige una zona cuya fecha
local AHORA no coincide con la fecha UTC, la configura en Umbrales KPI y siembra
una supervisión con el reloj de pared de "hoy local".

Usa el arranque común de tests/sekapp_testing.py (Postgres desechable). Correr con:
    monolith/venv/bin/python tests/test_zona_horaria.py
"""
import unittest
import zoneinfo
from datetime import datetime, time, timedelta, timezone

from sekapp_testing import A, sql, configurar_app, recrear_base, crear_empresa, crear_usuario, login

ADMIN = 'admin@pruebas.sekapp'
D = {}


def zona_con_fecha_distinta():
    """Por la mañana UTC, UTC-12 todavía está en ayer; por la tarde, UTC+14 ya está
    en mañana. (En nombres POSIX el signo va invertido: Etc/GMT+12 es UTC-12.)"""
    return 'Etc/GMT+12' if datetime.now(timezone.utc).hour < 12 else 'Etc/GMT-14'


def setUpModule():
    configurar_app()
    recrear_base()
    cid = crear_empresa()
    D['admin'] = crear_usuario(ADMIN, 'Admin Pruebas', cid, is_admin=True, is_super_admin=True)

    D['zona'] = zona_con_fecha_distinta()
    tz = zoneinfo.ZoneInfo(D['zona'])
    ahora_local = datetime.now(tz).replace(tzinfo=None)
    D['hoy_local'] = ahora_local.date()
    D['hoy_utc'] = datetime.now(timezone.utc).date()
    assert D['hoy_local'] != D['hoy_utc'], 'la zona elegida debe tener otra fecha que UTC'

    # Umbrales KPI: zona de la operación y meta diaria de 5 supervisiones.
    from admin_bp import get_thresholds
    get_thresholds()   # deja kpi_thresholds creada y con sus valores por defecto
    for key, value, text in (('zona_horaria', 0, D['zona']),
                             ('supervision_periodicidad', 0, 'diario'),
                             ('supervision_meta', 5, None)):
        sql("INSERT INTO kpi_thresholds (key, value, text_value) VALUES (%s, %s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, text_value = EXCLUDED.text_value",
            [key, value, text])

    c = sql("INSERT INTO customer_companies (company_id, name, code, is_active) "
            "VALUES (%s, 'Cliente Z', 'Z', TRUE) RETURNING id", [cid], uno=True)['id']
    # Z1: supervisada hoy (hora local), hace unos minutos o a primera hora del día.
    # Z2: supervisada ayer (hora local) a las 23:30 → tiene actividad reciente pero no hoy.
    hoy_0 = datetime.combine(D['hoy_local'], time.min)
    cuando = {'1': max(hoy_0, ahora_local - timedelta(minutes=10)),
              '2': datetime.combine(D['hoy_local'] - timedelta(days=1), time(23, 30))}
    for letra, fh in cuando.items():
        p = sql("INSERT INTO propiedades (nombre, activa, customer_company_id) "
                "VALUES (%s, TRUE, %s) RETURNING id_propiedad", [f'Instalación Z{letra}', c], uno=True)['id_propiedad']
        # Reloj de pared sin zona, exactamente como lo manda el formulario.
        D[f'sup{letra}'] = sql(
            "INSERT INTO supervision_puesto (cliente_instalacion, id_propiedad, customer_company_id, company_id, "
            "supervisor, submitted_by_email, fecha_hora) VALUES (%s, %s, %s, %s, 'Sup Prueba', %s, %s) "
            "RETURNING id_supervision",
            [f'Instalación Z{letra}', p, c, cid, ADMIN, fh.strftime('%Y-%m-%d %H:%M')], uno=True)['id_supervision']


class ZonaHorariaTests(unittest.TestCase):
    admin = None

    @classmethod
    def setUpClass(cls):
        cls.admin = A.app.test_client()
        assert login(cls.admin, ADMIN).status_code == 302

    def test_01_helpers_python_y_sql_coinciden(self):
        from admin_bp import hoy_operacion, ahora_operacion, sql_hoy, sql_ahora, _periodo_inicio_actual
        self.assertEqual(hoy_operacion(), D['hoy_local'])
        self.assertEqual(ahora_operacion().date(), D['hoy_local'])
        self.assertIsNone(ahora_operacion().tzinfo, 'reloj de pared sin zona, como fecha_hora')
        self.assertEqual(_periodo_inicio_actual('diario'), D['hoy_local'])
        self.assertEqual(_periodo_inicio_actual('mensual'), D['hoy_local'].replace(day=1))
        self.assertIn(f"'{D['zona']}'", sql_hoy())
        # Postgres llega a la misma fecha que Python, y es distinta de la UTC de su sesión.
        self.assertEqual(sql(f"SELECT {sql_hoy()} AS d", uno=True)['d'], D['hoy_local'])
        self.assertEqual(sql(f"SELECT ({sql_ahora()})::date AS d", uno=True)['d'], D['hoy_local'])
        self.assertEqual(sql("SELECT CURRENT_DATE AS d", uno=True)['d'], D['hoy_utc'],
                         'la sesión de Postgres sigue en UTC: la prueba sólo tiene sentido así')

    def test_02_briefing_cuenta_la_supervision_de_hoy(self):
        r = self.admin.get('/cgeo/api/morning-briefing-data')
        self.assertEqual(r.status_code, 200, r.data[:300])
        d = r.get_json()
        # Desde la unificación con la tabla (KANAN 2026-10-07) la ventana cierra en ayer y lo de
        # hoy va aparte: con periodicidad diaria nunca hay día cerrado, así que la de hoy local
        # aparece en "En curso" (y no la de ayer local a las 23:30, que es de otra ventana).
        self.assertEqual(d['kpis']['sup_en_curso'], 1, 'sólo la de hoy local')
        self.assertEqual((d['kpis']['sup_completadas'], d['kpis']['sup_programadas'], d['kpis']['sup_pct']), (0, 0, None))
        self.assertTrue(d['kpis']['sup_sin_dia_cerrado'])
        tendencia = d['tendencia_semana']
        self.assertEqual(tendencia[-1]['fecha'], D['hoy_local'].isoformat(), 'el gráfico termina en el hoy de la operación')
        self.assertEqual(tendencia[-1]['completadas'], 1)
        por_fecha = {t['fecha']: t['completadas'] for t in tendencia}
        self.assertEqual(por_fecha.get((D['hoy_local'] - timedelta(days=1)).isoformat()), 1, 'la de ayer local')
        if D['hoy_utc'] > D['hoy_local']:
            self.assertNotIn(D['hoy_utc'].isoformat(), por_fecha, 'el "mañana" de UTC no aparece en el gráfico')

    def test_03_alertas_sin_supervision_hoy(self):
        r = self.admin.get('/cgeo/api/alertas')
        self.assertEqual(r.status_code, 200, r.data[:300])
        ids = {a['id'] for a in r.get_json()['alertas']}
        self.assertNotIn('r3_Instalación Z1', ids, 'Z1 sí fue supervisada hoy en hora local')
        self.assertIn('r3_Instalación Z2', ids, 'Z2 tiene actividad reciente pero no hoy')
        self.assertFalse({'r1_Instalación Z1', 'r1_Instalación Z2'} & ids, 'ninguna lleva 48 h sin supervisión')

    def test_04_operacion_matrices_y_demas_endpoints(self):
        r = self.admin.get('/cgeo/api/operacion-data')
        self.assertEqual(r.status_code, 200, r.data[:300])
        self.assertEqual(r.get_json()['supervisiones']['hoy'], 1)
        r = self.admin.get('/matrices/api/stats')
        self.assertEqual(r.status_code, 200, r.data[:300])
        self.assertEqual(r.get_json()['mes_iso'], D['hoy_local'].strftime('%Y-%m'), 'el mes por defecto es el local')
        # El resto de pantallas con "hoy" responde: ninguna cadena SQL quedó sin interpolar.
        # (Una llave sin f-string llega a Postgres como texto → error de sintaxis → 500.)
        for ruta in ('/cgeo/api/semaforo-global', '/cgeo/api/recursos-data', '/cgeo/hallazgos',
                     '/dashboard/api/stats', '/dashboard/api/debug/thisweek',
                     '/dashboard/api/incidentes/data', '/dashboard/api/incidentes/detalles',
                     '/dashboard/api/visitas/data', '/dashboard/api/visitas/detalles',
                     '/dashboard/api/cumplimiento/data', '/dashboard/api/cumplimiento/detalles',
                     '/dashboard/api/supervision/data', '/dashboard/api/supervision/detalles',
                     '/dashboard/api/incidents/weekly', '/dashboard/api/incidents/monthly',
                     '/dashboard/api/incidents/yearly', '/dashboard/api/incidents/types'):
            self.assertEqual(self.admin.get(ruta).status_code, 200, ruta)


if __name__ == '__main__':
    unittest.main(verbosity=2)
