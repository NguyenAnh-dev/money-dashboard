from __future__ import annotations

from datetime import date

from pydantic import BaseModel, EmailStr, Field

from core.models import Account, AccountType, Transaction, TransactionKind
from core.passwords import MIN_PASSWORD_LENGTH


class AccountOut(BaseModel):
    id: str
    name: str
    institution: str
    type: AccountType
    balance_cents: int
    currency: str
    mask: str | None = None
    available_cents: int | None = None
    as_of: date | None = None

    @classmethod
    def of(cls, account: Account) -> "AccountOut":
        return cls(
            id=account.id,
            name=account.name,
            institution=account.institution,
            type=account.type,
            balance_cents=account.balance_cents,
            currency=account.currency,
            mask=account.mask,
            available_cents=account.available_cents,
            as_of=account.as_of,
        )


class TransactionOut(BaseModel):
    id: str
    account_id: str
    date: date
    name: str
    merchant: str
    amount_cents: int
    category: str
    currency: str
    kind: TransactionKind
    pending: bool

    @classmethod
    def of(cls, txn: Transaction) -> "TransactionOut":
        return cls(
            id=txn.id,
            account_id=txn.account_id,
            date=txn.date,
            name=txn.name,
            merchant=txn.merchant,
            amount_cents=txn.amount_cents,
            category=txn.category,
            currency=txn.currency,
            kind=txn.kind,
            pending=txn.pending,
        )


class CategorySpendOut(BaseModel):
    category: str
    amount_cents: int


class RecurringEvidenceOut(BaseModel):
    date: date
    amount_cents: int


class RecurringChargeOut(BaseModel):
    merchant: str
    account_id: str
    latest_cents: int
    previous_cents: int | None
    delta_cents: int | None
    direction: str | None
    day: int
    last_date: date
    occurrences: int
    evidence: list[RecurringEvidenceOut]

    @classmethod
    def of(cls, charge) -> "RecurringChargeOut":
        return cls(
            merchant=charge.merchant,
            account_id=charge.account_id,
            evidence=[RecurringEvidenceOut(date=d, amount_cents=c)
                      for d, c in charge.evidence],
            latest_cents=charge.latest_cents,
            previous_cents=charge.previous_cents,
            delta_cents=charge.delta_cents,
            direction=charge.direction,
            day=charge.day,
            last_date=charge.last_date,
            occurrences=charge.occurrences,
        )


class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: str
    email: EmailStr


class LinkTokenOut(BaseModel):
    link_token: str


class ExchangeIn(BaseModel):
    public_token: str = Field(min_length=1)


class ConnectionOut(BaseModel):
    provider: str
    connection_id: str
    linked: bool
