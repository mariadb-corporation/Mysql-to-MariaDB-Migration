"""Unit tests for orchestrator/tui/app.py helpers that don't need a running
pilot (see test_integration_flow.py for the full navigation-graph tests).
"""

from __future__ import annotations

from orchestrator.tui.app import MigrationApp
from orchestrator.tui.models import ConfigDraft


def _cfg_path(tmp_path):
    cfg_path = tmp_path / "config" / "migration.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    return cfg_path


def test_load_draft_for_resume_missing_config_falls_back_to_bare_draft(tmp_path):
    # No config/migration.yaml at all -- read_text() raises OSError
    # (FileNotFoundError), which _load_draft_for_resume must swallow.
    app = MigrationApp(repo_root=tmp_path)
    draft = app._load_draft_for_resume("one_step")
    assert draft.mode == "one_step"
    assert draft.ALLOW_ROOT_USERS == ConfigDraft().ALLOW_ROOT_USERS


def test_load_draft_for_resume_malformed_yaml_falls_back_to_bare_draft(tmp_path):
    # Malformed YAML must be handled the same way as a missing/unreadable
    # file (bare ConfigDraft with only mode set), not raise yaml.YAMLError
    # up through _load_draft_for_resume / _push_resume.
    cfg_path = _cfg_path(tmp_path)
    cfg_path.write_text("env:\n  SRC_HOST: [unterminated\n", encoding="utf-8")

    app = MigrationApp(repo_root=tmp_path)
    draft = app._load_draft_for_resume("one_step")
    assert draft.mode == "one_step"
    assert draft.ALLOW_ROOT_USERS == ConfigDraft().ALLOW_ROOT_USERS


def test_load_draft_for_resume_non_utf8_bytes_falls_back_to_bare_draft(tmp_path):
    # cfg_path.read_text(encoding="utf-8") raises UnicodeDecodeError (a
    # ValueError subclass) on non-UTF-8 bytes -- also must not raise.
    cfg_path = _cfg_path(tmp_path)
    cfg_path.write_bytes(b"env:\n  SRC_HOST: \xff\xfe not utf-8\n")

    app = MigrationApp(repo_root=tmp_path)
    draft = app._load_draft_for_resume("one_step")
    assert draft.mode == "one_step"
    assert draft.ALLOW_ROOT_USERS == ConfigDraft().ALLOW_ROOT_USERS
