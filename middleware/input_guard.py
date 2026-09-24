import json
from fastapi import Request
from fastapi.responses import JSONResponse
from app.config import settings


async def input_guard_middleware(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/query":
        body = await request.body()
        # re-attach body so downstream handlers can still read it
        request._body = body

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(status_code=400, content={"detail": "Invalid JSON body"})

        query = data.get("query", "")

        if not isinstance(query, str):
            return JSONResponse(status_code=400, content={"detail": "query must be a string"})

        if not query.strip():
            return JSONResponse(status_code=400, content={"detail": "query must not be empty"})

        if len(query) > settings.MAX_INPUT_CHARS:
            return JSONResponse(
                status_code=400,
                content={"detail": f"query exceeds maximum length of {settings.MAX_INPUT_CHARS} characters"},
            )

    return await call_next(request)
