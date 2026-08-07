"""OfflineAckModal(ConfirmModal) -- ports mariadb-migrator:1441-1456 verbatim."""

from __future__ import annotations

from orchestrator.tui.modals.confirm import ConfirmModal

_BODY = """\
==============================================================================
  IMPORTANT: Offline Copy IS AN OFFLINE MIGRATION

  Writes to the SOURCE database during the dump WILL NOT be captured and may
  be lost when the data is loaded on the target. To ensure a consistent
  cutover:

    - Stop application traffic to the source BEFORE proceeding.
    - Confirm no other writers (cron jobs, replication, batch jobs).
    - Resume traffic on the TARGET only after the load completes.

  If you cannot afford application downtime, abort now and use
  Replication (binlog) mode.
=============================================================================="""


class OfflineAckModal(ConfirmModal):
    def __init__(self, id: str | None = None) -> None:
        super().__init__(
            "Have you stopped writes to the source?",
            default=False,
            body=_BODY,
            id=id,
        )
