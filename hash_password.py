# hash_password.py
from flask_bcrypt import Bcrypt
from flask import Flask # Flask app is needed to initialize Bcrypt

# Create a dummy Flask app for Bcrypt initialization
# It doesn't need to be running, just for object creation
app = Flask(__name__)
# The secret key for Bcrypt initialization doesn't need to match app.config['SECRET_KEY']
# for password hashing specifically, but it's good practice to set it.
app.config['SECRET_KEY'] = 'any_dummy_secret_key_for_bcrypt_init'
bcrypt = Bcrypt(app)

# --- Configuration ---
PASSWORD_TO_HASH = "Tzolkin1!" # <--- Use the EXACT password you want to log in with
# ---------------------

hashed_password = bcrypt.generate_password_hash(PASSWORD_TO_HASH).decode('utf-8')

print(f"Original Password: {PASSWORD_TO_HASH}")
print(f"Hashed Password (COPY THIS EXACTLY): {hashed_password}")

# Optional verification (run only after you copy the hash)
# is_valid = bcrypt.check_password_hash(hashed_password, PASSWORD_TO_HASH)
# print(f"Verification successful: {is_valid}")
