"""RootUserBlockModal(ConfirmModal) -- ports mariadb-migrator:1865-1870."""

from __future__ import annotations

from orchestrator.tui.modals.confirm import ConfirmModal

_QUESTION = (
    "root user is not allowed for admin or migration users. Set "
    "ALLOW_ROOT_USERS=1 only if explicitly intended."
)


class RootUserBlockModal(ConfirmModal):
    def __init__(self, id: str | None = None) -> None:
        super().__init__(
            _QUESTION,
            default=False,
            danger=True,
            yes_label="Override (sets ALLOW_ROOT_USERS=1)",
            no_label="Go back and change the user",
            id=id,
        )
