"""Fold long-idle sessions out of the sidebar, and bring them back on activity.

The sidebar grew without bound because nothing ever archived an idle session.
This sweeper archives sessions with no activity for ``AUTO_ARCHIVE_INACTIVE_DAYS``
and reverses itself when activity returns.

The distinction that makes this safe is that an auto-archive is a *fold*, not a
*lock*:

* A user archive is an explicit decision and is never undone automatically.
  Only rows with ``auto_archived = 1`` are ever revived here.
* An auto-archive reopens the moment real activity arrives. Sending a message
  revives the session synchronously (see ``services.session_run``); this
  sweeper's reverse pass is the backstop that guarantees correctness for
  activity arriving by any other path.

A manual unarchive stamps a tombstone (``auto_archived_at``) that survives the
unarchive, so a session the user deliberately pulled back into the sidebar is
not re-archived on the very next pass. See ``_manual_archive_values``.
"""

from __future__ import annotations

import asyncio
import random
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from agent_server.services.dashboard_events import publish_dashboard_changed
from agent_server.services.repository_ports import SessionAutoArchiveRepository

if TYPE_CHECKING:
    from agent_server.infra.redis_coordinator import RedisCoordinator
    from agent_server.infra.timeline_broker import TimelineBroker

AUTO_ARCHIVE_INACTIVE_DAYS = 30
AUTO_ARCHIVE_SWEEP_SECONDS = 3600
AUTO_ARCHIVE_BATCH_SIZE = 500
AUTO_ARCHIVE_MAX_BATCHES_PER_SWEEP = 10
AUTO_ARCHIVE_LOCK = "session-auto-archive"
AUTO_ARCHIVE_LOCK_TIMEOUT_SECONDS = 5.0
AUTO_ARCHIVE_LOCK_LEASE_SECONDS = 300.0


def auto_archive_cutoff(now: datetime | None = None) -> str:
    """Cutoff timestamp in the same shape ``utc_now()`` writes.

    Activity columns are compared lexicographically, so the cutoff has to be
    formatted identically to the values already stored.
    """

    moment = (now or datetime.now(UTC)) - timedelta(days=AUTO_ARCHIVE_INACTIVE_DAYS)
    return moment.isoformat().replace("+00:00", "Z")


class SessionAutoArchiveSweeper:
    def __init__(
        self,
        store: SessionAutoArchiveRepository,
        coordinator: RedisCoordinator,
        broker: TimelineBroker,
    ) -> None:
        self._store = store
        self._coordinator = coordinator
        self._broker = broker

    async def run_once(self) -> None:
        cutoff = auto_archive_cutoff()
        # Every worker wakes on the same interval. The loser of the lock races
        # should no-op cheaply rather than queue up a redundant sweep, hence the
        # short timeout plus suppress. Without Redis configured, lock() falls
        # back to a process-local asyncio.Lock, so dev runs need no special case.
        with suppress(TimeoutError):
            async with self._coordinator.lock(
                AUTO_ARCHIVE_LOCK,
                timeout_seconds=AUTO_ARCHIVE_LOCK_TIMEOUT_SECONDS,
                lease_seconds=AUTO_ARCHIVE_LOCK_LEASE_SECONDS,
            ):
                # Revive first: a session that just came back should not be
                # re-archived by the pass that follows it.
                await self._sweep(
                    cutoff,
                    self._store.auto_unarchive_active_sessions,
                    "sessions.auto_unarchived",
                )
                await self._sweep(
                    cutoff,
                    self._store.auto_archive_inactive_sessions,
                    "sessions.auto_archived",
                )

    async def _sweep(self, cutoff: str, operation, reason: str) -> None:
        for _ in range(AUTO_ARCHIVE_MAX_BATCHES_PER_SWEEP):
            try:
                changed = await operation(cutoff=cutoff, limit=AUTO_ARCHIVE_BATCH_SIZE)
            except Exception as exc:  # noqa: BLE001 - retry on the next sweep
                logger.warning("session auto-archive batch deferred error={}", exc)
                return
            if not changed:
                return
            logger.info(
                "session auto-archive reason={} sessions={}", reason, len(changed)
            )
            # One publish per affected user, not one per session.
            for user_id in {user_id for _, user_id in changed}:
                await publish_dashboard_changed(
                    self._store, self._broker, user_id=user_id, reason=reason
                )
            if len(changed) < AUTO_ARCHIVE_BATCH_SIZE:
                return

    async def run(self) -> None:
        # Jitter before the first pass so restarting a multi-worker deployment
        # does not stampede the lock; sleep after the body so a crash cannot spin.
        await asyncio.sleep(random.uniform(0, AUTO_ARCHIVE_SWEEP_SECONDS))
        while True:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.warning("session auto-archive sweep deferred error={}", exc)
            await asyncio.sleep(AUTO_ARCHIVE_SWEEP_SECONDS)
