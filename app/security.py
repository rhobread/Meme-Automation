from cryptography.fernet import Fernet

from app.config import get_settings


def encrypt_token(token: str) -> bytes:
    return Fernet(get_settings().token_encryption_key.get_secret_value().encode()).encrypt(token.encode())


def decrypt_token(ciphertext: bytes) -> str:
    return Fernet(get_settings().token_encryption_key.get_secret_value().encode()).decrypt(ciphertext).decode()
