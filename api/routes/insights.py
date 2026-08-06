from __future__ import annotations

from fastapi import APIRouter

from api.deps import CurrentUser, Db
from api.schemas import CategorySpendOut, RecurringChargeOut
from core import repository
from insights import find_recurring, spending_by_category

router = APIRouter(tags=["data"])


@router.get("/spending", response_model=list[CategorySpendOut])
def spending(user_id: CurrentUser, db: Db) -> list[CategorySpendOut]:
    transactions = repository.load_transactions(db, user_id)
    return [
        CategorySpendOut(category=category, amount_cents=cents)
        for category, cents in spending_by_category(transactions)
    ]


@router.get("/recurring", response_model=list[RecurringChargeOut])
def recurring(user_id: CurrentUser, db: Db) -> list[RecurringChargeOut]:
    transactions = repository.load_transactions(db, user_id)
    return [
        RecurringChargeOut.of(charge) for charge in find_recurring(transactions)
    ]
