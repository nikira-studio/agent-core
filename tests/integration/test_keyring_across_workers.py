"""With more than one worker, a key rotation in one reaches the others."""

from cryptography.fernet import Fernet

from app.security import encryption


def test_a_worker_picks_up_a_rotation_made_by_another_worker(test_client):
    first = encryption.get_primary_key()
    assert encryption.get_keyring() == [first]
    # Another worker rotates: it rewrites the keyring file, and this process is not told.
    other = Fernet.generate_key()
    encryption._save_keyring([other, first])
    assert encryption.get_primary_key() == other
    assert encryption.get_keyring() == [other, first]
    # New values are encrypted with the new primary, old ones still decrypt.
    assert Fernet(other).decrypt(encryption.encrypt_value("x").encode()) == b"x"
    old = Fernet(first).encrypt(b"before").decode()
    assert encryption.decrypt_value(old) == "before"


def test_rotating_here_still_updates_this_process_at_once(test_client):
    before = encryption.get_primary_key()
    new_key, keyring = encryption.rotate_key()
    assert new_key != before and encryption.get_primary_key() == new_key and encryption.get_keyring() == keyring
    assert encryption.get_keyring()[1:] == [before]


_WORKER = """
import sys
from app.security import encryption
for _ in range(int(sys.argv[1])):
    encryption.rotate_key()
"""


def test_workers_rotating_at_the_same_moment_do_not_lose_each_others_keys(tmp_path):
    """Without a file lock two processes each prepend to the same old list and one key is lost."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo = str(Path(__file__).resolve().parents[2])
    env = {**os.environ, "AGENT_CORE_DATA_PATH": str(tmp_path), "AGENT_CORE_ENCRYPTION_KEY": "auto", "PYTHONPATH": repo}
    env.pop("AGENT_CORE_TEST_MODE", None)
    # start with one key on disk so both workers begin from the same list
    subprocess.run([sys.executable, "-c", "from app.security import encryption; encryption.get_keyring()"], env=env, check=True, cwd=repo)
    workers = [subprocess.Popen([sys.executable, "-c", _WORKER, "6"], env=env, cwd=repo) for _ in range(3)]
    assert [w.wait(timeout=120) for w in workers] == [0, 0, 0]
    out = subprocess.run(
        [sys.executable, "-c", "from app.security import encryption; print(len(encryption.get_keyring()))"],
        env=env, cwd=repo, capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == str(1 + 3 * 6)


def test_a_key_operation_waits_for_another_process_and_gives_up_after_the_limit(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    import pytest

    from app.config import settings

    repo = str(Path(__file__).resolve().parents[2])
    holder_code = (
        "import sys,time\\nfrom app.security import encryption\\n"
        "with encryption.KEY_OPERATION_LOCK:\\n print('held', flush=True)\\n time.sleep(30)\\n"
    )
    env = {**os.environ, "AGENT_CORE_DATA_PATH": str(settings.data_dir), "AGENT_CORE_ENCRYPTION_KEY": "auto", "PYTHONPATH": repo}
    holder = subprocess.Popen([sys.executable, "-c", holder_code.replace("\\n", "\n")], env=env, cwd=repo, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        monkeypatch.setattr(encryption, "KEY_OPERATION_WAIT_SECONDS", 0.3)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            with encryption.KEY_OPERATION_LOCK:
                pass
        assert time.monotonic() - started >= 0.3
        # the failed attempt must not leave this thread holding anything
        assert encryption.KEY_OPERATION_LOCK._depth == 0
    finally:
        holder.kill()
        holder.wait()
    with encryption.KEY_OPERATION_LOCK:  # free again once the holder is gone
        with encryption.KEY_OPERATION_LOCK:  # and reentrant
            pass


def test_releasing_a_key_operation_lock_this_thread_does_not_hold_changes_nothing():
    import pytest

    with pytest.raises(RuntimeError):
        encryption.KEY_OPERATION_LOCK.release()
    assert encryption.KEY_OPERATION_LOCK._depth == 0
