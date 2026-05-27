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
    event_time TEXT,
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

-- FTS5 virtual table for memory search
CREATE VIRTUAL TABLE IF NOT EXISTS memory_records_fts USING fts5(
    content, domain, topic,
    content='memory_records',
    content_rowid='rowid'
);

-- FTS triggers
CREATE TRIGGER IF NOT EXISTS memory_records_ai AFTER INSERT ON memory_records BEGIN
    INSERT INTO memory_records_fts(rowid, content, domain, topic)
    VALUES (new.rowid, new.content, new.domain, new.topic);
END;

CREATE TRIGGER IF NOT EXISTS memory_records_au AFTER UPDATE ON memory_records BEGIN
    INSERT INTO memory_records_fts(memory_records_fts, rowid, content, domain, topic)
    VALUES('delete', old.rowid, old.content, old.domain, old.topic);
    INSERT INTO memory_records_fts(rowid, content, domain, topic)
    VALUES (new.rowid, new.content, new.domain, new.topic);
END;

CREATE TRIGGER IF NOT EXISTS memory_records_ad AFTER DELETE ON memory_records BEGIN
    INSERT INTO memory_records_fts(memory_records_fts, rowid, content, domain, topic)
    VALUES('delete', old.rowid, old.content, old.domain, old.topic);
END;

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
"""


def create_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)
    _ensure_activity_columns(conn)
    _ensure_memory_metadata_columns(conn)
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
    conn.commit()
    _ensure_user_timezone_column(conn)
    _ensure_connector_type_action_state_column(conn)
    _ensure_connector_type_spec_columns(conn)
    _ensure_connector_type_backend_columns(conn)
    _ensure_webhook_tables(conn)
    _ensure_inbound_webhook_table(conn)
    _ensure_connector_session_cache_table(conn)
    _seed_connector_types(conn)


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
    ]
    for column_name, column_type in additions:
        if column_name not in columns:
            conn.execute(
                f"""
                ALTER TABLE memory_records
                ADD COLUMN {column_name} {column_type}
                """
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
