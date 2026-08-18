"""Development entrypoint.

Run the API with ``python main.py``. Nothing else belongs in this file — all
application wiring lives in ``app/main.py``.
"""

import uvicorn

from app.core.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
    )
