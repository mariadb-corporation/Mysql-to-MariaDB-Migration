"""SaveSecretsModal(ConfirmModal) -- ports mariadb-migrator:2117.

dismiss(True) means "include secrets" (draft_to_yaml(..., include_secrets=True));
dismiss(False) means "blank them" (include_secrets=False) -- same boolean
direction as configio.draft_to_yaml's own parameter.
"""

from __future__ import annotations

from orchestrator.tui.configio import SECRET_KEYS
from orchestrator.tui.modals.confirm import ConfirmModal


class SaveSecretsModal(ConfirmModal):
    def __init__(self, id: str | None = None) -> None:
        super().__init__(
            "Include passwords/secrets in saved config?",
            default=False,
            body=(
                "The following keys will be blanked if declined: "
                f"{', '.join(sorted(SECRET_KEYS))}"
            ),
            id=id,
        )
