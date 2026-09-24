import assert from "node:assert/strict"
import test from "node:test"
import { applyManualOrder, moveInOrder } from "../src/components/sidebar/sidebar-manual-order.ts"

const rows = (...ids) => ids.map(id => ({ id }))
const ids = items => items.map(item => item.id)

test("placed rows keep their slot when activity order changes; unplaced rows show first, newest created first", () => {
  const order = ["a", "b", "c"]
  // "c" started running, so activity order would float it up.
  assert.deepEqual(ids(applyManualOrder(rows("c", "a", "b"), order)), ["a", "b", "c"])
  const items = [
    { id: "c" },
    { id: "old", createdAt: "2026-09-01T00:00:00Z" },
    { id: "a" },
    { id: "new", createdAt: "2026-09-20T00:00:00Z" },
    { id: "b" },
  ]
  assert.deepEqual(ids(applyManualOrder(items, order)), ["new", "old", "a", "b", "c"])
  // Activity moving "old" ahead of "new" in the input does not reorder them.
  assert.deepEqual(ids(applyManualOrder([items[1], items[3]], [])), ["new", "old"])
  // Without creation times (older servers) unplaced rows keep the input order.
  assert.deepEqual(ids(applyManualOrder(rows("x", "y"), [])), ["x", "y"])
  assert.deepEqual(ids(applyManualOrder(rows("b"), order)), ["b"])
})

test("moving before or after a target works in both directions; no-op moves return the same list", () => {
  const order = ["a", "b", "c", "d"]
  assert.deepEqual(moveInOrder(order, "d", "b", "before"), ["a", "d", "b", "c"])
  assert.deepEqual(moveInOrder(order, "a", "c", "after"), ["b", "c", "a", "d"])
  assert.equal(moveInOrder(order, "a", "b", "before"), order)
  assert.equal(moveInOrder(order, "a", "missing", "after"), order)
  assert.equal(moveInOrder(order, "b", "b", "after"), order)
})

test("dragging inside a filtered subset reorders that subset in the full list", () => {
  // Project p1 shows s1, s3; the full list interleaves other projects' sessions.
  const full = ["s1", "x", "s3", "y"]
  const next = moveInOrder(full, "s3", "s1", "before")
  assert.deepEqual(ids(applyManualOrder(rows("s1", "s3"), next)), ["s3", "s1"])
  assert.deepEqual(ids(applyManualOrder(rows("x", "y"), next)), ["x", "y"])
})
