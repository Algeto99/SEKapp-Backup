# login/main.py
import os
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash
from flask_bcrypt import Bcrypt
from flask_jwt_extended import (
    create_access_token, create_refresh_token, JWTManager, jwt_required,
    get_jwt_identity
)
import psycopg2
from psycopg2 import extras
from datetime import timedelta

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY')
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'super-secret')
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=1)
app.config['JWT_TOKEN_LOCATION'] = ['headers']

jwt = JWTManager(app)
bcrypt = Bcrypt(app)

def get_db_connection():
    return psycopg2.connect(os.environ.get('DATABASE_URL'))

@app.route('/')
def home():
    return render_template('login.html')  # This form should post to /token

@app.route('/token', methods=['POST'])
def login():
    username = request.form.get('username')
    password = request.form.get('password')

    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=extras.DictCursor)
        cur.execute("SELECT username, password_hash FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if user and bcrypt.check_password_hash(user['password_hash'], password):
            access_token = create_access_token(identity=username)
            return jsonify(access_token=access_token), 200
        else:
            return jsonify(msg="Usuario o contraseña incorrectos."), 401
    except Exception as e:
        return jsonify(msg=f"Error: {str(e)}"), 500

@app.route('/protected')
@jwt_required()
def protected():
    return jsonify(msg=f"Hello, {get_jwt_identity()}! You are authenticated."), 200

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8080)
