from __future__ import annotations

from fastapi import APIRouter

from api.deps import CurrentUser, Db
from api.schemas import TransactionOut
from core import repository

router = APIRouter(tags=["data"])


@router.get("/transactions", response_model=list[TransactionOut])
def list_transactions(user_id: CurrentUser, db: Db) -> list[TransactionOut]:
    return [
        TransactionOut.of(txn)
        for txn in repository.load_transactions(db, user_id)
    ]
