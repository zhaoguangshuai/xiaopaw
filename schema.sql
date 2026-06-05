-- XiaoPaw v3 pgvector schema
-- Requires: CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    routing_key     TEXT NOT NULL,
    user_message    TEXT NOT NULL,
    assistant_reply TEXT NOT NULL,
    summary         TEXT NOT NULL,
    tags            TEXT[] NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    turn_ts         BIGINT NOT NULL,
    summary_vec     vector(1024),
    message_vec     vector(1024),
    search_text     TEXT NOT NULL DEFAULT '',
    search_tsv      TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED
);

-- HNSW indexes for vector search
CREATE INDEX IF NOT EXISTS idx_memories_summary_vec
    ON memories USING hnsw (summary_vec vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_memories_message_vec
    ON memories USING hnsw (message_vec vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Full-text search index
CREATE INDEX IF NOT EXISTS idx_memories_search_tsv
    ON memories USING gin (search_tsv);

-- Tag search
CREATE INDEX IF NOT EXISTS idx_memories_tags
    ON memories USING gin (tags);

-- Routing key isolation
CREATE INDEX IF NOT EXISTS idx_memories_routing_key
    ON memories (routing_key);

-- Time-based queries
CREATE INDEX IF NOT EXISTS idx_memories_created_at
    ON memories (created_at DESC);
