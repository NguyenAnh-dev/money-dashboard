from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from api.deps import CurrentUser, Db
from api.schemas import ConnectionOut, ExchangeIn, LinkTokenOut
from adapters import plaid_source
from core import repository
from core.models import ConnectionRef, Provider

router = APIRouter(prefix="/plaid", tags=["plaid"])


@router.post("/link-token", response_model=LinkTokenOut)
def link_token(user_id: CurrentUser) -> LinkTokenOut:
    return LinkTokenOut(link_token=plaid_source.create_link_token(user_id))


@router.post(
    "/exchange", response_model=ConnectionOut,
    status_code=status.HTTP_201_CREATED,
)
def exchange(body: ExchangeIn, user_id: CurrentUser, db: Db) -> ConnectionOut:
    access_token, item_id = plaid_source.exchange_public_token(
        body.public_token
    )
    connection = ConnectionRef(
        provider=Provider.PLAID, connection_id=item_id
    )

    owner = repository.owner_of(db, connection)
    if owner is not None and owner != user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That institution is already linked to another account",
        )

    with db:
        repository.save_access_token(db, user_id, connection, access_token)

    return ConnectionOut(
        provider=connection.provider.value,
        connection_id=connection.connection_id,
        linked=True,
    )


@router.get("/connections", response_model=list[ConnectionOut])
def connections(user_id: CurrentUser, db: Db) -> list[ConnectionOut]:
    linked = {
        ref.connection_id
        for ref, _ in repository.linked_connections(db, user_id)
    }
    return [
        ConnectionOut(
            provider=ref.provider.value,
            connection_id=ref.connection_id,
            linked=ref.connection_id in linked,
        )
        for ref in repository.connections_for(db, user_id)
    ]
