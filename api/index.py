import sys
from pathlib import Path

# Add project root to sys.path so backend imports work seamlessly in Vercel Serverless Functions
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from backend.app import app as fastapi_app


class VercelFastAPIMiddleware:
    """
    ASGI Middleware ensuring proper path resolution under Vercel Serverless Functions
    regardless of internal rewrite destinations or header rewrites.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            headers = dict(scope.get("headers", []))
            forwarded_uri = headers.get(b"x-forwarded-uri", b"").decode("utf-8", "ignore").strip()

            if forwarded_uri:
                scope["path"] = forwarded_uri.split("?")[0]
            elif path.startswith("/api/index.py"):
                sub = path[len("/api/index.py"):]
                scope["path"] = sub if sub else "/"
            elif path.startswith("/api/index"):
                sub = path[len("/api/index"):]
                scope["path"] = sub if sub else "/"

        await self.inner(scope, receive, send)


app = VercelFastAPIMiddleware(fastapi_app)
