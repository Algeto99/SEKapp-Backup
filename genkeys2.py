import secrets
landing_flask_key = secrets.token_urlsafe(32)
print(f"Landing FLASK_SECRET_KEY: {landing_flask_key}")
