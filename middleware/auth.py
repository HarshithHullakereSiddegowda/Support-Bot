from app.config import settings
from fastapi import Request
from fastapi.responses import JSONResponse
from jose import JWTError, jwt


async def auth_middleware(request: Request, call_next):
    # skip health check endpoint
    if request.url.path in ("/health", "/docs", "/openapi.json"):
        return await call_next(request)

    # NOTE: return, do not raise. HTTPException raised inside middleware is not
    # caught by FastAPI's handler (that only wraps route handlers), so it escapes
    # as an unhandled error and the client gets 500 instead of 401.
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"detail": "Missing or malformed token"})

    token = auth_header.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        request.state.user_id = payload["sub"]
        request.state.user_payload = payload
    except JWTError as e:
        return JSONResponse(status_code=401, content={"detail": f"Invalid token: {e}"})

    return await call_next(request)
