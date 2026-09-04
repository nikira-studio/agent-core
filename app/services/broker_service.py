import os
import secrets
import hashlib
import tempfile
from contextlib import contextmanager

import fcntl
from app.branding import APP_SLUG
from app.database import get_db
from app.config import settings


BROKER_NAME = f"{APP_SLUG}-broker"
BROKER_CREDENTIAL_FILE = "broker.credential"
BROKER_LOCK_FILE = ".broker.credential.lock"


def _get_broker_credential_path() -> str:
    return os.path.join(settings.data_dir, BROKER_CREDENTIAL_FILE)


def _credential_hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


@contextmanager
def _credential_lock():
    """Serialize the one shared credential file across threads and processes."""
    os.makedirs(settings.data_dir, exist_ok=True)
    lock_path = os.path.join(settings.data_dir, BROKER_LOCK_FILE)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _read_broker_credential() -> str | None:
    try:
        with open(_get_broker_credential_path()) as credential_file:
            plaintext = credential_file.read().strip()
    except FileNotFoundError:
        return None
    return plaintext or None


def _write_broker_credential(plaintext: str) -> None:
    """Replace the credential file atomically after durable staging."""
    os.makedirs(settings.data_dir, exist_ok=True)
    fd, staged_path = tempfile.mkstemp(prefix=".broker.", dir=settings.data_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as credential_file:
            fd = -1
            credential_file.write(plaintext)
            credential_file.flush()
            os.fsync(credential_file.fileno())
        os.replace(staged_path, _get_broker_credential_path())
        directory_fd = os.open(settings.data_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(staged_path)
        except FileNotFoundError:
            pass


def _activate_broker_credential(credential_hash: str) -> None:
    """Make exactly one database row active in one transaction."""
    with get_db() as conn:
        conn.execute(
            "UPDATE broker_credentials SET is_active = 0, "
            "rotated_at = CURRENT_TIMESTAMP WHERE name = ? AND is_active = 1",
            (BROKER_NAME,),
        )
        row = conn.execute(
            "SELECT id FROM broker_credentials WHERE name = ? AND credential_hash = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (BROKER_NAME, credential_hash),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE broker_credentials SET is_active = 1, rotated_at = NULL WHERE id = ?",
                (row["id"],),
            )
        else:
            conn.execute(
                "INSERT INTO broker_credentials "
                "(id, name, credential_hash, is_active) VALUES (?, ?, ?, 1)",
                (secrets.token_urlsafe(16), BROKER_NAME, credential_hash),
            )
        conn.commit()


def _stage_broker_credential(credential_hash: str) -> None:
    """Accept a replacement before publishing it to the credential file."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM broker_credentials WHERE name = ? AND credential_hash = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (BROKER_NAME, credential_hash),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE broker_credentials SET is_active = 1, rotated_at = NULL WHERE id = ?",
                (row["id"],),
            )
        else:
            conn.execute(
                "INSERT INTO broker_credentials "
                "(id, name, credential_hash, is_active) VALUES (?, ?, ?, 1)",
                (secrets.token_urlsafe(16), BROKER_NAME, credential_hash),
            )
        conn.commit()


def _active_broker_credential_hashes() -> list[str]:
    with get_db() as conn:
        return [
            row["credential_hash"]
            for row in conn.execute(
                "SELECT credential_hash FROM broker_credentials "
                "WHERE name = ? AND is_active = 1 ORDER BY created_at DESC, rowid DESC",
                (BROKER_NAME,),
            ).fetchall()
        ]


def ensure_broker_credential() -> str:
    """Reconcile the active database hash with the recoverable file value."""
    with _credential_lock():
        plaintext = _read_broker_credential()
        active_hashes = _active_broker_credential_hashes()
        if not plaintext:
            plaintext = f"ac_broker_{secrets.token_urlsafe(32)}"
            _write_broker_credential(plaintext)
        cred_hash = _credential_hash(plaintext)
        if active_hashes != [cred_hash]:
            _activate_broker_credential(cred_hash)
        return cred_hash


def get_broker_credential_hash() -> str | None:
    hashes = _active_broker_credential_hashes()
    return hashes[0] if hashes else None


def verify_broker_credential(plaintext: str) -> bool:
    expected = _credential_hash(plaintext)
    return any(
        secrets.compare_digest(expected, stored_hash)
        for stored_hash in _active_broker_credential_hashes()
    )


def rotate_broker_credential() -> str:
    with _credential_lock():
        plaintext = f"ac_broker_{secrets.token_urlsafe(32)}"
        credential_hash = _credential_hash(plaintext)
        _stage_broker_credential(credential_hash)
        _write_broker_credential(plaintext)
        _activate_broker_credential(credential_hash)
        return plaintext
