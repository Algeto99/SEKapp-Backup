# forms/app.py
import os
from flask import Flask, request, render_template, flash, redirect, url_for
from flask_jwt_extended import JWTManager, jwt_required, get_jwt_identity
import psycopg2
import psycopg2.extras

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY')
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'super-secret')
app.config['JWT_TOKEN_LOCATION'] = ['headers']

jwt = JWTManager(app)

def get_db_connection():
    return psycopg2.connect(os.environ.get('DATABASE_URL'))

@app.route('/')
@jwt_required()
def index():
    return redirect(url_for('show_report_form'))

@app.route('/report_form')
@jwt_required()
def show_report_form():
    current_user = get_jwt_identity()
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT id_tipo_incidencia AS id, nombre FROM tipo_incidencia ORDER BY nombre")
        tipo_incidencia = cur.fetchall()
        cur.close()
        conn.close()
        return render_template('form.html',
                               tipo_incidencia=tipo_incidencia,
                               username=current_user)
    except Exception as e:
        flash(f"Error: {str(e)}", 'danger')
        return render_template('form.html', tipo_incidencia=[], username=current_user)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8081)
