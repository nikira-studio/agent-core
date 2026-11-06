import fcntl
import threading
import time
import os
import json
import tempfile
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, MultiFernet

from app.branding import ENV_PREFIX
from app.config import settings


# Backup, restore, key rotation and key restoration all read or replace the key
# files. Interleaved, they produce an archive whose manifest describes one key
# and whose payload holds another, or a database re-encrypted half under each.
# It lives here because it is a property of the key material, not of any one
# caller, and because both sides of the problem already import this module.
#
# With more than one worker process a thread lock is not enough: two workers rotating at the
# same moment would each prepend a new key to the same old list, the second write would drop the
# first worker's key, and anything encrypted with it could never be read again. So the same lock
# also holds a file lock in the data directory, for as long as the outermost holder keeps it.
# The kernel drops a file lock when its process dies, so a crashed worker cannot leave it held.
KEY_OPERATION_LOCK_FILE = ".key-operation.lock"
KEY_OPERATION_WAIT_SECONDS = 300


class _KeyOperationLock:
    """Reentrant in a thread, exclusive across threads, and exclusive across processes."""

    def __init__(self) -> None:
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._fd = -1

    def acquire(self) -> bool:
        self._thread_lock.acquire()
        if self._depth == 0:
            try:
                self._fd = self._lock_file()
            except BaseException:
                self._thread_lock.release()
                raise
        self._depth += 1
        return True

    def release(self) -> None:
        if not self._thread_lock._is_owned():
            raise RuntimeError("Cannot release a key operation lock this thread does not hold")
        self._depth -= 1
        if self._depth == 0:
            fd, self._fd = self._fd, -1
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self._thread_lock.release()

    @staticmethod
    def _lock_file() -> int:
        # The lock file must not be removed or replaced while a key operation holds it (a restore
        # that swaps the data directory holds this lock for its whole run, so it never does).
        os.makedirs(settings.data_dir, exist_ok=True)
        fd = os.open(os.path.join(settings.data_dir, KEY_OPERATION_LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + KEY_OPERATION_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise TimeoutError("Another key operation is still running; try again shortly")
                time.sleep(0.05)
            except BaseException:
                os.close(fd)
                raise

    def _is_owned(self) -> bool:
        """True when the calling thread holds it (the same check a threading.RLock offers)."""
        return self._thread_lock._is_owned() and self._depth > 0

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


KEY_OPERATION_LOCK = _KeyOperationLock()

_fernet: Optional[MultiFernet] = None
_keyring: Optional[list[bytes]] = None
# When the keyring file was last read. With more than one worker, a rotation done in one
# worker rewrites the file; every other worker notices the new stamp and reloads, so none
# of them goes on encrypting with a key that is no longer the primary.
_keyring_stamp: Optional[int] = None


def _keyring_file_stamp() -> Optional[int]:
    try:
        return os.stat(settings.data_dir / "credential.keyring").st_mtime_ns
    except OSError:
        return None


def _drop_cache_if_file_changed() -> None:
    global _fernet, _keyring, _keyring_stamp
    if _keyring is not None and _keyring_stamp is not None and _keyring_file_stamp() != _keyring_stamp:
        _fernet = None
        _keyring = None


def write_private_key(path, data: bytes) -> None:
    """Atomically replace sensitive material without a world-readable temp file."""
    target = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix="credential.key.tmp-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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
        os.chmod(key_path, 0o600)
        with open(key_path, "rb") as f:
            key = f.read()
        if len(key) != 44:
            raise ValueError("credential.key must be 44 bytes (Fernet base64)")
        return key

    os.makedirs(settings.data_dir, exist_ok=True)
    key = Fernet.generate_key()
    write_private_key(key_path, key)
    return key


def _load_keyring() -> list[bytes]:
    if os.path.exists(settings.credential_key_path):
        os.chmod(settings.credential_key_path, 0o600)
    keyring_path = settings.data_dir / "credential.keyring"
    if not os.path.exists(keyring_path):
        primary = _load_or_generate_key()
        _save_keyring([primary])
        return [primary]
    os.chmod(keyring_path, 0o600)
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
        write_private_key(backup_path, prev)
    write_private_key(keyring_path, json.dumps({"keys": [k.decode() for k in keys]}).encode())


def _build_fernet(keys: list[bytes]) -> MultiFernet:
    return MultiFernet([Fernet(k) for k in keys])


def reset_key_cache() -> None:
    """Forget the keys held in this process.

    The keyring is read once and cached for the life of the process, which is
    right for every path except one: a restore replaces the key files on disk
    underneath us. Without this, the process keeps decrypting with the old key
    while the files on disk belong to the restored installation — the database
    and the keys disagree, and nothing says so until the next resolve fails.
    """
    global _fernet, _keyring, _keyring_stamp
    _fernet = None
    _keyring = None
    _keyring_stamp = None


def _loaded() -> list[bytes]:
    global _keyring, _keyring_stamp
    _drop_cache_if_file_changed()
    if _keyring is None:
        # Stamp first, then read: a change made while reading leaves the stamp behind the
        # file, so the next call reloads once more instead of trusting stale keys.
        stamp = _keyring_file_stamp()
        _keyring = _load_keyring()
        # A first start creates the file while loading; then the file we wrote is the one we hold.
        _keyring_stamp = stamp if stamp is not None else _keyring_file_stamp()
    return _keyring


def get_fernet() -> MultiFernet:
    global _fernet
    keys = _loaded()
    fernet = _fernet  # a local, so another thread dropping the cache cannot turn this into None
    if fernet is None:
        fernet = _fernet = _build_fernet(keys)
    return fernet


def get_primary_key() -> bytes:
    return _loaded()[0]


def get_keyring() -> list[bytes]:
    return list(_loaded())


def encrypt_value(plaintext: str) -> str:
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    return get_fernet().decrypt(ciphertext.encode()).decode()


def rotate_key() -> tuple[bytes, list[bytes]]:
    with KEY_OPERATION_LOCK:
        return _rotate_key_locked()


def _rotate_key_locked() -> tuple[bytes, list[bytes]]:
    global _fernet, _keyring, _keyring_stamp
    current = _loaded()
    new_key = Fernet.generate_key()
    new_keyring = [new_key] + current
    _keyring = new_keyring
    _fernet = _build_fernet(new_keyring)
    _save_keyring(new_keyring)
    _keyring_stamp = _keyring_file_stamp()
    key_path = settings.credential_key_path
    backup_path = str(key_path) + ".rotated.bak"
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            prev = f.read()
        write_private_key(backup_path, prev)
    write_private_key(key_path, new_key)
    return new_key, new_keyring


def decrypt_with_key(ciphertext: str, key: bytes) -> Optional[str]:
    try:
        return Fernet(key).decrypt(ciphertext.encode()).decode()
    except Exception:
        return None
