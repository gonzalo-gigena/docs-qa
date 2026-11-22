-- Alone in its file, like 000003.
CREATE INDEX CONCURRENTLY IF NOT EXISTS chunks_tsv_gin ON chunks USING gin (tsv);
