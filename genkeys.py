import secrets

# Generate FLASK_SECRET_KEY for Forms Service (unique)
forms_flask_key = secrets.token_urlsafe(32)
print(f"Forms FLASK_SECRET_KEY: {forms_flask_key}")

# Generate FLASK_SECRET_KEY for Dashboard Service (unique)
dashboard_flask_key = secrets.token_urlsafe(32)
print(f"Dashboard FLASK_SECRET_KEY: {dashboard_flask_key}")

# Generate FLASK_SECRET_KEY for Login Service (unique)
login_flask_key = secrets.token_urlsafe(32)
print(f"Login FLASK_SECRET_KEY: {login_flask_key}")

# Generate JWT_SECRET_KEY (MUST be the same for all three services)
jwt_key = secrets.token_hex(32)
print(f"COMMON JWT_SECRET_KEY: {jwt_key}")
