-- LegacyLens IR schema, version 2.
--
-- Applied by legacylenscli.ir.store.FactStore.initialize(). The schema version is stored in
-- PRAGMA user_version and must match SCHEMA_VERSION in store.py.
--
-- Conventions:
--   * file paths are repository-relative, POSIX-separated and match files.path;
--   * lines are 1-based, columns are 0-based;
--   * multi-valued attributes are JSON arrays in TEXT columns;
--   * booleans are INTEGER 0/1, timestamps are ISO 8601 UTC strings;
--   * child facts cascade when their file is deleted, which makes per-file replacement
--     atomic and incremental re-analysis safe.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS files (
    path            TEXT PRIMARY KEY,
    language        TEXT NOT NULL
                    CHECK (language IN ('php', 'java', 'jsp', 'xml', 'other')),
    content_hash    TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL CHECK (size_bytes >= 0),
    loc             INTEGER NOT NULL DEFAULT 0 CHECK (loc >= 0),
    package         TEXT,
    module          TEXT,
    is_generated    INTEGER NOT NULL DEFAULT 0 CHECK (is_generated IN (0, 1)),
    is_vendored     INTEGER NOT NULL DEFAULT 0 CHECK (is_vendored IN (0, 1)),
    parse_error     TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_files_language ON files (language);
CREATE INDEX IF NOT EXISTS idx_files_content_hash ON files (content_hash);
CREATE INDEX IF NOT EXISTS idx_files_scope ON files (is_generated, is_vendored);

CREATE TABLE IF NOT EXISTS classes (
    id                  TEXT PRIMARY KEY,
    file_path           TEXT NOT NULL
                        REFERENCES files (path) ON DELETE CASCADE ON UPDATE CASCADE,
    name                TEXT NOT NULL,
    qualified_name      TEXT NOT NULL,
    kind                TEXT NOT NULL DEFAULT 'class'
                        CHECK (kind IN ('class', 'interface', 'trait', 'enum', 'record')),
    extends             TEXT,
    implements          TEXT NOT NULL DEFAULT '[]',
    span_start_line     INTEGER NOT NULL CHECK (span_start_line >= 1),
    span_end_line       INTEGER NOT NULL CHECK (span_end_line >= span_start_line),
    span_start_column   INTEGER NOT NULL DEFAULT 0 CHECK (span_start_column >= 0),
    span_end_column     INTEGER NOT NULL DEFAULT 0 CHECK (span_end_column >= 0),
    modifiers           TEXT NOT NULL DEFAULT '[]',
    annotations         TEXT NOT NULL DEFAULT '[]'
) STRICT;

CREATE INDEX IF NOT EXISTS idx_classes_file ON classes (file_path);
CREATE INDEX IF NOT EXISTS idx_classes_qualified_name ON classes (qualified_name);

CREATE TABLE IF NOT EXISTS functions (
    id                      TEXT PRIMARY KEY,
    file_path               TEXT NOT NULL
                            REFERENCES files (path) ON DELETE CASCADE ON UPDATE CASCADE,
    class_id                TEXT REFERENCES classes (id) ON DELETE SET NULL ON UPDATE CASCADE,
    name                    TEXT NOT NULL,
    qualified_name          TEXT NOT NULL,
    signature               TEXT NOT NULL,
    parameters              TEXT NOT NULL DEFAULT '[]',
    return_type             TEXT,
    visibility              TEXT NOT NULL DEFAULT 'public'
                            CHECK (visibility IN ('public', 'protected', 'private', 'package')),
    is_static               INTEGER NOT NULL DEFAULT 0 CHECK (is_static IN (0, 1)),
    is_abstract             INTEGER NOT NULL DEFAULT 0 CHECK (is_abstract IN (0, 1)),
    span_start_line         INTEGER NOT NULL CHECK (span_start_line >= 1),
    span_end_line           INTEGER NOT NULL CHECK (span_end_line >= span_start_line),
    span_start_column       INTEGER NOT NULL DEFAULT 0 CHECK (span_start_column >= 0),
    span_end_column         INTEGER NOT NULL DEFAULT 0 CHECK (span_end_column >= 0),
    loc                     INTEGER NOT NULL DEFAULT 0 CHECK (loc >= 0),
    cyclomatic_complexity   INTEGER NOT NULL DEFAULT 1 CHECK (cyclomatic_complexity >= 1),
    max_nesting             INTEGER NOT NULL DEFAULT 0 CHECK (max_nesting >= 0),
    modifiers               TEXT NOT NULL DEFAULT '[]',
    annotations             TEXT NOT NULL DEFAULT '[]'
) STRICT;

CREATE INDEX IF NOT EXISTS idx_functions_file ON functions (file_path);
CREATE INDEX IF NOT EXISTS idx_functions_class ON functions (class_id);
CREATE INDEX IF NOT EXISTS idx_functions_qualified_name ON functions (qualified_name);

CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY,
    src         TEXT NOT NULL,
    dst         TEXT NOT NULL,
    kind        TEXT NOT NULL
                CHECK (kind IN ('include', 'require', 'import', 'call', 'instantiate',
                                'extends', 'implements', 'uses_trait', 'route', 'view')),
    file_path   TEXT NOT NULL
                REFERENCES files (path) ON DELETE CASCADE ON UPDATE CASCADE,
    line        INTEGER NOT NULL CHECK (line >= 1),
    resolved    INTEGER NOT NULL DEFAULT 1 CHECK (resolved IN (0, 1)),
    confidence  TEXT NOT NULL DEFAULT 'verified'
                CHECK (confidence IN ('verified', 'inferred', 'unsupported')),
    raw         TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_edges_file ON edges (file_path);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges (src, kind);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst, kind);
CREATE INDEX IF NOT EXISTS idx_edges_unresolved ON edges (kind, file_path) WHERE resolved = 0;

CREATE TABLE IF NOT EXISTS data_access (
    id          INTEGER PRIMARY KEY,
    function_id TEXT REFERENCES functions (id) ON DELETE SET NULL ON UPDATE CASCADE,
    file_path   TEXT NOT NULL
                REFERENCES files (path) ON DELETE CASCADE ON UPDATE CASCADE,
    operation   TEXT NOT NULL
                CHECK (operation IN ('select', 'insert', 'update', 'delete', 'replace',
                                     'merge', 'ddl', 'exec', 'other')),
    table_name  TEXT,
    sql         TEXT NOT NULL,
    line        INTEGER NOT NULL CHECK (line >= 1),
    driver      TEXT,
    parameters  TEXT NOT NULL DEFAULT '[]',
    confidence  TEXT NOT NULL DEFAULT 'verified'
                CHECK (confidence IN ('verified', 'inferred', 'unsupported'))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_data_access_file ON data_access (file_path);
CREATE INDEX IF NOT EXISTS idx_data_access_function ON data_access (function_id);
CREATE INDEX IF NOT EXISTS idx_data_access_table ON data_access (table_name, operation);

CREATE TABLE IF NOT EXISTS entry_points (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL
                    CHECK (kind IN ('route', 'servlet', 'filter', 'action', 'jsp',
                                    'cli', 'cron')),
    file_path       TEXT NOT NULL
                    REFERENCES files (path) ON DELETE CASCADE ON UPDATE CASCADE,
    function_id     TEXT REFERENCES functions (id) ON DELETE SET NULL ON UPDATE CASCADE,
    handler_ref     TEXT,
    http_method     TEXT
                    CHECK (http_method IS NULL OR http_method IN
                           ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS',
                            'ANY')),
    path            TEXT NOT NULL,
    framework       TEXT,
    line            INTEGER NOT NULL CHECK (line >= 1),
    requires_auth   INTEGER CHECK (requires_auth IS NULL OR requires_auth IN (0, 1)),
    auth_hint       TEXT,
    confidence      TEXT NOT NULL DEFAULT 'verified'
                    CHECK (confidence IN ('verified', 'inferred', 'unsupported'))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_entry_points_file ON entry_points (file_path);
CREATE INDEX IF NOT EXISTS idx_entry_points_function ON entry_points (function_id);
CREATE INDEX IF NOT EXISTS idx_entry_points_route ON entry_points (kind, path);

CREATE TABLE IF NOT EXISTS request_inputs (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL
                    CHECK (kind IN ('query', 'form', 'request', 'cookie', 'file',
                                    'header', 'body', 'path', 'session', 'other')),
    file_path       TEXT NOT NULL
                    REFERENCES files (path) ON DELETE CASCADE ON UPDATE CASCADE,
    function_id     TEXT REFERENCES functions (id) ON DELETE SET NULL ON UPDATE CASCADE,
    name            TEXT NOT NULL,
    framework       TEXT,
    line            INTEGER NOT NULL CHECK (line >= 1),
    raw             TEXT,
    confidence      TEXT NOT NULL DEFAULT 'verified'
                    CHECK (confidence IN ('verified', 'inferred', 'unsupported'))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_request_inputs_file ON request_inputs (file_path);
CREATE INDEX IF NOT EXISTS idx_request_inputs_function ON request_inputs (function_id);
CREATE INDEX IF NOT EXISTS idx_request_inputs_name ON request_inputs (kind, name);

CREATE TABLE IF NOT EXISTS side_effects (
    id          INTEGER PRIMARY KEY,
    function_id TEXT REFERENCES functions (id) ON DELETE SET NULL ON UPDATE CASCADE,
    file_path   TEXT NOT NULL
                REFERENCES files (path) ON DELETE CASCADE ON UPDATE CASCADE,
    kind        TEXT NOT NULL
                CHECK (kind IN ('database', 'filesystem', 'http', 'email', 'session',
                                'output', 'process', 'logging', 'cache', 'other')),
    detail      TEXT NOT NULL,
    sink        TEXT,
    line        INTEGER NOT NULL CHECK (line >= 1),
    confidence  TEXT NOT NULL DEFAULT 'verified'
                CHECK (confidence IN ('verified', 'inferred', 'unsupported'))
) STRICT;

CREATE INDEX IF NOT EXISTS idx_side_effects_file ON side_effects (file_path);
CREATE INDEX IF NOT EXISTS idx_side_effects_function ON side_effects (function_id);
CREATE INDEX IF NOT EXISTS idx_side_effects_kind ON side_effects (kind);

CREATE TABLE IF NOT EXISTS claims (
    id          INTEGER PRIMARY KEY,
    subject     TEXT NOT NULL,
    kind        TEXT NOT NULL
                CHECK (kind IN ('summary', 'purpose', 'behaviour', 'business_rule', 'risk',
                                'data_model', 'entry_point', 'table', 'other')),
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'inferred'
                CHECK (status IN ('verified', 'inferred', 'unsupported')),
    citations   TEXT NOT NULL DEFAULT '[]',
    origin      TEXT NOT NULL DEFAULT 'static' CHECK (origin IN ('static', 'llm')),
    model       TEXT,
    prompt_hash TEXT,
    created_at  TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_claims_subject ON claims (subject);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims (status, origin);
CREATE INDEX IF NOT EXISTS idx_claims_kind ON claims (kind);

-- Operational cache for LLM completions. It lives in the same database so a project can
-- be copied or evaluated as one file; it is not part of the published IR.
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key           TEXT PRIMARY KEY,
    response            TEXT NOT NULL,
    provider            TEXT,
    model               TEXT,
    prompt_hash         TEXT,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens   INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    cost_usd            REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    created_at          TEXT NOT NULL,
    last_used_at        TEXT NOT NULL,
    hits                INTEGER NOT NULL DEFAULT 0 CHECK (hits >= 0)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_llm_cache_model ON llm_cache (model);
CREATE INDEX IF NOT EXISTS idx_llm_cache_last_used ON llm_cache (last_used_at);
