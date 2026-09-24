# CLAUDE.md

Guidance for Claude Code when working in this repository.

---

## 1. Problem statement

**Build an Apple device/service support bot that can be put in front of real users without a human reviewing every answer.**

A naive RAG chatbot fails in production for reasons that have nothing to do with answer quality:

| Failure mode | What this repo does about it |
|---|---|
| Users paste serial numbers, emails, phone numbers into the prompt | Presidio scrubs PII before the query reaches any LLM (`graph/nodes/safety_gate.py`) |
| Prompt injection / jailbreak attempts | A dedicated Bhairava-0.4B classifier microservice scores every query; attacks terminate the graph before generation |
| The model invents Apple procedures that don't exist | Every answer is scored by Ragas Faithfulness against the retrieved context |
| The model answers only half of a two-part question | A custom LLM-as-judge completeness metric scores coverage of the decomposed sub-queries |
| Cost blows up because every query hits a frontier model | Queries are classified `low`/`high` complexity and routed to Flash vs Pro |
| Repeat questions pay full inference cost | GPTCache semantic cache short-circuits before the graph runs |
| A dependency goes down and takes the whole service with it | Circuit breakers + tenacity retries, each with an explicit degraded-mode fallback |
| A prompt or model change silently regresses quality | Golden dataset + eval gates in CI that block merge on regression |
| "It worked in my terminal" | Terraform-managed ECS Fargate deploy, LangSmith traces, structlog JSON logs keyed by `request_id` |

The scope is deliberately narrow: **one endpoint** (`POST /query`), **one document corpus** (Apple support PDFs indexed offline), **one graph**. Everything else is production plumbing.

---

## 2. Architecture

### 2.1 Runtime topology

Five processes (`docker-compose.yml` locally, ECS Fargate services in `terraform/`):

| Service | Port | Role |
|---|---|---|
| `app` | 8000 | FastAPI + the LangGraph graph. Stateless, scales 2→10 |
| `rival-service` | 8002 | Attack detection. **Separate process on purpose** — Bhairava-0.4B needs ~1.5–2 GB and must not compete with the API for memory, and it scales independently |
| `gptcache` | 8001 (→8000 internal) | Semantic cache server |
| `mongodb` | 27017 | PageIndex document trees (`support_bot.document_trees`) |
| `postgres` | 5432 | LangGraph `AsyncPostgresSaver` checkpointer = session memory |

### 2.2 Request path

```
POST /query
  │
  ├─ auth_middleware        JWT (HS256) → request.state.user_id     401 on failure
  ├─ input_guard_middleware JSON valid, non-empty, ≤ MAX_INPUT_CHARS 400 on failure
  ├─ slowapi limiter        30/min, keyed by user_id (falls back to IP)  429
  │
  ├─ check_cache()          GPTCache /get, 2s timeout
  │     └─ HIT → return immediately (scores hardcoded 1.0, model_used="cache")
  │
  └─ graph.ainvoke(state, config={"thread_id": session_id})
```

Note the middleware registration order in `main.py` is inverted at runtime: **last added runs first**, so `auth` executes before `input_guard`. That ordering matters — `input_guard` consumes the request body and re-attaches it via `request._body`.

### 2.3 The graph (`graph/graph.py`)

```
        ┌─ pii_scrub ─────┐          ← two entry points; LangGraph runs them in parallel
START ──┤                 ├─→ safety_merge ─→ (is_attack? END : continue)
        └─ attack_detect ─┘
                                  ↓
                          query_intelligence      1 structured LLM call
                                  ↓
                          session_memory          trims history to MAX_SESSION_TURNS
                                  ↓
                          context_retrieval       PageIndex tree search over MongoDB
                                  ↓
                          route_execution         ← conditional edge
              ┌───────────────────┼───────────────────┐
      generate_flash        generate_pro       Send(generate_subquery) × N
              │                   │                   ↓
              │                   │            merge_subqueries
              └───────────────────┴───────────────────┘
                          ┌───────┴────────┐          ← parallel again
                   faithfulness      completeness
                          └───────┬────────┘
                          validation_merge            sets validation_passed + final_response
                                  ↓
                             cache_store              GPTCache /put + final structlog summary
                                  ↓
                                 END
```

**Three distinct parallelism patterns, used deliberately — do not "simplify" them into each other:**

1. **Static fan-out at entry** — `pii_scrub` + `attack_detect`. Two `set_entry_point` calls; `safety_merge` is a barrier that waits for both. Deliberately *not* `asyncio.gather`, so each shows as its own span in LangSmith.
2. **Static fan-out mid-graph** — `faithfulness` + `completeness`, converging at `validation_merge`. Same reasoning.
3. **Dynamic fan-out** — `route_execution` returns `list[Send]` when the query decomposes, because the sub-query count is only known at runtime. `sub_responses` is `Annotated[list[str], append_list]` so concurrent nodes can all write without clobbering each other.

`SupportBotState` (`graph/state.py`) is a flat TypedDict; the section comments (input / safety / query intelligence / context / execution / validation / output) mirror the node order. Every node returns a **partial dict**, never the whole state.

### 2.4 Model routing

| Condition | Path | Model |
|---|---|---|
| `needs_decomp and len(sub_queries) > 1` | `Send` fan-out | per-sub-query, uses `state["complexity"]` |
| `complexity == "low"` | `generate_flash` | `settings.LOW_COMPLEXITY_MODEL` |
| else | `generate_pro` | `settings.HIGH_COMPLEXITY_MODEL` (auto-switches to `ChatOpenAI` if the name starts with `gpt`) |

### 2.5 Resilience contract

Every external dependency has a breaker and a **defined degraded behaviour** — a failing dependency must never 5xx the user:

| Dependency | Breaker (`resilience/breakers.py`) | Fallback |
|---|---|---|
| Rival | `fail_max=5`, `reset=30s` | Fail **open**: allow the request, log `rival_circuit_open` |
| PageIndex/Mongo | `fail_max=5`, `reset=30s` | Empty context → LLM answers from parametric knowledge; faithfulness scoring is skipped (returns 1.0) |
| GPTCache | `fail_max=10`, `reset=15s` | Skip cache silently |
| LLM providers | none — `llm_retry` (3 attempts, exp backoff 2→20s) | exception propagates → 503 |

When adding a new external call, add a breaker + fallback in the same change. That is the house rule.

### 2.6 Observability

- **structlog** JSON to stdout. Always obtain the logger via `get_logger(state["request_id"], node="<name>")` so every line carries `request_id` and is joinable across nodes.
- **LangSmith** traces the whole graph (`LANGCHAIN_TRACING_V2=true`). Node granularity is why the parallelism uses graph edges instead of `asyncio.gather`.
- `cache_store_node` emits the single `request_complete` summary line: model, both scores, `validation_passed`, PII types found, prompt version, sub-query count.
- CloudWatch alarms in `terraform/main.tf`: p95 latency > 10s, 5xx rate > 5%.

---

## 3. Workflow

### 3.1 Offline: index the corpus (run before anything works)

```bash
python -m prep.index_docs --pdf path/to/apple-support-guide.pdf --doc-id apple-support
```

Submits the PDF to PageIndex, polls up to 5 minutes for the tree, upserts it into `support_bot.document_trees`. `context_retrieval_node` currently hardcodes `doc_id: "apple-support"` — changing the doc id means changing that node too.

### 3.2 Local development

```bash
cp .env.example .env      # fill in GOOGLE_API_KEY, OPENAI_API_KEY, LANGCHAIN_API_KEY, PAGEINDEX_API_KEY, JWT_SECRET
make docker-up            # all five services
make dev                  # or: app only, uvicorn --reload (needs the others reachable)
make test                 # pytest tests/
make lint                 # ruff (line-length 120, rules E,F,I,N,W)
```

Auth is required on every route except `/health`, `/docs`, `/openapi.json`. Mint a dev token the same way the eval runner does:

```python
from jose import jwt; jwt.encode({"sub": "dev"}, "<JWT_SECRET>", algorithm="HS256")
```

### 3.3 Eval-driven change workflow

**Any change to a prompt, a model, the routing logic, or a threshold goes through evals.** Three tiers:

| Tier | Command | Needs a server? | Runs where |
|---|---|---|---|
| Offline | `make evals-offline` | No | Every push + PR (`ci.yml`) |
| Live | `make evals` | Yes | Every PR (`eval-gate.yml`), post-deploy, nightly |
| Trace | `make evals-traces` | Yes | Nightly |

- **Offline** (`evals/eval_offline.py`) — routing decisions, decomposition consistency, prompt-file integrity, token-budget estimates. Fast, no API keys, no Docker. It re-implements `route_execution`'s logic; **if you change routing, change it in both places.**
- **Live** (`evals/run_evals.py`) — hits `POST /query` for all 8 golden cases, then checks latency vs `max_latency_ms`, faithfulness ≥ 0.7, completeness ≥ 0.6, and model routing. Compares against `evals/baselines/latest.json` and fails on: >20% avg-latency regression, >30% cost regression, any pass-rate drop, or any previously-passing case now failing. Hard floor: pass rate < 80% fails regardless of baseline.
- **Trace** (`evals/eval_traces.py`) — asserts the request took the expected path through the agent nodes and that validation actually ran.

Golden dataset: `evals/datasets/golden.json` — 8 cases (4 low/flash, 4 high/pro, 3 decomposing). Each carries `expected_complexity`, `expected_model`, `reference_answer`, `max_latency_ms`, `max_tokens`. **Add a case here whenever you fix a class of bad answer.**

Baselines are intentionally manual — a regression should require a human to say "this is the new normal":

```bash
make baseline-update     # re-runs live evals, copies report → evals/baselines/latest.json
# then commit evals/baselines/latest.json
```
Or run the `eval-gate` workflow via `workflow_dispatch` with `update_baseline: true`.

### 3.4 Prompt changes

Prompts are files, not string literals: `prompts/v{n}/{query_intelligence,generation,completeness_judge}.txt`.

To revise a prompt: **copy `prompts/v1/` → `prompts/v2/`, edit there, bump `PROMPT_VERSION` in `graph/nodes/query_intelligence.py`.** Never edit a released version in place — `prompt_version` is written into state and logged with every request, and that is the only way to correlate a quality shift with a prompt change in LangSmith. After bumping, add the new paths to `eval_prompt_versioning()` in `evals/eval_offline.py`.

### 3.5 CI/CD

```
push/PR → ci.yml         lint · mypy (non-blocking) · pytest · offline evals · docker build (both images)
PR       → eval-gate.yml spins up postgres+mongo, boots the app, runs live evals,
                         comments a results table on the PR, blocks merge on regression
merge    → deploy.yml    ci.yml → build+push both images to ECR (tagged by SHA)
                         → terraform plan/apply with the SHA as image tag
                         → post-deploy smoke evals against PRODUCTION_URL
nightly  → nightly-evals.yml  6am UTC live + trace evals vs production, Slack alert on regression
```

Infra lives in `terraform/` (S3-backed state): VPC across 2 AZs, ALB terminating TLS on 443, two Fargate services joined by Cloud Map service discovery (`rival-service.support-bot.local`), Multi-AZ RDS Postgres with deletion protection, Secrets Manager for API keys, CloudWatch logs + alarms. `make tf-plan` / `make tf-apply` for local runs.

---

## 4. Conventions and known landmines

**Package aliasing — read this before running anything.** The directory on disk is `apple_support_bot/`, but every import says `from app.…`. Three different mechanisms paper over this:
- Docker: `WORKDIR /srv`, `COPY . ./app/`, run as `uvicorn app.main:app`
- Tests: `tests/conftest.py` synthesizes an `app` module pointing at the repo root
- CI eval-gate: runs `uvicorn app.main:app` from the checkout root

So `python -m` and `pytest` behave differently depending on where you launch them. If you hit `ModuleNotFoundError: app`, this is why — fix the launch context, don't rewrite the imports.

**Prompt loading is CWD-relative and happens at import time.** `execution.py`, `query_intelligence.py`, and `metrics/completeness.py` all do `Path("prompts/v1/....txt").read_text()` at module scope. The process CWD must be the directory containing `prompts/`. This is fragile — in the Docker image, CWD is `/srv` while prompts live at `/srv/app/prompts`, so the container as currently built will raise `FileNotFoundError` on import. If you touch the Dockerfile or any prompt-loading module, verify this path resolves.

**Model IDs are split across two places.** `config.py` sets `LOW/HIGH_COMPLEXITY_MODEL` (env-overridable), but three call sites hardcode their own model: the PageIndex tree-search LLM and the query-intelligence LLM (`gemini-2.0-flash`), the completeness judge (`gemini-2.0-flash`), and the Ragas faithfulness judge (`gpt-4o-mini` via OpenAI). Only the generation models are configurable. When updating models, check all of these plus `TOKEN_COST_PER_1K` in `evals/run_evals.py`, which is keyed by exact model name — an unknown name silently falls back to a $0.001/1k estimate and quietly corrupts the cost-regression gate.

**The cache path bypasses the graph entirely.** A GPTCache hit returns before safety checks, before PII scrubbing, and with `faithfulness_score`/`completeness_score` hardcoded to `1.0`. Anything that must run on *every* request belongs in middleware, not in a graph node.

**`GPTCACHE_URL` port differs by environment** — `8001` in `config.py`'s default (host-mapped), `8000` in docker-compose (container-internal). Both are correct in their context; don't "fix" one to match the other.

**Node contract.** Async, takes `SupportBotState`, returns a partial dict of only the keys it owns, catches its own dependency failures and returns a safe default rather than raising. Get the logger from `get_logger(state["request_id"], node=...)`. Register the node *and* its edges in `graph/graph.py`.

**Secrets.** `.env` is real and gitignored. Never read it into context, echo it, or commit it. In production every secret comes from Secrets Manager via the ECS task definition.
