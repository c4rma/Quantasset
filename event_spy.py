#!/usr/bin/env python3
"""
InfoHunter Event Spy — run this in Termux, swipe left/right,
then check ~/termux_events.log to see what events fire.
Ctrl+C to quit.
"""
from textual.app import App, ComposeResult
from textual.widgets import Static
from textual import events
import datetime, os

LOG_PATH = os.path.expanduser("~/termux_events.log")

class EventSpy(App):
    def compose(self) -> ComposeResult:
        yield Static(
            "[bold]Event Spy[/bold]\n\n"
            "Swipe LEFT, RIGHT, UP, DOWN with your finger\n"
            "Then check:  cat ~/termux_events.log\n\n"
            "Ctrl+C to quit"
        )

    def on_event(self, event: events.Event) -> None:
        name = type(event).__name__
        if any(x in name for x in ("Mouse", "Scroll", "Key", "Touch", "Move")):
            attrs = {k: v for k, v in vars(event).items()
                     if not k.startswith("_") and k not in ("sender",)}
            line = f"{datetime.datetime.now().strftime('%H:%M:%S.%f')} {name} {attrs}\n"
            with open(LOG_PATH, "a") as f:
                f.write(line)

EventSpy().run()
