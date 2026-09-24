from __future__ import annotations

from fastapi import APIRouter, Depends

from agent_server.core.models import SidebarOrderResponse, SidebarOrderUpdateRequest
from agent_server.core.utc import utc_now
from agent_server.deps import current_user_id, get_store, get_timeline_broker
from agent_server.infra.repositories.facade import Store
from agent_server.infra.timeline_broker import TimelineBroker
from agent_server.services.dashboard_events import publish_dashboard_changed

router = APIRouter(prefix="/sidebar-order", tags=["sidebar-order"])


@router.get("", response_model=SidebarOrderResponse)
async def get_sidebar_order(
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
) -> SidebarOrderResponse:
    return SidebarOrderResponse(
        sidebarOrder=await store.get_sidebar_order(user_id=user_id),
        serverTime=utc_now(),
    )


@router.put("", response_model=SidebarOrderResponse)
async def put_sidebar_order(
    payload: SidebarOrderUpdateRequest,
    user_id: str = Depends(current_user_id),
    store: Store = Depends(get_store),
    broker: TimelineBroker = Depends(get_timeline_broker),
) -> SidebarOrderResponse:
    order = await store.set_sidebar_order(user_id=user_id, kind=payload.kind, ids=payload.ids)
    # Other devices of this user pick the new order up from the next snapshot.
    await publish_dashboard_changed(store, broker, user_id=user_id, reason="sidebar_order")
    return SidebarOrderResponse(sidebarOrder=order, serverTime=utc_now())
