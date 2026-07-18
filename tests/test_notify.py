"""Alert push notifier tests (services/notify): cursor semantics + message shape."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from services.notify.main import Notifier, format_push


class _Row(dict):
    """asyncpg.Record stand-in — mapping access is all the code uses."""


def _alert_row(ts: datetime, entities: list[str] | None = None) -> _Row:
    return _Row(
        story_id=uuid4(),
        ts=ts,
        gap=9.9,
        velocity_z=12.34,
        trust_state="corroborated",
        entity_set=json.dumps(entities if entities is not None else ["tokyo", "quake"]),
    )


def test_format_push_is_compact_and_names_entities() -> None:
    title, body = format_push(_alert_row(datetime.now(UTC)))
    assert title == "Developing story: tokyo, quake"
    assert "gap 9.9" in body and "velocity z 12.3" in body and "corroborated" in body


def test_format_push_handles_missing_story() -> None:
    row = _alert_row(datetime.now(UTC), entities=None)
    row["entity_set"] = None  # story aged out between alert and push
    title, _ = format_push(row)
    assert "unknown entities" in title


class _FakePool:
    def __init__(self, newest: datetime | None, batches: list[list[_Row]]) -> None:
        self.newest = newest
        self.batches = batches

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return _Conn(pool)

            async def __aexit__(self, *a):
                return False

        return _Ctx()


class _Conn:
    def __init__(self, pool: _FakePool) -> None:
        self.pool = pool

    async def fetchval(self, sql: str) -> datetime | None:
        return self.pool.newest

    async def fetch(self, sql: str, cursor: datetime, batch: int) -> list[_Row]:
        rows = self.pool.batches.pop(0) if self.pool.batches else []
        return [r for r in rows if r["ts"] > cursor]


class _PushRecorder:
    def __init__(self, fail_after: int | None = None) -> None:
        self.sent: list[str] = []
        self.fail_after = fail_after

    async def post(self, url: str, **kw) -> httpx.Response:
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            return httpx.Response(500, request=httpx.Request("POST", url))
        self.sent.append(kw["headers"]["Title"])
        return httpx.Response(200, request=httpx.Request("POST", url))


NOW = datetime(2026, 7, 6, 3, 0, tzinfo=UTC)


async def test_cursor_boots_at_newest_and_skips_preexisting() -> None:
    old = _alert_row(NOW - timedelta(hours=1))
    pool = _FakePool(newest=old["ts"], batches=[[old]])
    n = Notifier(pool, _PushRecorder())  # type: ignore[arg-type]
    await n.cycle()
    assert n.pushed == 0  # pre-existing alert not replayed
    assert n.cursor == old["ts"]


async def test_new_alerts_push_and_advance_cursor() -> None:
    a, b = _alert_row(NOW), _alert_row(NOW + timedelta(minutes=1))
    pool = _FakePool(newest=NOW - timedelta(hours=2), batches=[[a, b]])
    rec = _PushRecorder()
    n = Notifier(pool, rec)  # type: ignore[arg-type]
    await n.cycle()
    assert n.pushed == 2 and len(rec.sent) == 2
    assert n.cursor == b["ts"]


async def test_failed_push_holds_cursor_for_redelivery() -> None:
    a, b = _alert_row(NOW), _alert_row(NOW + timedelta(minutes=1))
    pool = _FakePool(newest=NOW - timedelta(hours=2), batches=[[a, b], [a, b]])
    rec = _PushRecorder(fail_after=1)  # first push ok, second 500s
    n = Notifier(pool, rec)  # type: ignore[arg-type]
    try:
        await n.cycle()
    except httpx.HTTPStatusError:
        pass
    assert n.pushed == 1
    assert n.cursor == a["ts"]  # b stays unacknowledged -> re-read next cycle
    rec.fail_after = None
    await n.cycle()
    assert n.pushed == 2 and n.cursor == b["ts"]
