"""A tiny signal garden, driven by transfer progress rather than a fake timer."""
from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static


class SignalGarden(Static):
    """A warm, deliberately quiet animation that also works in narrow terminals."""

    progress = reactive(0.0)
    direction = reactive("send")
    state = reactive("idle")
    reduced_motion = reactive(False)
    frame = reactive(0)

    DEFAULT_CSS = """
    SignalGarden { height: 6; padding: 0 1; color: #d8dfc2; }
    .narrow SignalGarden { height: 4; padding: 0; }
    """

    def on_mount(self):
        self.set_interval(.16, self._tick)

    def _tick(self):
        if self.state == "success" and self.frame >= 12:
            return
        if not self.reduced_motion and self.state in ("sending", "receiving", "waiting", "success"):
            self.frame = (self.frame + 1) % 120

    def watch_state(self):
        self.frame = 0

    def set_transfer(self, done, total, direction="send"):
        self.direction = direction
        self.progress = min(1.0, done / total) if total else 0.0
        self.state = "sending" if direction == "send" else "receiving"

    def render(self):
        width = max(20, self.size.width - 2)
        narrow = width < 60
        span = max(6, width - 16)
        packet = min(span - 1, int(self.progress * (span - 1)))
        if self.direction == "receive":
            packet = span - 1 - packet
        line = ["·"] * span
        active = self.state in ("sending", "receiving")
        if active:
            if self.direction == "receive":
                line[packet] = "◉" if self.reduced_motion or self.frame % 4 < 2 else "o"
                if packet + 1 < span: line[packet + 1] = "~"
            else:
                line[packet] = "◆" if self.reduced_motion or self.frame % 4 < 2 else "◇"
                if packet > 0: line[packet - 1] = "─"
        elif self.state == "waiting":
            position = span // 2 if self.reduced_motion else self.frame % span
            line[position] = "o"
        elif self.state == "success":
            bloom = "✿" if self.reduced_motion else ("·", "*", "✿")[min(2, self.frame // 4)]
            for i in range(span):
                line[i] = bloom if i % 5 == 0 else "─"
        else:
            line[span // 2] = "*"
        label = {"idle": "a quiet connection", "waiting": "listening for a little signal",
                 "sending": "sending", "receiving": "receiving", "success": "arrived safely",
                 "error": "let’s try that again"}.get(self.state, self.state)
        if active:
            label += f" · {self.progress:.0%}"
        art = Text()
        if not narrow:
            art.append("   /\\" + " " * (span + 2) + "/\\\n", style="#e1b477")
        art.append("  [o] ", style="#b1c99c")
        art.append("".join(line), style="#ecc48d" if self.direction == "send" else "#a7c9bf")
        art.append(" [o]\n", style="#b1c99c")
        art.append(label.center(min(width, span + 12)), style="#91a49a")
        return art
