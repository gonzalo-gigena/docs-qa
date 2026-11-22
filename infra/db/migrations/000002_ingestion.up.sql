SET lock_timeout = '3s';

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE workspaces (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- The app needs one to work at all, so it lives here and not in the optional seed.
INSERT INTO workspaces (id, name) VALUES ('00000000-0000-0000-0000-000000000001', 'Default workspace');

-- One row per version. A new version is built next to the ready one, then replaces it in one transaction.
CREATE TABLE documents (
  id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces (id),
  name text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('pdf', 'html', 'md')),
  version int NOT NULL,
  attempt int NOT NULL DEFAULT 1,
  content_hash text NOT NULL,
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'parsing', 'embedding', 'ready', 'failed')),
  error text,
  pages int,
  chunks_done int NOT NULL DEFAULT 0,
  chunks_total int,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  UNIQUE (workspace_id, name, version),
  UNIQUE (workspace_id, id)
);

CREATE INDEX documents_recent ON documents (workspace_id, created_at DESC);

CREATE TABLE chunks (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id uuid NOT NULL,
  doc_id uuid NOT NULL,
  ordinal int NOT NULL,
  text text NOT NULL,
  heading_path text[] NOT NULL,
  page int,
  char_start int NOT NULL,
  char_end int NOT NULL CHECK (char_end > char_start),
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  embedding vector(384) NOT NULL,
  UNIQUE (doc_id, ordinal),
  -- Composite, so a chunk can't claim a different workspace than its document.
  FOREIGN KEY (workspace_id, doc_id) REFERENCES documents (workspace_id, id) ON DELETE CASCADE
);
