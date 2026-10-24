import sys
from pathlib import Path

# Add project root to sys.path so backend imports work seamlessly in Vercel Serverless Functions
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from fastapi import Request
from backend.app import app as fastapi_app


@fastapi_app.get("/api/debug-path")
async def debug_path(request: Request):
    return {
        "url": str(request.url),
        "path": request.scope.get("path"),
        "raw_headers": [k.decode("utf-8", "ignore") for k, _ in request.scope.get("headers", [])],
        "matched_path": request.headers.get("x-matched-path"),
        "forwarded_uri": request.headers.get("x-forwarded-uri"),
    }


class VercelFastAPIMiddleware:
    """
    ASGI Middleware ensuring proper path resolution under Vercel Serverless Functions
    regardless of internal rewrite destinations or header rewrites.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers = dict(scope.get("headers", []))
            matched = headers.get(b"x-matched-path", b"").decode("utf-8", "ignore").strip()
            forwarded = headers.get(b"x-forwarded-uri", b"").decode("utf-8", "ignore").strip()
            original = matched or forwarded

            if original:
                scope["path"] = original.split("?")[0]
            else:
                path = scope.get("path", "")
                if path.startswith("/api/index.py"):
                    sub = path[len("/api/index.py"):]
                    scope["path"] = sub if sub else "/"
                elif path.startswith("/api/index"):
                    sub = path[len("/api/index"):]
                    scope["path"] = sub if sub else "/"

        await self.inner(scope, receive, send)


app = VercelFastAPIMiddleware(fastapi_app)
