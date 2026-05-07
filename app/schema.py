SCHEMA_SQL = """
-- Users table
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
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
    backup_codes_json TEXT,
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

-- Vault entries table
CREATE TABLE IF NOT EXISTS vault_entries (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    label TEXT,
    value_encrypted TEXT NOT NULL,
    value_type TEXT NOT NULL DEFAULT 'other' CHECK (value_type IN ('api', 'password', 'url', 'config', 'other')),
    metadata_json TEXT,
    expires_at TEXT,
    reference_name TEXT NOT NULL UNIQUE,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(scope, name)
);

CREATE INDEX IF NOT EXISTS idx_vault_scope ON vault_entries(scope, name);
CREATE INDEX IF NOT EXISTS idx_vault_reference ON vault_entries(reference_name);

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
    supersedes_id TEXT
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

-- Connector types table
CREATE TABLE IF NOT EXISTS connector_types (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT,
    auth_type TEXT NOT NULL DEFAULT 'api_key' CHECK (auth_type IN ('api_key', 'bearer', 'oauth2', 'basic')),
    supported_actions_json TEXT NOT NULL DEFAULT '[]',
    required_credential_fields_json TEXT NOT NULL DEFAULT '[]',
    default_binding_rules_json TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_connector_types_active ON connector_types(is_active);

-- Connector bindings table
CREATE TABLE IF NOT EXISTS connector_bindings (
    id TEXT PRIMARY KEY,
    connector_type_id TEXT NOT NULL,
    name TEXT NOT NULL,
    scope TEXT NOT NULL,
    credential_id TEXT,
    config_json TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    last_tested_at TEXT,
    last_error TEXT,
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (connector_type_id) REFERENCES connector_types(id),
    FOREIGN KEY (credential_id) REFERENCES vault_entries(id)
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
"""


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _column_exists(conn, table_name: str, column_name: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row[1] == column_name for row in rows)


def create_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)

    if not _column_exists(conn, "otp_secrets", "pending_secret_encrypted"):
        conn.execute("ALTER TABLE otp_secrets ADD COLUMN pending_secret_encrypted TEXT")
    if not _column_exists(conn, "otp_secrets", "pending_at"):
        conn.execute("ALTER TABLE otp_secrets ADD COLUMN pending_at TEXT")

    _seed_connector_types(conn)


def _seed_connector_types(conn) -> None:
    import json

    connectors = [
        {
            "id": "github",
            "display_name": "GitHub",
            "description": "GitHub API integration for issues, comments, and repo metadata",
            "auth_type": "bearer",
            "supported_actions": ["create_issue", "comment_issue", "read_repo"],
            "required_credential_fields": ["token"],
            "default_binding_rules": {"scope": "workspace"},
        },
        {
            "id": "slack",
            "display_name": "Slack",
            "description": "Slack API for posting messages and fetching channel lists",
            "auth_type": "bearer",
            "supported_actions": ["post_message", "list_channels"],
            "required_credential_fields": ["token"],
            "default_binding_rules": {"scope": "workspace"},
        },
        {
            "id": "generic_http",
            "display_name": "Generic HTTP API",
            "description": "Generic authenticated HTTP API connector",
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
            (id, display_name, description, auth_type, supported_actions_json,
             required_credential_fields_json, default_binding_rules_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                c["id"],
                c["display_name"],
                c["description"],
                c["auth_type"],
                json.dumps(c["supported_actions"]),
                json.dumps(c["required_credential_fields"]),
                json.dumps(c["default_binding_rules"])
                if c["default_binding_rules"]
                else None,
            ),
        )
    conn.commit()
