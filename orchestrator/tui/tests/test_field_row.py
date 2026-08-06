"""Pilot tests for orchestrator.tui.widgets.field_row.LabeledField."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, Label, Select, Static, Switch

from orchestrator.tui.widgets.field_row import LabeledField


class _FieldApp(App):
    def __init__(self, **field_kwargs):
        super().__init__()
        self.field_kwargs = field_kwargs
        self.changed_messages: list[LabeledField.Changed] = []

    def compose(self) -> ComposeResult:
        yield LabeledField(**self.field_kwargs)

    def on_labeled_field_changed(self, message: LabeledField.Changed) -> None:
        self.changed_messages.append(message)


@pytest.mark.asyncio
async def test_renders_label_text():
    app = _FieldApp(key="SRC_HOST", label="Source host")
    async with app.run_test() as pilot:
        label = pilot.app.query_one(Label)
        assert str(label.content) == "Source host"


@pytest.mark.asyncio
async def test_text_field_mounts_input():
    app = _FieldApp(key="SRC_HOST", label="Source host")
    async with app.run_test() as pilot:
        control = pilot.app.query_one("#control")
        assert isinstance(control, Input)
        assert control.password is False


@pytest.mark.asyncio
async def test_switch_field_mounts_switch():
    app = _FieldApp(key="STAGED_COMPRESS", label="Compress", field_type="switch", default=True)
    async with app.run_test() as pilot:
        control = pilot.app.query_one("#control")
        assert isinstance(control, Switch)
        assert control.value is True


@pytest.mark.asyncio
async def test_select_field_mounts_select():
    options = [("Ubuntu", "ubuntu"), ("Debian", "debian")]
    app = _FieldApp(
        key="REPLACE_TARGET_OS",
        label="Target OS",
        field_type="select",
        select_options=options,
    )
    async with app.run_test() as pilot:
        control = pilot.app.query_one("#control")
        assert isinstance(control, Select)


@pytest.mark.asyncio
async def test_secret_field_mounts_password_input_even_when_text_type():
    app = _FieldApp(key="SRC_PASS", label="Source password", field_type="text", secret=True)
    async with app.run_test() as pilot:
        control = pilot.app.query_one("#control")
        assert isinstance(control, Input)
        assert control.password is True


@pytest.mark.asyncio
async def test_typing_into_input_updates_value_and_posts_changed():
    app = _FieldApp(key="SRC_HOST", label="Source host")
    async with app.run_test() as pilot:
        field = pilot.app.query_one(LabeledField)
        control = pilot.app.query_one("#control", Input)
        control.focus()
        await pilot.pause()
        await pilot.press(*"host1")
        await pilot.pause()
        assert field.value == "host1"
        assert app.changed_messages
        last = app.changed_messages[-1]
        assert last.key == "SRC_HOST"
        assert last.value == "host1"


@pytest.mark.asyncio
async def test_required_empty_shows_fail_mark():
    app = _FieldApp(key="SRC_HOST", label="Source host", required=True)
    async with app.run_test() as pilot:
        mark = pilot.app.query_one("#mark", Static)
        assert str(mark.content) == "✗"


@pytest.mark.asyncio
async def test_required_filled_shows_pass_mark():
    app = _FieldApp(key="SRC_HOST", label="Source host", required=True)
    async with app.run_test() as pilot:
        control = pilot.app.query_one("#control", Input)
        control.focus()
        await pilot.pause()
        await pilot.press(*"host1")
        await pilot.pause()
        mark = pilot.app.query_one("#mark", Static)
        assert str(mark.content) == "✓"


@pytest.mark.asyncio
async def test_failing_validator_shows_fail_mark_even_when_nonempty():
    app = _FieldApp(
        key="SRC_PORT",
        label="Source port",
        validator=lambda v: v.isdigit(),
    )
    async with app.run_test() as pilot:
        control = pilot.app.query_one("#control", Input)
        control.focus()
        await pilot.pause()
        await pilot.press(*"abc")
        await pilot.pause()
        mark = pilot.app.query_one("#mark", Static)
        assert str(mark.content) == "✗"


@pytest.mark.asyncio
async def test_optional_empty_field_has_blank_mark():
    app = _FieldApp(key="SRC_DB", label="Database")
    async with app.run_test() as pilot:
        mark = pilot.app.query_one("#mark", Static)
        assert str(mark.content) == ""


def test_secret_field_repr_never_contains_value():
    field = LabeledField(key="SRC_PASS", label="Source password", secret=True, default="hunter2")
    rendered = repr(field)
    assert "hunter2" not in rendered
    assert "secret=True" in rendered
