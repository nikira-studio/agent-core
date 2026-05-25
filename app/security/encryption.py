import os
import json
from typing import Optional

from cryptography.fernet import Fernet, MultiFernet

from app.branding import ENV_PREFIX
from app.config import settings


_fernet: Optional[MultiFernet] = None
_keyring: Optional[list[bytes]] = None


def _load_or_generate_key() -> bytes:
    env_key = settings.ENCRYPTION_KEY
    if env_key and env_key.lower() != "auto":
        try:
            key_bytes = env_key.encode()
            if len(key_bytes) != 44:
                raise ValueError(
                    f"ENCRYPTION_KEY must be 44 bytes (got {len(key_bytes)})"
                )
            Fernet(key_bytes)
            return key_bytes
        except ValueError as ex:
            raise ValueError(
                f"{ENV_PREFIX}ENCRYPTION_KEY is not a valid Fernet key: {ex}"
            )

    key_path = settings.credential_key_path
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            key = f.read()
        if len(key) != 44:
            raise ValueError("credential.key must be 44 bytes (Fernet base64)")
        return key

    os.makedirs(settings.data_dir, exist_ok=True)
    key = Fernet.generate_key()
    with open(key_path, "wb") as f:
        f.write(key)
    os.chmod(key_path, 0o600)
    return key


def _load_keyring() -> list[bytes]:
    keyring_path = settings.data_dir / "credential.keyring"
    if not os.path.exists(keyring_path):
        primary = _load_or_generate_key()
        _save_keyring([primary])
        return [primary]
    with open(keyring_path, "rb") as f:
        data = json.load(f)
    keys = []
    for key in data["keys"]:
        key_bytes = key.encode() if isinstance(key, str) else bytes(key)
        Fernet(key_bytes)
        keys.append(key_bytes)
    if not keys:
        raise ValueError("credential.keyring must contain at least one key")
    return keys


def _save_keyring(keys: list[bytes]) -> None:
    keyring_path = settings.data_dir / "credential.keyring"
    backup_path = settings.data_dir / "credential.keyring.bak"
    if os.path.exists(keyring_path):
        with open(keyring_path, "rb") as f:
            prev = f.read()
        with open(backup_path, "wb") as f:
            f.write(prev)
    with open(keyring_path, "w", encoding="utf-8") as f:
        json.dump({"keys": [k.decode() for k in keys]}, f)
    os.chmod(keyring_path, 0o600)


def _build_fernet(keys: list[bytes]) -> MultiFernet:
    return MultiFernet([Fernet(k) for k in keys])


def get_fernet() -> MultiFernet:
    global _fernet, _keyring
    if _fernet is None:
        _keyring = _load_keyring()
        _fernet = _build_fernet(_keyring)
    return _fernet


def get_primary_key() -> bytes:
    global _keyring
    if _keyring is None:
        _keyring = _load_keyring()
    return _keyring[0]


def get_keyring() -> list[bytes]:
    global _keyring
    if _keyring is None:
        _keyring = _load_keyring()
    return list(_keyring)


def encrypt_value(plaintext: str) -> str:
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    return get_fernet().decrypt(ciphertext.encode()).decode()


def rotate_key() -> tuple[bytes, list[bytes]]:
    global _fernet, _keyring
    if _keyring is None:
        _keyring = _load_keyring()
    new_key = Fernet.generate_key()
    new_keyring = [new_key] + _keyring
    _keyring = new_keyring
    _fernet = _build_fernet(new_keyring)
    _save_keyring(new_keyring)
    key_path = settings.credential_key_path
    backup_path = str(key_path) + ".rotated.bak"
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            prev = f.read()
        with open(backup_path, "wb") as f:
            f.write(prev)
    with open(key_path, "wb") as f:
        f.write(new_key)
    os.chmod(key_path, 0o600)
    return new_key, new_keyring


def decrypt_with_key(ciphertext: str, key: bytes) -> Optional[str]:
    try:
        return Fernet(key).decrypt(ciphertext.encode()).decode()
    except Exception:
        return None
