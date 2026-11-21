# 06 — Docs Q&A with citations and an eval suite

Upload a team's docs (Markdown, PDF, HTML, a GitHub repo's `/docs`), ask questions,
and get streamed answers where every claim links to the exact source passage. Ships
with an evaluation suite that runs in CI and measures answer quality. Follows the shared baseline
(`common/docs/baseline.md`).

**What it proves:** retrieval engineering, async ingestion pipelines, LLM
integration done properly (streaming, cost, caching, evals). This is not a chatbot wrapper.

## Stack

| Service | Tech | Scales? |
|---|---|---|
| `web` | Next.js: workspaces, upload, chat UI with citation highlights | yes |
| `api` | Python 3.12 + FastAPI: auth, search, answer streaming (SSE) | yes, stateless |
| `worker` | Python, same image: parse → chunk → embed → index | yes |
| `postgres` | `pgvector/pgvector:pg17` image: documents, chunks, embeddings, full-text index | single primary |
| `redis` | job queue (RQ/arq), rate limits, answer cache | single |
| `minio` | original uploaded files | — |
| `embedder` | small FastAPI service using `fastembed` (ONNX on CPU, native on arm64 and amd64). The model is baked into the image at build time, so the first run needs no download. About 1–2 GB RAM | yes |
| `llm-mock` | returns canned answers from the retrieved text. The default for CI/offline | — |

Answer generation uses the Claude API (`claude-opus-5-5`, set by env var), with the mock as the
default so the project runs with no key. Embeddings are local by default (no key needed);
a hosted embedding provider is opt-in.

## Steps

### Phase 0 — Scaffold
1. Baseline. Pin an embedding model that runs on CPU on both architectures
   (e.g. `bge-small-en-v1.5`, 384 dims, 512-token input limit).
- **Done when:** `docker compose up` runs everything on a laptop with no GPU and no API keys.

### Phase 1 — Ingestion pipeline
1. A minimal `workspaces` table with one seeded workspace. Every document and chunk
   carries `workspace_id` from day one; real auth arrives in phase 4.
   Upload → store in MinIO → `documents` row (`status=queued`) → enqueue job.
2. Worker: extract text (PDF via `pypdf`/`pymupdf`, HTML via `trafilatura`, Markdown as is),
   keep page numbers and heading paths.
3. Chunk by structure (headings/paragraphs), at most ~400 tokens with ~15% overlap. Count
   tokens with the embedding model's own tokenizer: bge-small silently truncates past 512. Store
   `chunks(doc_id, ordinal, text, heading_path, page, char_start, char_end, tsv tsvector)`.
4. Embed in batches and store `embedding vector(384)`. HNSW index (`vector_cosine_ops`).
5. Idempotent re-ingestion: content hash per document; unchanged docs are skipped,
   changed docs are replaced atomically (new version, swap, delete old).
6. Progress shown in the UI (queued → parsing → embedding → ready / failed with reason).
- **Done when:** ingesting a 300-page PDF completes, and re-uploading it is a no-op.

### Phase 2 — Retrieval
1. Hybrid search: vector top-k (pgvector) + keyword top-k (Postgres full-text,
   `ts_rank`), merged with Reciprocal Rank Fusion.
2. **Every query filters by `workspace_id`**, enforced in SQL. HNSW applies that filter
   after the index scan, so a small workspace can get fewer than k results, or none.
   Turn on pgvector's iterative scans (`SET hnsw.iterative_scan = relaxed_order`,
   pgvector 0.8+) and test with a tiny workspace next to a large one.
3. Optional reranker step (cross-encoder in `embedder`) behind a flag, measured in Phase 5.
4. `GET /search` endpoint returning chunks + scores, which is useful for debugging and evals.
5. A minimal retrieval eval now: 20 questions with expected chunk IDs, plus a script that
   prints recall@5. Phase 5 grows it.
- **Done when:** that script reports recall@5, and hybrid beats vector-only.

### Phase 3 — Answers with citations
1. Build the prompt: a fixed system prompt (instructions: answer only from the sources,
   say "I don't know" when they don't cover it) + the retrieved chunks as document
   blocks + the question.
2. Use the Claude API's built-in **citations** (`citations: {enabled: true}` on the document
   blocks) so each cited sentence maps back to a chunk and character range, instead
   of asking the model to print `[1]` markers and parsing them. Citations must be enabled
   on every document block in a request, and they can't be combined with structured outputs.
3. Stream the answer to the browser via SSE. Clicking a citation opens the source with the passage highlighted.
4. Log input, output and cached token counts per request in a `usage` table, and show
   cost per answer in an admin view. Prompt caching only works for a stable prefix above
   the model's minimum cacheable size, and retrieved chunks change with every question,
   so it mostly helps follow-up questions within one conversation. Measure
   `cache_read_input_tokens` before claiming any savings.
5. Treat document text as data: documents can contain prompt-injection text, so the system
   prompt says sources are untrusted, and the model has no tools that could take actions.
6. Check `stop_reason` (handle `max_tokens` and `refusal`), set timeouts, retry
   429/5xx with backoff, and use a per-workspace rate limit.
- **Done when:** every answer sentence that states a fact has a working citation, and questions outside the docs get "I don't know".

### Phase 4 — Workspaces and auth
1. Users, workspace members (owner/member), invite links. Email + password, an
   HTTP-only session cookie, and a role check on every route. Reuse project 05's code if you built it.
2. Connectors: a GitHub repo URL → fetch `/docs/**/*.md` on a schedule (worker cron),
   and re-index only what changed.

### Phase 5 — Evaluation suite (the part that impresses)
1. `evals/dataset.jsonl`: 50–100 questions over a bundled public docs corpus
   (e.g. Postgres or FastAPI docs) with expected source chunk IDs and the key facts
   each answer must contain. Include questions that have *no* answer in the docs.
2. Metrics:
   - Retrieval: recall@k, MRR (no LLM needed, cheap, runs on every PR).
   - Answers: citation accuracy (does the cited text support the claim?), fact
     coverage, correct "I don't know" rate. Grade with an LLM judge using a fixed
     rubric; check the judge against ~20 labels you made by hand.
3. `docker compose run --rm evals` prints a table and writes `evals/results/<sha>.json`.
4. CI: retrieval evals on every PR, with a threshold (fail if recall@5 drops > 3
   points). Full answer evals are manual or nightly (they cost money).
5. README: a results table comparing chunk sizes, hybrid vs vector, reranker on/off.
- **Done when:** changing chunk size shows up as a measured difference in the README.

### Phase 6 — Scale and cost
1. `--scale worker=4` and measure ingestion pages/min. Scale `embedder` separately.
2. Answer cache in Redis keyed by `(workspace_id, normalized question, corpus_version)`.
   Bump `corpus_version` on any re-index.
3. k6: 50 concurrent askers. Measure time to first token, and keep the LLM call
   out of any DB transaction.
4. Document the next steps: a dedicated vector DB only past ~10M chunks, and why pgvector
   is enough until then.

## Headline test
The retrieval eval in CI: a fixed corpus and dataset, recall@5 ≥ the threshold committed
in the repo. Plus an isolation test: a question in workspace B never retrieves workspace A's chunks.

## Scaling notes
- Ingestion and answering are separate paths. Heavy uploads never slow down chat.
- The LLM provider is behind one small interface (real vs mock) so tests and
  offline runs never need a key. This is the only abstraction worth having here.
