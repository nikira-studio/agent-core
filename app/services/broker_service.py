import os
import secrets
import hashlib
from app.database import get_db
from app.config import settings


BROKER_NAME = "agent-core-broker"
BROKER_CREDENTIAL_FILE = "broker.credential"


def _get_broker_credential_path() -> str:
    return os.path.join(settings.data_dir, BROKER_CREDENTIAL_FILE)


def ensure_broker_credential() -> str:
    existing = get_broker_credential_hash()
    if existing:
        return existing

    plaintext = f"ac_broker_{secrets.token_urlsafe(32)}"
    cred_hash = hashlib.sha256(plaintext.encode()).hexdigest()

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO broker_credentials (id, name, credential_hash, is_active) VALUES (?, ?, ?, 1)",
            (secrets.token_urlsafe(16), BROKER_NAME, cred_hash),
        )
        conn.commit()

    os.makedirs(settings.data_dir, exist_ok=True)
    with open(_get_broker_credential_path(), "w") as f:
        f.write(plaintext)
    os.chmod(_get_broker_credential_path(), 0o600)

    return cred_hash


def get_broker_credential_hash() -> str | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT credential_hash FROM broker_credentials WHERE name = ? AND is_active = 1",
            (BROKER_NAME,),
        ).fetchone()
        return row["credential_hash"] if row else None


def verify_broker_credential(plaintext: str) -> bool:
    stored_hash = get_broker_credential_hash()
    if not stored_hash:
        return False
    expected = hashlib.sha256(plaintext.encode()).hexdigest()
    return secrets.compare_digest(expected, stored_hash)


def rotate_broker_credential() -> str:
    with get_db() as conn:
        conn.execute(
            "UPDATE broker_credentials SET is_active = 0, rotated_at = CURRENT_TIMESTAMP WHERE name = ?",
            (BROKER_NAME,),
        )
        conn.commit()

    plaintext = f"ac_broker_{secrets.token_urlsafe(32)}"
    cred_hash = hashlib.sha256(plaintext.encode()).hexdigest()

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO broker_credentials (id, name, credential_hash, is_active) VALUES (?, ?, ?, 1)",
            (secrets.token_urlsafe(16), BROKER_NAME, cred_hash),
        )
        conn.commit()

    path = _get_broker_credential_path()
    os.makedirs(settings.data_dir, exist_ok=True)
    with open(path, "w") as f:
        f.write(plaintext)
    os.chmod(path, 0o600)

    return plaintext