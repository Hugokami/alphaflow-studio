import os
import uvicorn
from backend.app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8090"))
    print(f"Starting LTstudio Studio at http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port)
