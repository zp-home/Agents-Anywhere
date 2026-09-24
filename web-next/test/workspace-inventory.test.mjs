import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { setTimeout as delay } from 'node:timers/promises'
import { JSDOM } from 'jsdom'
import { registerSource } from './helpers/onboarding-source.mjs'
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'https://fixture.example/', pretendToBeVisual: true })
for (const name of ['window', 'document', 'navigator', 'HTMLElement', 'HTMLInputElement', 'HTMLButtonElement', 'HTMLFormElement', 'Element', 'Node', 'NodeFilter', 'Event', 'CustomEvent', 'MutationObserver', 'getComputedStyle', 'requestAnimationFrame', 'cancelAnimationFrame']) Object.defineProperty(globalThis, name, { configurable: true, value: dom.window[name] })
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
window.HTMLElement.prototype.scrollIntoView = function () {}
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
globalThis.IS_REACT_ACT_ENVIRONMENT = true
globalThis.DocumentFragment = window.DocumentFragment
class Socket {
  static instances = []
  constructor() { Socket.instances.push(this) }
  close() {}
  emit(value) { this.onmessage?.({ data: JSON.stringify(value) }) }
}
globalThis.WebSocket = Socket
const { createElement: h, act } = await import('react')
const { createRoot } = await import('react-dom/client')
const { NextIntlClientProvider } = await import('next-intl')
const hooks = registerSource()
const authHooks = registerHooks({ load(url, context, nextLoad) {
  if (url.endsWith('/components/auth/auth-context.tsx')) return { format: 'module', shortCircuit: true, source: 'export const useAuth = () => globalThis.inventoryAuth' }
  if (url.endsWith('/components/agent-setup-provider.tsx')) return { format: 'module', shortCircuit: true, source: 'export const useAgentSetupPairing = () => ({ requestAgentSetup() {}, waitForConnector() {}, readyConnectorIds: [] })' }
  return nextLoad(url, context)
} })
const { WorkspaceProvider, useWorkspace } = await import('../src/components/workspace-context.tsx')
const { AppSidebar } = await import('../src/components/app-sidebar.tsx')
const { SidebarProvider } = await import('../src/components/ui/sidebar.tsx')
const { dashboardApi } = await import('../src/features/dashboard/api.ts')
const { ArchivedSessionsTab } = await import('../src/components/settings/archived-sessions-tab.tsx')
authHooks.deregister(); hooks.deregister()
const messages = JSON.parse(readFileSync(new URL('../messages/zh-CN.json', import.meta.url), 'utf8'))
const time = '2026-09-08T10:00:00Z'
const connector = { id: 'conn', userId: 'user', name: 'Device', status: 'online' }
const project = (id = 'p1', name = 'Project One') => ({ id, name, connectorId: 'conn', workspacePath: `/${id}`, manuallyCreated: true, pinned: false, createdAt: time, updatedAt: time, lastActivityAt: time, activeSessionCount: 0, sidebarSessionCounts: { active: 0, archived: 0 } })
const session = (id = 's1', projectId = 'p1', extra = {}) => ({ id, projectId, connectorId: 'conn', connectorStatus: 'online', runtime: 'dsh', runtimeType: 'dsh', runtimeId: 'rti', runtimeName: 'DSH', title: `Session ${id}`, cwd: `/${projectId}`, status: 'idle', archived: false, pinned: false, unread: false, lastReadSeq: 1, latestTurnEndSeq: 1, updatedSeq: 1, sortAt: time, lastActivityAt: time, ...extra })
const snapshot = (projects = [project()], sessions = [session()]) => ({ type: 'dashboard.snapshot', projects, sessions, connectors: [connector], serverTime: time, sessionPages: { active: { hasMore: false, nextCursor: null }, archived: { hasMore: false, nextCursor: null } } })
async function render(t, { sidebar = false, archives = false } = {}) {
  window.localStorage.clear(); window.sessionStorage.clear()
  globalThis.inventoryAuth = { session: { userId: 'user', accessToken: 'fixture-token' }, me: { displayName: 'User', role: 'admin' }, signOut() {} }
  Socket.instances = []
  const calls = { projects: 0, inventory: 0, connectors: 0, pages: 0 }
  t.mock.method(dashboardApi, 'createDashboardWsTicket', async () => ({ ticket: 'fixture-ticket' }))
  t.mock.method(dashboardApi, 'listProjects', async () => { calls.projects++; return { projects: [project()] } })
  t.mock.method(dashboardApi, 'listSessionInventory', async () => { calls.inventory++; return { sessions: [session()], serverTime: time } })
  t.mock.method(dashboardApi, 'listConnectors', async () => { calls.connectors++; return { connectors: [connector] } })
  t.mock.method(dashboardApi, 'getSidebarOrder', async () => ({ sidebarOrder: { projects: [], sessions: [] }, serverTime: time }))
  for (const name of ['listProjectSessions', 'listSessions']) t.mock.method(dashboardApi, name, async () => { calls.pages++; throw new Error('Per-project and paginated reads must not run') })
  let state
  function Probe() {
    state = useWorkspace()
    return archives ? h(ArchivedSessionsTab, { token: 'fixture-token', projects: state.projects, sessions: state.sessions, loading: state.isLoading, onOpenSession() {}, onSessionUpdated: state.upsertSession, onWorkspaceRefresh: state.refreshData }) : null
  }
  const container = document.createElement('div'); document.body.append(container)
  const root = createRoot(container)
  await act(async () => root.render(h(NextIntlClientProvider, { locale: 'zh-CN', messages, timeZone: 'Asia/Shanghai' }, h(WorkspaceProvider, null, h(Probe), sidebar ? h(SidebarProvider, null, h(AppSidebar)) : null))))
  t.after(async () => { await act(async () => root.unmount()); container.remove() })
  return { calls, container, get state() { return state }, async emit(value) { await act(async () => { Socket.instances.at(-1).emit(value); await delay(30) }) } }
}
function projectButton(container, name) {
  const button = [...container.querySelectorAll('button[aria-expanded]')].find(button => button.textContent.includes(name))
  assert.ok(button, `Missing project ${name}`)
  return button
}
test('a complete snapshot supplies every project without requests on expand and reopen', async (t) => {
  const view = await render(t, { sidebar: true })
  const rows = Array.from({ length: 105 }, (_, i) => session(`old-${i}`))
  await view.emit(snapshot([project(), project('p2', 'Project Two')], [...rows, session('other', 'p2')]))
  for (const name of ['Project One', 'Project Two', 'Project One', 'Project One']) await act(async () => projectButton(view.container, name).click())
  assert.equal(view.state.sessions.length, 106)
  // The sidebar caps what it renders, so the tail sits behind "show more".
  // Revealing it must still come out of the snapshot already in memory.
  const showMore = [...view.container.querySelectorAll('button')].find(button => button.textContent.startsWith('显示其余'))
  assert.ok(showMore, 'Expected the overflow of 105 sessions to be collapsed')
  assert.doesNotMatch(view.container.textContent, /Session old-104/)
  await act(async () => showMore.click())
  assert.match(view.container.textContent, /Session old-104/)
  assert.match(view.container.textContent, /Session other/)
  assert.deepEqual(view.calls, { projects: 0, inventory: 0, connectors: 0, pages: 0 })
})
test('concurrent refreshes share one session inventory and one project request', async (t) => {
  const view = await render(t)
  await act(async () => { await Promise.all([view.state.refreshData(), view.state.refreshData()]) })
  assert.deepEqual(view.calls, { projects: 1, inventory: 1, connectors: 1, pages: 0 })
  assert.deepEqual(view.state.sessions.map(row => row.id), ['s1'])
})
test('unknown project sessions share a refresh and appear in the project when it arrives', async (t) => {
  const view = await render(t, { sidebar: true }); await view.emit(snapshot())
  let finish
  t.mock.method(dashboardApi, 'listProjects', () => { view.calls.projects++; return new Promise(resolve => { finish = resolve }) })
  await act(async () => { view.state.upsertSession(session('new-a', 'p2')); view.state.upsertSession(session('new-b', 'p2')) })
  assert.equal(view.calls.projects, 1)
  assert.match(view.container.textContent, /未分组会话/)
  await act(async () => finish({ projects: [project(), project('p2', 'Project Two')] }))
  await act(async () => projectButton(view.container, 'Project Two').click())
  assert.match(view.container.textContent, /Session new-a/)
  assert.match(view.container.textContent, /Session new-b/)
  assert.doesNotMatch(view.container.textContent, /未分组会话/)
  assert.equal(view.calls.inventory, 0)
})
test('unassigned sessions do not refetch projects on every message', async (t) => {
  const view = await render(t); await view.emit(snapshot())
  await act(async () => { view.state.upsertSession(session('orphan', null)) })
  assert.equal(view.calls.projects, 1)
  await act(async () => { view.state.upsertSession(session('orphan', null, { title: 'updated', updatedSeq: 2 })) })
  assert.equal(view.calls.projects, 1)
  assert.equal(view.calls.inventory, 0)
})
test('project edits refresh only projects; late reads cannot replace a newer push', async (t) => {
  const view = await render(t); await view.emit(snapshot())
  let finish
  t.mock.method(dashboardApi, 'updateProject', async () => ({ project: project('p1', 'Edited') }))
  t.mock.method(dashboardApi, 'listProjects', () => { view.calls.projects++; return new Promise(resolve => { finish = resolve }) })
  await act(async () => { await view.state.updateProject('p1', { name: 'Edited' }) })
  await view.emit(snapshot([project('p1', 'Newer push')]))
  await act(async () => finish({ projects: [project('p1', 'Old response')] }))
  assert.equal(view.state.projects[0].name, 'Newer push')
  assert.equal(view.calls.projects, 1)
  assert.equal(view.calls.inventory, 0)
})
test('a failed project refresh can retry after a later session update', async (t) => {
  const view = await render(t); await view.emit(snapshot())
  t.mock.method(dashboardApi, 'listProjects', async () => {
    view.calls.projects++
    if (view.calls.projects === 1) throw new Error('Temporary failure')
    return { projects: [project(), project('p2', 'Project Two')] }
  })
  await act(async () => { view.state.upsertSession(session('new', 'p2')) })
  assert.equal(view.state.sessions.length, 2)
  await act(async () => { view.state.upsertSession(session('new', 'p2', { updatedSeq: 2 })) })
  assert.equal(view.calls.projects, 2)
  assert.equal(view.state.projects.length, 2)
})
test('fresh inventories remove deleted rows and apply project and archive changes locally', async (t) => {
  const view = await render(t); const projects = [project(), project('p2', 'Project Two')]
  await view.emit(snapshot(projects, [session(), session('deleted')]))
  await view.emit(snapshot(projects, [session('s1', 'p2', { archived: true, updatedSeq: 3 })]))
  assert.equal(view.state.sessions.length, 1)
  assert.equal(view.state.sessions[0].projectId, 'p2')
  assert.equal(view.state.sessions[0].archived, true)
  assert.equal(view.calls.pages, 0)
})
test('archived settings read the shared inventory without a separate list request', async (t) => {
  const view = await render(t, { archives: true })
  await view.emit(snapshot([project()], [session(), session('archived', 'p1', { archived: true, archivedAt: time })]))
  assert.match(view.container.textContent, /Session archived/)
  assert.doesNotMatch(view.container.textContent, /Session s1/)
  assert.equal(view.calls.pages, 0)
  assert.equal(view.calls.inventory, 0)
})
test('the archived list distinguishes the sweeper from the user', async (t) => {
  // A session the user never archived has to say so, or the list looks like
  // something silently threw work away.
  const view = await render(t, { archives: true })
  await view.emit(snapshot([project()], [
    session('by-hand', 'p1', { archived: true, archivedAt: time, userArchived: true }),
    session('by-sweeper', 'p1', { archived: true, archivedAt: time, userArchived: false, autoArchived: true }),
  ]))
  const rows = [...view.container.querySelectorAll('div')].filter(node => node.textContent.includes('Session by-'))
  const sweeperRow = rows.find(node => node.textContent.includes('Session by-sweeper') && !node.textContent.includes('Session by-hand'))
  const manualRow = rows.find(node => node.textContent.includes('Session by-hand') && !node.textContent.includes('Session by-sweeper'))
  assert.ok(sweeperRow && manualRow, 'Expected one row per archived session')
  assert.match(sweeperRow.textContent, /自动归档/)
  assert.doesNotMatch(manualRow.textContent, /自动归档/)
})
test('a late full inventory cannot undo a completed project edit', async (t) => {
  const view = await render(t); await view.emit(snapshot())
  let finish
  t.mock.method(dashboardApi, 'listSessionInventory', () => new Promise(resolve => { finish = resolve }))
  t.mock.method(dashboardApi, 'updateProject', async () => ({ project: project('p1', 'Edited') }))
  await act(async () => { view.state.refreshData() })
  t.mock.method(dashboardApi, 'listProjects', async () => ({ projects: [project('p1', 'Edited')] }))
  await act(async () => { await view.state.updateProject('p1', { name: 'Edited' }) })
  await act(async () => finish({ sessions: [session()], serverTime: time }))
  assert.equal(view.state.projects[0].name, 'Edited')
  assert.equal(view.state.isLoading, false)
})

const sessionTitles = container => [...container.querySelectorAll('li')].map(node => node.textContent.match(/Session (s\d)/)?.[1]).filter(Boolean).filter((id, index, all) => all.indexOf(id) === index)
test('the sidebar follows the stored order, and a drag being saved is not undone by an older snapshot', async (t) => {
  const view = await render(t, { sidebar: true })
  const rows = [session('s1'), session('s2'), session('s3')]
  const stored = { projects: [], sessions: ['s3', 's1', 's2'] }
  await view.emit({ ...snapshot([project()], rows), sidebarOrder: stored })
  assert.deepEqual(view.state.sidebarOrder, stored)
  await act(async () => projectButton(view.container, 'Project One').click())
  assert.deepEqual(sessionTitles(view.container), ['s3', 's1', 's2'])

  const puts = []
  let land
  t.mock.method(dashboardApi, 'updateSidebarOrder', (token, kind, ids) => { puts.push([token, kind, ids]); return new Promise(resolve => { land = resolve }) })
  await act(async () => view.state.saveSidebarOrder('sessions', ['s2', 's3', 's1']))
  assert.deepEqual(puts, [['fixture-token', 'sessions', ['s2', 's3', 's1']]])
  assert.deepEqual(sessionTitles(view.container), ['s2', 's3', 's1'])
  // A push that was taken before the save landed still carries the old order.
  await view.emit({ ...snapshot([project()], [...rows, session('s4')]), sidebarOrder: stored })
  assert.deepEqual(view.state.sidebarOrder.sessions, ['s2', 's3', 's1'])
  await act(async () => { land({ sidebarOrder: { projects: [], sessions: ['s2', 's3', 's1'] }, serverTime: time }); await delay(10) })
  await view.emit({ ...snapshot([project()], rows), sidebarOrder: { projects: [], sessions: ['s1', 's2', 's3'] } })
  // Once nothing is pending, pushes from other devices apply again.
  assert.deepEqual(sessionTitles(view.container), ['s1', 's2', 's3'])
})
test('a drag the server rejects falls back to the stored order', async (t) => {
  const view = await render(t)
  const stored = { projects: ['p1'], sessions: ['s1'] }
  await view.emit({ ...snapshot(), sidebarOrder: stored })
  t.mock.method(dashboardApi, 'getSidebarOrder', async () => ({ sidebarOrder: stored, serverTime: time }))
  t.mock.method(dashboardApi, 'updateSidebarOrder', async () => { throw new Error('offline') })
  await act(async () => { view.state.saveSidebarOrder('projects', ['p2', 'p1']); await delay(30) })
  assert.deepEqual(view.state.sidebarOrder, stored)
})
test('sessions not placed yet show first, newest created first, and do not move with activity', async (t) => {
  const view = await render(t, { sidebar: true })
  const rows = [
    session('s1', 'p1', { createdAt: '2026-09-01T00:00:00Z', status: 'running', sortAt: '2026-09-08T11:00:00Z' }),
    session('s2', 'p1', { createdAt: '2026-09-05T00:00:00Z' }),
    session('s3', 'p1', { createdAt: '2026-09-03T00:00:00Z' }),
  ]
  await view.emit({ ...snapshot([project()], rows), sidebarOrder: { projects: [], sessions: [] } })
  await act(async () => projectButton(view.container, 'Project One').click())
  assert.deepEqual(sessionTitles(view.container), ['s2', 's3', 's1'])
})
