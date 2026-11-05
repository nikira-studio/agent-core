"""database.all_or_nothing: every get_db() inside shares one transaction."""

import pytest

from app.database import all_or_nothing, get_db


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM system_settings WHERE key LIKE 'aon_%'").fetchone()[0]


def _put(key):
    with get_db() as conn:
        conn.execute("INSERT INTO system_settings (key, value) VALUES (?, 'x')", (key,))
        conn.commit()


def _put_with_own_transaction(key):
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO system_settings (key, value) VALUES (?, 'x')", (key,))
        conn.commit()


def test_all_writes_land_together(clean_db):
    from app.database import init_db
    init_db()
    with all_or_nothing():
        _put("aon_1")
        _put_with_own_transaction("aon_2")
    with get_db() as conn:
        assert _count(conn) == 2


def test_a_failure_undoes_earlier_commits(clean_db):
    from app.database import init_db
    init_db()
    with pytest.raises(RuntimeError):
        with all_or_nothing():
            _put("aon_1")
            _put_with_own_transaction("aon_2")
            raise RuntimeError("third operation failed")
    with get_db() as conn:
        assert _count(conn) == 0


def test_inner_rollback_keeps_the_rest(clean_db):
    from app.database import init_db
    init_db()
    with all_or_nothing():
        _put("aon_1")
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO system_settings (key, value) VALUES ('aon_bad', 'x')")
            conn.rollback()
    with get_db() as conn:
        assert _count(conn) == 1


def test_units_do_not_nest(clean_db):
    with all_or_nothing():
        with pytest.raises(RuntimeError):
            with all_or_nothing():
                pass
