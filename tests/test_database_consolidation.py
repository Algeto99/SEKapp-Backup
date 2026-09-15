"""Exercise consolidation SQL with SQLite (only PostgreSQL text casts adapted)."""
import ast
from pathlib import Path
import sqlite3
import sys
import unittest

MONOLITH = Path(__file__).resolve().parents[1] / 'monolith'
sys.path.insert(0, str(MONOLITH))
from normalizacion import sql_clave_identificador  # noqa: E402

SOURCE = MONOLITH / 'dashboard_bp.py'
module = ast.parse(SOURCE.read_text())
helpers = ast.Module(body=[node for node in module.body
                          if isinstance(node, ast.FunctionDef)
                          and node.name in {'_bd_identifier_sql', '_bd_latest_cte'}],
                     type_ignores=[])
# Los helpers se extraen sueltos del blueprint, asi que hay que darles el unico
# nombre del modulo que usan. sql_clave_identificador se escribio con UPPER/TRIM/
# REPLACE justamente para que la misma expresion corra aqui sobre SQLite.
namespace = {'sql_clave_identificador': sql_clave_identificador}
exec(compile(helpers, str(SOURCE), 'exec'), namespace)


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE supervision_puesto (
            id_supervision INTEGER, company_id INTEGER, creado_en TEXT,
            serie_arma TEXT, radio_asignado_serial TEXT,
            documento_guardia TEXT, numero_empleado TEXT,
            nombre_guardia TEXT, cliente TEXT, score INTEGER)''')

    def tearDown(self):
        self.db.close()

    def add(self, id, company=1, created='2026-09-08', serial='ABC',
            doc='123', employee='001', name='Actual', client='Nuevo', score=5):
        self.db.execute('INSERT INTO supervision_puesto VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (id, company, created, serial, serial, doc, employee,
                         name, client, score))

    def rows(self, *keys, where=''):
        key = namespace['_bd_identifier_sql'](*keys)
        sql = namespace['_bd_latest_cte'](key, where, 'score')
        sql += ' SELECT * FROM ranked WHERE bd_rank = 1 ORDER BY id_supervision'
        return self.db.execute(sql.replace('::TEXT', '')).fetchall()

    def test_latest_complete_record_and_normalized_serials(self):
        self.add(1, created='2026-09-07', serial=' abc ', name='Anterior', score=1)
        self.add(2)
        for key in ('serie_arma', 'radio_asignado_serial'):
            rows = self.rows(key)
            self.assertEqual([r['id_supervision'] for r in rows], [2])
            self.assertEqual(rows[0]['nombre_guardia'], 'Actual')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM supervision_puesto').fetchone()[0], 2)

    def test_person_changes_do_not_duplicate_and_average_is_preserved(self):
        self.add(1, name='Anterior', client='Anterior', employee='002', score=1)
        self.add(2)
        rows = self.rows('documento_guardia', 'numero_empleado')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['numero_empleado'], '001')
        self.assertEqual(rows[0]['cliente'], 'Nuevo')
        self.assertEqual(rows[0]['avg_score'], 3)

    def test_company_boundaries_missing_ids_and_ties(self):
        self.add(1)
        self.add(2)
        self.add(3, company=2)
        self.add(4, serial=None)
        self.add(5, serial=' ')
        self.assertEqual([r['id_supervision'] for r in self.rows('serie_arma')], [2, 3, 4, 5])

    def test_employee_fallback_does_not_merge_by_name_or_cross_identifier_type(self):
        self.add(1, doc=None)
        self.add(2, doc=' ')
        self.add(3, doc='001')
        self.add(4, doc=None, employee=None)
        self.add(5, doc=None, employee=None)
        self.assertEqual([r['id_supervision'] for r in self.rows('documento_guardia', 'numero_empleado')], [2, 3, 4, 5])

    def test_latest_within_selected_filters(self):
        self.add(1, client='Anterior')
        self.add(2)
        rows = self.rows('serie_arma', where="WHERE cliente = 'Anterior'")
        self.assertEqual([r['id_supervision'] for r in rows], [1])


if __name__ == '__main__':
    unittest.main()
