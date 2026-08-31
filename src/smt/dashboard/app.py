"""FastAPI application: read-only JSON API + built React SPA."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings, get_market, get_settings
from ..logging_setup import get_logger
from ..market import MarketData
from ..store import Store
from .auth import auth_required, verify_dashboard_token
from .schemas import (
    HealthResponse,
    OpportunitiesResponse,
    OverviewResponse,
    PerformanceResponse,
    PositionsResponse,
    RiskResponse,
    ShadowResponse,
    TradesResponse,
)
from .service import DashboardService, coinbase_equity_reader

log = get_logger("smt.dashboard")
STATIC_DIR = Path(__file__).resolve().parent / "static"
_FALLBACK_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>SMT dashboard</title>
<style>body{font-family:Segoe UI,system-ui,sans-serif;background:#0e1116;color:#d7dde5;
padding:48px;max-width:640px}code{background:#1a2028;padding:2px 6px;border-radius:4px}</style>
</head><body>
<h1>SMT dashboard</h1>
<p>API is running. Build the UI with <code>npm install && npm run build</code> in
<code>web/</code>, or run the Vite dev server against this API.</p>
</body></html>
"""


def create_app(
    *,
    store: Store | None = None,
    settings: Settings | None = None,
    token: str | None = None,
    require_auth: bool | None = None,
    bind_host: str = "127.0.0.1",
    marks: dict[str, float] | None = None,
    market: MarketData | None = None,
    service: DashboardService | None = None,
) -> FastAPI:
    settings = settings if settings is not None else get_settings()
    store = store if store is not None else Store(settings.database_url)
    expected_token = settings.dashboard_token if token is None else token
    must_auth = (
        auth_required(require_auth=settings.dashboard_require_auth, bind_host=bind_host)
        if require_auth is None
        else require_auth
    )
    if service is None:
        if market is None and marks is None:
            try:
                market = MarketData(get_market())
            except Exception as exc:  # noqa: BLE001
                log.warning("dashboard market data unavailable: %s", exc)
                market = None
        service = DashboardService(
            store,
            settings,
            market=market,
            marks=marks,
            live_equity=coinbase_equity_reader(settings),
        )

    app = FastAPI(title="SMT dashboard", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.dashboard_token = expected_token
    app.state.require_auth = must_auth
    app.state.service = service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=True,
        allow_methods=["GET"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    api = APIRouter(prefix="/api", dependencies=[Depends(verify_dashboard_token)])

    def _svc(request: Request) -> DashboardService:
        return request.app.state.service

    @api.get("/health", response_model=HealthResponse)
    def api_health(request: Request) -> HealthResponse:
        return _svc(request).health()

    @api.get("/overview", response_model=OverviewResponse)
    def api_overview(request: Request) -> OverviewResponse:
        return _svc(request).overview()

    @api.get("/positions", response_model=PositionsResponse)
    def api_positions(request: Request) -> PositionsResponse:
        return _svc(request).positions()

    @api.get("/trades", response_model=TradesResponse)
    def api_trades(
        request: Request,
        strategy: Annotated[str | None, Query()] = None,
        ticker: Annotated[str | None, Query()] = None,
        exit_reason: Annotated[str | None, Query()] = None,
        start: Annotated[datetime | None, Query()] = None,
        end: Annotated[datetime | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> TradesResponse:
        try:
            return _svc(request).trades(
                strategy=strategy,
                ticker=ticker,
                exit_reason=exit_reason,
                start=start,
                end=end,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/performance", response_model=PerformanceResponse)
    def api_performance(request: Request) -> PerformanceResponse:
        return _svc(request).performance()

    @api.get("/risk", response_model=RiskResponse)
    def api_risk(request: Request) -> RiskResponse:
        return _svc(request).risk()

    @api.get("/opportunities", response_model=OpportunitiesResponse)
    def api_opportunities(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> OpportunitiesResponse:
        return _svc(request).opportunities(limit)

    @api.get("/shadow", response_model=ShadowResponse)
    def api_shadow(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> ShadowResponse:
        return _svc(request).shadow(limit)

    app.include_router(api)

    if STATIC_DIR.is_dir():
        assets = STATIC_DIR / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = (STATIC_DIR / full_path).resolve()
        if STATIC_DIR.resolve() in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        index = STATIC_DIR / "index.html"
        if index.is_file():
            return FileResponse(index)
        return HTMLResponse(_FALLBACK_HTML)

    return app
