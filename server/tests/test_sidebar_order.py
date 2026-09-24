from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from test_backend_mvp import (
    account_user_id,
    auth_headers,
    create_connector_and_session,
    dashboard_ws_ticket,
    make_client,
)

from agent_server.infra.db import user_sidebar_orders


def test_sidebar_order_is_per_user_and_each_kind_is_replaced_whole(tmp_path) -> None:
    client = make_client(tmp_path)
    headers = auth_headers(client)
    other = auth_headers(client, user_id="user2")

    empty = client.get("/sidebar-order", headers=headers)
    assert empty.status_code == 200, empty.text
    assert empty.json()["sidebarOrder"] == {"projects": [], "sessions": []}

    saved = client.put(
        "/sidebar-order",
        headers=headers,
        json={"kind": "sessions", "ids": ["s2", "s1", "s2", "s3"]},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["sidebarOrder"] == {"projects": [], "sessions": ["s2", "s1", "s3"]}

    # Writing projects leaves the stored session order alone; a second session
    # write replaces the list instead of merging into it.
    client.put("/sidebar-order", headers=headers, json={"kind": "projects", "ids": ["p1"]})
    client.put("/sidebar-order", headers=headers, json={"kind": "sessions", "ids": ["s3", "s1"]})
    assert client.get("/sidebar-order", headers=headers).json()["sidebarOrder"] == {
        "projects": ["p1"],
        "sessions": ["s3", "s1"],
    }
    assert client.get("/sidebar-order", headers=other).json()["sidebarOrder"] == {
        "projects": [],
        "sessions": [],
    }

    for bad in ({"kind": "threads", "ids": []}, {"kind": "sessions", "ids": [""]}, {"kind": "sessions"}):
        assert client.put("/sidebar-order", headers=headers, json=bad).status_code == 422
    assert client.get("/sidebar-order").status_code == 401


def test_sidebar_order_rides_the_dashboard_snapshot_and_pushes_on_change(tmp_path) -> None:
    client = make_client(tmp_path)
    _, _, session_id, headers = create_connector_and_session(client)
    ticket = dashboard_ws_ticket(client, headers)

    with client.websocket_connect(f"/dashboard/ws?ticket={ticket}") as ws:
        initial = ws.receive_json()
        assert initial["sidebarOrder"] == {"projects": [], "sessions": []}

        saved = client.put(
            "/sidebar-order",
            headers=headers,
            json={"kind": "sessions", "ids": [session_id]},
        )
        assert saved.status_code == 200, saved.text
        pushed = ws.receive_json()

    assert pushed["type"] == "dashboard.snapshot"
    assert pushed["sidebarOrder"] == {"projects": [], "sessions": [session_id]}


def test_deleting_a_user_removes_their_sidebar_order(tmp_path) -> None:
    client = make_client(tmp_path)
    auth_headers(client)
    member = auth_headers(client, user_id="user2")
    member_id = account_user_id(client, "user2")
    client.put("/sidebar-order", headers=member, json={"kind": "projects", "ids": ["p1"]})

    store = client.app.state.store

    async def remaining() -> int:
        async with store._engine.connect() as conn:
            return (
                await conn.execute(
                    select(func.count())
                    .select_from(user_sidebar_orders)
                    .where(user_sidebar_orders.c.user_id == member_id)
                )
            ).scalar_one()

    assert asyncio.run(remaining()) == 1
    asyncio.run(store.delete_user(member_id))
    assert asyncio.run(remaining()) == 0
