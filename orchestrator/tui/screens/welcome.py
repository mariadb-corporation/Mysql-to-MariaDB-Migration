"""WelcomeScreen(Screen[str]) per design doc §4.1.

Reproduces ``mariadb-migrator:8-27`` (the welcome banner) and
``mariadb-migrator:466-495`` (the top-level action menu) verbatim -- both
ranges were read directly from the script, not invented.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, OptionList, Static
from textual.widgets.option_list import Option

from orchestrator.tui import rundir
from orchestrator.tui.modals.resume import ResumeChoiceModal

# MariaDB's sea lion mascot, downsampled from a supplied source image's
# ASCII rendering (not hand-drawn -- freehand ASCII art reliably comes out
# lopsided) into a 34-wide block-density grid (space/./:/+/*/# by fill
# fraction), small enough to leave room for the option list below it on a
# normal terminal. A separate widget from _BANNER below, which stays a
# verbatim reproduction of the real CLI's own text.
_LOGO = r"""                            :****#
                          :#####*:
                         *####*.  
                        *#####    
                      :######+    
                 ::+*########.    
            :+*#############+     
          *################*      
   .    :############*###+:       
  .*#####*:.    .::::##*          
   *##*:           :#*.           """

# mariadb-migrator:9-27, verbatim (the version/build line is hardcoded here
# to match the script's own literal values at :5-6 -- this is a static
# banner, not a live read of the script).
_BANNER = """\
---------------------------------------------------------------------
 Welcome to the MySQL to MariaDB Migration Tool
 Tool Version: 1.4.0-beta (Build 20260724)
 Supported Sources: MySQL 8.0, 8.4

 Copyright (c) 2026, MariaDB Plc. All rights reserved.
 This software is distributed free of charge and is provided "as is".

 MariaDB Software License Terms apply to all MariaDB Software unless
 otherwise stated. They do not alter the license terms of any free and
 open-source software (FOSS) or software subject to the Business Source
 License (BSL) (see Section 7 of the MariaDB Software License Terms).

 License Terms: https://legal.mariadb.com/agreements/enterprise/MariaDB_Software_License_Terms_2026-05-15.pdf
 Additional Terms: https://mariadb.com/terms/
---------------------------------------------------------------------
Type '--help' for usage instructions."""

# mariadb-migrator:474-480 (prompt_top_level_action), values are the wizard's
# own PHASE_MODE strings ("assess_plan" / "all") plus a local "quit" sentinel.
_ACTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "assess_plan",
        "1) Assess & Plan",
        "Inspect source and target, validate connectivity and compatibility, "
        "and produce an assessment report and the migration plan. No data is moved.",
    ),
    (
        "all",
        "2) Assess + Run",
        "Assess the source, then proceed to the full migration "
        "(plan + run, with confirm steps between phases).",
    ),
    (
        "demo",
        "3) See what this could look like (demo)",
        "Watch a synthetic run animate -- step list, throughput, "
        "replication lag. No real connection, no data moved.",
    ),
    ("quit", "q) Quit", ""),
)


def _make_option(label: str, description: str, value: str) -> Option:
    text = (
        Text.from_markup(f"{label}\n[dim]{description}[/dim]")
        if description
        else Text.from_markup(label)
    )
    return Option(text, id=value)


class WelcomeScreen(Screen[str]):
    BINDINGS = [
        Binding("q", "quit_action", "q quit", show=True),
    ]

    def __init__(
        self,
        repo_root: Path,
        *,
        skip_resume_check: bool = False,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.repo_root = repo_root
        self.skip_resume_check = skip_resume_check

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            # The license banner's longest lines are far short of a normal
            # terminal's width -- the logo sits in that dead space to its
            # right instead of stacking above it and costing extra rows.
            # #banner-spacer is a blank 1fr filler, not a real gap: it
            # eats whatever width neither sibling needs, which is what
            # pins the logo to the right edge instead of butting it right
            # up against the banner text.
            with Horizontal(id="banner-row"):
                yield Static(_BANNER, id="banner")
                yield Static("", id="banner-spacer")
                yield Static(_LOGO, id="logo")
            yield OptionList(
                *(_make_option(label, desc, value) for value, label, desc in _ACTIONS),
                id="actions",
            )
            yield Static("", id="hint")
        yield Footer()

    def on_mount(self) -> None:
        if self.skip_resume_check:
            return
        candidate = rundir.discover(self.repo_root)
        if candidate is None:
            return
        self.query_one("#hint", Static).update(
            "detects a resumable run automatically"
        )
        # No mode/draft exists yet at this point in the flow (discover() runs
        # before ModeSelectScreen), so the best this screen can do is judge
        # resumability against the candidate's own recorded mode/signature --
        # resume_decision()'s real caller-supplied mode/current_sig comparison
        # happens later, once a run is actually being resumed (out of scope
        # here; see the Phase-2-stub handling of "resume" in app.py).
        decision = rundir.resume_decision(
            candidate,
            mode=candidate.dir_mode or "",
            current_sig=candidate.last_sig,
            force_new_run=rundir.force_new_run_from_env(),
        )
        self.app.push_screen(ResumeChoiceModal(decision), self._on_resume_choice)

    def _on_resume_choice(self, choice: str | None) -> None:
        if choice == "resume":
            self.dismiss("resume")
        # "fresh", "cancel", or None (escape) -- nothing to do, the action
        # list underneath is already actionable.

    @on(OptionList.OptionSelected, "#actions")
    def _on_action_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_quit_action(self) -> None:
        self.dismiss("quit")
