"""A backup has to contain what was committed, and a restore has to take effect.

Both halves were wrong in the same quiet way. The export archived the `.db`
file directly, but the database runs in WAL mode, so anything committed and not
yet checkpointed simply was not in the archive. The restore swapped the files
on disk while the running process kept the previous installation's keys in
memory, then reported success — the database and the keys disagreed and nothing
said so until the next resolve.

The existing restore test asserted HTTP 200 and the mode echo, which both of
those defects satisfy.
"""

import io

from cryptography.fernet import InvalidToken

from app.config import settings
from app.database import get_db
from app.services import backup_service, credential_service


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _key_path():
    return settings.credential_key_path


def _ensure_key():
    from cryptography.fernet import Fernet

    if not _key_path().exists():
        _key_path().write_bytes(Fernet.generate_key())


# --- the archive itself ----------------------------------------------------


def test_a_backup_contains_data_still_sitting_in_the_wal(clean_db, tmp_path):
    """Commit, do not checkpoint, export: the row has to be in the archive.

    Reading the file off disk misses it — in the worst case the archive does
    not contain the table at all.
    """
    import sqlite3 as _sqlite3

    _ensure_key()
    # The connection stays open on purpose. SQLite checkpoints when the last
    # connection closes, so a test that commits and closes cannot show the
    # problem — the data is already folded back into the .db file by then. A
    # live server always has connections open.
    held = _sqlite3.connect(str(clean_db))
    try:
        held.execute("PRAGMA journal_mode=WAL")
        held.execute(
            "INSERT INTO memory_records (id, content, memory_class, scope,"
            " created_at, status_changed_at) VALUES"
            " ('wal-1','Committed but not checkpointed','fact','workspace:proj',"
            " '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
        )
        held.commit()

        import os

        assert os.path.exists(f"{clean_db}-wal"), "no WAL to test against"

        archive = backup_service.build_backup_zip(
            str(clean_db), str(_key_path()), "admin"
        )
    finally:
        held.close()

    import sqlite3
    import zipfile

    with zipfile.ZipFile(io.BytesIO(archive.getvalue())) as zf:
        db_bytes = zf.read("agent-core.db")

    extracted = tmp_path / "restored.db"
    extracted.write_bytes(db_bytes)
    con = sqlite3.connect(str(extracted))
    try:
        rows = con.execute(
            "SELECT content FROM memory_records WHERE id = 'wal-1'"
        ).fetchall()
    finally:
        con.close()

    assert rows, "the archived database was missing a committed record"


def test_the_manifest_describes_the_archived_bytes(clean_db):
    """The checksum must cover what is in the zip, not a file beside it."""
    _ensure_key()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_settings (key, value)"
            " VALUES ('marker','present')"
        )
        conn.commit()

    archive = backup_service.build_backup_zip(str(clean_db), str(_key_path()), "admin")

    import hashlib
    import json
    import zipfile

    with zipfile.ZipFile(io.BytesIO(archive.getvalue())) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        db_bytes = zf.read("agent-core.db")

    assert (
        hashlib.sha256(db_bytes).hexdigest() == manifest["files"]["agent-core.db"]
    ), "the manifest checksum does not describe the archived database"


def test_a_restored_archive_validates(clean_db):
    """The round trip the checksum mismatch would have broken."""
    _ensure_key()
    archive = backup_service.build_backup_zip(str(clean_db), str(_key_path()), "admin")
    ok, msg, _, _ = backup_service._read_validated_backup(io.BytesIO(archive.getvalue()))
    assert ok, msg


# --- restore actually taking effect ----------------------------------------


def _on_disk_keyring():
    import json

    path = settings.data_dir / "credential.keyring"
    return json.loads(path.read_text())["keys"] if path.exists() else []


def test_a_restore_leaves_the_process_holding_the_restored_keys(clean_db):
    """After a restore the process must agree with the files it just wrote.

    The keyring is read once and cached for the life of the process. A restore
    replaces that file on disk, so without dropping the cache the process keeps
    using the pre-restore keys while the database and key files belong to the
    restored installation. It reports success either way; the disagreement
    surfaces at some later resolve, or at the next restart.
    """
    from cryptography.fernet import Fernet

    from app.security import encryption

    _ensure_key()

    # An unrelated installation with a key of its own.
    other_key = Fernet.generate_key()
    _key_path().write_bytes(other_key)
    (settings.data_dir / "credential.keyring").write_text(
        f'{{"keys": ["{other_key.decode()}"]}}'
    )
    encryption.reset_key_cache()

    entry = credential_service.create_credential(
        scope="user:admin", name="theirs", value_plaintext="THEIR-SECRET"
    )
    reference = entry["reference_name"]
    encrypted, backup_key = backup_service.build_encrypted_backup_package(
        str(clean_db), str(_key_path()), "admin"
    )

    # Now this installation: a different key, and a warm cache holding it.
    our_key = Fernet.generate_key()
    _key_path().write_bytes(our_key)
    (settings.data_dir / "credential.keyring").write_text(
        f'{{"keys": ["{our_key.decode()}"]}}'
    )
    encryption.reset_key_cache()
    with get_db() as conn:
        conn.execute("DELETE FROM credentials")
        conn.commit()
    credential_service.create_credential(
        scope="user:admin", name="ours", value_plaintext="OUR-SECRET"
    )
    assert encryption.get_keyring() == [our_key], "the cache should be warm"

    # Driven through the service rather than the route: under test the route
    # writes to settings.db_path while the suite reads through a TEST_DB
    # override, so only the service call touches the database being asserted on.
    plain = backup_service.decrypt_backup_package(encrypted.getvalue(), backup_key)
    ok, msg, _ = backup_service.restore_from_zip(
        plain, str(clean_db), str(_key_path())
    )
    assert ok, msg

    cached = [k.decode() for k in encryption.get_keyring()]
    assert cached == _on_disk_keyring(), (
        "the process is still holding the pre-restore keys; the database on "
        "disk and the keys in memory belong to different installations"
    )

    # And the practical consequence: their credential resolves, in this process,
    # with no restart.
    try:
        resolved = credential_service.resolve_reference(reference)
    except InvalidToken:
        raise AssertionError("restore succeeded but the credential cannot be read")
    assert resolved == "THEIR-SECRET", (
        f"restore reported success but the credential resolved to {resolved!r}"
    )


def test_restore_clears_the_wal_of_the_replaced_database(clean_db):
    """Sidecars describe the file that was replaced, not the one restored."""
    import os

    _ensure_key()
    encrypted, backup_key = backup_service.build_encrypted_backup_package(
        str(clean_db), str(_key_path()), "admin"
    )

    # Held open so a WAL actually exists at restore time, which is the state a
    # running server is always in. Closing first checkpoints it away and the
    # test proves nothing.
    import sqlite3 as _sqlite3

    held = _sqlite3.connect(str(clean_db))
    held.execute("PRAGMA journal_mode=WAL")
    held.execute(
        "INSERT OR REPLACE INTO system_settings (key, value)"
        " VALUES ('written_after_backup','1')"
    )
    held.commit()
    assert os.path.exists(f"{clean_db}-wal"), "no WAL to test against"

    plain = backup_service.decrypt_backup_package(encrypted.getvalue(), backup_key)
    ok, msg, _ = backup_service.restore_from_zip(
        plain, str(clean_db), str(_key_path())
    )
    assert ok, msg
    held.close()

    assert not os.path.exists(f"{clean_db}-wal"), "a stale WAL survived the restore"
    assert not os.path.exists(f"{clean_db}-shm"), "a stale SHM survived the restore"

    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key = 'written_after_backup'"
        ).fetchone()
    assert row is None, "the replaced database's WAL was replayed over the restore"


# --- a restore must not run over live work ---------------------------------


def test_a_restore_waits_for_work_already_in_flight(clean_db):
    """A commit that succeeds and then vanishes is the worst possible outcome.

    A connection opened before the swap keeps its handle on the replaced file.
    It accepts writes and commits them successfully, and no later reader ever
    sees them. So the restore does not begin until in-flight work has finished.
    """
    import threading
    import time

    _ensure_key()
    encrypted, backup_key = backup_service.build_encrypted_backup_package(
        str(clean_db), str(_key_path()), "admin"
    )
    plain = backup_service.decrypt_backup_package(encrypted.getvalue(), backup_key)

    inside = threading.Event()
    may_finish = threading.Event()
    restore_began = threading.Event()

    def unit_of_work():
        with get_db() as conn:
            inside.set()
            may_finish.wait(timeout=5)
            conn.execute(
                "INSERT OR REPLACE INTO system_settings (key, value)"
                " VALUES ('written_during_restore','1')"
            )
            conn.commit()

    worker = threading.Thread(target=unit_of_work)
    worker.start()
    assert inside.wait(timeout=5), "the unit of work never started"

    outcome = {}

    def do_restore():
        restore_began.set()
        outcome["result"] = backup_service.restore_from_zip(
            plain, str(clean_db), str(_key_path()), drain_timeout=10
        )

    restorer = threading.Thread(target=do_restore)
    restorer.start()
    assert restore_began.wait(timeout=5)

    # The restore must still be waiting: the unit of work has not finished.
    time.sleep(0.3)
    assert not outcome, "the restore swapped the database out from under live work"

    may_finish.set()
    worker.join(timeout=10)
    restorer.join(timeout=15)

    ok, msg, _ = outcome["result"]
    assert ok, msg


def test_work_arriving_during_a_restore_is_refused_not_lost(clean_db):
    """Whatever a caller is told, it must be true.

    A request arriving while the database is held is refused — quickly and by
    name — rather than waiting out the restore or, far worse, committing into a
    file that is about to be unlinked. The wait is deliberately short because
    many routes reach the database from the event loop.
    """
    import threading

    from app.database import DatabaseUnavailable, exclusive_access

    result = {}

    def late_writer():
        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO system_settings (key, value)"
                    " VALUES ('late','1')"
                )
                conn.commit()
            result["outcome"] = "committed"
        except DatabaseUnavailable:
            result["outcome"] = "refused"

    with exclusive_access(drain_timeout=5):
        writer = threading.Thread(target=late_writer)
        writer.start()
        writer.join(timeout=3)
        assert result.get("outcome") == "refused", (
            "a write slipped through while the database was held"
        )

    # Nothing was written, and the caller was told so.
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key = 'late'"
        ).fetchone()
    assert row is None, "a refused write left data behind"

    # And once the gate is open the same work succeeds.
    late_writer()
    assert result["outcome"] == "committed"


def test_a_refusal_is_reported_as_a_retryable_503(test_client, admin_token):
    """An unhandled DatabaseUnavailable would surface as a generic 500.

    That is both wrong and unhelpful: the condition is brief and self-resolving,
    so it is reported as one.
    """
    import threading

    from app.database import exclusive_access

    holding = threading.Event()
    release = threading.Event()

    def hold():
        with exclusive_access(drain_timeout=5):
            holding.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(timeout=5)
    try:
        r = test_client.get(
            "/api/memory/proposals", headers=_headers(admin_token)
        )
    finally:
        release.set()
        holder.join(timeout=10)

    assert r.status_code == 503, r.text[:200]
    assert r.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert r.headers.get("Retry-After")


def test_a_restore_refuses_rather_than_running_over_stuck_work(clean_db):
    """If it cannot drain, it changes nothing and says so."""
    import threading

    _ensure_key()
    encrypted, backup_key = backup_service.build_encrypted_backup_package(
        str(clean_db), str(_key_path()), "admin"
    )
    plain = backup_service.decrypt_backup_package(encrypted.getvalue(), backup_key)

    inside = threading.Event()
    release = threading.Event()

    def stuck():
        with get_db():
            inside.set()
            release.wait(timeout=10)

    worker = threading.Thread(target=stuck)
    worker.start()
    assert inside.wait(timeout=5)

    ok, msg, _ = backup_service.restore_from_zip(
        plain, str(clean_db), str(_key_path()), drain_timeout=0.5
    )

    release.set()
    worker.join(timeout=10)

    assert ok is False
    assert "nothing was replaced" in msg


def test_the_gate_reopens_after_a_failed_drain(clean_db):
    """A refused restore must not leave the database closed for business.

    The earlier version of this test simply opened the database, which proves
    nothing: it never caused the failed drain it claimed to be recovering from.
    """
    import threading

    from app.database import DatabaseUnavailable, exclusive_access

    inside = threading.Event()
    release = threading.Event()

    def stuck():
        with get_db():
            inside.set()
            release.wait(timeout=10)

    worker = threading.Thread(target=stuck)
    worker.start()
    assert inside.wait(timeout=5)

    # Actually fail a drain.
    try:
        with exclusive_access(drain_timeout=0.3):
            raise AssertionError("the drain should not have succeeded")
    except DatabaseUnavailable:
        pass

    release.set()
    worker.join(timeout=10)

    # And the database is open for business again.
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_settings (key, value)"
            " VALUES ('after_failed_drain','1')"
        )
        conn.commit()
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key = 'after_failed_drain'"
        ).fetchone()
    assert row is not None


def test_two_restores_cannot_overlap(clean_db):
    """The flag alone did not keep exclusive holders apart.

    Both would see the gate closed, both would proceed, and whichever finished
    first would reopen it while the other was still swapping files.
    """
    import threading
    import time

    from app.database import exclusive_access

    both_inside = []
    first_inside = threading.Event()
    let_first_go = threading.Event()

    def first():
        with exclusive_access(drain_timeout=5):
            first_inside.set()
            both_inside.append("first")
            let_first_go.wait(timeout=5)
            both_inside.remove("first")

    def second():
        with exclusive_access(drain_timeout=5):
            assert both_inside == [], (
                f"entered while another exclusive holder was active: {both_inside}"
            )
            both_inside.append("second")
            both_inside.remove("second")

    a = threading.Thread(target=first)
    a.start()
    assert first_inside.wait(timeout=5)

    b = threading.Thread(target=second)
    b.start()
    time.sleep(0.2)
    assert b.is_alive(), "the second exclusive holder did not wait"

    let_first_go.set()
    a.join(timeout=5)
    b.join(timeout=5)
    assert not a.is_alive() and not b.is_alive()


def test_ordinary_work_is_refused_while_an_exclusive_holder_is_active(clean_db):
    """The consequence of the above, from a caller's point of view."""
    import threading

    from app.database import DatabaseUnavailable, exclusive_access

    outcome = {}

    def worker():
        try:
            with get_db() as conn:
                conn.execute("SELECT 1").fetchone()
            outcome["result"] = "admitted"
        except DatabaseUnavailable:
            outcome["result"] = "refused"

    with exclusive_access(drain_timeout=5):
        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=3)

    assert outcome.get("result") == "refused", (
        "ordinary work was admitted into a database being replaced"
    )


def test_restore_and_key_rotation_do_not_deadlock(clean_db):
    """Both take both locks; they have to take them in the same order.

    Restore held the database gate and wanted the key lock; rotation held the
    key lock and wanted the database. Each waited for what the other had, and
    it only resolved when one timed out — after doing part of its work.
    """
    import threading

    from app.security.encryption import KEY_OPERATION_LOCK

    _ensure_key()
    encrypted, backup_key = backup_service.build_encrypted_backup_package(
        str(clean_db), str(_key_path()), "admin"
    )
    plain = backup_service.decrypt_backup_package(encrypted.getvalue(), backup_key)

    rotation_holds_key = threading.Event()
    rotation_may_finish = threading.Event()
    rotation_touched_db = threading.Event()

    def rotation_shaped():
        # The shape of rotate_key: key lock first, then database work.
        with KEY_OPERATION_LOCK:
            rotation_holds_key.set()
            rotation_may_finish.wait(timeout=10)
            with get_db() as conn:
                conn.execute("SELECT 1").fetchone()
            rotation_touched_db.set()

    rotator = threading.Thread(target=rotation_shaped)
    rotator.start()
    assert rotation_holds_key.wait(timeout=5)

    outcome = {}

    def do_restore():
        outcome["result"] = backup_service.restore_from_zip(
            plain, str(clean_db), str(_key_path()), drain_timeout=10
        )

    restorer = threading.Thread(target=do_restore)
    restorer.start()
    # Give the restore time to reach its first lock. Without this the rotation
    # can finish before the restore acquires anything, and the test proves
    # nothing about the order they take them in.
    import time

    time.sleep(0.5)

    # The restore must be waiting on the key lock, holding nothing the rotation
    # needs. Releasing the rotation lets both complete.
    rotation_may_finish.set()
    assert rotation_touched_db.wait(timeout=10), (
        "the rotation could not reach the database: the restore is holding it"
    )
    rotator.join(timeout=10)
    restorer.join(timeout=15)

    ok, msg, _ = outcome["result"]
    assert ok, msg


def test_the_gate_never_holds_the_event_loop(clean_db):
    """A closed gate must not stall unrelated work.

    Waiting for the gate — even briefly — happens on whatever thread called in,
    and routes reach the database straight from the event loop. A 0.5s wait
    turned a restore into a half-second pause for every other request in the
    process, which is exactly what the offloading work removed. Refusing
    immediately keeps the loop free.
    """
    import asyncio
    import time

    from app.database import DatabaseUnavailable, exclusive_access

    async def scenario():
        loop = asyncio.get_running_loop()

        async def unrelated():
            began = time.monotonic()
            await asyncio.sleep(0.05)
            return time.monotonic() - began

        def touch_database():
            with get_db() as conn:
                conn.execute("SELECT 1").fetchone()

        with exclusive_access(drain_timeout=5):
            task = asyncio.ensure_future(unrelated())
            # Let the task actually start and begin its sleep. Without this it
            # first runs *after* the blocking call and measures nothing.
            await asyncio.sleep(0)
            # The refusal happens on the loop, exactly as a route would do it.
            try:
                touch_database()
                refused = False
            except DatabaseUnavailable:
                refused = True
            elapsed = await task

        assert refused, "the database should have refused while held"
        assert elapsed < 0.2, (
            f"an unrelated 50ms task took {elapsed:.3f}s: the gate blocked the loop"
        )
        assert loop.is_running()

    asyncio.run(scenario())


def test_a_refusal_is_immediate(clean_db):
    """The caller is told now, not after a wait it cannot use."""
    import time

    from app.database import DatabaseUnavailable, exclusive_access

    with exclusive_access(drain_timeout=5):
        began = time.monotonic()
        try:
            with get_db():
                pass
            raise AssertionError("expected a refusal")
        except DatabaseUnavailable:
            elapsed = time.monotonic() - began

    assert elapsed < 0.05, f"the refusal took {elapsed:.3f}s"
