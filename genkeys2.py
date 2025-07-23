import secrets
viewer_flask_key = secrets.token_urlsafe(32)
print(f"Viewer FLASK_SECRET_KEY: {viewer_flask_key}")
