-- MCP Multiplex core storage schema.
--
-- TASK-004 establishes durable table boundaries only. Behavioral validation,
-- typed models, and audit writing are introduced by later task packets.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS agents (
  agent_id TEXT PRIMARY KEY,
  agent_kind TEXT NOT NULL,
  display_name TEXT NOT NULL,
  workspace_root TEXT,
  control_plane_mount TEXT NOT NULL DEFAULT 'mcp_hub',
  auth_token_ref TEXT,
  certification_level TEXT NOT NULL DEFAULT 'unverified'
    CHECK (certification_level IN ('certified', 'best_effort', 'unverified', 'unsupported')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_config_paths (
  config_path_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  precedence INTEGER NOT NULL DEFAULT 0,
  format TEXT NOT NULL,
  is_project_shared INTEGER NOT NULL DEFAULT 0 CHECK (is_project_shared IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (agent_id, path)
);

CREATE TABLE IF NOT EXISTS catalog_entries (
  catalog_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL DEFAULT 1,
  name TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  family_id TEXT NOT NULL,
  variant_name TEXT,
  display_label TEXT NOT NULL,
  review_state TEXT NOT NULL,
  lifecycle_state TEXT NOT NULL,
  risk_tier TEXT NOT NULL,
  transport_json TEXT NOT NULL,
  runtime_json TEXT NOT NULL,
  credentials_json TEXT NOT NULL DEFAULT '[]',
  active_set_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (canonical_name, variant_name)
);

CREATE TABLE IF NOT EXISTS catalog_aliases (
  alias_id TEXT PRIMARY KEY,
  catalog_id TEXT NOT NULL REFERENCES catalog_entries(catalog_id) ON DELETE CASCADE,
  alias TEXT NOT NULL UNIQUE,
  alias_kind TEXT NOT NULL DEFAULT 'name',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_provenance (
  provenance_id TEXT PRIMARY KEY,
  catalog_id TEXT NOT NULL REFERENCES catalog_entries(catalog_id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  source_ref TEXT,
  observed_entry_id TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS catalog_candidates (
  candidate_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL DEFAULT 1,
  source TEXT NOT NULL,
  observed_entry_id TEXT,
  proposed_name TEXT NOT NULL,
  classification TEXT NOT NULL,
  review_state TEXT NOT NULL DEFAULT 'pending',
  risk_tier TEXT NOT NULL DEFAULT 'unknown',
  confidence TEXT NOT NULL DEFAULT 'low',
  backend_shape_json TEXT NOT NULL,
  approval_required INTEGER NOT NULL DEFAULT 1 CHECK (approval_required IN (0, 1)),
  reasons_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS profiles (
  profile_id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS profile_servers (
  profile_id TEXT NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
  catalog_id TEXT NOT NULL REFERENCES catalog_entries(catalog_id) ON DELETE CASCADE,
  mount_name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  reason TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (profile_id, catalog_id, mount_name)
);

CREATE TABLE IF NOT EXISTS observed_entries (
  observed_entry_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL DEFAULT 1,
  agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
  agent_kind TEXT NOT NULL,
  config_path TEXT NOT NULL,
  container_path_json TEXT NOT NULL,
  mount_name TEXT NOT NULL,
  enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
  transport TEXT NOT NULL,
  command TEXT,
  args_json TEXT NOT NULL DEFAULT '[]',
  url TEXT,
  headers_present_json TEXT NOT NULL DEFAULT '[]',
  env_names_json TEXT NOT NULL DEFAULT '[]',
  cwd TEXT,
  tool_filters_json TEXT NOT NULL DEFAULT '{}',
  approval_policy TEXT,
  entry_hash TEXT NOT NULL,
  raw_shape TEXT NOT NULL,
  parser_confidence TEXT NOT NULL,
  first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (agent_id, config_path, entry_hash)
);

CREATE TABLE IF NOT EXISTS remediation_plans (
  plan_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL DEFAULT 1,
  plan_type TEXT NOT NULL,
  status TEXT NOT NULL,
  agent_id TEXT REFERENCES agents(agent_id) ON DELETE SET NULL,
  target_path TEXT,
  observed_entry_id TEXT REFERENCES observed_entries(observed_entry_id) ON DELETE SET NULL,
  catalog_id TEXT REFERENCES catalog_entries(catalog_id) ON DELETE SET NULL,
  policy_json TEXT NOT NULL DEFAULT '{}',
  diff_format TEXT NOT NULL DEFAULT 'unified',
  diff_text TEXT NOT NULL DEFAULT '',
  expected_preimage_hash TEXT,
  rollback_strategy TEXT,
  risk_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL DEFAULT 1,
  plan_id TEXT NOT NULL REFERENCES remediation_plans(plan_id) ON DELETE CASCADE,
  state TEXT NOT NULL,
  actor TEXT NOT NULL,
  channel TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TEXT,
  decision_at TEXT,
  comment TEXT
);

CREATE TABLE IF NOT EXISTS config_backups (
  backup_id TEXT PRIMARY KEY,
  plan_id TEXT REFERENCES remediation_plans(plan_id) ON DELETE SET NULL,
  target_path TEXT NOT NULL,
  backup_path TEXT NOT NULL,
  before_hash TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  restored_at TEXT
);

CREATE TABLE IF NOT EXISTS runtime_backends (
  backend_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL DEFAULT 1,
  catalog_id TEXT NOT NULL REFERENCES catalog_entries(catalog_id) ON DELETE CASCADE,
  runtime_pool_key TEXT NOT NULL,
  state TEXT NOT NULL,
  pid INTEGER,
  account_scope TEXT,
  workspace_root TEXT,
  backend_initialize_count INTEGER NOT NULL DEFAULT 0,
  frontend_session_count INTEGER NOT NULL DEFAULT 0,
  initialize_result_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_used_at TEXT,
  UNIQUE (catalog_id, runtime_pool_key)
);

CREATE TABLE IF NOT EXISTS runtime_frontend_sessions (
  frontend_session_id TEXT PRIMARY KEY,
  backend_id TEXT REFERENCES runtime_backends(backend_id) ON DELETE SET NULL,
  agent_id TEXT REFERENCES agents(agent_id) ON DELETE SET NULL,
  server_name TEXT NOT NULL,
  workspace_root TEXT,
  protocol_version TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS credential_refs (
  credential_ref_id TEXT PRIMARY KEY,
  catalog_id TEXT REFERENCES catalog_entries(catalog_id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  readiness_state TEXT NOT NULL DEFAULT 'missing',
  last_checked_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE (catalog_id, name)
);

CREATE TABLE IF NOT EXISTS auth_tokens (
  token_id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  subject_type TEXT NOT NULL CHECK (subject_type IN ('operator', 'agent', 'daemon')),
  subject_id TEXT,
  scopes_json TEXT NOT NULL DEFAULT '[]',
  token_ref TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TEXT,
  last_used_at TEXT,
  revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_registration_tokens (
  registration_token_id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
  agent_kind TEXT NOT NULL,
  scopes_json TEXT NOT NULL DEFAULT '[]',
  token_ref TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TEXT,
  consumed_at TEXT,
  revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL DEFAULT 1,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  agent_id TEXT REFERENCES agents(agent_id) ON DELETE SET NULL,
  plan_id TEXT REFERENCES remediation_plans(plan_id) ON DELETE SET NULL,
  target_path TEXT,
  before_hash TEXT,
  after_hash TEXT,
  backup_id TEXT REFERENCES config_backups(backup_id) ON DELETE SET NULL,
  result TEXT NOT NULL,
  redaction TEXT NOT NULL DEFAULT 'secret_values_removed',
  payload_json TEXT NOT NULL DEFAULT '{}',
  previous_event_hash TEXT,
  event_hash TEXT NOT NULL UNIQUE,
  timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_config_paths_agent ON agent_config_paths(agent_id);
CREATE INDEX IF NOT EXISTS idx_catalog_aliases_catalog ON catalog_aliases(catalog_id);
CREATE INDEX IF NOT EXISTS idx_catalog_candidates_review ON catalog_candidates(review_state);
CREATE INDEX IF NOT EXISTS idx_observed_entries_agent ON observed_entries(agent_id);
CREATE INDEX IF NOT EXISTS idx_observed_entries_mount ON observed_entries(mount_name);
CREATE INDEX IF NOT EXISTS idx_remediation_plans_status ON remediation_plans(status);
CREATE INDEX IF NOT EXISTS idx_approvals_plan ON approvals(plan_id);
CREATE INDEX IF NOT EXISTS idx_runtime_backends_pool ON runtime_backends(runtime_pool_key);
CREATE INDEX IF NOT EXISTS idx_runtime_frontend_backend ON runtime_frontend_sessions(backend_id);
CREATE INDEX IF NOT EXISTS idx_credential_refs_catalog ON credential_refs(catalog_id);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_subject ON auth_tokens(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_agent_registration_tokens_agent
  ON agent_registration_tokens(agent_id);
CREATE INDEX IF NOT EXISTS idx_events_type_timestamp ON events(event_type, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_plan ON events(plan_id);
