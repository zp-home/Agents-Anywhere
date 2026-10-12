"""Apply Immer JSON patches emitted by the Codex App conversation store.

Owners publish ``{op, path, value}`` patches produced by Immer against their
conversation state. A batch applies atomically: on any failure the caller keeps
its previous state and must request a fresh snapshot instead of guessing.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any


class PatchError(Exception):
    pass


def apply_patches(state: Any, patches: Sequence[Any]) -> Any:
    """Return a new state with every patch applied; ``state`` is never mutated."""

    result = copy.deepcopy(state)
    for patch in patches:
        result = _apply_one(result, patch)
    return result


def _apply_one(root: Any, patch: Any) -> Any:
    if not isinstance(patch, dict):
        raise PatchError("patch is not an object")
    op = patch.get("op")
    path = patch.get("path")
    if op not in {"add", "replace", "remove"}:
        raise PatchError(f"unsupported op {op!r}")
    if not isinstance(path, list):
        raise PatchError("patch path is not a list")
    value = copy.deepcopy(patch.get("value"))
    if not path:
        if op == "remove":
            raise PatchError("cannot remove the root")
        return value
    parent = root
    for key in path[:-1]:
        parent = _child(parent, key)
    key = path[-1]
    if isinstance(parent, list):
        _apply_to_list(parent, op, key, value)
    elif isinstance(parent, dict):
        _apply_to_dict(parent, op, key, value)
    else:
        raise PatchError("patch parent is not a container")
    return root


def _child(container: Any, key: Any) -> Any:
    if isinstance(container, list):
        index = _index(key)
        if not 0 <= index < len(container):
            raise PatchError(f"index {key!r} out of range")
        return container[index]
    if isinstance(container, dict):
        if not isinstance(key, str) or key not in container:
            raise PatchError(f"missing key {key!r}")
        return container[key]
    raise PatchError("path traverses a non-container")


def _index(key: Any) -> int:
    if isinstance(key, bool):
        raise PatchError("boolean array index")
    if isinstance(key, int):
        return key
    if isinstance(key, str) and key.isdigit():
        return int(key)
    raise PatchError(f"invalid array index {key!r}")


def _apply_to_list(items: list[Any], op: str, key: Any, value: Any) -> None:
    if key == "length":
        if op != "replace" or isinstance(value, bool) or not isinstance(value, int):
            raise PatchError("invalid array length patch")
        if value < 0:
            raise PatchError("negative array length")
        if value < len(items):
            del items[value:]
        else:
            items.extend([None] * (value - len(items)))
        return
    if op == "add" and key == "-":
        items.append(value)
        return
    index = _index(key)
    if op == "add":
        if not 0 <= index <= len(items):
            raise PatchError(f"add index {index} out of range")
        items.insert(index, value)
    elif op == "replace":
        if not 0 <= index < len(items):
            raise PatchError(f"replace index {index} out of range")
        items[index] = value
    else:
        if not 0 <= index < len(items):
            raise PatchError(f"remove index {index} out of range")
        del items[index]


def _apply_to_dict(mapping: dict[str, Any], op: str, key: Any, value: Any) -> None:
    if not isinstance(key, str):
        raise PatchError(f"invalid object key {key!r}")
    if op == "remove":
        if key not in mapping:
            raise PatchError(f"remove missing key {key!r}")
        del mapping[key]
        return
    mapping[key] = value
