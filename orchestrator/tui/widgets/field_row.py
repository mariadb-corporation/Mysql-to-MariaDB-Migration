"""LabeledField(Widget): Label + control + trailing validity marker.

Design doc §5.6: a single wizard form field, one of Input / Input(password)
/ Switch / Select, with a `Static#mark` showing the mockup's `✓` / `✗`
validity marker.
"""

from __future__ import annotations

from typing import Callable

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Label, Select, Static, Switch


class LabeledField(Widget):
    class Changed(Message):
        def __init__(self, labeled_field: "LabeledField", key: str, value) -> None:
            super().__init__()
            self.labeled_field = labeled_field
            self.key = key
            self.value = value

    def __init__(
        self,
        key: str,
        label: str,
        *,
        field_type: str = "text",
        required: bool = False,
        default: str | bool = "",
        validator: Callable[[str], bool] | None = None,
        secret: bool = False,
        select_options: list[tuple[str, str]] | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.key = key
        self.label = label
        self.field_type = field_type
        self.required = required
        self.default = default
        self.validator = validator
        self.secret = secret
        self.select_options = select_options

    def compose(self) -> ComposeResult:
        yield Label(self.label)
        if self.secret:
            yield Input(value=str(self.default), password=True, id="control")
        elif self.field_type == "switch":
            yield Switch(value=bool(self.default), id="control")
        elif self.field_type == "select":
            # textual 8.2.8 has no real Select.BLANK sentinel (it resolves to
            # Widget.BLANK, plain False, via inheritance) -- passing that as
            # the initial value crashes on mount with InvalidSelectValueError
            # since False matches neither Select.NULL nor a legal option.
            # Select.NULL is the actual "no selection" sentinel here.
            yield Select(
                self.select_options or [],
                value=self.default or Select.NULL,
                id="control",
            )
        else:
            yield Input(value=str(self.default), id="control")
        yield Static("", id="mark")

    def on_mount(self) -> None:
        self.refresh_mark()

    @property
    def value(self) -> str | bool:
        control = self.query_one("#control")
        if isinstance(control, Switch):
            return control.value
        if isinstance(control, Select):
            return "" if control.value == Select.NULL else control.value
        # mariadb-migrator strips leading/trailing whitespace from every
        # password it prompts for (e.g. :1512-1513, :1631-1632, :1775-1776)
        # before both use and save -- a pasted trailing space here would
        # otherwise be stored verbatim and cause an auth failure that looks
        # like a wrong password.
        return control.value.strip()

    @property
    def is_valid(self) -> bool:
        value = self.value
        if self.field_type == "switch":
            return True
        if self.required and not value:
            return False
        if self.validator is not None and value and not self.validator(value):
            return False
        return True

    def refresh_mark(self) -> None:
        # Public: a caller that mutates `.required`/`.validator` after mount
        # (e.g. a screen applying per-mode required rules) must be able to
        # force the mark to recompute immediately, not just wait for the
        # next Changed event.
        mark = self.query_one("#mark", Static)
        value = self.value
        if self.field_type == "switch":
            mark.update("✓")
            mark.set_classes("ok")
            return
        if self.required and not value:
            mark.update("✗")
            mark.set_classes("bad")
            return
        if self.validator is not None and value and not self.validator(value):
            mark.update("✗")
            mark.set_classes("bad")
            return
        if value:
            mark.update("✓")
            mark.set_classes("ok")
            return
        mark.update("")
        mark.set_classes("")

    def _emit_changed(self) -> None:
        self.refresh_mark()
        self.post_message(self.Changed(self, self.key, self.value))

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._emit_changed()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        event.stop()
        self._emit_changed()

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        self._emit_changed()

    def __repr__(self) -> str:
        if self.secret:
            return f"LabeledField(key={self.key!r}, secret=True)"
        # Guard on is_mounted (a plain bool, no query) rather than calling
        # self.value directly: an unmounted query_one raises NoMatches whose
        # own message embeds repr(self), which would recurse into this
        # method before the exception can even be constructed.
        value = self.value if self.is_mounted else self.default
        return f"LabeledField(key={self.key!r}, label={self.label!r}, value={value!r})"
