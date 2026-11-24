from fastapi import APIRouter, Depends, FastAPI

from app.api import routes_runs, routes_sources
from app.api.deps import require_auth


def create_app() -> FastAPI:
    app = FastAPI(title="KPI Monitor", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    protected = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
    protected.include_router(routes_sources.router)
    protected.include_router(routes_runs.router)
    app.include_router(protected)
    return app


app = create_app()
