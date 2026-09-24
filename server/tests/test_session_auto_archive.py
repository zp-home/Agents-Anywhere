"""Behaviour of the inactivity sweeper.

The point of these tests is the *asymmetry* between an auto-archive and a user
archive. An auto-archive is a fold that undoes itself; a user archive is a
decision that nothing undoes. Every test below pins one edge of that line.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ApiV2TestClient as TestClient
from sqlalchemy import select, update

from agent_server.app import create_app
from agent_server.infra.db import sessions as sessions_t
from agent_server.infra.db import timeline_items as timeline_items_t
from agent_server.services.session_auto_archive import (
    AUTO_ARCHIVE_INACTIVE_DAYS,
    auto_archive_cutoff,
)


def _iso(days_ago: float) -> str:
    moment = datetime.now(UTC) - timedelta(days=days_ago)
    return moment.isoformat().replace("+00:00", "Z")


LONG_AGO = _iso(AUTO_ARCHIVE_INACTIVE_DAYS + 5)
RECENTLY = _iso(1)


@dataclass
class SweepClient:
    client: TestClient
    headers: dict[str, str]
    connector_headers: dict[str, str]
    connector_id: str

    @property
    def store(self):
        return self.client.app.state.store

    def create(self, session_id: str, *, runtime: str = "codex") -> None:
        response = self.client.post(
            "/connector/ingest",
            headers=self.connector_headers,
            json={
                "notifications": [
                    {
                        "method": "session.meta.upsert",
                        "params": {
                            "sessionId": session_id,
                            "runtime": runtime,
                            "externalSessionId": f"external_{session_id}",
                            "title": session_id,
                            "cwd": "/repo",
                        },
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text

    def set_activity(self, session_id: str, when: str) -> None:
        """Force every column the activity expression can read.

        ``_session_sort_at`` coalesces the newest timeline row with
        ``last_activity_at`` and then falls back to ``sort_at``/``created_at``,
        so a test that moved only one of them would be asserting against
        whichever branch happened to win.
        """

        async def run() -> None:
            async with self.store.engine.begin() as conn:
                await conn.execute(
                    update(sessions_t)
                    .where(sessions_t.c.id == session_id)
                    .values(
                        last_activity_at=when,
                        sort_at=when,
                        created_at=when,
                        updated_at=when,
                    )
                )
                await conn.execute(
                    update(timeline_items_t)
                    .where(timeline_items_t.c.session_id == session_id)
                    .values(item_time=when)
                )

        asyncio.run(run())

    def set_status(self, session_id: str, status: str) -> None:
        async def run() -> None:
            async with self.store.engine.begin() as conn:
                await conn.execute(
                    update(sessions_t)
                    .where(sessions_t.c.id == session_id)
                    .values(status=status)
                )

        asyncio.run(run())

    def row(self, session_id: str) -> tuple[int, int, str | None]:
        async def run():
            async with self.store.engine.connect() as conn:
                result = (
                    await conn.execute(
                        select(
                            sessions_t.c.archived,
                            sessions_t.c.auto_archived,
                            sessions_t.c.auto_archived_at,
                        ).where(sessions_t.c.id == session_id)
                    )
                ).one()
                return int(result.archived), int(result.auto_archived), result.auto_archived_at

        return asyncio.run(run())

    def sweep(self) -> None:
        asyncio.run(self.client.app.state.session_auto_archive_sweeper.run_once())

    def session(self, session_id: str) -> dict:
        response = self.client.get(f"/sessions/{session_id}/meta", headers=self.headers)
        assert response.status_code == 200, response.text
        return response.json()["session"]

    def listed_ids(self, *, archived: bool) -> set[str]:
        response = self.client.get(
            "/sessions",
            headers=self.headers,
            params={"archived": archived, "limit": 100},
        )
        assert response.status_code == 200, response.text
        return {session["id"] for session in response.json()["sessions"]}

    def pin(self, session_id: str) -> None:
        response = self.client.patch(
            f"/sessions/{session_id}/meta",
            headers=self.headers,
            json={"pinned": True},
        )
        assert response.status_code == 200, response.text

    def unarchive(self, session_id: str) -> None:
        response = self.client.post(
            "/sessions/unarchive", headers=self.headers, json=[session_id]
        )
        assert response.status_code == 200, response.text

    def archive(self, session_id: str) -> None:
        response = self.client.post(
            "/sessions/archive", headers=self.headers, json=[session_id]
        )
        assert response.status_code == 200, response.text


@pytest.fixture
def sweep_client(tmp_path):
    client = TestClient(create_app(tmp_path / "auto-archive.sqlite3"))
    client.get("/auth/config")
    registered = client.post(
        "/auth/register",
        json={
            "email": "sweeper-owner@example.com",
            "displayName": "Sweeper Owner",
            "password": "secret",
            "setupToken": client.app.state.setup_token.peek(),
        },
    )
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['accessToken']}"}
    connector_response = client.post(
        "/connectors", headers=headers, json={"name": "Device"}
    )
    assert connector_response.status_code == 200, connector_response.text
    connector = connector_response.json()
    connector_id = connector["connector"]["id"]
    authenticated = client.post(
        "/connector/auth",
        headers={
            "Authorization": f"Connector {connector_id}:{connector['connectorToken']}"
        },
    )
    assert authenticated.status_code == 200, authenticated.text
    try:
        yield SweepClient(
            client,
            headers,
            {"Authorization": f"Bearer {authenticated.json()['accessToken']}"},
            connector_id,
        )
    finally:
        client.close()
        asyncio.run(client.app.state.store.close())


def test_cutoff_matches_the_stored_timestamp_shape():
    # Activity is compared lexicographically, so a cutoff formatted even
    # slightly differently would compare wrong rather than fail loudly.
    cutoff = auto_archive_cutoff()
    assert cutoff.endswith("Z")
    assert "+00:00" not in cutoff
    assert LONG_AGO < cutoff < RECENTLY


def test_idle_session_past_the_threshold_leaves_the_sidebar(sweep_client):
    api = sweep_client
    api.create("old")
    api.set_activity("old", LONG_AGO)

    api.sweep()

    assert api.row("old")[:2] == (1, 1)
    assert "old" not in api.listed_ids(archived=False)
    assert "old" in api.listed_ids(archived=True)
    view = api.session("old")
    assert view["archived"] is True
    assert view["autoArchived"] is True
    # The settings page must not label this as the user's own decision.
    assert view["userArchived"] is False
    assert view["archiveSource"] is None


def test_a_session_inside_the_threshold_is_untouched(sweep_client):
    api = sweep_client
    api.create("fresh")
    api.set_activity("fresh", RECENTLY)

    api.sweep()

    assert api.row("fresh")[:2] == (0, 0)
    assert "fresh" in api.listed_ids(archived=False)


def test_pinned_sessions_are_exempt_however_old(sweep_client):
    api = sweep_client
    api.create("pinned")
    api.pin("pinned")
    api.set_activity("pinned", LONG_AGO)

    api.sweep()

    assert api.row("pinned")[:2] == (0, 0)
    assert "pinned" in api.listed_ids(archived=False)


@pytest.mark.parametrize(
    "status",
    ["running", "pending", "stopping", "waiting_approval", "waiting", "blocked"],
)
def test_only_settled_sessions_are_archived(sweep_client, status):
    # A session carrying an outstanding obligation must not be folded away.
    # "waiting_approval" is literally a question the user has not answered yet.
    api = sweep_client
    api.create("busy")
    api.set_activity("busy", LONG_AGO)
    api.set_status("busy", status)

    api.sweep()

    assert api.row("busy")[:2] == (0, 0)


def test_manual_unarchive_is_not_undone_by_the_next_sweep(sweep_client):
    """The tombstone. Without it the sweeper immediately overrules the user."""

    api = sweep_client
    api.create("rescued")
    api.set_activity("rescued", LONG_AGO)
    api.sweep()
    assert api.row("rescued")[:2] == (1, 1)

    api.unarchive("rescued")
    archived, auto_archived, tombstone = api.row("rescued")
    assert (archived, auto_archived) == (0, 0)
    assert tombstone is not None, "unarchive must leave the tombstone behind"

    # The session is still older than the threshold. It must survive anyway.
    api.set_activity("rescued", LONG_AGO)
    api.sweep()
    api.sweep()

    assert api.row("rescued")[:2] == (0, 0)
    assert "rescued" in api.listed_ids(archived=False)


def test_a_user_archive_is_never_revived_by_the_sweeper(sweep_client):
    api = sweep_client
    api.create("deliberate")
    api.archive("deliberate")
    api.set_activity("deliberate", RECENTLY)

    api.sweep()

    archived, auto_archived, _ = api.row("deliberate")
    assert (archived, auto_archived) == (1, 0)
    assert "deliberate" in api.listed_ids(archived=True)
    assert api.session("deliberate")["userArchived"] is True


def test_new_activity_brings_an_auto_archived_session_back(sweep_client):
    api = sweep_client
    api.create("returning")
    api.set_activity("returning", LONG_AGO)
    api.sweep()
    assert api.row("returning")[:2] == (1, 1)

    api.set_activity("returning", RECENTLY)
    api.sweep()

    archived, auto_archived, tombstone = api.row("returning")
    assert (archived, auto_archived) == (0, 0)
    # Real activity clears the tombstone, unlike a manual unarchive, so the
    # session is eligible for another fold once it goes quiet again.
    assert tombstone is None
    assert "returning" in api.listed_ids(archived=False)
    assert api.session("returning")["archivedAt"] is None

    api.set_activity("returning", LONG_AGO)
    api.sweep()
    assert api.row("returning")[:2] == (1, 1)


def test_clear_auto_archive_revives_only_the_sweeper_s_own_archive(sweep_client):
    api = sweep_client
    api.create("auto")
    api.create("manual")
    api.set_activity("auto", LONG_AGO)
    api.sweep()
    api.archive("manual")

    async def run(session_id):
        return await api.store.clear_auto_archive(session_id)

    assert asyncio.run(run("auto")) is True
    assert api.row("auto") == (0, 0, None)

    assert asyncio.run(run("manual")) is False
    assert api.row("manual")[:2] == (1, 0)


def test_a_stuck_run_keeps_its_session_out_of_the_sweep(sweep_client):
    """``status`` can drift; ``session_active_runs`` is the authoritative record.

    A run that never reported completion leaves the row at "idle" while work is
    still outstanding, so the status allow-list alone is not enough.
    """

    api = sweep_client
    api.create("stuck")
    api.set_activity("stuck", LONG_AGO)
    api.set_status("stuck", "idle")
    asyncio.run(
        api.store.start_active_run(session_id="stuck", runtime="codex")
    )

    api.sweep()

    assert api.row("stuck")[:2] == (0, 0)

    asyncio.run(api.store.clear_active_run("stuck"))
    api.sweep()
    assert api.row("stuck")[:2] == (1, 1)


def test_errored_sessions_are_settled_enough_to_archive(sweep_client):
    api = sweep_client
    api.create("failed")
    api.set_activity("failed", LONG_AGO)
    api.set_status("failed", "error")

    api.sweep()

    assert api.row("failed")[:2] == (1, 1)


def test_sweeping_an_empty_database_is_a_no_op(sweep_client):
    sweep_client.sweep()


def test_repeated_sweeps_do_not_rewrite_settled_rows(sweep_client):
    api = sweep_client
    api.create("stable")
    api.set_activity("stable", LONG_AGO)
    api.sweep()
    first = api.row("stable")

    api.sweep()
    api.sweep()

    assert api.row("stable") == first
