# forms/app.py
import os
from flask import Flask, render_template, request, redirect, url_for
import psycopg2
import json
from datetime import datetime

app = Flask(__name__)

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

@app.route('/form', methods=['GET'])
def show_form():
    """
    Renders the basic data submission form.
    """
    return render_template('form.html')

@app.route('/submit_form', methods=['POST'])
def submit_form():
    """
    Handles the submission of the form data.
    Inserts the data into the PostgreSQL database.
    """
    name = request.form.get('name')
    email = request.form.get('email')
    message = request.form.get('message')

    if not name or not email or not message:
        return "All fields are required!", 400

    conn = None
    try:
        conn = get_db_connection()
        if conn is None:
            return "Failed to connect to database.", 500

        cur = conn.cursor()

        # Create table if it doesn't exist
        # Using TEXT for simplicity for name, email, message.
        # JSONB is great for flexible data.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS form_submissions (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                email VARCHAR(255) NOT NULL,
                message TEXT NOT NULL,
                submission_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Insert data into the table
        cur.execute(
            "INSERT INTO form_submissions (name, email, message) VALUES (%s, %s, %s)",
            (name, email, message)
        )
        conn.commit()
        cur.close()
        return render_template('success.html', message="Form submitted successfully!")
    except psycopg2.Error as e:
        print(f"Database error: {e}")
        if conn:
            conn.rollback() # Rollback in case of error
        return f"An error occurred while submitting the form: {e}", 500
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return f"An unexpected error occurred: {e}", 500
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    # This is for local development only.
    # Cloud Run will run the app using Gunicorn or similar.
    app.run(host='0.0.0.0', port=8080, debug=True)
