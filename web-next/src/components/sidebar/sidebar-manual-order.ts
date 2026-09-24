/**
 * User-controlled sidebar order, modelled on the Codex desktop sidebar
 * (`project-order` / `sidebar-project-thread-orders` in its global state).
 *
 * Activity-based ordering made rows jump every time a session started or
 * finished running. Instead the server keeps one id list per kind for each
 * user (so every device shows the same order). Ids the list does not contain
 * yet show first, newest created first — creation time never changes, so they
 * do not move either. Every section (pinned, project children, unassigned)
 * shows its subset in the order of that single list, so a drag inside any
 * section is just "move id next to target id" in the full list, and the full
 * list as displayed is what gets saved.
 */

import type { SidebarOrder, SidebarOrderKind } from "@/features/dashboard/types"

export type { SidebarOrder, SidebarOrderKind }
export type SidebarDropPlacement = "before" | "after"

export const EMPTY_SIDEBAR_ORDER: SidebarOrder = { projects: [], sessions: [] }

type Orderable = { id: string; createdAt?: string | null }

/**
 * Items the order lists come in that order; the rest go first, newest created
 * first. Items without a creation time keep their input order among themselves.
 */
export function applyManualOrder<T extends Orderable>(items: readonly T[], order: readonly string[]): T[] {
  const rank = new Map(order.map((id, index) => [id, index]))
  const unplaced: T[] = []
  const placed: T[] = []
  for (const item of items) (rank.has(item.id) ? placed : unplaced).push(item)
  unplaced.sort((left, right) => (right.createdAt ?? "").localeCompare(left.createdAt ?? ""))
  placed.sort((left, right) => rank.get(left.id)! - rank.get(right.id)!)
  return [...unplaced, ...placed]
}

/** Returns `order` itself when the move changes nothing. */
export function moveInOrder(
  order: readonly string[],
  draggedId: string,
  targetId: string,
  placement: SidebarDropPlacement,
): readonly string[] {
  if (draggedId === targetId || !order.includes(draggedId) || !order.includes(targetId)) return order
  const next = order.filter((id) => id !== draggedId)
  const targetIndex = next.indexOf(targetId)
  next.splice(placement === "before" ? targetIndex : targetIndex + 1, 0, draggedId)
  return next.every((id, index) => id === order[index]) ? order : next
}
