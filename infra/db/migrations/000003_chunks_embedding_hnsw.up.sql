-- Alone in its file: CONCURRENTLY can't run in a transaction, and a multi-statement file is one.
CREATE INDEX CONCURRENTLY IF NOT EXISTS chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
