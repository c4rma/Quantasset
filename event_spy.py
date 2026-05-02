#!/usr/bin/env python3
"""
InfoHunter Event Spy — run in Termux, swipe in all directions,
then check ~/termux_events.log
"""
from textual.app import App, ComposeResult
from textual.widgets import Static
from textual import events
import datetime, os

LOG_PATH = os.path.expanduser("~/termux_events.log")

# Clear log on start
open(LOG_PATH, "w").close()

class EventSpy(App):
    def compose(self) -> ComposeResult:
        yield Static(
            "[bold]Event Spy[/bold]\n\n"
            "Swipe LEFT, RIGHT, UP, DOWN with your finger\n\n"
            "Then:  cat ~/termux_events.log\n\n"
            "Ctrl+C to quit"
        )

    async def on_event(self, event: events.Event) -> None:
        await super().on_event(event)
        name = type(event).__name__
        if any(x in name for x in ("Mouse", "Scroll", "Key", "Touch", "Move")):
            attrs = {k: v for k, v in vars(event).items()
                     if not k.startswith("_") and k not in ("sender", "style")}
            line = f"{datetime.datetime.now().strftime('%H:%M:%S.%f')} {name} {attrs}\n"
            with open(LOG_PATH, "a") as f:
                f.write(line)

EventSpy().run()
