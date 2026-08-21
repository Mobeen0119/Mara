import hashlib
import os
import secrets


def new_token():
    return secrets.token_hex(24)


def hash_password(password):
    salt = os.environ.get("PASSWORD_SALT", "mara-static-salt")
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def verify_password(password, password_hash):
    return hash_password(password) == password_hash
