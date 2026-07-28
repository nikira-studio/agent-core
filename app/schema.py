SCHEMA_SQL = """
-- Users table
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    timezone TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Sessions table
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_activity TEXT DEFAULT CURRENT_TIMESTAMP,
    channel TEXT NOT NULL DEFAULT 'dashboard' CHECK (channel IN ('dashboard', 'pending_otp')),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- OTP secrets table
CREATE TABLE IF NOT EXISTS otp_secrets (
    user_id TEXT PRIMARY KEY,
    secret_encrypted TEXT NOT NULL,
    pending_secret_encrypted TEXT,
    enrolled_at TEXT DEFAULT CURRENT_TIMESTAMP,
    pending_at TEXT,
    last_used TEXT,
    is_active BOOLEAN DEFAULT 1,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Agents table with separate read/write scopes
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT,
    owner_user_id TEXT NOT NULL,
    default_user_id TEXT,
    read_scopes_json TEXT NOT NULL DEFAULT '[]',
    write_scopes_json TEXT NOT NULL DEFAULT '[]',
    default_recall_scopes_json TEXT,
    api_key_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (owner_user_id) REFERENCES users(id),
    FOREIGN KEY (default_user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_agents_owner ON agents(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_agents_active ON agents(is_active);

-- Workspaces table
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    description TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (owner_user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_workspaces_owner ON workspaces(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_workspaces_active ON workspaces(is_active);

-- Workspace collaborators table
CREATE TABLE IF NOT EXISTS workspace_collaborators (
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'editor' CHECK (role IN ('owner', 'editor', 'viewer')),
    can_read INTEGER NOT NULL DEFAULT 1 CHECK (can_read IN (0, 1)),
    can_write INTEGER NOT NULL DEFAULT 0 CHECK (can_write IN (0, 1)),
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workspace_id, user_id),
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_workspace_collaborators_user ON workspace_collaborators(user_id, workspace_id);
CREATE INDEX IF NOT EXISTS idx_workspace_collaborators_workspace ON workspace_collaborators(workspace_id, user_id);

-- Credentials table
CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    label TEXT,
    value_encrypted TEXT NOT NULL,
    metadata_json TEXT,
    expires_at TEXT,
    reference_name TEXT NOT NULL UNIQUE,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(scope, name)
);

CREATE INDEX IF NOT EXISTS idx_credentials_scope ON credentials(scope, name);
CREATE INDEX IF NOT EXISTS idx_credentials_reference ON credentials(reference_name);

-- Memory records table
CREATE TABLE IF NOT EXISTS memory_records (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    memory_class TEXT NOT NULL CHECK (memory_class IN ('fact', 'preference', 'decision', 'scratchpad')),
    scope TEXT NOT NULL,
    domain TEXT,
    topic TEXT,
    confidence REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    importance REAL NOT NULL DEFAULT 0.5 CHECK (importance >= 0.0 AND importance <= 1.0),
    source_kind TEXT NOT NULL DEFAULT 'agent_inference' CHECK (source_kind IN ('operator_authored', 'human_direct', 'tool_output', 'agent_inference', 'episodic_inference', 'semantic_inference', 'external_import')),
    created_at TEXT NOT NULL,
    record_status TEXT NOT NULL DEFAULT 'active' CHECK (record_status IN ('active', 'superseded', 'retracted', 'held')),
    superseded_by_id TEXT,
    supersedes_id TEXT,
    provenance_json TEXT,
    slot_key TEXT,
    valid_from TEXT,
    valid_to TEXT,
    last_confirmed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_records(scope);
CREATE INDEX IF NOT EXISTS idx_memory_class ON memory_records(memory_class);
CREATE INDEX IF NOT EXISTS idx_memory_status ON memory_records(record_status);
CREATE INDEX IF NOT EXISTS idx_memory_supersedes ON memory_records(supersedes_id);
CREATE INDEX IF NOT EXISTS idx_memory_superseded_by ON memory_records(superseded_by_id);


-- Memory embeddings table (for vector search)
CREATE TABLE IF NOT EXISTS memory_embeddings (
    record_id TEXT PRIMARY KEY,
    vector BLOB NOT NULL,
    model TEXT DEFAULT 'nomic-embed-text',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (record_id) REFERENCES memory_records(id)
);

-- Agent activity table
CREATE TABLE IF NOT EXISTS agent_activity (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    assigned_agent_id TEXT,
    reassigned_from_agent_id TEXT,
    task_description TEXT NOT NULL,
    task_note TEXT,
    task_result TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'stale', 'reassigned', 'completed', 'blocked', 'cancelled')),
    memory_scope TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    heartbeat_at TEXT,
    ended_at TEXT,
    metadata_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_activity_user_status ON agent_activity(user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_agent_status ON agent_activity(agent_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_assigned_agent ON agent_activity(assigned_agent_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_heartbeat ON agent_activity(status, heartbeat_at);

-- Audit log table
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    result TEXT NOT NULL,
    details_json TEXT,
    ip_address TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_type, actor_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, timestamp);

-- Broker credentials table
CREATE TABLE IF NOT EXISTS broker_credentials (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    credential_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    rotated_at TEXT
);

-- System settings table
CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Insert initial system settings
INSERT OR IGNORE INTO system_settings (key, value) VALUES ('scratchpad_retention_days', '7');
INSERT OR IGNORE INTO system_settings (key, value) VALUES ('solo_mode_enabled', 'true');
INSERT OR IGNORE INTO system_settings (key, value) VALUES ('installed_version', '1.0.0');
INSERT OR IGNORE INTO system_settings (key, value) VALUES ('vector_search_enabled', 'false');
INSERT OR IGNORE INTO system_settings (key, value) VALUES ('vector_provider', 'ollama');
INSERT OR IGNORE INTO system_settings (key, value) VALUES ('vector_model', 'nomic-embed-text');
INSERT OR IGNORE INTO system_settings (key, value) VALUES ('vector_url', 'http://localhost:11434');
INSERT OR IGNORE INTO system_settings (key, value) VALUES ('vector_dimension', '768');
INSERT OR IGNORE INTO system_settings (key, value) VALUES ('vector_auth_type', 'none');

-- Connector types table
CREATE TABLE IF NOT EXISTS connector_types (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT,
    version TEXT,
    provider_type TEXT NOT NULL DEFAULT 'openapi' CHECK (provider_type IN ('openapi', 'mcp', 'builtin')),
    auth_type TEXT NOT NULL DEFAULT 'api_key' CHECK (auth_type IN ('api_key', 'bearer', 'oauth2', 'basic', 'none')),
    supported_actions_json TEXT NOT NULL DEFAULT '[]',
    required_credential_fields_json TEXT NOT NULL DEFAULT '[]',
    default_binding_rules_json TEXT,
    disabled_actions_json TEXT NOT NULL DEFAULT '[]',
    endpoint_url TEXT,
    transport_type TEXT,
    capabilities_json TEXT,
    tool_snapshot_json TEXT,
    spec_url TEXT,
    operations_json TEXT,
    backend_type TEXT,
    backend_json TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_connector_types_active ON connector_types(is_active);

-- Adapter installation state table
CREATE TABLE IF NOT EXISTS adapter_installations (
    adapter_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('system', 'user', 'git')),
    source_path TEXT NOT NULL,
    installed_connector_type_id TEXT NOT NULL,
    installed_version TEXT,
    installed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_adapter_installations_source ON adapter_installations(source_kind, adapter_id);

-- Connector bindings table
CREATE TABLE IF NOT EXISTS connector_bindings (
    id TEXT PRIMARY KEY,
    connector_type_id TEXT NOT NULL,
    name TEXT NOT NULL,
    scope TEXT NOT NULL,
    credential_id TEXT,
    config_json TEXT,
    rate_limit_config_json TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    last_tested_at TEXT,
    last_error TEXT,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (connector_type_id) REFERENCES connector_types(id),
    FOREIGN KEY (credential_id) REFERENCES credentials(id)
);

CREATE INDEX IF NOT EXISTS idx_bindings_scope ON connector_bindings(scope, enabled);
CREATE INDEX IF NOT EXISTS idx_bindings_connector ON connector_bindings(connector_type_id);
CREATE INDEX IF NOT EXISTS idx_bindings_credential ON connector_bindings(credential_id);

-- Short-lived OAuth authorization state shared across app workers
CREATE TABLE IF NOT EXISTS connector_oauth_states (
    state TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (binding_id) REFERENCES connector_bindings(id)
);

CREATE INDEX IF NOT EXISTS idx_connector_oauth_states_expires ON connector_oauth_states(expires_at);

-- Connector executions table
CREATE TABLE IF NOT EXISTS connector_executions (
    id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL,
    action TEXT NOT NULL,
    params_json TEXT,
    result_status TEXT NOT NULL CHECK (result_status IN ('success', 'failure', 'error')),
    result_body_json TEXT,
    error_message TEXT,
    duration_ms INTEGER,
    executed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (binding_id) REFERENCES connector_bindings(id)
);

CREATE INDEX IF NOT EXISTS idx_executions_binding ON connector_executions(binding_id, executed_at DESC);

-- Webhook registrations table
CREATE TABLE IF NOT EXISTS webhook_registrations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    secret_encrypted TEXT NOT NULL,
    event_types_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_webhook_registrations_enabled ON webhook_registrations(enabled);

-- Webhook delivery log table
CREATE TABLE IF NOT EXISTS webhook_delivery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'failure')),
    http_status INTEGER,
    error_message TEXT,
    delivered_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (webhook_id) REFERENCES webhook_registrations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_webhook_delivery_webhook ON webhook_delivery_log(webhook_id, delivered_at DESC);

-- Inbound webhook keys table (installation-wide, one active key at a time)
CREATE TABLE IF NOT EXISTS inbound_webhook_keys (
    id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    rotated_at TEXT
);

-- Connector session cache table
CREATE TABLE IF NOT EXISTS connector_session_cache (
    binding_id TEXT PRIMARY KEY,
    session_data_encrypted TEXT,
    expires_at TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Tool result spill table: large MCP tool outputs are offloaded here and
-- replaced in the response with a summary + handle, so big payloads do not
-- flood the agent's context window. Retrieved in slices via result_fetch and
-- swept after expiry.
CREATE TABLE IF NOT EXISTS tool_result_spill (
    id TEXT PRIMARY KEY,
    agent_id TEXT,
    tool TEXT,
    content TEXT NOT NULL,
    total_chars INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_result_spill_expires ON tool_result_spill(expires_at);
"""


SCHEMA_SQL_MEMORY_FTS = """
-- FTS5 virtual table for memory search
CREATE VIRTUAL TABLE IF NOT EXISTS memory_records_fts USING fts5(
    content, topic,
    content='memory_records',
    content_rowid='rowid'
);

-- FTS triggers
CREATE TRIGGER IF NOT EXISTS memory_records_ai AFTER INSERT ON memory_records BEGIN
    INSERT INTO memory_records_fts(rowid, content, topic)
    VALUES (new.rowid, new.content, new.topic);
END;

CREATE TRIGGER IF NOT EXISTS memory_records_au AFTER UPDATE ON memory_records BEGIN
    INSERT INTO memory_records_fts(memory_records_fts, rowid, content, topic)
    VALUES('delete', old.rowid, old.content, old.topic);
    INSERT INTO memory_records_fts(rowid, content, topic)
    VALUES (new.rowid, new.content, new.topic);
END;

CREATE TRIGGER IF NOT EXISTS memory_records_ad AFTER DELETE ON memory_records BEGIN
    INSERT INTO memory_records_fts(memory_records_fts, rowid, content, topic)
    VALUES('delete', old.rowid, old.content, old.topic);
END;
"""


def create_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.executescript(SCHEMA_SQL_MEMORY_FTS)
    _ensure_activity_columns(conn)
    _ensure_activity_fts(conn)
    _ensure_memory_proposals_table(conn)
    _widen_proposal_actions(conn)
    _ensure_agents_default_recall_column(conn)
    _ensure_memory_metadata_columns(conn)
    _drop_retired_memory_columns(conn)
    _ensure_connector_type_provider_columns(conn)
    _ensure_adapter_installations_table(conn)
    conn.execute(
        """
        INSERT OR IGNORE INTO workspace_collaborators
        (workspace_id, user_id, role, can_read, can_write, created_by)
        SELECT id, owner_user_id, 'owner', 1, 1, owner_user_id
        FROM workspaces
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_slot ON memory_records(scope, memory_class, slot_key, record_status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_freshness ON memory_records(valid_from, valid_to, last_confirmed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_subject_anchor ON memory_records(subject_anchor, record_status)"
    )
    # The nightly verification pass: anchored facts, oldest confirmation first.
    # Partial, because the overwhelming majority of records have no anchor and
    # indexing them would only make the index bigger without making it useful.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_verification ON memory_records"
        "(memory_class, record_status, last_verify_attempt_at) WHERE subject_anchor IS NOT NULL"
    )
    # Both the retention sweep and the per-action health rollup filter the
    # execution log by time; without this each was a full table scan.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_time ON connector_executions(executed_at)"
    )
    # Answering "what did that session actually conclude?" — the reverse of the
    # citation a record already carries. An expression index rather than a
    # duplicated column, so provenance stays the single source of truth and
    # records written before this existed are queryable without a backfill.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_source_activity ON memory_records"
        "(json_extract(provenance_json, '$.activity_id'))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_pinned ON memory_records(scope) "
        "WHERE pinned = 1 AND record_status = 'active'"
    )
    conn.commit()
    _ensure_user_timezone_column(conn)
    _ensure_connector_type_action_state_column(conn)
    _ensure_connector_type_spec_columns(conn)
    _ensure_connector_type_backend_columns(conn)
    _ensure_webhook_tables(conn)
    _ensure_inbound_webhook_table(conn)
    _ensure_connector_session_cache_table(conn)
    _seed_connector_types(conn)


def _ensure_agents_default_recall_column(conn) -> None:
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(agents)").fetchall()
    }
    if "default_recall_scopes_json" not in columns:
        conn.execute("ALTER TABLE agents ADD COLUMN default_recall_scopes_json TEXT")
        conn.commit()


def _ensure_activity_columns(conn) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(agent_activity)").fetchall()
    }
    if "task_note" not in columns:
        conn.execute("ALTER TABLE agent_activity ADD COLUMN task_note TEXT")
    if "task_result" not in columns:
        conn.execute("ALTER TABLE agent_activity ADD COLUMN task_result TEXT")
    conn.commit()


def _widen_proposal_actions(conn) -> None:
    """Allow the 'pin' action on databases created before it existed.

    SQLite cannot alter a CHECK constraint in place, so the table is rebuilt.
    Only runs when the old constraint is actually present.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_proposals'"
    ).fetchone()
    if not row or not row["sql"] or "'pin'" in row["sql"]:
        return
    conn.executescript(
        """
        ALTER TABLE memory_proposals RENAME TO memory_proposals_old;
        CREATE TABLE memory_proposals (
            id TEXT PRIMARY KEY,
            rule TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('retract', 'confirm', 'pin')),
            scope TEXT NOT NULL,
            target_ids_json TEXT NOT NULL,
            rationale TEXT NOT NULL,
            evidence_json TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'accepted', 'rejected', 'stale')),
            created_at TEXT NOT NULL,
            decided_at TEXT,
            decided_by TEXT,
            applied_count INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO memory_proposals SELECT * FROM memory_proposals_old;
        DROP TABLE memory_proposals_old;
        """
    )
    conn.commit()


def _ensure_memory_proposals_table(conn) -> None:
    """Review queue for consolidation proposals, plus the operator's verdicts.

    Verdicts are the point, not a side effect. Every accept/reject is labelled
    data for the rule that produced it, so a rule's precision can be measured
    from real decisions before anyone considers letting it act unattended.
    Nothing here applies itself.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_proposals (
            id TEXT PRIMARY KEY,
            rule TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('retract', 'confirm', 'pin')),
            scope TEXT NOT NULL,
            target_ids_json TEXT NOT NULL,
            rationale TEXT NOT NULL,
            evidence_json TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'accepted', 'rejected', 'stale')),
            created_at TEXT NOT NULL,
            decided_at TEXT,
            decided_by TEXT,
            applied_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_memory_proposals_status
            ON memory_proposals(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_proposals_rule
            ON memory_proposals(rule, status);

        -- One open proposal per rule per target set. Without this a second
        -- generation pass would queue the same suggestion again.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_proposals_open
            ON memory_proposals(rule, target_ids_json)
            WHERE status = 'pending';
        """
    )
    conn.commit()


def _ensure_activity_fts(conn) -> None:
    """Index the activity trail for full-text search, backfilling existing rows.

    Activity records are the episodic tier of memory: what an agent actually
    worked on, one row per task, with a result. They were write-only — reachable
    through activity_list and briefings but never searchable — so agents wrote
    per-task work logs into memory_records instead, where nothing ages them out.
    Indexing the trail gives that content a queryable home of its own.

    Runs after _ensure_activity_columns because the trigger bodies reference
    task_note / task_result, which SQLite resolves at CREATE TRIGGER time; on a
    pre-existing database missing those columns, creating the triggers first
    would fail.
    """
    already_indexed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agent_activity_fts'"
    ).fetchone()
    conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS agent_activity_fts USING fts5(
            task_description, task_note, task_result,
            content='agent_activity',
            content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS agent_activity_ai AFTER INSERT ON agent_activity BEGIN
            INSERT INTO agent_activity_fts(rowid, task_description, task_note, task_result)
            VALUES (new.rowid, new.task_description, new.task_note, new.task_result);
        END;

        CREATE TRIGGER IF NOT EXISTS agent_activity_au AFTER UPDATE ON agent_activity BEGIN
            INSERT INTO agent_activity_fts(agent_activity_fts, rowid, task_description, task_note, task_result)
            VALUES('delete', old.rowid, old.task_description, old.task_note, old.task_result);
            INSERT INTO agent_activity_fts(rowid, task_description, task_note, task_result)
            VALUES (new.rowid, new.task_description, new.task_note, new.task_result);
        END;

        CREATE TRIGGER IF NOT EXISTS agent_activity_ad AFTER DELETE ON agent_activity BEGIN
            INSERT INTO agent_activity_fts(agent_activity_fts, rowid, task_description, task_note, task_result)
            VALUES('delete', old.rowid, old.task_description, old.task_note, old.task_result);
        END;
        """
    )
    # Triggers only cover rows written from here on, so a trail that predates
    # this migration stays invisible until the index is rebuilt once. Keyed off
    # whether the table existed before this call rather than off whether the
    # index looks empty: this is an external-content table, so reading from it
    # (SELECT ... FROM agent_activity_fts) returns rows out of agent_activity
    # even when nothing is indexed, which makes any "is it empty" probe against
    # it report false. A rebuild on a fresh empty database costs nothing.
    if not already_indexed:
        conn.execute(
            "INSERT INTO agent_activity_fts(agent_activity_fts) VALUES('rebuild')"
        )
    conn.commit()


def _drop_retired_memory_columns(conn) -> None:
    """Drop columns that duplicated something else and are no longer written.

    event_time was identical to created_at on all but 2 of 425 records in the
    first real corpus — it recorded when a write happened, which created_at
    already does. Restores stay compatible because _insert_missing_rows
    intersects the backup's columns with the current schema.
    """
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(memory_records)").fetchall()
    }
    if "event_time" in columns:
        conn.execute("ALTER TABLE memory_records DROP COLUMN event_time")
        conn.commit()

    if "domain" in columns:
        # The FTS index and its triggers reference the column, so they are
        # rebuilt before it can go. Rebuilding also re-tokenises every record
        # against the new (content, topic) shape rather than leaving a stale
        # index that still believes it has three columns.
        conn.executescript(
            """
            DROP TRIGGER IF EXISTS memory_records_ai;
            DROP TRIGGER IF EXISTS memory_records_au;
            DROP TRIGGER IF EXISTS memory_records_ad;
            DROP TABLE IF EXISTS memory_records_fts;
            """
        )
        conn.execute("ALTER TABLE memory_records DROP COLUMN domain")
        conn.commit()
        conn.executescript(SCHEMA_SQL_MEMORY_FTS)
        conn.execute("INSERT INTO memory_records_fts(memory_records_fts) VALUES('rebuild')")
        conn.commit()


def _ensure_memory_metadata_columns(conn) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(memory_records)").fetchall()
    }
    additions = [
        ("provenance_json", "TEXT"),
        ("slot_key", "TEXT"),
        ("valid_from", "TEXT"),
        ("valid_to", "TEXT"),
        ("last_confirmed_at", "TEXT"),
        ("expires_at", "TEXT"),
        ("status_changed_at", "TEXT"),
        # What a later session would go and look at to check this record: a repo
        # path, a host, or a connector binding. The one thing retrieval cannot
        # infer from the content, and the prerequisite for verifying a fact
        # rather than merely noting that nobody has checked it.
        ("subject_anchor", "TEXT"),
        # Observed usefulness, to replace the self-assigned scores. A writer
        # rating its own record produces no variance (confidence sits at >=0.95
        # on most of the corpus); how often a record is actually recalled, and
        # whether anyone said it helped, are signals nobody can inflate.
        ("recall_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_recalled_at", "TEXT"),
        ("helpful_count", "INTEGER NOT NULL DEFAULT 0"),
        ("unhelpful_count", "INTEGER NOT NULL DEFAULT 0"),
        # When verification last LOOKED at this record, whatever the answer.
        # Distinct from last_confirmed_at, which only moves on a successful
        # check: ordering a capped nightly sweep by confirmation starves it,
        # because records that can never be confirmed keep sorting to the front
        # and consume the whole budget every run.
        ("last_verify_attempt_at", "TEXT"),
        # Records the operator wants every session to see without having to
        # search for them. Ranking can bury a constraint, and a buried
        # constraint is the same as no constraint.
        ("pinned", "INTEGER NOT NULL DEFAULT 0"),
    ]
    status_changed_at_added = "status_changed_at" not in columns
    for column_name, column_type in additions:
        if column_name not in columns:
            conn.execute(
                f"""
                ALTER TABLE memory_records
                ADD COLUMN {column_name} {column_type}
                """
            )
    if status_changed_at_added:
        # One-time backfill: existing retracted/superseded rows predate this
        # column, so we genuinely don't know when they stopped being active.
        # Stamp them "now" rather than leaving NULL (which would make the
        # retention prune below skip them forever) or backdating to created_at
        # (which could make an old-but-just-retracted record immediately
        # purge-eligible with zero grace period). This gives every existing
        # non-active record a fresh, full grace period starting from upgrade.
        from app.time_utils import utc_now_iso

        conn.execute(
            """
            UPDATE memory_records
            SET status_changed_at = ?
            WHERE record_status IN ('retracted', 'superseded') AND status_changed_at IS NULL
            """,
            (utc_now_iso(),),
        )
    conn.commit()


def _ensure_user_timezone_column(conn) -> None:
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "timezone" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN timezone TEXT")
        conn.commit()


def _ensure_connector_type_action_state_column(conn) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(connector_types)").fetchall()
    }
    if "disabled_actions_json" not in columns:
        conn.execute(
            """
            ALTER TABLE connector_types
            ADD COLUMN disabled_actions_json TEXT NOT NULL DEFAULT '[]'
            """
        )
        conn.commit()


def _ensure_connector_type_provider_columns(conn) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(connector_types)").fetchall()
    }
    additions = [
        ("provider_type", "TEXT NOT NULL DEFAULT 'openapi'"),
        ("endpoint_url", "TEXT"),
        ("transport_type", "TEXT"),
        ("capabilities_json", "TEXT"),
        ("tool_snapshot_json", "TEXT"),
    ]
    for column_name, column_def in additions:
        if column_name not in columns:
            conn.execute(
                f"""
                ALTER TABLE connector_types
                ADD COLUMN {column_name} {column_def}
                """
            )
    conn.commit()


def _ensure_connector_type_spec_columns(conn) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(connector_types)").fetchall()
    }
    additions = [
        ("spec_url", "TEXT"),
        ("operations_json", "TEXT"),
    ]
    for column_name, column_type in additions:
        if column_name not in columns:
            conn.execute(
                f"""
                ALTER TABLE connector_types
                ADD COLUMN {column_name} {column_type}
                """
            )
    conn.commit()


def _ensure_connector_type_backend_columns(conn) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(connector_types)").fetchall()
    }
    additions = [
        ("backend_type", "TEXT"),
        ("backend_json", "TEXT"),
    ]
    for column_name, column_type in additions:
        if column_name not in columns:
            conn.execute(
                f"""
                ALTER TABLE connector_types
                ADD COLUMN {column_name} {column_type}
                """
            )
    conn.commit()


def _ensure_adapter_installations_table(conn) -> None:
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "adapter_installations" not in tables:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS adapter_installations (
                adapter_id TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL CHECK (source_kind IN ('system', 'user', 'git')),
                source_path TEXT NOT NULL,
                installed_connector_type_id TEXT NOT NULL,
                installed_version TEXT,
                installed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_adapter_installations_source ON adapter_installations(source_kind, adapter_id);
            """
        )
        conn.commit()


def _ensure_inbound_webhook_table(conn) -> None:
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "inbound_webhook_keys" not in tables:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS inbound_webhook_keys (
                id TEXT PRIMARY KEY,
                key_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                rotated_at TEXT
            );
            """
        )
        conn.commit()


def _ensure_connector_session_cache_table(conn) -> None:
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "connector_session_cache" not in tables:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS connector_session_cache (
                binding_id TEXT PRIMARY KEY,
                session_data_encrypted TEXT,
                expires_at TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()


def _ensure_webhook_tables(conn) -> None:
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "webhook_registrations" not in tables:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS webhook_registrations (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                secret_encrypted TEXT NOT NULL,
                event_types_json TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                created_by TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_webhook_registrations_enabled ON webhook_registrations(enabled);
            """
        )
    if "webhook_delivery_log" not in tables:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS webhook_delivery_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                webhook_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('success', 'failure')),
                http_status INTEGER,
                error_message TEXT,
                delivered_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (webhook_id) REFERENCES webhook_registrations(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_webhook_delivery_webhook ON webhook_delivery_log(webhook_id, delivered_at DESC);
            """
        )
    conn.commit()


def _seed_connector_types(conn) -> None:
    import json

    existing = conn.execute("SELECT COUNT(*) AS count FROM connector_types").fetchone()
    if existing and existing["count"] > 0:
        return

    connectors = [
        {
            "id": "generic_http",
            "display_name": "Generic HTTP API",
            "description": "Generic authenticated HTTP API connector",
            "provider_type": "builtin",
            "auth_type": "api_key",
            "supported_actions": ["call_endpoint"],
            "required_credential_fields": ["token"],
            "default_binding_rules": None,
        },
    ]

    for c in connectors:
        conn.execute(
            """
            INSERT OR IGNORE INTO connector_types
            (id, display_name, description, provider_type, auth_type,
             supported_actions_json, required_credential_fields_json,
             default_binding_rules_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                c["id"],
                c["display_name"],
                c["description"],
                c["provider_type"],
                c["auth_type"],
                json.dumps(c["supported_actions"]),
                json.dumps(c["required_credential_fields"]),
                json.dumps(c["default_binding_rules"])
                if c["default_binding_rules"]
                else None,
            ),
        )
    conn.commit()
