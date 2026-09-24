from __future__ import annotations

from agent_server.infra.repositories.store_support import *
from agent_server.infra.db import user_sidebar_orders as user_sidebar_orders_t
from agent_server.core.models import SidebarOrderKind, SidebarOrderView

_COLUMNS: dict[str, str] = {"projects": "projects_json", "sessions": "sessions_json"}


def _ids(raw: str | None) -> list[str]:
    try:
        value = _json_loads(raw)
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item))


class SidebarOrderRepositoryMixin:
    async def get_sidebar_order(self, *, user_id: str) -> SidebarOrderView:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(user_sidebar_orders_t).where(
                        user_sidebar_orders_t.c.user_id == user_id
                    )
                )
            ).first()
        if row is None:
            return SidebarOrderView()
        return SidebarOrderView(
            projects=_ids(row.projects_json),
            sessions=_ids(row.sessions_json),
        )

    async def set_sidebar_order(
        self,
        *,
        user_id: str,
        kind: SidebarOrderKind,
        ids: list[str],
    ) -> SidebarOrderView:
        """Overwrite one kind's list; the other kind is left as stored."""
        column = _COLUMNS[kind]
        value = _json_dumps(list(dict.fromkeys(item for item in ids if item)))
        now = utc_now()
        where = user_sidebar_orders_t.c.user_id == user_id
        values = {column: value, "updated_at": now}
        for attempt in range(2):
            try:
                async with self._engine.begin() as conn:
                    result = await conn.execute(
                        update(user_sidebar_orders_t).where(where).values(values)
                    )
                    if result.rowcount == 0:
                        await conn.execute(
                            insert(user_sidebar_orders_t).values({"user_id": user_id, **values})
                        )
                break
            except IntegrityError:
                # A concurrent first write inserted the row; the retry updates it.
                if attempt == 1:
                    raise
        return await self.get_sidebar_order(user_id=user_id)
