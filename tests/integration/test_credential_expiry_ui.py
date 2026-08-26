"""An expired credential should be visible before it breaks something.

`expires_at` has been stored and returned by the API all along, and the
dashboard ignored it. The failure mode is quiet: the credential lapses, the
connector that depends on it starts failing, and nothing on the page connects
the two.
"""

from pathlib import Path

from app.services import credential_service
from app.time_utils import utc_now


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _credential(name, expires_at=None, scope="user:admin"):
    entry = credential_service.create_credential(
        scope=scope,
        name=name,
        value_plaintext="secret-value",
        label=name,
        expires_at=expires_at,
    )
    return entry["id"] if isinstance(entry, dict) else entry[0]["id"]


def _in_days(days):
    from datetime import timedelta

    return (utc_now() + timedelta(days=days)).isoformat()


def test_an_expired_credential_is_marked_expired(test_client, admin_token):
    _credential("lapsed", expires_at=_in_days(-3))
    page = test_client.get("/credentials", headers=_headers(admin_token)).text
    assert "badge-danger'>expired" in page


def test_a_credential_expiring_soon_is_flagged(test_client, admin_token):
    _credential("soon", expires_at=_in_days(5))
    page = test_client.get("/credentials", headers=_headers(admin_token)).text
    assert "badge-warning'>5d left" in page or "badge-warning'>4d left" in page


def test_a_distant_expiry_is_shown_without_alarm(test_client, admin_token):
    _credential("later", expires_at=_in_days(200))
    page = test_client.get("/credentials", headers=_headers(admin_token)).text
    assert "badge-danger'>expired" not in page
    assert "badge-warning" not in page


def test_a_credential_without_an_expiry_is_not_flagged(test_client, admin_token):
    _credential("forever")
    page = test_client.get("/credentials", headers=_headers(admin_token)).text
    assert "badge-danger'>expired" not in page


def test_an_unparseable_expiry_does_not_break_the_page(test_client, admin_token):
    """Whatever is in the column, the page still renders."""
    credential_id = _credential("odd")
    from app.database import get_db

    with get_db() as conn:
        conn.execute(
            "UPDATE credentials SET expires_at = 'sometime' WHERE id = ?",
            (credential_id,),
        )
        conn.commit()

    r = test_client.get("/credentials", headers=_headers(admin_token))
    assert r.status_code == 200
    assert "sometime" in r.text


def test_the_forms_offer_an_expiry(test_client, admin_token):
    _credential("anything")  # the table only renders when there is a row
    page = test_client.get("/credentials", headers=_headers(admin_token)).text
    assert 'id="credential-expires"' in page, "no expiry on the create form"
    assert 'id="edit-credential-expires"' in page, "no expiry on the edit form"
    assert "<th>Expires</th>" in page


def test_provenance_is_shown_on_the_row(test_client, admin_token):
    _credential("traceable")
    page = test_client.get("/credentials", headers=_headers(admin_token)).text
    assert "added " in page


def test_actions_column_keeps_table_cell_layout():
    """Flex belongs on a button wrapper, never on a table header or table cell."""
    css = Path("app/dashboard/static/css/dashboard.css").read_text()

    assert "th.actions-cell {\n  display: table-cell;" in css
    assert "td.actions-cell {\n  display: table-cell;" in css


# --- the API behind it -----------------------------------------------------


def test_an_expiry_can_be_set_and_then_cleared(test_client, admin_token):
    """Clearing the field has to actually remove the expiry.

    The update route skipped any field that arrived as null, so an operator
    could set an expiry and never take it off again.
    """
    credential_id = _credential("editable", expires_at=_in_days(10))

    r = test_client.put(
        f"/api/credentials/entries/{credential_id}",
        headers=_headers(admin_token),
        json={"expires_at": None},
    )
    assert r.status_code == 200, r.json()
    assert credential_service.get_credential(credential_id)["expires_at"] is None


def test_omitting_the_field_leaves_the_expiry_alone(test_client, admin_token):
    """Not sending it is not the same as clearing it."""
    expiry = _in_days(10)
    credential_id = _credential("kept", expires_at=expiry)

    r = test_client.put(
        f"/api/credentials/entries/{credential_id}",
        headers=_headers(admin_token),
        json={"label": "renamed"},
    )
    assert r.status_code == 200, r.json()
    assert credential_service.get_credential(credential_id)["expires_at"] == expiry


# --- rendering and validation ----------------------------------------------


def test_dates_render_as_elements_not_as_text(test_client, admin_token):
    """local_dt returns markup the browser localises.

    Escaping it a second time printed the raw `<span class="local-dt" ...>` into
    the cell, which is what a browser run actually showed.
    """
    _credential("dated", expires_at=_in_days(30))
    page = test_client.get("/credentials", headers=_headers(admin_token)).text

    assert "&lt;span class=&quot;local-dt&quot;" not in page
    assert 'class="local-dt"' in page, "the date should still be a localisable element"


def test_a_malformed_expiry_is_refused(test_client, admin_token):
    """It used to be stored, and then every resolve of that credential 500ed."""
    r = test_client.post(
        "/api/credentials/entries",
        headers=_headers(admin_token),
        json={
            "scope": "user:admin",
            "name": "bad-date",
            "value": "secret-value",
            "expires_at": "not-a-date",
        },
    )
    assert r.status_code == 422, r.text[:200]


def test_a_malformed_expiry_cannot_be_set_by_update(test_client, admin_token):
    credential_id = _credential("fine")
    r = test_client.put(
        f"/api/credentials/entries/{credential_id}",
        headers=_headers(admin_token),
        json={"expires_at": "whenever"},
    )
    assert r.status_code == 422, r.text[:200]


def test_a_legacy_malformed_expiry_fails_closed(test_client, admin_token):
    """Rows written before validation existed must not raise on resolve.

    Refusing to hand out a secret is recoverable; a 500 in the resolve path is
    the credential broker going down.
    """
    from app.database import get_db

    credential_id = _credential("legacy")
    entry = credential_service.get_credential(credential_id)
    with get_db() as conn:
        conn.execute(
            "UPDATE credentials SET expires_at = 'sometime' WHERE id = ?",
            (credential_id,),
        )
        conn.commit()

    assert credential_service.resolve_reference(entry["reference_name"]) is None
    assert credential_service.resolve_credential(entry["reference_name"]) is None

    r = test_client.post(
        f"/api/credentials/entries/{credential_id}/reveal",
        headers=_headers(admin_token),
    )
    assert r.status_code == 410, r.text[:200]
    assert r.json()["error"]["code"] == "CREDENTIAL_EXPIRED"


def test_an_expiry_is_stored_in_one_canonical_form(test_client, admin_token):
    """Whatever shape it arrives in, it is stored as an instant."""
    r = test_client.post(
        "/api/credentials/entries",
        headers=_headers(admin_token),
        json={
            "scope": "user:admin",
            "name": "canonical",
            "value": "secret-value",
            "expires_at": "2026-12-31T23:59:59Z",
        },
    )
    assert r.status_code == 201, r.json()
    stored = r.json()["data"]["entry"]["expires_at"]
    from app.time_utils import parse_utc_datetime

    assert parse_utc_datetime(stored)


def test_the_expiry_date_is_built_in_the_viewers_timezone():
    """A date input has no timezone; the operator means their own end of day.

    Stamping 23:59:59Z put the expiry on the wrong calendar day for anyone east
    of UTC — a browser run in Pacific/Auckland showed a day later than chosen.
    """
    import pathlib

    source = pathlib.Path("app/dashboard/static/js/credentials.js").read_text()
    assert "function endOfDayUtc" in source
    assert "T23:59:59+00:00" not in source, "the expiry is no longer stamped in UTC"
    assert "toISOString()" in source


def test_the_setup_page_names_the_product(test_client):
    """First run showed a literal {APP_NAME}: the block was never interpolated."""
    from app.branding import APP_NAME

    r = test_client.get("/")
    assert r.status_code == 200
    assert "{APP_NAME}" not in r.text
    assert APP_NAME in r.text
