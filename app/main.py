import faulthandler
import logging
import signal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes_analysis import router as analysis_router
from app.config import get_settings
from app.db.base import init_db


# `kill -USR1 <api pid>` dumps every thread's stack to stderr. Run 28 sat in
# one stage for 30+ minutes with 0 CPU and no open sockets, and there was no
# way to see where — a stuck runner thread should be diagnosable in one command.
faulthandler.register(signal.SIGUSR1, all_threads=True)
# Our own loggers speak at INFO (stage timings, shared-mechanism reuse); uvicorn's
# default config only shows WARNING for them.
logging.getLogger("careloop").setLevel(logging.INFO)
logging.getLogger("app").setLevel(logging.INFO)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)

    # Hackathon-permissive: the UI runs on a different origin/port locally.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def _startup() -> None:
        init_db()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(analysis_router)
    return app


app = create_app()
