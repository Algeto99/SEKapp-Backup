# Secapp/forms/app.py
import os
from flask import Flask, render_template, request, redirect, url_for, flash
import psycopg2
import psycopg2.extras
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_key_for_forms_service')

# --- Database Connection ---
def get_db_connection():
    try:
        conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
        return conn
    except Exception as e:
        app.logger.error(f"Database connection error: {e}")
        flash('Error de conexión a la base de datos.', 'error')
        return None

# --- CORS Headers (optional, safe to keep) ---
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = os.environ.get('LANDING_SERVICE_URL', '*')
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

@app.route('/')
def index():
    return redirect(url_for('show_report_form'))

@app.route('/report_form', methods=['GET'])
def show_report_form():
    conn = get_db_connection()
    if conn is None:
        return redirect(os.environ.get('LOGIN_SERVICE_URL', '/') + '/login')

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT id_tipo_incidencia AS id, nombre FROM tipo_incidencia ORDER BY nombre;")
        tipo_incidencia_data = cur.fetchall()

        cur.execute("SELECT id_tipo_cliente AS id, nombre FROM tipo_cliente ORDER BY nombre;")
        tipo_cliente_data = cur.fetchall()

        cur.execute("SELECT id_lugar_incidente AS id, nombre FROM lugar_incidente ORDER BY nombre;")
        lugar_incidente_data = cur.fetchall()

        cur.execute("SELECT id_supervisor AS id, nombre FROM supervisor ORDER BY nombre;")
        supervisor_data = cur.fetchall()
        cur.close()

        return render_template(
            'form.html',
            tipo_incidencia=tipo_incidencia_data,
            tipo_cliente=tipo_cliente_data,
            lugar_incidente=lugar_incidente_data,
            supervisor=supervisor_data,
            login_service_url=os.environ.get('LOGIN_SERVICE_URL', '#'),
            dashboard_service_url=os.environ.get('DASHBOARD_SERVICE_URL', '#')
        )
    except Exception as e:
        app.logger.error(f"Error fetching form data: {e}")
        flash("Error al cargar el formulario.", 'danger')
        return redirect(url_for('index'))
    finally:
        if conn:
            conn.close()

@app.route('/submit_report', methods=['POST'])
def submit_report():
    data = request.form
    if not data:
        flash("No se recibió información del formulario.", 'error')
        return redirect(url_for('show_report_form'))

    required_fields = [
        'tipo_incidencia', 'tipo_cliente', 'lugar_incidente',
        'fecha_incidente', 'hora_incidente',
        'descripcion_incidente', 'nombre_persona', 'supervisor'
    ]
    for field in required_fields:
        if not data.get(field):
            flash(f"El campo '{field.replace('_', ' ').capitalize()}' es requerido.", 'error')
            return redirect(url_for('show_report_form'))

    try:
        conn = get_db_connection()
        if not conn:
            flash("Fallo al conectar con la base de datos.", 'error')
            return redirect(url_for('show_report_form'))

        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO reportes_incidentes (
                id_tipo_incidencia, id_tipo_cliente, id_lugar_incidente,
                descripcion_zona_comun, fecha_incidente, hora_incidente,
                descripcion_incidente, valor_aproximado, pertenencias_sustraidas,
                nombre_persona, telefono_persona, numero_identidad_persona,
                numero_local, direccion, imagenes_pdfs, id_supervisor
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                data.get('tipo_incidencia'), data.get('tipo_cliente'), data.get('lugar_incidente'),
                data.get('descripcion_zona_comun') or None,
                data.get('fecha_incidente'), data.get('hora_incidente'),
                data.get('descripcion_incidente'),
                float(data.get('valor_aproximado')) if data.get('valor_aproximado') else None,
                data.get('pertenencias_sustraidas') or None,
                data.get('nombre_persona'),
                data.get('telefono_persona') or None,
                data.get('numero_identidad_persona') or None,
                data.get('numero_local') or None,
                data.get('direccion') or None,
                data.get('imagenes_pdfs') or None,
                data.get('supervisor')
            )
        )
        conn.commit()
        cur.close()
        flash('Reporte enviado exitosamente.', 'success')
        return redirect(url_for('show_report_form'))
    except Exception as e:
        app.logger.error(f"Error submitting report: {e}")
        flash('Ocurrió un error al enviar el reporte.', 'error')
        return redirect(url_for('show_report_form'))
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    # --- Local Dev Defaults ---
    os.environ.setdefault('FLASK_SECRET_KEY', 'dev_flask_secret_key_for_forms')
    os.environ.setdefault('DATABASE_URL', 'postgresql://your_user:your_pass@localhost:5432/your_db')
    os.environ.setdefault('LOGIN_SERVICE_URL', 'http://localhost:8080')
    os.environ.setdefault('DASHBOARD_SERVICE_URL', 'http://localhost:5002')
    os.environ.setdefault('LANDING_SERVICE_URL', 'http://localhost:5000')

    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8081)), debug=True)
