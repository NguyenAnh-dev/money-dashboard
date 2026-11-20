from __future__ import annotations

from datetime import date

import pytest

import sync as sync_module
from core import repository, store, users
from core.models import (
    Account,
    AccountType,
    ConnectionRef,
    Provider,
    SourceRef,
    SyncResult,
    Transaction,
    make_id,
)


class Source:
    def __init__(self, cid: str, version: int = 1, fail_fetch: bool = False):
        self.cid = cid
        self.version = version
        self.fail_fetch = fail_fetch
        self.ref = SourceRef(Provider.FAKE, f"acct-{cid}", cid)

    def connection(self):
        return ConnectionRef(provider=Provider.FAKE, connection_id=self.cid)

    def fetch_accounts(self):
        return [Account(
            id=make_id(self.ref), source=self.ref,
            name=f"Acct {self.cid} v{self.version}",
            institution="Bank", type=AccountType.CHECKING,
            balance_cents=100 * self.version,
        )]

    def _txn(self, key: str, amount: int):
        tref = SourceRef(Provider.FAKE, f"{key}-{self.cid}", self.cid)
        return Transaction(
            id=make_id(tref), source=tref, account_id=make_id(self.ref),
            date=date(2026, 1, 5), name=f"SHOP v{self.version}",
            merchant="Shop", amount_cents=amount, category="Shopping",
        )

    def fetch_transactions(self, cursor=None):
        if self.fail_fetch:
            raise RuntimeError("network down")
        added = [self._txn("txn", 500 * self.version)]
        if self.version > 1:
            added.append(self._txn("extra", 99))
        return SyncResult(added=added, cursor=f"cursor-{self.cid}-v{self.version}")


def _state(cid: str):
    conn = store.open_db()
    try:
        uid = users.ensure_local_user(conn)
        ref = ConnectionRef(provider=Provider.FAKE, connection_id=cid)
        txns = sorted(
            (t.id, t.name, t.amount_cents)
            for t in repository.load_transactions(conn, uid)
            if t.source.connection_id == cid
        )
        accts = sorted(
            (a.id, a.name, a.balance_cents)
            for a in repository.load_accounts(conn, uid)
            if a.source.connection_id == cid
        )
        return txns, accts, repository.cursor_for(conn, uid, ref)
    finally:
        conn.close()


def _use(monkeypatch, tmp_path, sources):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "sync.db"))
    monkeypatch.setattr(sync_module, "sources_for",
                        lambda conn, uid, inc=True: list(sources))


def _fail_after_real_write(monkeypatch, cid: str):
    real = repository.save_transactions
    calls = []

    def writer(conn, user_id, transactions):
        written = real(conn, user_id, transactions)
        if any(t.source.connection_id == cid for t in transactions):
            calls.append(written)
            raise RuntimeError("disk exploded")
        return written

    monkeypatch.setattr(repository, "save_transactions", writer)
    return calls


def test_later_failure_preserves_b_and_keeps_a(tmp_path, monkeypatch, capsys):
    _use(monkeypatch, tmp_path, [Source("a"), Source("b")])
    assert sync_module.main() == 0
    capsys.readouterr()

    before_a = _state("a")
    before_b = _state("b")
    assert before_b[0] and before_b[1] and before_b[2] == "cursor-b-v1", (
        "positive control: B is seeded"
    )

    _use(monkeypatch, tmp_path, [Source("a", 2), Source("b", 2)])
    writes = _fail_after_real_write(monkeypatch, "b")

    with pytest.raises(sync_module.SyncFailed) as info:
        sync_module.main()
    err = capsys.readouterr().err

    assert writes == [2], "the real writer ran for B before the failure"
    assert isinstance(info.value.__cause__, RuntimeError)
    assert info.value.failed.connection_id == "b"
    assert [r.connection.connection_id for r in info.value.saved] == ["a"]
    assert "sync FAILED on fake:b" in err
    assert "disk exploded" in err
    assert "did not finish saving" in err
    assert "already saved and stay saved: fake:a" in err
    assert "rolled back" not in err
    assert "database is unchanged" not in err

    after_a = _state("a")
    assert after_a != before_a, "positive control: A really changed"
    assert after_a[2] == "cursor-a-v2"
    assert len(after_a[0]) == 2
    assert after_a[1][0][1] == "Acct a v2"

    assert _state("b") == before_b, "B's rows and cursor are as seeded"


def test_first_failure_during_save_reports_no_completed_save(
        tmp_path, monkeypatch, capsys):
    _use(monkeypatch, tmp_path, [Source("a"), Source("b")])
    _fail_after_real_write(monkeypatch, "a")

    with pytest.raises(sync_module.SyncFailed) as info:
        sync_module.main()
    err = capsys.readouterr().err

    assert info.value.saved == []
    assert "sync FAILED on fake:a" in err
    assert "no connection save had completed" in err
    assert "may already exist" in err
    assert "already saved and stay saved" not in err
    assert "database is unchanged" not in err
    assert "rolled back" not in err
    assert _state("a") == ([], [], None)
    assert _state("b") == ([], [], None), "b never ran"


def test_failure_before_save_begins_is_not_called_a_rollback(
        tmp_path, monkeypatch, capsys):
    _use(monkeypatch, tmp_path, [Source("a"), Source("b", fail_fetch=True)])

    with pytest.raises(sync_module.SyncFailed) as info:
        sync_module.main()
    err = capsys.readouterr().err

    assert info.value.failed.connection_id == "b"
    assert "network down" in err
    assert "fake:b did not finish saving" in err
    assert "already saved and stay saved: fake:a" in err
    assert "rolled back" not in err
    assert _state("a")[2] == "cursor-a-v1", "positive control: A saved"
    assert _state("b") == ([], [], None)


def test_failure_outside_any_connection_does_not_claim_unchanged_database(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "sync.db"))

    def boom(conn, uid, inc=True):
        raise RuntimeError("no sources")

    monkeypatch.setattr(sync_module, "sources_for", boom)

    with pytest.raises(RuntimeError):
        sync_module.main()
    err = capsys.readouterr().err

    assert "no sources" in err
    assert "before any connection save completed" in err
    assert "database is unchanged" not in err


def test_success_still_saves_every_connection(tmp_path, monkeypatch):
    _use(monkeypatch, tmp_path, [Source("a"), Source("b")])

    assert sync_module.main() == 0
    for cid in ("a", "b"):
        txns, accts, cursor = _state(cid)
        assert len(txns) == 1 and len(accts) == 1
        assert cursor == f"cursor-{cid}-v1"
