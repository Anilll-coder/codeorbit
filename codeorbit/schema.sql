-- CodeGraph schema. One SQLite file per indexed project (.codegraph/graph.db).
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS files (
  id         INTEGER PRIMARY KEY,
  path       TEXT UNIQUE NOT NULL,   -- repo-relative, forward slashes
  lang       TEXT NOT NULL,          -- python | javascript
  hash       TEXT NOT NULL,          -- sha1 of contents, for incremental re-index
  loc        INTEGER NOT NULL DEFAULT 0,
  indexed_at REAL NOT NULL
);

-- A symbol: a definition the graph can point at.
CREATE TABLE IF NOT EXISTS nodes (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,         -- bare name, e.g. "login"
  qname       TEXT NOT NULL,         -- qualified, e.g. "auth.User.login"
  kind        TEXT NOT NULL,         -- module|class|function|method|variable|import
  file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  start_line  INTEGER NOT NULL,
  end_line    INTEGER NOT NULL,
  signature   TEXT,
  docstring   TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_name  ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_qname ON nodes(qname);
CREATE INDEX IF NOT EXISTS idx_nodes_file  ON nodes(file_id);
CREATE INDEX IF NOT EXISTS idx_nodes_kind  ON nodes(kind);

-- A relationship between two symbols.
CREATE TABLE IF NOT EXISTS edges (
  id         INTEGER PRIMARY KEY,
  src        INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  dst        INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,          -- contains|calls|imports|extends|references
  line       INTEGER,                -- call site line, for "opened at the line that calls"
  resolution TEXT NOT NULL DEFAULT 'exact',  -- exact | heuristic
  UNIQUE(src, dst, kind, line)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, kind);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, kind);

-- Call sites parsed in pass 1 but not yet bound to a definition. Pass 2 (resolve)
-- drains this into `edges`. Kept as a table so resolution is re-runnable and its
-- misses are inspectable -- that is where recall is won or lost.
CREATE TABLE IF NOT EXISTS pending_calls (
  id      INTEGER PRIMARY KEY,
  src     INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  callee  TEXT NOT NULL,             -- name as written at the call site
  recv    TEXT,                      -- receiver for x.y() calls, else NULL
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  line    INTEGER NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pending_callee ON pending_calls(callee, resolved);

-- Import statements, used by the resolver to scope name matching to what a file
-- can actually see. Keeping them separate from `edges` lets the resolver read
-- them before any cross-file edge exists.
CREATE TABLE IF NOT EXISTS imports (
  id       INTEGER PRIMARY KEY,
  file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  module   TEXT NOT NULL,            -- "os.path", "./utils", "react"
  symbol   TEXT,                     -- imported name, NULL for whole-module
  alias    TEXT,                     -- local binding if renamed
  line     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_id);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
  name, qname, docstring, content='nodes', content_rowid='id', tokenize='porter'
);

-- Vector embeddings for semantic symbol search. Optional: absent until
-- `codeorbit embed` runs, and every read path degrades to keyword search
-- when the table is empty.
CREATE TABLE IF NOT EXISTS embeddings (
  node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
  dim     INTEGER NOT NULL,
  vec     BLOB NOT NULL,          -- float32 little-endian, `dim` values
  model   TEXT NOT NULL
);
