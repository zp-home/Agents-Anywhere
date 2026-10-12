"""Project a mirrored Codex App conversation into app-server shaped data.

The App's conversation state keeps turns either in a flat ``turns`` list or,
for newer builds, in canonical history: ``turnHistory.history.islands[]``
entries whose ``value`` keys ``entitiesByKey``. Turn entities carry
``turnId``/``status``/``items`` where items are app-server ThreadItems, so the
projected thread can reuse the existing Codex timeline mapping unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from connector.runtimes.codex.domain.approvals import is_approval_request
from connector.runtimes.codex.domain.input_requests import is_user_input_request

ACTIVE_TURN_STATUSES = {"inProgress", "in_progress", "running"}
TERMINAL_TURN_OUTCOMES = {
    "completed": "completed",
    "failed": "failed",
    "interrupted": "interrupted",
}


def conversation_turns(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    history = state.get("turnHistory")
    if isinstance(history, Mapping) and history.get("kind") == "canonical":
        canonical = history.get("history")
        if isinstance(canonical, Mapping):
            return _canonical_turns(canonical)
    turns = state.get("turns")
    if not isinstance(turns, list):
        return []
    return [turn for turn in turns if isinstance(turn, dict)]


def _canonical_turns(history: Mapping[str, Any]) -> list[dict[str, Any]]:
    entities = history.get("entitiesByKey")
    islands = history.get("islands")
    if not isinstance(entities, Mapping) or not isinstance(islands, list):
        return []
    turns: list[dict[str, Any]] = []
    for island in islands:
        entries = island.get("entries") if isinstance(island, Mapping) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            key = entry.get("value") if isinstance(entry, Mapping) else None
            turn = entities.get(key) if isinstance(key, str) else None
            if isinstance(turn, dict):
                turns.append(turn)
    return turns


def history_complete(state: Mapping[str, Any]) -> bool:
    history = state.get("turnHistory")
    if isinstance(history, Mapping) and history.get("kind") == "canonical":
        canonical = history.get("history")
        return (
            isinstance(canonical, Mapping)
            and canonical.get("isComplete") is True
            and isinstance(canonical.get("islands"), list)
            and len(canonical["islands"]) == 1
        )
    pagination = state.get("turnsPagination")
    if isinstance(pagination, Mapping):
        return pagination.get("hasLoadedOldest") is not False
    return True


def thread_from_conversation(thread_id: str, state: Mapping[str, Any]) -> dict[str, Any]:
    """An app-server ``Thread``-like dict accepted by ``timeline_items_from_thread``."""

    turns = []
    for turn in conversation_turns(state):
        items = turn.get("items")
        turns.append(
            {
                "id": turn.get("turnId"),
                "status": turn.get("status"),
                "error": turn.get("error"),
                "items": [item for item in items if isinstance(item, dict)]
                if isinstance(items, list)
                else [],
            }
        )
    thread: dict[str, Any] = {"id": thread_id, "turns": turns}
    title = state.get("title")
    if isinstance(title, str) and title:
        thread["name"] = title
    cwd = state.get("cwd")
    if isinstance(cwd, str) and cwd:
        thread["cwd"] = cwd
    return thread


def active_turn_id(state: Mapping[str, Any]) -> str | None:
    for turn in reversed(conversation_turns(state)):
        turn_id = turn.get("turnId")
        if isinstance(turn_id, str) and turn.get("status") in ACTIVE_TURN_STATUSES:
            return turn_id
    return None


def turn_statuses(state: Mapping[str, Any]) -> dict[str, str]:
    return {
        turn["turnId"]: str(turn.get("status"))
        for turn in conversation_turns(state)
        if isinstance(turn.get("turnId"), str)
    }


def pending_requests(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Outstanding app-server server requests the owner is waiting on."""

    requests = state.get("requests")
    if not isinstance(requests, list):
        return []
    result = []
    for request in requests:
        if not isinstance(request, dict):
            continue
        method = request.get("method")
        if not isinstance(method, str) or not isinstance(request.get("id"), str | int):
            continue
        if is_approval_request(method) or is_user_input_request(method):
            result.append(request)
    return result


def session_status(state: Mapping[str, Any]) -> str:
    """Status matching the SDK path: approvals block, questions wait for input."""

    requests = pending_requests(state)
    if any(is_approval_request(request["method"]) for request in requests):
        return "waiting_approval"
    if requests:
        return "waiting"
    if active_turn_id(state) is not None:
        return "running"
    return "idle"


def conversation_title(state: Mapping[str, Any]) -> str | None:
    title = state.get("title")
    return title if isinstance(title, str) and title else None
