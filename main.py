import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from app.api_extra import make_document_endpoints, make_stream_endpoint
from app.api_extra import router as extra_router
from app.config import settings
from app.graph.graph import build_graph
from app.graph.nodes.context_retrieval import get_mongo
from app.middleware.auth import auth_middleware
from app.middleware.input_guard import input_guard_middleware
from app.middleware.rate_limit import limiter
from app.observability.logging import configure_logging, get_logger
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBearer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel
from slowapi.errors import RateLimitExceeded

configure_logging()

# ── App state ─────────────────────────────────────────────────────────────────

_graph = None
_pg_pool = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _graph, _pg_pool
    # LangGraph's AsyncPostgresSaver is built on psycopg, NOT asyncpg — handing it
    # an asyncpg pool raises "Invalid connection type: asyncpg.pool.Pool".
    # autocommit=True is required by checkpointer.setup(); prepare_threshold=0
    # keeps it safe behind connection poolers; dict_row is what the saver expects.
    _pg_pool = AsyncConnectionPool(
        conninfo=settings.POSTGRES_DSN,
        min_size=2,
        max_size=10,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await _pg_pool.open()
    _graph = await build_graph(_pg_pool)
    yield
    await _pg_pool.close()


# Declared so /docs renders an Authorize button and generated clients know a
# bearer token is required. Enforcement still lives in auth_middleware --
# middleware is invisible to FastAPI's routing, so without this the OpenAPI
# spec claimed /query was public and Swagger UI offered no way to send a token.
bearer_scheme = HTTPBearer(auto_error=False, description="JWT signed with JWT_SECRET (HS256)")

app = FastAPI(title="Apple Support Bot", lifespan=lifespan)

# ── Middleware (order matters: added last = runs first) ───────────────────────

app.middleware("http")(input_guard_middleware)
app.middleware("http")(auth_middleware)
app.state.limiter = limiter

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})


# ── Request / response models ─────────────────────────────────────────────────

class QueryRequest(BaseModel):
    query: str
    session_id: str | None = None
    # Which indexed corpus to search. /query/stream already accepted this; without
    # it here, API clients were silently pinned to DEFAULT_DOC_ID and would get an
    # answer about the wrong document with no indication why.
    doc_id: str | None = None


class QueryResponse(BaseModel):
    response: str
    session_id: str
    request_id: str
    faithfulness_score: float
    completeness_score: float
    validation_passed: bool
    model_used: str


def _blank_state(query: str, session_id: str, request_id: str) -> dict:
    """Starting state for a request. session_history is deliberately absent: any
    value passed in overwrites what the checkpointer restored for this thread."""
    return {
        "raw_query": query,
        "session_id": session_id,
        "request_id": request_id,
        "scrubbed_query": "",
        "pii_found": [],
        "is_attack": False,
        "attack_confidence": 0.0,
        "intent": "",
        "sub_queries": [],
        "complexity": "low",
        "needs_decomp": False,
        "prompt_version": "",
        "current_subquery": "",
        "doc_id": settings.DEFAULT_DOC_ID,
        "retrieved_context": [],
        "sub_responses": [],
        "raw_response": "",
        "model_used": "",
        "faithfulness_score": 0.0,
        "completeness_score": 0.0,
        "validation_passed": False,
        "final_response": "",
    }


# ── Cache helper ──────────────────────────────────────────────────────────────

async def check_cache(query: str) -> str | None:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.GPTCACHE_URL}/get",
                json={"prompt": query},
                timeout=2.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                answer = data.get("answer")
                if answer:
                    return answer
    except Exception:
        pass
    return None


# ── Main endpoint ─────────────────────────────────────────────────────────────

@app.post("/query", response_model=QueryResponse, dependencies=[Depends(bearer_scheme)])
@limiter.limit("30/minute")
async def query_endpoint(body: QueryRequest, request: Request):
    request_id = str(uuid.uuid4())
    session_id = body.session_id or str(uuid.uuid4())
    log = get_logger(request_id, session_id=session_id, user_id=request.state.user_id)

    log.info("request_received", query_length=len(body.query))

    # ── Cache check (before graph) ────────────────────────────────────────────
    # A cache hit returns hardcoded faithfulness/completeness of 1.0, so any
    # eval that hits the cache measures nothing. Callers that need to exercise
    # the real graph send X-Bypass-Cache.
    bypass_cache = request.headers.get("X-Bypass-Cache", "").lower() in ("1", "true", "yes")
    cached = None if bypass_cache else await check_cache(body.query)
    if cached:
        log.info("cache_hit")
        return QueryResponse(
            response=cached,
            session_id=session_id,
            request_id=request_id,
            faithfulness_score=1.0,
            completeness_score=1.0,
            validation_passed=True,
            model_used="cache",
        )

    log.info("cache_miss")

    # ── LangGraph invocation ──────────────────────────────────────────────────
    initial_state = _blank_state(body.query, session_id, request_id)
    initial_state["doc_id"] = body.doc_id or settings.DEFAULT_DOC_ID

    config = {"configurable": {"thread_id": session_id}}

    try:
        result = await _graph.ainvoke(initial_state, config=config)
    except Exception as exc:
        log.error("graph_invocation_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")

    # Attack was detected — graph ended early
    if result.get("is_attack"):
        log.warning("request_rejected_attack", confidence=result.get("attack_confidence"))
        raise HTTPException(status_code=403, detail="Request rejected")

    log.info("request_success")

    return QueryResponse(
        response=result["final_response"],
        session_id=session_id,
        request_id=request_id,
        faithfulness_score=result.get("faithfulness_score", 0.0),
        completeness_score=result.get("completeness_score", 0.0),
        validation_passed=result.get("validation_passed", False),
        model_used=result.get("model_used", "unknown"),
    )


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/chat", include_in_schema=False)
async def chat_page():
    """The product UI. Served from this app rather than anywhere else because a
    browser page must be same-origin to call these endpoints without CORS.

    no-store is not optional here. FileResponse sends etag and last-modified but
    no Cache-Control, so browsers apply heuristic freshness and serve a stored
    copy WITHOUT revalidating. After a deploy that means users keep running the
    previous build of the page -- which is exactly how a fixed bug appears to
    persist. This page is an app shell that changes on every deploy, so it must
    always be fetched.
    """
    page = Path(__file__).resolve().parent / "static" / "chat.html"
    return FileResponse(
        page,
        media_type="text/html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@app.get("/", include_in_schema=False)
async def root():
    """Unauthenticated landing response. Without this the bare URL returns a
    bare 401 and the service looks broken to anyone opening the link."""
    return {
        "service": "Apple Support Bot",
        "chat": "/chat",
        "docs": "/docs",
        "health": "/health",
        "query": "POST /query with an Authorization: Bearer <jwt> header",
    }


# ── Chat-page endpoints ───────────────────────────────────────────────────────
# Factories so api_extra never imports main (which would be circular). They read
# the module-level graph lazily, because it does not exist until lifespan runs.
make_stream_endpoint(lambda: _graph, _blank_state)
make_document_endpoints(get_mongo)
app.include_router(extra_router)


@app.get("/health")
async def health():
    return {"status": "ok", "graph_ready": _graph is not None}
