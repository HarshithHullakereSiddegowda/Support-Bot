from app.config import settings
from fastapi import Request
from fastapi.responses import JSONResponse
from jose import JWTError, jwt

# Paths reachable without a token.
#   /chat        the login page itself; it then calls /auth/token for a JWT and
#                sends that as a bearer header on every subsequent call
#   /auth/token  the exchange endpoint, which validates a password instead
#   the rest     health and API documentation
PUBLIC_PATHS = frozenset({
    "/", "/chat", "/health", "/docs", "/openapi.json", "/auth/token",
})


async def auth_middleware(request: Request, call_next):
    # Compare on the path with any trailing slash removed. FastAPI would redirect
    # /chat/ to /chat, but middleware runs BEFORE routing, so an exact-match check
    # rejected /chat/ with a 401 before the redirect could happen -- the page
    # simply refused to load for anyone who typed the slash.
    path = request.url.path
    normalised = path.rstrip("/") or "/"
    if normalised in PUBLIC_PATHS:
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
