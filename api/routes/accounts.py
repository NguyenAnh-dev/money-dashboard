from __future__ import annotations

from fastapi import APIRouter

from api.deps import CurrentUser, Db
from api.schemas import AccountOut
from core import repository

router = APIRouter(tags=["data"])


@router.get("/accounts", response_model=list[AccountOut])
def list_accounts(user_id: CurrentUser, db: Db) -> list[AccountOut]:
    return [
        AccountOut.of(account)
        for account in repository.load_accounts(db, user_id)
    ]
