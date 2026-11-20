"""Datasette's small, keyboard-friendly terminal signal station."""
from __future__ import annotations

import os
import threading
import time
import math
import sys
from pathlib import Path
from urllib.parse import urlsplit

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, DirectoryTree, Footer, Input, Label, OptionList, ProgressBar, Static
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from .animation import SignalGarden


def human_size(size):
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


class BootScreen(ModalScreen[None]):
    """A short, purely decorative boot sequence; every key skips it."""
    DEFAULT_CSS = """
    BootScreen { background: #101a1c; align: center middle; }
    BootScreen #boot-card { width: 78; max-width: 100%; height: auto; padding: 1; }
    BootScreen #boot-wordmark { height: auto; color: #ecc48d; text-align: center; }
    BootScreen #boot-radar { height: 7; margin-top: 1; color: #abc9a7; text-align: center; }
    BootScreen #boot-caption { height: 2; text-align: center; color: #a8b9ae; }
    BootScreen.compact #boot-card { padding: 0; }
    """
    LARGE = (
        r" ____    _  _____ _    ____  _____ _____ _____ _____ ",
        r"|  _ \  / \|_   _/ \  / ___|| ____|_   _|_   _| ____|",
        r"| | | |/ _ \ | |/ _ \ \___ \|  _|   | |   | | |  _|  ",
        r"| |_| / ___ \| / ___ \ ___) | |___  | |   | | | |___ ",
        r"|____/_/   \_\_/_/   \_\____/|_____| |_|   |_| |_____|",
    )
    SMALL = {
        "D": (" _ ", "| \\", "|_/"), "A": (" _ ", "/_\\", "| |"),
        "T": ("___", " | ", " | "), "S": (" __", "(_ ", "__)"),
        "E": (" __", "|_ ", "|__"),
    }

    def __init__(self, duration=1.5):
        super().__init__()
        self.duration = duration
        self.phase = 0
        self.finished = False

    def compose(self) -> ComposeResult:
        with Vertical(id="boot-card"):
            yield Static(id="boot-wordmark", markup=False)
            yield Static(id="boot-radar", markup=False)
            yield Static("A little signal goes a long way.\nPress any key to settle in.", id="boot-caption", markup=False)

    def on_mount(self):
        self.set_class(self.size.width < 62, "compact")
        self.paint_boot()
        self.set_interval(.1, self.tick)
        self.set_timer(self.duration, self.finish)

    def on_resize(self):
        if self.is_mounted:
            self.set_class(self.size.width < 62, "compact")
            self.paint_boot()

    def tick(self):
        self.phase += 1
        self.paint_boot()

    def paint_boot(self):
        if self.size.width >= 62:
            wordmark = "\n".join(self.LARGE)
        else:
            wordmark = "\n".join(" ".join(self.SMALL[letter][row] for letter in "DATASETTE") for row in range(3))
        self.query_one("#boot-wordmark", Static).update(Text(wordmark, style="#ecc48d"))
        width = max(28, min(68, self.size.width - 4))
        grid = [[" "] * width for _ in range(7)]
        radar = ("   .-----.   ", " .'       '. ", "(     +     )", " '.       .' ", "   '-----'   ")
        x0 = max(0, width // 5 - 6)
        for y, line in enumerate(radar):
            for x, char in enumerate(line): grid[y + 1][x0 + x] = char
        endpoints = ((6, 0), (10, 0), (12, 2), (10, 4), (6, 4), (2, 4), (0, 2), (2, 0))
        ex, ey = endpoints[self.phase % len(endpoints)]
        for step in range(1, 5):
            x, y = round(6 + (ex - 6) * step / 4), round(2 + (ey - 2) * step / 4)
            grid[y + 1][x0 + x] = "/" if (ex - 6) * (ey - 2) < 0 else "\\" if ex != 6 and ey != 2 else "|" if ex == 6 else "-"
        angle = self.phase * .4
        satellite_x = min(width - 7, max(15, width - 11 + round(math.cos(angle) * 3)))
        satellite_y = 2 + round(math.sin(angle) * 2)
        for i, char in enumerate("=[+]="):
            grid[satellite_y][satellite_x + i] = char
        # Equal-width lines keep the radar and satellite aligned under centered
        # rendering; trimming individual rows makes the circle drift apart.
        radar_text = Text("\n".join("".join(row) for row in grid), style="#abc9a7")
        self.query_one("#boot-radar", Static).update(radar_text)

    def on_key(self, event):
        event.stop()
        event.prevent_default()
        self.finish()

    def finish(self):
        if not self.finished:
            self.finished = True
            self.dismiss(None)


class DatasetteApp(App):
    TITLE = "Datasette · a little signal station"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("1", "panel('push')", "Clipboard ↑", show=False),
        Binding("2", "panel('wait')", "Clipboard ↓", show=False),
        Binding("3", "panel('send')", "Send file", show=False),
        Binding("4", "panel('receive')", "Receive file", show=False),
        Binding("5", "panel('devices')", "Devices", show=False),
        Binding("6", "panel('update')", "Version", show=False),
        Binding("escape", "home", "Menu"),
        Binding("m", "motion", "Motion"),
        Binding("q", "quit", "Quit"),
    ]
    CSS = """
    Screen { background: #101a1c; color: #d8dfc2; }
    #masthead { height: 3; padding: 0 2; background: #172528; color: #ecc48d; }
    #connection { height: 1; padding: 0 2; color: #9eb2a4; background: #142124; }
    #workspace { height: 1fr; }
    #dock { width: 27; height: 1fr; padding: 1; background: #132024; grid-size: 1; grid-rows: 3; grid-gutter: 1 0; }
    #dock Button { width: 100%; min-width: 0; height: 3; background: #1d3032; color: #c6d6bc; border: none; text-align: left; padding: 0 1; }
    #dock Button:hover { background: #294143; }
    #dock Button:focus { background: #374c43; color: #ffe0a8; text-style: bold; }
    #dock Button.selected { background: #3c4436; color: #f1cd94; }
    #room { width: 1fr; padding: 1 2; }
    .panel { height: auto; display: none; padding-bottom: 1; }
    .panel.active { display: block; }
    .section-title { height: 2; color: #ecc48d; text-style: bold; }
    .hint { height: auto; margin-bottom: 1; color: #a8b9ae; }
    .action-row { height: auto; min-height: 3; margin-top: 1; }
    .action-row Button { min-width: 12; width: 1fr; margin-right: 1; }
    Button { background: #263f3c; color: #e1e7cc; border: tall #3d5d50; }
    Button:focus { border: tall #ecc48d; }
    Input { background: #142427; border: tall #456354; margin-top: 1; }
    Input:focus { border: tall #ecc48d; }
    Checkbox { background: #142427; border: none; padding: 0; height: 3; }
    OptionList { background: #142427; border: round #456354; min-height: 4; max-height: 11; margin-top: 1; }
    DirectoryTree { display: none; height: 10; border: round #456354; margin-top: 1; }
    #file-browser { height: auto; }
    #incoming-preview { padding: 1; background: #142427; border: round #456354; height: auto; min-height: 4; max-height: 12; }
    #device-results, #version-results { height: auto; padding: 1; background: #142427; }
    #progress { height: 1; margin-top: 1; }
    #activity { height: auto; min-height: 2; margin-top: 1; color: #b1c99c; }
    Footer { background: #172528; }
    .narrow #masthead { height: 2; padding: 0 1; }
    .narrow #connection { padding: 0 1; }
    .narrow #workspace { layout: vertical; }
    .narrow #dock { width: 100%; height: 4; padding: 0; grid-size: 3 2; grid-rows: 2 2; grid-gutter: 0 1; }
    .narrow #dock Button { height: 2; padding: 0; text-align: center; }
    .narrow #room { width: 100%; padding: 0 1; }
    .narrow #signal { height: 3; padding: 0; }
    .narrow .section-title { height: 1; }
    .narrow .hint { margin-bottom: 0; }
    .narrow Checkbox { height: 1; }
    .narrow Input, .narrow OptionList, .narrow .action-row { margin-top: 0; }
    .narrow #inbox-list { height: 3; min-height: 3; max-height: 3; }
    .narrow .action-row Button { margin-right: 0; min-width: 8; }
    .narrow #incoming-preview { padding: 0 1; min-height: 3; }
    """

    LABELS = {"push": ("1  Send clipboard", "1 Clip ↑"), "wait": ("2  Wait for clipboard", "2 Clip ↓"),
              "send": ("3  Send a file", "3 Send"), "receive": ("4  Receive a file", "4 Receive"),
              "devices": ("5  Devices & status", "5 Devices"), "update": ("6  Updates & version", "6 Version")}

    def __init__(self, api, reduced_motion=None, startup_animation=True):
        super().__init__()
        self.api = api
        self.reduced_motion = (os.environ.get("DSETTE_REDUCED_MOTION", "").lower() in ("1", "true", "yes")) if reduced_motion is None else reduced_motion
        self.current_panel = "push"
        self.incoming_clip = None
        self.inbox_items = {}
        self.selected_transfer = None
        self.stop_wait = threading.Event()
        self.busy = False
        self.startup_animation = startup_animation
        self.is_admin = bool((getattr(api, "config", {}) or {}).get("admin", False))

    def compose(self) -> ComposeResult:
        yield Static("[b]DATASETTE[/b]  ·  a little signal station\nA warm place for things in transit.", id="masthead")
        yield Static("Connecting your little station…", id="connection")
        with Horizontal(id="workspace"):
            with Grid(id="dock"):
                for panel, (label, _) in self.LABELS.items():
                    yield Button(label, id="nav-" + panel, classes="selected" if panel == "push" else "")
            with VerticalScroll(id="room"):
                yield SignalGarden(id="signal")
                with Vertical(id="panel-push", classes="panel active"):
                    yield Label("A note from here", classes="section-title")
                    yield Static("Send your current clipboard to your other devices. Notes fade after ten minutes.", classes="hint")
                    yield Button("Send clipboard  ↑", id="push-clipboard")
                with Vertical(id="panel-wait", classes="panel"):
                    yield Label("A note arriving", classes="section-title")
                    yield Static("Listen for another device. Copy only when you choose.", classes="hint")
                    yield Static("Nothing has arrived yet.", id="incoming-preview", markup=False)
                    with Horizontal(classes="action-row"):
                        yield Button("Start listening", id="start-wait")
                        yield Button("Copy note", id="copy-clipboard", disabled=True)
                with Vertical(id="panel-send", classes="panel"):
                    yield Label("Pack a little parcel", classes="section-title")
                    yield Static("Choose a file. Live mode streams while the other device receives.", classes="hint")
                    yield Input(placeholder="File path…", id="send-path")
                    yield Checkbox("Live transfer · both devices online", id="send-live", value=True)
                    with Horizontal(classes="action-row"):
                        yield Button("Browse…", id="browse-file")
                        yield Button("Send file  ↑", id="send-file")
                    yield Vertical(id="file-browser")
                with Vertical(id="panel-receive", classes="panel"):
                    yield Label("The receiving shelf", classes="section-title")
                    yield Checkbox("Show live transfers", id="receive-live", value=True)
                    yield OptionList(id="inbox-list")
                    yield Input(placeholder="Save as… (full file path)", id="receive-path")
                    with Horizontal(classes="action-row"):
                        yield Button("Refresh", id="refresh-inbox")
                        yield Button("Receive  ↓", id="receive-file")
                with Vertical(id="panel-devices", classes="panel"):
                    yield Label("Our little stations", classes="section-title")
                    yield Static("Choose Refresh to check your connected devices.", id="device-results", markup=False)
                    yield Button("Refresh status", id="refresh-devices")
                with Vertical(id="panel-update", classes="panel"):
                    yield Label("Tend the station", classes="section-title")
                    yield Static("Check the installed version and available updates.", id="version-results", markup=False)
                    with Horizontal(classes="action-row"):
                        yield Button("Check version", id="check-version")
                        yield Button("Update server", id="update-server")
                    yield Button("Update & restart client", id="update-client", disabled=True)
                    yield Static("Server updates require an administrator device and trusted signing keys.", classes="hint")
                yield ProgressBar(total=100, show_eta=False, id="progress")
                yield Static("Ready when you are. Choose 1–6, or use Tab and Enter.", id="activity", markup=False)
        yield Footer()

    def on_mount(self):
        self.query_one(SignalGarden).reduced_motion = self.reduced_motion
        self._resize_layout(self.size.width)
        self._connection()
        self.query_one("#update-server", Button).disabled = not self.is_admin
        self.query_one("#update-client", Button).display = False
        self.query_one("#nav-push", Button).focus()
        if self.startup_animation and not self.reduced_motion:
            self.push_screen(BootScreen())

    def on_resize(self, event):
        if self.is_mounted:
            self._resize_layout(event.size.width)

    def _resize_layout(self, width):
        narrow = width < 70
        self.set_class(narrow, "narrow")
        for panel, labels in self.LABELS.items():
            self.query_one("#nav-" + panel, Button).label = labels[int(narrow)]
        self.query_one("#masthead", Static).update("[b]DATASETTE[/b] · little signal station\nA little warmth between devices." if narrow else "[b]DATASETTE[/b]  ·  a little signal station\nA warm place for things in transit.")
        self._connection()

    def _connection(self):
        cfg = getattr(self.api, "config", {}) or {}
        server = getattr(self.api, "server_url", cfg.get("url", "server"))
        name = cfg.get("device_name", "this device")
        fingerprint = getattr(self.api, "fingerprint", "") or cfg.get("fingerprint", "")
        if self.size.width < 70:
            host = urlsplit(server).hostname or server
            text = f"{name[:10]} · {host[:13]} · TLS"
        else:
            text = f"{name} · {server} · key ready"
        if fingerprint and self.size.width >= 70:
            text += " · " + str(fingerprint)[:12]
        self.query_one("#connection", Static).update(Text(text))

    def action_panel(self, panel):
        if panel not in self.LABELS:
            return
        self.stop_wait.set()
        self.current_panel = panel
        for widget in self.query(".panel"):
            widget.set_class(widget.id == "panel-" + panel, "active")
        for button in self.query("#dock Button"):
            button.set_class(button.id == "nav-" + panel, "selected")
        if not self.busy:
            self.query_one(SignalGarden).state = "idle"
        self.query_one("#room", VerticalScroll).scroll_home(animate=False)

    def action_home(self):
        self.query_one("#nav-" + self.current_panel, Button).focus()

    def action_motion(self):
        self.reduced_motion = not self.reduced_motion
        self.query_one(SignalGarden).reduced_motion = self.reduced_motion
        self._notice("Motion reduced." if self.reduced_motion else "Gentle motion enabled.")

    def _notice(self, message, state=None):
        self.query_one("#activity", Static).update(Text(str(message)))
        if state:
            self.query_one(SignalGarden).state = state

    def _progress(self, done, total, direction):
        self.query_one(SignalGarden).set_transfer(done, total, direction)
        self.query_one(ProgressBar).update(total=max(total, 1), progress=done)
        self._notice(f"{human_size(done)} of {human_size(total)} · {'sending' if direction == 'send' else 'receiving'}")

    def _busy(self, value):
        self.busy = value
        for name in ("send-file", "receive-file", "push-clipboard", "update-server"):
            self.query_one("#" + name, Button).disabled = value or (name == "update-server" and not self.is_admin)
        cfg = getattr(self.api, "config", {}) or {}
        self.query_one("#update-client", Button).disabled = value or not bool(cfg.get("client_checkout") and cfg.get("trusted_signers"))

    async def on_button_pressed(self, event: Button.Pressed):
        key = event.button.id
        if key.startswith("nav-"):
            self.action_panel(key[4:]); return
        if key == "browse-file":
            trees = list(self.query(DirectoryTree))
            if not trees:
                tree = DirectoryTree(str(Path.home()), id="file-tree")
                await self.query_one("#file-browser", Vertical).mount(tree)
            else:
                tree = trees[0]
            tree.display = not tree.display
            if tree.display: tree.focus()
            return
        if key == "start-wait":
            self.stop_wait.set(); self.stop_wait = threading.Event()
            self.listen(self.stop_wait); self._notice("Listening. Your clipboard stays yours until you choose Copy.", "waiting"); return
        if key == "copy-clipboard":
            self.perform("copy"); return
        if key == "send-file":
            self.perform("send", self.query_one("#send-path", Input).value, self.query_one("#send-live", Checkbox).value); return
        if key == "receive-file":
            if not self.selected_transfer:
                self._notice("Choose a transfer from the shelf first.", "error"); return
            self.perform("receive", self.selected_transfer, self.query_one("#receive-path", Input).value, self.query_one("#receive-live", Checkbox).value); return
        operations = {"push-clipboard": "push", "refresh-inbox": "inbox", "refresh-devices": "devices", "check-version": "version", "update-server": "update", "update-client": "client-update"}
        if key in operations:
            self.perform(operations[key])

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected):
        self.query_one("#send-path", Input).value = str(event.path)
        self.query_one(DirectoryTree).display = False
        self.query_one("#send-file", Button).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.selected_transfer = event.option.id
        item = self.inbox_items.get(self.selected_transfer, {})
        name = item.get("name", "received-file")
        self.query_one("#receive-path", Input).value = str(Path.home() / "Downloads" / name)

    def _show_inbox(self, items):
        self.selected_transfer = None
        self.inbox_items = {str(x.get("transfer_id", x.get("id"))): x for x in items}
        options = self.query_one(OptionList)
        options.clear_options()
        for tid, item in self.inbox_items.items():
            options.add_option(Option(Text(f"{item['name']}  ·  {human_size(item.get('size', 0))}"), id=tid))
        if self.inbox_items:
            options.highlighted = 0
        self._notice(f"{len(items)} parcel(s) waiting." if items else "The shelf is quiet. Send from another device, then refresh.")

    def _incoming(self, item):
        self.incoming_clip = item
        content = str(item.get("data", ""))
        preview = content[:4096] + ("\n… Preview shortened. Copy note copies the full text." if len(content) > 4096 else "")
        self.query_one("#incoming-preview", Static).update(Text(preview))
        self.query_one("#copy-clipboard", Button).disabled = False
        self._notice("A note arrived. Press Copy note when you are ready.", "success")

    def _show_version(self, result):
        from .selfupdate import update_required
        text = "\n".join(f"{k.replace('_', ' ')}: {v}" for k, v in result.items())
        required = bool(result.get("min_client_version") and update_required(result["min_client_version"]))
        self.query_one("#update-client", Button).display = required
        cfg = getattr(self.api, "config", {}) or {}
        if required:
            text += "\n\nClient update required."
            if cfg.get("client_checkout") and cfg.get("trusted_signers"):
                text += " Verify and install the signed update below, then restart automatically."
            else:
                text += " Configure client_checkout (source folder) and trusted_signers (SSH allowed-signers file) in your device config to update here. Or run: dset self-update --checkout PATH"
        self.query_one("#version-results", Static).update(Text(text))

    @work(thread=True, group="clipboard-watch", exclusive=True)
    def listen(self, stop):
        after = int((self.incoming_clip or {}).get("clip_id", 0))
        try:
            while not stop.is_set():
                item = self.api.wait_clipboard(after=after, timeout=8)
                if item and not stop.is_set():
                    after = int(item["clip_id"])
                    self.call_from_thread(self._incoming, item)
        except Exception as exc:
            if not stop.is_set(): self.call_from_thread(self._notice, self._friendly_error(exc), "error")

    @staticmethod
    def _friendly_error(exc):
        text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return "Couldn’t finish: " + text[:240]

    @work(thread=True, group="operations", exclusive=True)
    def perform(self, operation, *args):
        restart_client = False
        self.call_from_thread(self._busy, True)
        try:
            from .clipboard import read_text, write_text
            progress = lambda done, total: self.call_from_thread(self._progress, done, total, "receive" if operation == "receive" else "send")
            if operation == "push":
                self.api.push_clipboard(read_text())
                self.call_from_thread(self._notice, "Your note is on its way. It expires in ten minutes.", "success")
            elif operation == "copy":
                if self.incoming_clip:
                    write_text(self.incoming_clip["data"])
                    self.call_from_thread(self._notice, "Note copied to your clipboard.", "success")
            elif operation == "send":
                path, live = args
                if not path.strip(): raise ValueError("choose a file first")
                if live:
                    announce = lambda tid: self.call_from_thread(self._notice, "Live parcel ready. On the other device, open Receive and refresh. ID: " + tid, "waiting")
                    self.api.send_live(Path(path).expanduser(), progress=progress, announced=announce)
                else:
                    self.api.send_file(Path(path).expanduser(), progress=progress)
                self.call_from_thread(self._notice, "Parcel delivered and SHA-256 verified.", "success")
            elif operation == "receive":
                tid, path, live = args
                if not path.strip(): raise ValueError("choose a destination filename")
                method = self.api.receive_live if live else self.api.receive_file
                destination = method(tid, Path(path).expanduser(), progress=progress)
                self.call_from_thread(self._notice, "Arrived safely · " + str(destination), "success")
            elif operation == "inbox":
                live = self.call_from_thread(lambda: self.query_one("#receive-live", Checkbox).value)
                self.call_from_thread(self._show_inbox, self.api.live_list() if live else self.api.inbox())
            elif operation == "devices":
                items = self.api.devices()
                text = "\n".join(f"{x.get('name', 'device')} {'· admin' if x.get('admin') else ''}\n  {x.get('id', '')}" for x in items) or "No devices found."
                self.call_from_thread(self.query_one("#device-results", Static).update, Text(text))
                self.call_from_thread(self._notice, "Stations checked.", "success")
            elif operation == "version":
                result = self.api.version()
                self.call_from_thread(self._show_version, result)
                self.call_from_thread(self._notice, "Version checked.", "success")
            elif operation == "client-update":
                from .selfupdate import update_checkout
                cfg = self.api.config
                if not cfg.get("client_checkout") or not cfg.get("trusted_signers"):
                    raise ValueError("configure client_checkout and trusted_signers before updating")
                self.call_from_thread(self._notice, "Checking the signed client update. This may take a moment.", "waiting")
                update_checkout(cfg["client_checkout"], branch=cfg.get("update_branch", "main"), remote=cfg.get("update_remote", "origin"), trusted_signers=cfg["trusted_signers"])
                restart_client = True
            elif operation == "update":
                result = self.api.update()
                self.call_from_thread(self._notice, "Update requested · " + str(result), "waiting")
                job_id = result.get("job_id")
                deadline = time.monotonic() + 300
                while job_id and time.monotonic() < deadline and not get_current_worker().is_cancelled:
                    status = self.api.update_status(job_id)
                    logs = status.get("log_tail", status.get("log", []))
                    if isinstance(logs, list): logs = "\n".join(str(x) for x in logs[-20:])
                    self.call_from_thread(self.query_one("#version-results", Static).update, Text("Update: " + str(status.get("state", "running")) + "\n" + str(logs)))
                    if status.get("state") in ("complete", "failed", "rolled_back"):
                        self.call_from_thread(self._notice, "Update " + status["state"], "success" if status["state"] == "complete" else "error")
                        break
                    time.sleep(1)
        except Exception as exc:
            self.call_from_thread(self._notice, self._friendly_error(exc), "error")
        finally:
            self.call_from_thread(self._busy, False)
        if restart_client:
            self.stop_wait.set()
            self.call_from_thread(self.exit, "restart-client")

    def on_unmount(self):
        self.stop_wait.set()


def run(api, *, startup_animation=True):
    result = DatasetteApp(api, startup_animation=startup_animation).run()
    if result == "restart-client":
        # App.run has restored the terminal before replacing the process.
        api.close()
        os.execv(sys.executable, [sys.executable, "-m", "dsette_client.main", "--config-dir", str(api.config_dir), "--no-animation"])
