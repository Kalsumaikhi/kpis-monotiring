from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import (
    routes_credentials, routes_kpis, routes_logins, routes_runs, routes_sources, routes_uploads,
    routes_watch,
)
from app.api.deps import require_auth

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER, CSRF_VALUE = "X-Requested-With", "kpi-monitor"


def create_app() -> FastAPI:
    app = FastAPI(title="KPI Monitor", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def require_requested_with(request: Request, call_next):
        # CSRF guard. Auth is HTTP Basic, which browsers resend on cross-site form posts; a
        # custom header cannot be set cross-site without a CORS preflight, which this app never
        # grants. Runs before routing, so it refuses before auth (401) or 404/405.
        if (request.method in UNSAFE_METHODS and request.url.path.startswith("/api/")
                and request.headers.get(CSRF_HEADER) != CSRF_VALUE):
            return JSONResponse({"detail": "missing X-Requested-With"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # FastAPI's default 422 echoes the offending input (and ctx); for /api/credentials
        # that can be the secret, so no 422 ever carries request data back.
        errors = [{k: v for k, v in e.items() if k not in ("input", "ctx")} for e in exc.errors()]
        return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    protected = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
    protected.include_router(routes_sources.router)
    protected.include_router(routes_runs.router)
    protected.include_router(routes_kpis.router)
    protected.include_router(routes_watch.router)
    protected.include_router(routes_uploads.router)
    protected.include_router(routes_credentials.router)
    protected.include_router(routes_logins.router)
    app.include_router(protected)
    return app


app = create_app()
