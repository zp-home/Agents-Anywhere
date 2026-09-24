"use client"

import * as React from "react"
import { cn } from "@/lib/utils"
import type { SidebarDropPlacement, SidebarOrderKind } from "./sidebar-manual-order"

type DragSource = { kind: SidebarOrderKind; id: string; group: string }

type SidebarReorderContextValue = {
  dragSource: React.MutableRefObject<DragSource | null>
  onReorder: (kind: SidebarOrderKind, draggedId: string, targetId: string, placement: SidebarDropPlacement) => void
}

const SidebarReorderContext = React.createContext<SidebarReorderContextValue | null>(null)

export function SidebarReorderProvider({
  onReorder,
  children,
}: {
  onReorder: SidebarReorderContextValue["onReorder"]
  children: React.ReactNode
}) {
  const dragSource = React.useRef<DragSource | null>(null)
  const value = React.useMemo(() => ({ dragSource, onReorder }), [onReorder])
  return <SidebarReorderContext.Provider value={value}>{children}</SidebarReorderContext.Provider>
}

/**
 * Drag handlers for one sidebar row. Rows only accept drops from the same
 * `group` (one visible list), so a session cannot be dragged into another
 * project and a pinned row cannot be dropped among unpinned ones.
 */
export function useSidebarReorderItem({ kind, id, group }: { kind: SidebarOrderKind; id: string; group: string }) {
  const context = React.useContext(SidebarReorderContext)
  const [placement, setPlacement] = React.useState<SidebarDropPlacement | null>(null)
  const [dragging, setDragging] = React.useState(false)

  const accepts = () => {
    const source = context?.dragSource.current
    return Boolean(source && source.kind === kind && source.group === group && source.id !== id)
  }

  const dragProps: React.HTMLAttributes<HTMLElement> & { draggable?: boolean } = context
    ? {
        draggable: true,
        onDragStart(event) {
          event.stopPropagation()
          event.dataTransfer.effectAllowed = "move"
          event.dataTransfer.setData("text/plain", id)
          context.dragSource.current = { kind, id, group }
          setDragging(true)
        },
        onDragEnd() {
          context.dragSource.current = null
          setDragging(false)
        },
        onDragOver(event) {
          if (!accepts()) return
          event.preventDefault()
          event.stopPropagation()
          event.dataTransfer.dropEffect = "move"
          const rect = event.currentTarget.getBoundingClientRect()
          const next = event.clientY < rect.top + rect.height / 2 ? "before" : "after"
          setPlacement((current) => (current === next ? current : next))
        },
        onDragLeave(event) {
          if (event.currentTarget.contains(event.relatedTarget as Node | null)) return
          setPlacement(null)
        },
        onDrop(event) {
          const source = context.dragSource.current
          setPlacement(null)
          if (!source || !accepts()) return
          event.preventDefault()
          event.stopPropagation()
          const rect = event.currentTarget.getBoundingClientRect()
          context.onReorder(kind, source.id, id, event.clientY < rect.top + rect.height / 2 ? "before" : "after")
        },
      }
    : {}

  return { dragProps, placement, dragging }
}

export function SidebarDropIndicator({ placement }: { placement: SidebarDropPlacement | null }) {
  if (!placement) return null
  return (
    <span
      aria-hidden
      className={cn(
        "pointer-events-none absolute inset-x-2 z-10 h-0.5 rounded-full bg-primary",
        placement === "before" ? "-top-px" : "-bottom-px",
      )}
    />
  )
}
