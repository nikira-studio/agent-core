"""Adapter library scanning, installation, and restore helpers."""

import json
import logging
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from app.adapter_paths import SYSTEM_ADAPTER_DIR, get_user_adapter_dir
from app.connectors.manifest import Manifest, load_and_validate
from app.database import get_db
from app.security.dangerous_pattern_scanner import validate_adapter_source
from app.services import connector_service

logger = logging.getLogger(__name__)


GIT_SOURCE_RE = re.compile(r"^git:(?P<owner>[^/]+)/(?P<repo>[^@]+)@(?P<ref>.+)$")


class AdapterInstallError(Exception):
    pass


def _load_manifest(path: Path) -> tuple[Manifest | None, str | None]:
    manifest, err = load_and_validate(path)
    if err:
        return None, err.message
    return manifest, None


def _stage_tree(source: Path, target: Path) -> Path:
    """Copy a directory tree into a sibling staging directory."""

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=str(target.parent))
    )
    shutil.copytree(source, staging, dirs_exist_ok=True)
    return staging


def _swap_tree(staging: Path, target: Path) -> Path | None:
    """Swap a validated staging tree into place and return any backup path."""

    backup = None
    if target.exists():
        backup = target.with_name(f".{target.name}.backup-{uuid.uuid4().hex}")
        target.rename(backup)
    staging.rename(target)
    return backup


def _restore_tree(target: Path, staging: Path | None, backup: Path | None) -> None:
    """Best-effort rollback for a failed tree swap."""

    try:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    finally:
        if backup and backup.exists() and not target.exists():
            try:
                backup.rename(target)
            except Exception:
                logger.exception("Failed to restore adapter tree backup for %s", target)
        if staging and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup and backup.exists() and target.exists():
            shutil.rmtree(backup, ignore_errors=True)


def _scan_root(adapter_root: Path, source_kind: str) -> list[dict]:
    entries: list[dict] = []
    if not adapter_root.is_dir():
        return entries

    for manifest_path in sorted(adapter_root.glob("*/adapter.json")):
        manifest, err = load_and_validate(manifest_path)
        if err:
            logger.warning("Skipping adapter at %s: %s", manifest_path, err)
            continue

        manifest_dir = manifest_path.parent
        requirements_met = _requirements_met(manifest, manifest_dir)
        requirements_summary = _requirements_summary(manifest)
        entries.append(
            {
                "id": manifest.id,
                "display_name": manifest.display_name or manifest.id,
                "description": manifest.description or "",
                "setup": manifest.setup or {},
                "version": manifest.version,
                "backend_type": manifest.backend.get("type"),
                "actions": manifest.actions,
                "requires": manifest.requires or {},
                "source_kind": source_kind,
                "source_path": str(manifest_dir),
                "manifest_path": str(manifest_path),
                "manifest_json": manifest_path.read_text(),
                "requirements_met": requirements_met,
                "installable": requirements_met,
                "requirements_summary": requirements_summary,
            }
        )
    return entries


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

    # requires.config describes fields that a binding must supply. It does not
    # block discovery or library visibility.
    return True


def _requirements_summary(manifest: Manifest) -> dict:
    req = manifest.requires or {}
    credential_fields: list[str] = []
    if isinstance(manifest.credential_schema, dict):
        for field in manifest.credential_schema.get("fields", []) or []:
            name = field.get("name")
            if name and field.get("required", True):
                credential_fields.append(name)

    return {
        "bins": list(req.get("bins", [])),
        "env": list(req.get("env", [])),
        "config": list(req.get("config", [])),
        "credential_fields": credential_fields,
    }


def _dangerous_scan_if_needed(manifest_json: str, manifest: Manifest) -> None:
    backend_type = manifest.backend.get("type")
    if backend_type not in ("mcp", "cli", "http"):
        return
    is_safe, dangerous_patterns = validate_adapter_source(manifest_json)
    if not is_safe:
        raise AdapterInstallError(
            f"Dangerous patterns detected in {manifest.id}: {dangerous_patterns}"
        )


def _seed_unavailable(manifest: Manifest) -> None:
    row = manifest.to_connector_type_row()
    description = f"[unavailable] {manifest.description or ''}".strip()
    existing = connector_service.get_connector_type(manifest.id)
    if existing:
        connector_service.update_connector_type(
            manifest.id,
            display_name=row["display_name"],
            description=description,
            provider_type=row["provider_type"],
            auth_type=row["auth_type"],
            required_credential_fields_json=row["required_credential_fields_json"],
            supported_actions_json=row["supported_actions_json"],
            backend_type=row["backend_type"],
            backend_json=row["backend_json"],
        )
        return

    connector_service.create_connector_type(
        connector_type_id=manifest.id,
        display_name=row["display_name"],
        description=description,
        provider_type=row["provider_type"],
        auth_type=row["auth_type"],
        supported_actions=manifest.actions,
        required_credential_fields=json.loads(row["required_credential_fields_json"]),
        backend_type=row["backend_type"],
        backend_json=row["backend_json"],
    )


def _upsert_connector_type(manifest: Manifest) -> dict:
    row = manifest.to_connector_type_row()
    existing = connector_service.get_connector_type(manifest.id)
    if existing:
        connector_service.update_connector_type(
            manifest.id,
            display_name=row["display_name"],
            description=row["description"],
            provider_type=row["provider_type"],
            auth_type=row["auth_type"],
            required_credential_fields_json=row["required_credential_fields_json"],
            supported_actions_json=row["supported_actions_json"],
            backend_type=row["backend_type"],
            backend_json=row["backend_json"],
        )
        return connector_service.get_connector_type(manifest.id)

    return connector_service.create_connector_type(
        connector_type_id=manifest.id,
        display_name=row["display_name"],
        description=row["description"],
        provider_type=row["provider_type"],
        auth_type=row["auth_type"],
        supported_actions=manifest.actions,
        required_credential_fields=json.loads(row["required_credential_fields_json"]),
        backend_type=row["backend_type"],
        backend_json=row["backend_json"],
    )


def _upsert_install_record(
    adapter_id: str,
    source_kind: str,
    source_path: str,
    installed_version: str,
) -> None:
    with get_db() as conn:
        existing = conn.execute(
            "SELECT adapter_id FROM adapter_installations WHERE adapter_id = ?",
            (adapter_id,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE adapter_installations
                SET source_kind = ?, source_path = ?, installed_version = ?, updated_at = CURRENT_TIMESTAMP
                WHERE adapter_id = ?
                """,
                (source_kind, source_path, installed_version, adapter_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO adapter_installations
                (adapter_id, source_kind, source_path, installed_connector_type_id, installed_version)
                VALUES (?, ?, ?, ?, ?)
                """,
                (adapter_id, source_kind, source_path, adapter_id, installed_version),
            )
        conn.commit()


def _clear_install_record(adapter_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "DELETE FROM adapter_installations WHERE adapter_id = ?", (adapter_id,)
        )
        conn.commit()


def _get_install_record(adapter_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM adapter_installations WHERE adapter_id = ?",
            (adapter_id,),
        ).fetchone()
        return dict(row) if row else None


def list_available_adapters() -> list[dict]:
    """Return available adapter templates from both roots.

    User-local adapters shadow identical system templates. If the user copy is
    byte-identical to the bundled system template and has not been installed yet,
    we show the bundled entry instead so the library stays de-duplicated.
    """

    system_entries = _scan_root(SYSTEM_ADAPTER_DIR, "system")
    user_entries = _scan_root(get_user_adapter_dir(), "user")
    install_records = {
        row["adapter_id"]: dict(row)
        for row in _get_all_install_records()
    }

    system_by_id = {entry["id"]: entry for entry in system_entries}
    user_by_id = {entry["id"]: entry for entry in user_entries}

    merged: list[dict] = []
    seen: set[str] = set()

    for adapter_id in sorted(set(system_by_id) | set(user_by_id)):
        system_entry = system_by_id.get(adapter_id)
        user_entry = user_by_id.get(adapter_id)
        chosen = None
        if system_entry and user_entry:
            if (
                user_entry["manifest_json"] == system_entry["manifest_json"]
                and adapter_id not in install_records
            ):
                chosen = system_entry
            else:
                chosen = user_entry
        else:
            chosen = user_entry or system_entry

        if not chosen:
            continue

        chosen = dict(chosen)
        install_record = install_records.get(adapter_id)
        connector_type = connector_service.get_connector_type(adapter_id)
        chosen["installed"] = bool(install_record or connector_type)
        chosen["installed_version"] = (
            install_record.get("installed_version")
            if install_record
            else connector_type.get("version")
            if connector_type
            else None
        )
        source_version = chosen["version"]
        if install_record and install_record.get("source_kind") == "system":
            source_entry = system_by_id.get(adapter_id)
            if source_entry:
                source_version = source_entry["version"]
        chosen["available_version"] = source_version
        chosen["update_available"] = bool(
            chosen["installed"]
            and chosen["installed_version"]
            and source_version
            and chosen["installed_version"] != source_version
        )
        chosen["installed_at"] = install_record.get("installed_at") if install_record else None
        chosen["installed_source_kind"] = (
            install_record.get("source_kind") if install_record else None
        )
        chosen["installed_source_path"] = (
            install_record.get("source_path") if install_record else None
        )
        chosen["connector_type"] = connector_type
        chosen.pop("manifest_json", None)
        merged.append(chosen)
        seen.add(adapter_id)

    return merged


def _get_all_install_records() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM adapter_installations ORDER BY installed_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def get_adapter_library_entry(adapter_id: str) -> dict | None:
    for entry in list_available_adapters():
        if entry["id"] == adapter_id:
            return entry
    return None


def install_adapter(adapter_id: str, source_kind: str | None = None) -> dict:
    """Install an adapter from the library into the connector catalog.

    System adapters are copied into the user adapter directory first so the
    installed copy survives future upgrades and can be edited locally.
    User adapters are already in the correct path, so installation just seeds
    the connector type and install record.
    """

    library_entry = get_adapter_library_entry(adapter_id)
    if not library_entry:
        raise AdapterInstallError(f"Adapter not found: {adapter_id}")
    if source_kind and library_entry["source_kind"] != source_kind:
        raise AdapterInstallError(
            f"Adapter {adapter_id} is available from {library_entry['source_kind']}, not {source_kind}"
        )
    if not library_entry.get("installable"):
        raise AdapterInstallError(
            f"Adapter {adapter_id} cannot be installed until its requirements are met"
        )

    source_kind = library_entry["source_kind"]
    source_path = Path(library_entry["source_path"])
    install_path = get_user_adapter_dir() / adapter_id
    staging = None
    backup = None
    previous_manifest = None
    if source_kind == "system":
        previous_manifest_path = install_path / "adapter.json"
        if previous_manifest_path.exists():
            previous_manifest, _ = _load_manifest(previous_manifest_path)
        staging = _stage_tree(source_path, install_path)
        manifest_path = staging / "adapter.json"
    else:
        install_path = source_path
        manifest_path = install_path / "adapter.json"

    manifest, err = load_and_validate(manifest_path)
    if err:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        raise AdapterInstallError(f"Unable to validate adapter after install: {err}")
    assert manifest is not None

    manifest_json = manifest_path.read_text()
    _dangerous_scan_if_needed(manifest_json, manifest)
    if staging:
        backup = _swap_tree(staging, install_path)
    try:
        ct = _upsert_connector_type(manifest)
        _upsert_install_record(
            adapter_id=adapter_id,
            source_kind=source_kind,
            source_path=str(install_path),
            installed_version=manifest.version,
        )
    except Exception:
        if source_kind == "system":
            if previous_manifest:
                _upsert_connector_type(previous_manifest)
            else:
                connector_service.delete_connector_type(adapter_id)
        if staging or backup:
            _restore_tree(install_path, staging, backup)
        raise
    logger.info("Installed adapter %s from %s", adapter_id, source_kind)
    return {
        "adapter_id": adapter_id,
        "source_kind": source_kind,
        "source_path": str(install_path),
        "connector_type": ct,
    }


def update_adapter(adapter_id: str) -> dict:
    record = _get_install_record(adapter_id)
    if not record:
        raise AdapterInstallError(f"Installed adapter not found: {adapter_id}")

    source_kind = record.get("source_kind")
    install_path = Path(record["source_path"])
    previous_version = record.get("installed_version")
    previous_manifest = None

    if source_kind == "system":
        source_path = SYSTEM_ADAPTER_DIR / adapter_id
        if not source_path.is_dir():
            raise AdapterInstallError(
                f"Bundled adapter template not found for {adapter_id}"
            )
        previous_manifest, _ = _load_manifest(install_path / "adapter.json")
        staging = _stage_tree(source_path, install_path)
    else:
        if not install_path.is_dir():
            raise AdapterInstallError(
                f"Installed adapter source missing for {adapter_id}: {install_path}"
            )
        staging = None

    manifest_path = (staging or install_path) / "adapter.json"
    manifest, err = load_and_validate(manifest_path)
    if err:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        raise AdapterInstallError(f"Unable to validate adapter after update: {err}")
    assert manifest is not None

    manifest_json = manifest_path.read_text()
    _dangerous_scan_if_needed(manifest_json, manifest)
    backup = None
    if staging:
        backup = _swap_tree(staging, install_path)
    try:
        ct = _upsert_connector_type(manifest)
        _upsert_install_record(
            adapter_id=adapter_id,
            source_kind=source_kind,
            source_path=str(install_path),
            installed_version=manifest.version,
        )
    except Exception:
        if source_kind == "system" and previous_manifest:
            _upsert_connector_type(previous_manifest)
        if staging or backup:
            _restore_tree(install_path, staging, backup)
        raise
    logger.info(
        "Updated adapter %s from %s to %s",
        adapter_id,
        previous_version or "unknown",
        manifest.version,
    )
    return {
        "adapter_id": adapter_id,
        "source_kind": source_kind,
        "source_path": str(install_path),
        "previous_version": previous_version,
        "installed_version": manifest.version,
        "connector_type": ct,
    }


def uninstall_adapter(adapter_id: str) -> bool:
    record = _get_install_record(adapter_id)
    removed_any = False
    if record:
        source_kind = record.get("source_kind")
        source_path = record.get("source_path")
        # System adapters are installed as a user-local copy under data/adapters.
        # Remove that installed copy on uninstall so the browse page falls back to
        # the shipped template and the adapter can be installed again cleanly.
        if source_kind == "system" and source_path:
            installed_path = Path(source_path)
            if installed_path.exists():
                shutil.rmtree(installed_path)
        removed_any = True

    _clear_install_record(adapter_id)
    removed_any = connector_service.delete_connector_type(adapter_id) or removed_any
    return removed_any


def sync_installed_adapters() -> None:
    """Restore installed adapter connector types after restart.

    The filesystem library is only a catalog. The database installation table is
    the source of truth for which adapters should appear in the service catalog.
    """

    records = _get_all_install_records()
    restored = 0
    for record in records:
        adapter_id = record["adapter_id"]
        manifest_path = Path(record["source_path"]) / "adapter.json"
        relink_path: str | None = None
        if not manifest_path.exists():
            # Install records store the absolute source_path captured at install
            # time. If the data directory later moves — e.g. the same data volume
            # mounted at a different path inside a container — that stored path no
            # longer resolves. Re-derive the canonical location under the current
            # user adapter dir and self-heal the record so future restarts stay
            # clean instead of warning forever and dropping the connector.
            canonical_dir = get_user_adapter_dir() / adapter_id
            canonical_manifest = canonical_dir / "adapter.json"
            if canonical_manifest.exists():
                logger.info(
                    "Adapter %s source_path %s is stale; relinking to %s",
                    adapter_id,
                    record["source_path"],
                    canonical_dir,
                )
                manifest_path = canonical_manifest
                relink_path = str(canonical_dir)
            else:
                logger.warning(
                    "Installed adapter %s missing manifest at %s",
                    adapter_id,
                    manifest_path,
                )
                continue
        manifest, err = load_and_validate(manifest_path)
        if err or manifest is None:
            logger.warning("Skipping installed adapter %s: %s", adapter_id, err)
            continue
        _upsert_connector_type(manifest)
        if relink_path is not None:
            _upsert_install_record(
                adapter_id=adapter_id,
                source_kind=record.get("source_kind") or "system",
                source_path=relink_path,
                installed_version=manifest.version,
            )
        restored += 1

    logger.info("Adapter install restore complete: %d restored", restored)


def discover_and_seed_adapters(adapters_dir=None) -> None:
    """Backwards-compatible discovery helper.

    When called with an explicit directory, this behaves like the old test
    helper and seeds connector types from that directory. When called without an
    argument, it restores only installed adapters from the database.
    """

    if adapters_dir is None:
        sync_installed_adapters()
        return

    adapters_dir = Path(adapters_dir)
    if not adapters_dir.is_dir():
        logger.info("No data/adapters/ directory found, skipping adapter discovery")
        return

    seeded = 0
    skipped = 0

    for manifest_path in adapters_dir.glob("*/adapter.json"):
        manifest_dir = manifest_path.parent
        manifest, err = load_and_validate(manifest_path)
        if err:
            logger.warning("Skipping adapter at %s: %s", manifest_path, err)
            skipped += 1
            continue

        if not _requirements_met(manifest, manifest_dir):
            logger.info("Skipping adapter %s: requirements not met", manifest.id)
            skipped += 1
            _seed_unavailable(manifest)
            continue

        _upsert_connector_type(manifest)
        logger.info("Seeded adapter: %s v%s", manifest.id, manifest.version)
        seeded += 1

    logger.info("Adapter discovery complete: %d seeded, %d skipped", seeded, skipped)


def install_from_git(source: str, adapters_dir: Path | None = None) -> str:
    """Install an adapter from a git source into the user adapter directory."""

    if adapters_dir is None:
        adapters_dir = get_user_adapter_dir()

    match = GIT_SOURCE_RE.match(source)
    if not match:
        raise AdapterInstallError(
            f"Invalid git source format: {source!r}. Expected 'git:owner/repo@ref'"
        )

    owner = match.group("owner")
    repo = match.group("repo")
    ref = match.group("ref")
    repo_url = f"https://github.com/{owner}/{repo}.git"
    adapter_id = f"{owner}_{repo}"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "-b", ref, repo_url, str(tmp_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            raise AdapterInstallError(f"Git clone timed out for {repo_url}")
        except subprocess.CalledProcessError as e:
            raise AdapterInstallError(f"Git clone failed for {repo_url}: {e.stderr}")

        manifest_path = tmp_path / "adapter.json"
        if not manifest_path.exists():
            manifest_path = tmp_path / "data" / "adapters" / adapter_id / "adapter.json"

        if not manifest_path.exists():
            raise AdapterInstallError(f"adapter.json not found in {owner}/{repo}@{ref}")

        with open(manifest_path) as f:
            adapter_json_content = f.read()

        is_safe, dangerous_patterns = validate_adapter_source(adapter_json_content)
        if not is_safe:
            raise AdapterInstallError(
                f"Dangerous patterns detected in {adapter_id}: {dangerous_patterns}"
            )

        adapter_target_dir = adapters_dir / adapter_id
        shutil.copytree(tmp_path, adapter_target_dir, dirs_exist_ok=True)

    logger.info("Installed adapter %s from %s", adapter_id, source)
    return adapter_id
