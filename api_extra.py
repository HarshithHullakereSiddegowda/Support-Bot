"""Endpoints that exist for the /chat page rather than for API clients.

Three things live here:

  POST /auth/token        exchange a shared password for a JWT, so a browser
                          user never handles a raw token
  POST /query/stream      Server-Sent Events: node progress, then answer tokens
                          as they are generated, then the validation scores
  POST /documents/upload  SSE: submit a PDF to PageIndex, poll, store the tree
  GET  /documents         list indexed corpora

Why streaming matters here: measured end to end, a request takes ~150s, but the
answer is finished at ~67s. The remaining 80s is the two judges scoring an
answer that already exists. Holding the response until they finish makes the
product feel broken. Streaming shows tokens at ~8s and delivers the scores when
they arrive.
"""
import asyncio
import json
import tempfile
import uuid
from pathlib import Path
from typing import AsyncIterator

from app.config import settings
from app.observability.logging import get_logger
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from jose import jwt
from pydantic import BaseModel

router = APIRouter()

# Nodes whose tokens are safe to stream straight to the browser. The
# generate_subquery fan-out runs N copies concurrently, so their tokens would
# interleave into nonsense -- for that path we report progress and send the
# merged answer once merge_subqueries has joined it.
_STREAMABLE_NODES = {"generate_flash", "generate_pro"}

_FRIENDLY = {
    "pii_scrub": "Removing personal information",
    "attack_detect": "Checking the request is safe",
    "safety_merge": "Safety checks complete",
    "query_intelligence": "Understanding the question",
    "session_memory": "Loading the conversation",
    "context_retrieval": "Searching the documents",
    "generate_flash": "Writing the answer",
    "generate_pro": "Writing the answer",
    "generate_subquery": "Answering each part",
    "merge_subqueries": "Combining the parts",
    "faithfulness": "Checking it against the source",
    "completeness": "Checking nothing was missed",
    "validation_merge": "Scoring",
    "cache_store": "Finishing up",
}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# ── browser login ────────────────────────────────────────────────────────────

class TokenRequest(BaseModel):
    password: str


@router.post("/auth/token")
async def issue_token(body: TokenRequest):
    """Swap a shared password for a JWT. The chat page keeps the token in
    sessionStorage and sends it as a bearer header, so the user never sees it."""
    if not settings.APP_PASSWORD:
        raise HTTPException(status_code=503, detail="Login is not configured")
    if body.password != settings.APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Wrong password")
    return {"token": jwt.encode({"sub": "chat-user"}, settings.JWT_SECRET, algorithm="HS256")}


# ── streaming query ──────────────────────────────────────────────────────────

class StreamRequest(BaseModel):
    query: str
    session_id: str | None = None
    doc_id: str | None = None


async def _run_stream(graph, initial_state: dict, config: dict, log) -> AsyncIterator[str]:
    streamed_any = False
    final_state: dict | None = None
    # on_chain_start fires for a node AND for inner chains that carry the same
    # langgraph_node in their metadata, so without this the UI shows each step
    # two or three times.
    announced: set[str] = set()
    try:
        async for ev in graph.astream_events(initial_state, config=config, version="v2"):
            kind = ev["event"]
            node = (ev.get("metadata") or {}).get("langgraph_node")

            if kind == "on_chain_start" and node in _FRIENDLY and node not in announced:
                announced.add(node)
                yield _sse({"type": "step", "node": node, "label": _FRIENDLY[node]})

            elif kind == "on_chat_model_stream" and node in _STREAMABLE_NODES:
                chunk = ev["data"].get("chunk")
                text = getattr(chunk, "content", "") if chunk is not None else ""
                # Newer Gemini returns content as a list of blocks, not a string.
                if isinstance(text, list):
                    text = "".join(
                        b.get("text", "") for b in text
                        if isinstance(b, dict) and b.get("type") in (None, "text")
                    )
                if text:
                    streamed_any = True
                    yield _sse({"type": "token", "text": text})

            elif kind == "on_chain_end" and ev.get("name") == "LangGraph":
                final_state = ev["data"].get("output") or {}
    except Exception as exc:
        log.error("stream_failed", error=str(exc)[:300])
        yield _sse({"type": "error", "message": "Something went wrong. Please try again."})
        return

    if not final_state:
        yield _sse({"type": "error", "message": "No response produced."})
        return

    if final_state.get("is_attack"):
        yield _sse({"type": "error", "message": "This request was rejected by the safety check."})
        return

    # The fan-out path could not stream, so deliver the merged answer now.
    if not streamed_any:
        yield _sse({"type": "answer", "text": final_state.get("final_response", "")})

    yield _sse({
        "type": "done",
        "faithfulness": round(float(final_state.get("faithfulness_score", 0.0)), 3),
        "completeness": round(float(final_state.get("completeness_score", 0.0)), 3),
        "validation_passed": bool(final_state.get("validation_passed", False)),
        "model_used": final_state.get("model_used", ""),
        "num_sub_queries": len(final_state.get("sub_queries", []) or []),
        "pii_found": final_state.get("pii_found", []) or [],
        "session_id": initial_state["session_id"],
    })


def make_stream_endpoint(get_graph, blank_state):
    """Built as a factory so main.py can hand in its module-level graph without
    this module importing from main (which would be circular)."""

    @router.post("/query/stream")
    async def query_stream(body: StreamRequest, request: Request):
        graph = get_graph()
        if graph is None:
            raise HTTPException(status_code=503, detail="Service starting, try again shortly")

        query = (body.query or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="query must not be empty")
        if len(query) > settings.MAX_INPUT_CHARS:
            raise HTTPException(status_code=400, detail="query is too long")

        request_id = str(uuid.uuid4())
        session_id = body.session_id or str(uuid.uuid4())
        log = get_logger(request_id, session_id=session_id,
                         user_id=getattr(request.state, "user_id", "chat"))
        log.info("stream_request_received", query_length=len(query))

        state = blank_state(query, session_id, request_id)
        state["doc_id"] = body.doc_id or settings.DEFAULT_DOC_ID
        config = {"configurable": {"thread_id": session_id}}

        return StreamingResponse(
            _run_stream(graph, state, config, log),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return query_stream


# ── documents ────────────────────────────────────────────────────────────────

def make_document_endpoints(get_mongo):
    @router.get("/documents")
    async def list_documents():
        db = get_mongo()
        out = []
        async for d in db.document_trees.find({}, {"doc_id": 1, "tree.title": 1}):
            tree = d.get("tree") or []
            out.append({
                "doc_id": d["doc_id"],
                "title": (tree[0].get("title") if tree else None) or d["doc_id"],
            })
        return {"documents": sorted(out, key=lambda x: x["doc_id"])}

    @router.post("/documents/upload")
    async def upload_document(request: Request, file: UploadFile = File(...), doc_id: str = ""):
        """Submit a PDF to PageIndex and store the resulting tree.

        Done as SSE rather than a background task on purpose: Cloud Run throttles
        CPU once a response is sent and scales to zero, so background work is not
        reliably completed. Streaming progress keeps the request alive instead.
        PageIndex typically takes 1-3 minutes; the platform request cap is 300s,
        so a very large PDF can still time out.
        """
        request_id = str(uuid.uuid4())
        log = get_logger(request_id, node="document_upload")

        name = (file.filename or "document.pdf")
        if not name.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Only PDF files are supported")
        target_id = (doc_id or Path(name).stem).strip().lower().replace(" ", "-")[:64]
        if not target_id:
            raise HTTPException(status_code=400, detail="Could not derive a document id")

        raw = await file.read()
        if not raw.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="That file is not a PDF")
        if len(raw) > 40 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="PDF is larger than 40MB")

        async def gen() -> AsyncIterator[str]:
            tmp = None
            try:
                yield _sse({"type": "step", "label": f"Uploading {name}"})
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
                    fh.write(raw)
                    tmp = fh.name

                from pageindex import PageIndexClient
                client = PageIndexClient(api_key=settings.PAGEINDEX_API_KEY)

                loop = asyncio.get_event_loop()
                submitted = await loop.run_in_executor(None, client.submit_document, tmp)
                pi_id = submitted["doc_id"]
                log.info("pageindex_submitted", pageindex_doc_id=pi_id, doc_id=target_id)
                yield _sse({"type": "step", "label": "Submitted. Building the document tree"})

                tree = None
                for attempt in range(28):
                    ready = await loop.run_in_executor(None, client.is_retrieval_ready, pi_id)
                    if ready:
                        result = await loop.run_in_executor(
                            None, lambda: client.get_tree(pi_id, node_summary=True))
                        tree = result["result"]
                        break
                    yield _sse({"type": "step",
                                "label": f"Still processing ({(attempt + 1) * 8}s)"})
                    await asyncio.sleep(8)

                if tree is None:
                    log.warning("pageindex_timeout", doc_id=target_id)
                    yield _sse({"type": "error",
                                "message": "PageIndex did not finish in time. Try a smaller PDF."})
                    return

                def count(ns):
                    n = 0
                    for x in ns:
                        n += 1
                        if x.get("nodes"):
                            n += count(x["nodes"])
                    return n

                db = get_mongo()
                await db.document_trees.replace_one(
                    {"doc_id": target_id},
                    {"doc_id": target_id, "tree": tree},
                    upsert=True,
                )
                nodes = count(tree)
                log.info("document_indexed", doc_id=target_id, nodes=nodes)
                yield _sse({"type": "indexed", "doc_id": target_id, "nodes": nodes,
                            "title": (tree[0].get("title") if tree else target_id)})
            except Exception as exc:
                log.error("upload_failed", error=str(exc)[:300])
                yield _sse({"type": "error", "message": f"Upload failed: {str(exc)[:160]}"})
            finally:
                if tmp:
                    Path(tmp).unlink(missing_ok=True)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    return list_documents, upload_document
