"""Adapter discovery and seeding from data/adapters/ manifests."""

import json
import logging
import shutil
from pathlib import Path

from app.config import settings
from app.connectors.manifest import Manifest, load_and_validate
from app.database import get_db

logger = logging.getLogger(__name__)


def discover_and_seed_adapters(adapters_dir=None) -> None:
    if adapters_dir is None:
        adapters_dir = Path(settings.data_dir) / "adapters"
    else:
        adapters_dir = Path(adapters_dir)
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

    # requires.config describes fields that a BINDING must supply (per-binding,
    # validated at binding create/execute time). It is NOT a hard gate at
    # adapter discovery — an adapter is still discoverable/listable so users can
    # see what bindings to create. Hard availability gates are `requires.bins`
    # (binaries the operator must install) and `requires.env` (env vars), above.
    return True


def _seed_unavailable(manifest: Manifest) -> None:
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM connector_types WHERE id = ?", (manifest.id,)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE connector_types SET display_name = ?, description = ?, supported_actions_json = ?
                   WHERE id = ?""",
                (
                    manifest.display_name or manifest.id,
                    f"[unavailable] {manifest.description or ''}",
                    json.dumps(manifest.actions),
                    manifest.id,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO connector_types (id, display_name, description, backend_type, supported_actions_json)
                   VALUES (?, ?, ?, 'http', ?)""",
                (
                    manifest.id,
                    manifest.display_name or manifest.id,
                    f"[unavailable] {manifest.description or ''}",
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
                   display_name = ?, description = ?,
                   backend_type = ?, backend_json = ?, supported_actions_json = ?
                   WHERE id = ?""",
                (
                    row["display_name"],
                    row["description"],
                    row["backend_type"],
                    row["backend_json"],
                    row["supported_actions_json"],
                    manifest.id,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO connector_types
                   (id, display_name, description, backend_type, backend_json, supported_actions_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    manifest.id,
                    row["display_name"],
                    row["description"],
                    row["backend_type"],
                    row["backend_json"],
                    row["supported_actions_json"],
                ),
            )
        conn.commit()


def _load_connector_engine(manifest: Manifest) -> None:
    """For data-only `http` adapters, no registration is needed — the engine is
    constructed on demand by `connector_service._resolve_executor` from the
    seeded connector_type row (which carries `backend_json`). Future code-bearing
    backends (mcp/cli) may need to register a handler here."""
    backend_type = manifest.backend.get("type")
    if backend_type == "http":
        return
