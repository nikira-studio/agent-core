"""Adapter discovery and seeding from data/adapters/ manifests."""

import json
import logging
import shutil
from pathlib import Path

from app.config import settings
from app.connectors import get_connector, register_connector
from app.connectors.manifest import Manifest, load_and_validate
from app.database import get_db

logger = logging.getLogger(__name__)


def discover_and_seed_adapters() -> None:
    adapters_dir = Path(settings.data_dir) / "adapters"
    if not adapters_dir.is_dir():
        logger.info("No data/adapters/ directory found, skipping adapter discovery")
        return

    seeded = 0
    skipped = 0

    for manifest_path in adapters_dir.glob("*/adapter.json"):
        manifest_dir = manifest_path.parent
        m, err = load_and_validate(manifest_path)
        if err:
            logger.warning("Skipping adapter at %s: %s", manifest_path, err)
            skipped += 1
            continue

        if not _requirements_met(m, manifest_dir):
            logger.info("Skipping adapter %s: requirements not met", m.id)
            skipped += 1
            _seed_unavailable(m)
            continue

        _seed_connector_type(m)
        _load_connector_engine(m)
        logger.info("Seeded adapter: %s v%s", m.id, m.version)
        seeded += 1

    logger.info("Adapter discovery complete: %d seeded, %d skipped", seeded, skipped)


def _requirements_met(manifest: Manifest, manifest_dir: Path) -> bool:
    req = manifest.requires
    if not req:
        return True

    for bin_name in req.get("bins", []):
        if shutil.which(bin_name) is None:
            logger.info(
                "Binary %r not found, skipping adapter %s", bin_name, manifest.id
            )
            return False

    for env_var in req.get("env", []):
        import os

        if not os.environ.get(env_var):
            logger.info("Env var %r not set, skipping adapter %s", env_var, manifest.id)
            return False

    config_fields = req.get("config", [])
    if config_fields:
        try:
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT config_json FROM bindings WHERE connector_type_id = ? LIMIT 1",
                    (manifest.id,),
                ).fetchall()
            if not rows:
                return True
            cfg = json.loads(rows[0]["config_json"] or "{}")
            for f in config_fields:
                if not cfg.get(f):
                    logger.info(
                        "Config field %r missing, skipping adapter %s", f, manifest.id
                    )
                    return False
        except Exception:
            pass

    return True


def _seed_unavailable(manifest: Manifest) -> None:
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM connector_types WHERE id = ?", (manifest.id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE connector_types SET display_name = ?, description = ?, version = ?, supported_actions = ? WHERE id = ?",
                (
                    manifest.display_name or manifest.id,
                    f"[unavailable] {manifest.description or ''}",
                    manifest.version,
                    json.dumps(manifest.actions),
                    manifest.id,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO connector_types (id, display_name, description, version, backend_type, supported_actions)
                   VALUES (?, ?, ?, ?, 'http', ?)""",
                (
                    manifest.id,
                    manifest.display_name or manifest.id,
                    f"[unavailable] {manifest.description or ''}",
                    manifest.version,
                    json.dumps(manifest.actions),
                ),
            )
        conn.commit()


def _seed_connector_type(manifest: Manifest) -> None:
    row = manifest.to_connector_type_row()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM connector_types WHERE id = ?", (manifest.id,)
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE connector_types SET
                   display_name = ?, description = ?, version = ?,
                   backend_type = ?, backend_json = ?, supported_actions = ?,
                   credential_schema = ?
                   WHERE id = ?""",
                (
                    row["display_name"],
                    row["description"],
                    row["version"],
                    row["backend_type"],
                    row["backend_json"],
                    row["supported_actions"],
                    row["credential_schema"],
                    manifest.id,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO connector_types
                   (id, display_name, description, version, backend_type, backend_json, supported_actions, credential_schema)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    manifest.id,
                    row["display_name"],
                    row["description"],
                    row["version"],
                    row["backend_type"],
                    row["backend_json"],
                    row["supported_actions"],
                    row["credential_schema"],
                ),
            )
        conn.commit()


def _load_connector_engine(manifest: Manifest) -> None:
    backend_type = manifest.backend.get("type")
    if backend_type == "http":
        from app.connectors.http_engine import HttpEngine

        existing = get_connector(manifest.id)
        if not existing:
            register_connector(manifest.id, type("HttpConnector", (HttpEngine,), {}))
