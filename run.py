"""Local launcher: `python run.py`.

Reads PORT (default 8000) and DAMBLE_RELOAD (default on) from the environment so
it works both for local dev and as a fallback prod entrypoint. In production
(Railway) the Procfile invokes uvicorn directly with --workers 1 and no reload.
"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("DAMBLE_RELOAD", "1") == "1",
    )
