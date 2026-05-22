import pytest
from cryptography.fernet import Fernet


def test_fernet_roundtrip():
    key = Fernet.generate_key()
    f = Fernet(key)
    plaintext = "super-secret-value"
    ciphertext = f.encrypt(plaintext.encode())
    assert f.decrypt(ciphertext).decode() == plaintext


def test_fernet_different_each_time():
    key = Fernet.generate_key()
    f = Fernet(key)
    c1 = f.encrypt(b"same text")
    c2 = f.encrypt(b"same text")
    assert c1 != c2


def test_fernet_wrong_key_fails():
    key1 = Fernet.generate_key()
    key2 = Fernet.generate_key()
    f1 = Fernet(key1)
    f2 = Fernet(key2)
    ciphertext = f1.encrypt(b"secret")
    with pytest.raises(Exception):
        f2.decrypt(ciphertext)