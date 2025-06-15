# landing_page_service/app.py
from flask import Flask, render_template

app = Flask(__name__)

@app.route('/')
def index():
    """
    Renders the main landing page with links to different forms.
    """
    return render_template('index.html')

if __name__ == '__main__':
    # This is for local development only.
    # Cloud Run will run the app using Gunicorn or similar.
    app.run(host='0.0.0.0', port=8080, debug=True)

