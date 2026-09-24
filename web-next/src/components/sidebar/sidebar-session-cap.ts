import type { WorkspaceSessionView } from "@/components/workspace-context"

/**
 * How many recent sessions the sidebar renders before collapsing the rest
 * behind a "show more" row.
 *
 * Auto-archiving handles sessions that are *old*; this handles sessions that
 * are merely *many*. A user with 300 active sessions has none of them old
 * enough to archive, and still cannot find anything in the list.
 *
 * The rest are hidden behind a button rather than dropped, because a list that
 * silently stops at N is indistinguishable from data loss — the complaint
 * Codex users filed against exactly this behaviour (openai/codex#23246).
 */
export const SIDEBAR_RECENT_SESSION_LIMIT = 50

export type CappedSessions<T> = {
  visible: T[]
  hiddenCount: number
}

/**
 * Truncate a sorted session list for sidebar rendering.
 *
 * The caller passes the list in the user's manual sidebar order, where new
 * sessions enter at the top, so a freshly started session is never hidden.
 * Rows the user dragged below the cut stay hidden even while running — that is
 * where they asked for them to be. Pinned sessions render in their own section
 * and never reach here at all.
 *
 * The session the user currently has open is always kept, even when it sorts
 * past the cut. Dropping it would leave the sidebar with no highlighted row
 * while its conversation fills the screen.
 */
export function capRecentSessions<T extends Pick<WorkspaceSessionView, "id">>(
  sessions: T[],
  {
    expanded,
    activeSessionId = null,
    limit = SIDEBAR_RECENT_SESSION_LIMIT,
  }: {
    expanded: boolean
    activeSessionId?: string | null
    limit?: number
  },
): CappedSessions<T> {
  if (expanded || sessions.length <= limit) {
    return { visible: sessions, hiddenCount: 0 }
  }
  const visible = sessions.slice(0, limit)
  const hidden = sessions.slice(limit)
  const active = activeSessionId
    ? hidden.find((session) => session.id === activeSessionId)
    : undefined
  if (active) {
    visible.push(active)
  }
  return { visible, hiddenCount: hidden.length - (active ? 1 : 0) }
}
