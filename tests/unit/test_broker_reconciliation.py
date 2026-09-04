import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services import broker_service


def test_ensure_repairs_database_from_credential_file(clean_db):
    credential = "ac_broker_existing-file-value"
    path = clean_db.parent / broker_service.BROKER_CREDENTIAL_FILE
    path.write_text(credential)

    active_hash = broker_service.ensure_broker_credential()

    assert active_hash == broker_service._credential_hash(credential)
    assert broker_service.verify_broker_credential(credential)


def test_ensure_replaces_unrecoverable_database_only_credential(clean_db):
    broker_service._activate_broker_credential("unrecoverable-hash")

    active_hash = broker_service.ensure_broker_credential()
    credential = (
        clean_db.parent / broker_service.BROKER_CREDENTIAL_FILE
    ).read_text()

    assert active_hash == broker_service._credential_hash(credential)
    assert active_hash != "unrecoverable-hash"


def test_rotation_atomically_replaces_file_and_active_hash(clean_db):
    broker_service.ensure_broker_credential()

    credential = broker_service.rotate_broker_credential()
    path = clean_db.parent / broker_service.BROKER_CREDENTIAL_FILE

    assert path.read_text() == credential
    assert broker_service.verify_broker_credential(credential)
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_ensure_repairs_interrupted_rotation(clean_db):
    old_credential = broker_service.rotate_broker_credential()
    replacement = "ac_broker_staged-before-database-commit"
    broker_service._write_broker_credential(replacement)
    assert broker_service.verify_broker_credential(old_credential)

    active_hash = broker_service.ensure_broker_credential()

    assert active_hash == broker_service._credential_hash(replacement)
    assert broker_service.verify_broker_credential(replacement)


def test_failed_rotation_keeps_published_credential_valid(clean_db, monkeypatch):
    old_credential = broker_service.rotate_broker_credential()

    def fail_write(_: str) -> None:
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(broker_service, "_write_broker_credential", fail_write)
        with pytest.raises(OSError, match="disk unavailable"):
            broker_service.rotate_broker_credential()

    assert broker_service.verify_broker_credential(old_credential)
    assert broker_service.ensure_broker_credential() == broker_service._credential_hash(
        old_credential
    )
    assert broker_service._active_broker_credential_hashes() == [
        broker_service._credential_hash(old_credential)
    ]


def test_concurrent_rotations_leave_file_and_database_in_agreement(clean_db):
    broker_service.ensure_broker_credential()

    with ThreadPoolExecutor(max_workers=8) as executor:
        credentials = list(executor.map(lambda _: broker_service.rotate_broker_credential(), range(24)))

    path = clean_db.parent / broker_service.BROKER_CREDENTIAL_FILE
    published = path.read_text()
    assert published in credentials
    assert broker_service.get_broker_credential_hash() == broker_service._credential_hash(published)
    assert broker_service.verify_broker_credential(published)
