# forms/app.py
import os
from flask import Flask, render_template, request, redirect, url_for, flash
import psycopg2
import psycopg2.extras # Needed for DictCursor
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_secret_key_for_dev') # Set a secret key for flash messages

# Database connection details from environment variables
DB_HOST = os.environ.get('DB_HOST')
DB_NAME = os.environ.get('DB_NAME')
DB_USER = os.environ.get('DB_USER')
DB_PASSWORD = os.environ.get('DB_PASSWORD')
DB_PORT = os.environ.get('DB_PORT', '5432') # Default PostgreSQL port

def get_db_connection():
    """
    Establishes and returns a connection to the PostgreSQL database.
    Retries connection for robustness.
    """
    conn = None
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            port=DB_PORT
        )
        print("Database connection successful.")
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}")
        # In a real application, you might implement retry logic or circuit breakers here.
        return None

@app.route('/')
def index():
    return redirect(url_for('show_report_form'))


@app.route('/report_form', methods=['GET'])
def show_report_form():
    """
    Renders the incident report form, populating dropdowns from the database.
    """
    conn = get_db_connection()
    if conn is None:
        flash('Error al conectar con la base de datos.', 'error')
        return render_template('form.html', tipos_incidencia=[], tipos_cliente=[], lugares_incidente=[], supervisores=[])

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # Fetch incident types
        cur.execute("SELECT id_tipo_incidencia AS id, nombre FROM tipo_incidencia ORDER BY nombre;")
        # FIX: Changed variable name from tipo_incidencia to tipos_incidencia to match the template variable
        tipos_incidencia = cur.fetchall()

        # Fetch client types
        cur.execute("SELECT id_tipo_cliente AS id, nombre FROM tipo_cliente ORDER BY nombre;")
        # FIX: Changed variable name from tipo_cliente to tipos_cliente to match the template variable
        tipos_cliente = cur.fetchall()

        # Fetch incident locations
        cur.execute("SELECT id_lugar_incidente AS id, nombre FROM lugar_incidente ORDER BY nombre;")
        # FIX: Changed variable name from lugar_incidente to lugares_incidente to match the template variable
        lugares_incidente = cur.fetchall()

        # Fetch supervisors
        cur.execute("SELECT id_supervisor AS id, nombre FROM supervisor ORDER BY nombre;")
        supervisores = cur.fetchall()

        cur.close()
        return render_template(
            'form.html',
            tipos_incidencia=tipos_incidencia, # This is now correctly defined
            tipos_cliente=tipos_cliente,       # This is now correctly defined
            lugares_incidente=lugares_incidente, # This is now correctly defined
            supervisores=supervisores
        )
    except psycopg2.Error as e:
        print(f"Database error fetching lookup data: {e}")
        flash(f"Error al cargar opciones del formulario: {e}", 'error')
        return render_template('form.html', tipos_incidencia=[], tipos_cliente=[], lugares_incidente=[], supervisores=[])
    finally:
        if conn:
            conn.close()

@app.route('/submit_report', methods=['POST'])
def submit_report():
    """
    Handles the submission of the incident report form data.
    Inserts the data into the PostgreSQL database.
    """
    # Extract form data
    id_tipo_incidencia = request.form.get('tipo_incidencia')
    id_tipo_cliente = request.form.get('tipo_cliente')
    id_lugar_incidente = request.form.get('lugar_incidente')
    descripcion_zona_comun = request.form.get('descripcion_zona_comun')
    fecha_incidente = request.form.get('fecha_incidente')
    hora_incidente = request.form.get('hora_incidente')
    descripcion_incidente = request.form.get('descripcion_incidente')
    valor_aproximado = request.form.get('valor_aproximado')
    pertenencias_sustraidas = request.form.get('pertenencias_sustraidas')
    nombre_persona = request.form.get('nombre_persona')
    telefono_persona = request.form.get('telefono_persona')
    numero_identidad_persona = request.form.get('numero_identidad_persona')
    numero_local = request.form.get('numero_local')
    direccion = request.form.get('direccion')
    imagenes_pdfs = request.form.get('imagenes_pdfs')
    id_supervisor = request.form.get('supervisor')

    # Basic validation (add more robust validation as needed)
    required_fields = {
        'tipo_incidencia': id_tipo_incidencia,
        'tipo_cliente': id_tipo_cliente,
        'lugar_incidente': id_lugar_incidente,
        'fecha_incidente': fecha_incidente,
        'hora_incidente': hora_incidente,
        'descripcion_incidente': descripcion_incidente,
        'nombre_persona': nombre_persona,
        'supervisor': id_supervisor
    }

    for field_name, value in required_fields.items():
        if not value:
            flash(f"El campo '{field_name.replace('_', ' ').capitalize()}' es requerido.", 'error')
            return redirect(url_for('show_report_form'))

    conn = None
    try:
        conn = get_db_connection()
        if conn is None:
            flash("Fallo al conectar con la base de datos.", 'error')
            return redirect(url_for('show_report_form'))

        cur = conn.cursor()

        # Convert empty strings to None for optional fields that can be NULL in DB
        descripcion_zona_comun = descripcion_zona_comun if descripcion_zona_comun else None
        valor_aproximado = float(valor_aproximado) if valor_aproximado else None
        pertenencias_sustraidas = pertenencias_sustraidas if pertenencias_sustraidas else None
        telefono_persona = telefono_persona if telefono_persona else None
        numero_identidad_persona = numero_identidad_persona if numero_identidad_persona else None
        numero_local = numero_local if numero_local else None
        direccion = direccion if direccion else None
        imagenes_pdfs = imagenes_pdfs if imagenes_pdfs else None

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
                id_tipo_incidencia, id_tipo_cliente, id_lugar_incidente,
                descripcion_zona_comun, fecha_incidente, hora_incidente,
                descripcion_incidente, valor_aproximado, pertenencias_sustraidas,
                nombre_persona, telefono_persona, numero_identidad_persona,
                numero_local, direccion, imagenes_pdfs, id_supervisor
            )
        )
        conn.commit()
        cur.close()
        flash('Reporte de incidencia enviado exitosamente!', 'success')
        return render_template('success.html', message="Reporte de incidencia enviado exitosamente!")
    except psycopg2.Error as e:
        print(f"Database error: {e}")
        if conn:
            conn.rollback() # Rollback in case of error
        flash(f"Ocurrió un error en la base de datos al enviar el reporte: {e}", 'error')
        return redirect(url_for('show_report_form'))
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        flash(f"Ocurrió un error inesperado: {e}", 'error')
        return redirect(url_for('show_report_form'))
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    # This is for local development only.
    # Cloud Run will run the app using Gunicorn or similar.
    app.run(host='0.0.0.0', port=8080, debug=True)