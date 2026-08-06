from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.routes import accounts, auth, insights, plaid, transactions
from core import crypto


@asynccontextmanager
async def lifespan(app: FastAPI):
    crypto.require_key()
    yield


app = FastAPI(
    title="Money Dashboard API",
    version="0.1",
    summary="Read your synced accounts and transactions over HTTP.",
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(insights.router)
app.include_router(plaid.router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}
