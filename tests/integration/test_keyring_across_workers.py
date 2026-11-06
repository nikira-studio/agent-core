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
