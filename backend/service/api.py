"""The analysis API. Runs on demand (plus a daily scheduled pass could hit /report),
reads only from the store, and never talks to a venue directly — that separation is what
keeps the live path and the replay path on identical code (implementation spec, "Two
services, two lifecycles"). Every route that isn't /health or /api/auth/* requires the
single shared-password session.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.core.config import load_config
from backend.core.registry import SourceRegistry
from backend.replay.source import LiveSource
from backend.report.contract import persist_report, run_analysis
from backend.report.exit_monitor import evaluate_exit
from backend.report.render import render_text
from backend.scripts import backfill_history
from backend.service import auth
from backend.service.collector import collect_once
from backend.sources.base import SourceError
from backend.store import db

app = FastAPI(title="TradeSafe evidence API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to your deployed frontend origin in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


def require_auth(tradesafe_session: str | None = Cookie(default=None)) -> None:
    if not auth.verify_session_token(tradesafe_session):
        raise HTTPException(status_code=401, detail="not authenticated")


class LoginBody(BaseModel):
    password: str


@app.post("/api/auth/login")
def login(body: LoginBody, response: Response):
    if not auth.check_password(body.password):
        raise HTTPException(status_code=401, detail="incorrect password")
    token = auth.create_session_token()
    import os

    # Secure cookies require HTTPS. Production always sits behind TLS (see
    # deploy/README.md) so this defaults on; only local/plain-HTTP testing opts out.
    secure = os.environ.get("TRADESAFE_INSECURE_COOKIE", "").lower() != "true"
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=auth.MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=secure,
    )
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


@app.get("/api/auth/status")
def auth_status(tradesafe_session: str | None = Cookie(default=None)):
    return {"authenticated": auth.verify_session_token(tradesafe_session)}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/instruments", dependencies=[Depends(require_auth)])
def list_instruments():
    with db.get_connection() as conn:
        return {"instruments": db.list_watched_instruments(conn)}


def _bootstrap_history(symbol: str, cfg) -> None:
    """Backfill price and open-interest history for a symbol if it has none yet, so
    realised_vol_in_band (which needs ~30 days of single-venue closes) and the OI-based
    checks aren't left unknown on a first-time lookup. Must be called from every entry point a symbol can
    first arrive through — the frontend analyses by calling GET /api/report/{symbol}
    directly and never touches POST /api/instruments/{symbol}, so wiring this only into
    the latter left first-time lookups waiting for a collector restart. Both backfills
    check what's stored before making any network call, so repeat calls are a single
    indexed query each."""
    with httpx.Client() as client:
        for fn in (backfill_history.backfill, backfill_history.backfill_open_interest):
            try:
                fn(symbol, cfg, client=client)
            except SourceError:
                pass


@app.post("/api/instruments/{symbol}", dependencies=[Depends(require_auth)])
async def add_instrument(symbol: str):
    symbol = symbol.upper()
    with db.get_connection() as conn:
        db.add_watched_instrument(conn, symbol)
    # Best-effort immediate fetch so a first-time lookup isn't empty — the collector
    # will keep polling it from here on for real history to accumulate.
    cfg = load_config()
    async with httpx.AsyncClient() as client:
        await collect_once(symbol, cfg, client=client)
    await asyncio.to_thread(_bootstrap_history, symbol, cfg)
    return {"ok": True, "instrument": symbol}


@app.delete("/api/instruments/{symbol}", dependencies=[Depends(require_auth)])
def remove_instrument(symbol: str):
    with db.get_connection() as conn:
        db.remove_watched_instrument(conn, symbol.upper())
    return {"ok": True}


@app.get("/api/report/{symbol}", dependencies=[Depends(require_auth)])
def get_report(symbol: str):
    symbol = symbol.upper()
    cfg = load_config()
    _bootstrap_history(symbol, cfg)
    with db.get_connection() as conn:
        registry = SourceRegistry.from_config(cfg, db.get_all_source_state(conn))
        ds = LiveSource(conn)
        report = run_analysis(symbol, ds, cfg, registry)
        persist_report(conn, report)
        db.add_watched_instrument(conn, symbol)
    return report.to_dict()


@app.get("/api/report/{symbol}/text", dependencies=[Depends(require_auth)])
def get_report_text(symbol: str):
    symbol = symbol.upper()
    cfg = load_config()
    _bootstrap_history(symbol, cfg)
    with db.get_connection() as conn:
        registry = SourceRegistry.from_config(cfg, db.get_all_source_state(conn))
        ds = LiveSource(conn)
        report = run_analysis(symbol, ds, cfg, registry)
        persist_report(conn, report)
        db.add_watched_instrument(conn, symbol)
    return Response(content=render_text(report), media_type="text/plain")


@app.get("/api/report/{symbol}/history", dependencies=[Depends(require_auth)])
def get_report_history(symbol: str, limit: int = 20):
    with db.get_connection() as conn:
        return {"records": db.list_decision_records(conn, symbol.upper(), limit=limit)}


class PositionBody(BaseModel):
    instrument: str
    setup: str
    entry_evidence: dict
    trapped_cohort: dict


@app.post("/api/positions", dependencies=[Depends(require_auth)])
def create_position(body: PositionBody):
    position_id = str(uuid.uuid4())
    record = {
        "position_id": position_id,
        "instrument": body.instrument.upper(),
        "setup": body.setup,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "entry_evidence": body.entry_evidence,
        "trapped_cohort": body.trapped_cohort,
    }
    with db.get_connection() as conn:
        db.save_position(conn, record)
    return {"ok": True, "position_id": position_id}


@app.get("/api/positions", dependencies=[Depends(require_auth)])
def list_positions():
    with db.get_connection() as conn:
        return {"positions": db.list_open_positions(conn)}


@app.get("/api/positions/{position_id}/exit-status", dependencies=[Depends(require_auth)])
def position_exit_status(position_id: str):
    cfg = load_config()
    with db.get_connection() as conn:
        position = db.get_position(conn, position_id)
        if position is None:
            raise HTTPException(status_code=404, detail="position not found")
        ds = LiveSource(conn)
        return evaluate_exit(position, ds, cfg)


@app.post("/api/positions/{position_id}/close", dependencies=[Depends(require_auth)])
def close_position(position_id: str):
    with db.get_connection() as conn:
        if db.get_position(conn, position_id) is None:
            raise HTTPException(status_code=404, detail="position not found")
        db.close_position(conn, position_id, at=datetime.now(timezone.utc))
    return {"ok": True}
