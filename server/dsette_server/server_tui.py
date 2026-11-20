"""Local inbox placement UI; all filesystem policy remains in admin.place."""
from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Footer, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from .config import config


class DirectoryPicker(DirectoryTree):
    def filter_paths(self, paths):
        directories = []
        for path in paths:
            try:
                if path.is_dir() and not path.is_symlink():
                    directories.append(path)
            except OSError:
                continue
        return directories


class ConfirmPlace(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    ConfirmPlace { align: center middle; background: #061012 75%; }
    ConfirmPlace #confirm-box { width: 68; max-width: 95%; height: auto; padding: 1 2; border: round #dcb67a; background: #172528; }
    ConfirmPlace Label { color: #ecc48d; text-style: bold; height: 2; }
    ConfirmPlace Static { height: auto; margin-bottom: 1; }
    ConfirmPlace Horizontal { height: 3; }
    ConfirmPlace Button { width: 1fr; min-width: 8; }
    """

    def __init__(self, name, destination):
        super().__init__()
        self.parcel_name = name
        self.destination = destination

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label("Give this parcel a home?")
            yield Static(Text(f"{self.parcel_name}\n\nMove to:\n{self.destination}\n\nThe inbox copy will be removed after placement. Existing files are never overwritten."))
            with Horizontal():
                yield Button("Go back", id="cancel-place")
                yield Button("Place file", id="confirm-place", variant="success")

    def on_mount(self):
        self.query_one("#cancel-place", Button).focus()

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(event.button.id == "confirm-place")

    def action_cancel(self):
        self.dismiss(False)


class ServerInboxApp(App):
    TITLE = "Datasette · the receiving shelf"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("r", "refresh", "Refresh"), Binding("escape", "shelf", "Shelf"), Binding("q", "quit", "Quit")]
    CSS = """
    Screen { background: #101a1c; color: #d8dfc2; }
    #heading { height: 3; padding: 0 2; color: #ecc48d; background: #172528; }
    #body { padding: 1 2; }
    #layout { height: auto; grid-size: 2; grid-columns: 1fr 1fr; grid-gutter: 0 3; }
    .column { height: auto; }
    .title { height: 2; color: #ecc48d; text-style: bold; }
    .help { height: auto; color: #a8b9ae; margin-bottom: 1; }
    #inbox { height: 15; min-height: 5; background: #142427; border: round #456354; }
    Input, Select { background: #142427; margin-bottom: 1; }
    Input { border: tall #456354; }
    Input:focus { border: tall #ecc48d; }
    Button { background: #263f3c; color: #e1e7cc; border: tall #3d5d50; min-width: 10; }
    Button:focus { border: tall #ecc48d; }
    #buttons { height: 3; }
    #buttons Button { width: 1fr; }
    #browser { height: auto; }
    DirectoryPicker { height: 10; border: round #456354; background: #142427; }
    #message { height: auto; min-height: 3; margin-top: 1; color: #b1c99c; }
    Footer { background: #172528; }
    .narrow #heading { height: 2; padding: 0 1; }
    .narrow #body { padding: 0 1; }
    .narrow #layout { grid-size: 1; grid-gutter: 1 0; }
    .narrow #inbox { height: 6; }
    .narrow .title { height: 1; }
    .narrow Input, .narrow Select { margin-bottom: 0; }
    """

    def __init__(self, *, roots=None, inbox_loader=None, place_file=None):
        super().__init__()
        self.roots = tuple(Path(p).expanduser().resolve() for p in (roots if roots is not None else config.allowed_roots))
        self.inbox_loader = inbox_loader or self._load_inbox
        self.place_file = place_file
        self.items = {}
        self.selected_id = None
        self.placing = False

    @staticmethod
    def _load_inbox():
        from .store import connect
        with connect() as db:
            return [dict(row) for row in db.execute("SELECT id,name,size,created FROM transfers WHERE status='verified' ORDER BY created DESC")]

    def compose(self) -> ComposeResult:
        yield Static("[b]DATASETTE[/b] · the receiving shelf\nA home for every little parcel.", id="heading")
        with VerticalScroll(id="body"):
            with Grid(id="layout"):
                with Vertical(classes="column"):
                    yield Label("1 · Choose a parcel", classes="title")
                    yield Static("Only complete, verified inbox files appear here.", classes="help")
                    yield OptionList(id="inbox")
                    yield Button("Refresh shelf", id="refresh")
                with Vertical(classes="column"):
                    yield Label("2 · Choose its home", classes="title")
                    yield Static("Select an allowed root, then type a folder or browse. Placement checks the real path again.", classes="help")
                    options = [(str(root), str(root)) for root in self.roots]
                    yield Select(options, value=str(self.roots[0]) if self.roots else Select.BLANK, allow_blank=not bool(self.roots), id="root")
                    yield Input(value=str(self.roots[0]) if self.roots else "", placeholder="Destination folder…", id="destination")
                    yield Input(placeholder="Rename file (optional)…", id="rename")
                    with Horizontal(id="buttons"):
                        yield Button("Browse…", id="browse")
                        yield Button("Place…", id="place", disabled=True)
                    yield Vertical(id="browser")
            yield Static("Choose a parcel, then give it a home.", id="message", markup=False)
        yield Footer()

    def on_mount(self):
        self.set_class(self.size.width < 78, "narrow")
        self.action_refresh()
        self.query_one(OptionList).focus()

    def on_resize(self, event):
        self.set_class(event.size.width < 78, "narrow")

    def _message(self, text):
        self.query_one("#message", Static).update(Text(str(text)))

    def action_shelf(self):
        self.query_one(OptionList).focus()

    def action_refresh(self):
        try:
            items = self.inbox_loader()
            self.items = {str(x.get("id", x.get("transfer_id"))): x for x in items}
            self.selected_id = None
            options = self.query_one(OptionList)
            options.clear_options()
            for tid, row in self.items.items():
                size = row.get("size", 0)
                size_label = f"{size / 1024**2:.1f} MB" if size >= 1024**2 else f"{size / 1024:.1f} KB"
                options.add_option(Option(Text(f"{row['name']}  ·  {size_label}\n{tid[:12]}"), id=tid))
            if self.items:
                options.highlighted = 0
            self.query_one("#place", Button).disabled = True
            self._message(f"{len(items)} parcel(s) on the shelf." if items else "The shelf is quiet. Send a file to this server, then refresh.")
        except Exception as exc:
            self._message("Couldn’t read inbox: " + str(exc).splitlines()[0][:220])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.selected_id = event.option.id
        self.query_one("#place", Button).disabled = self.placing or not self.roots
        self._message("Selected: " + self.items[self.selected_id]["name"])

    async def on_select_changed(self, event: Select.Changed):
        if event.select.id == "root" and event.value is not Select.BLANK:
            self.query_one("#destination", Input).value = str(event.value)
            await self.query_one("#browser", Vertical).remove_children()

    async def on_button_pressed(self, event: Button.Pressed):
        key = event.button.id
        if key == "refresh":
            self.action_refresh()
        elif key == "browse":
            browser = self.query_one("#browser", Vertical)
            if list(browser.children):
                await browser.remove_children()
                return
            root = self.query_one("#root", Select).value
            if root is Select.BLANK:
                self._message("Configure DSETTE_ALLOWED_ROOTS before placing files.")
            elif not Path(str(root)).is_dir():
                self._message("This root will be created when placing a file. Type the destination folder above.")
            else:
                tree = DirectoryPicker(str(root))
                await browser.mount(tree)
                tree.focus()
        elif key == "place" and self.selected_id:
            destination = self.query_one("#destination", Input).value.strip()
            rename = self.query_one("#rename", Input).value.strip() or None
            if not destination:
                self._message("Choose a destination folder first.")
                return
            tid = self.selected_id
            filename = rename or self.items[tid]["name"]
            target = Path(destination).expanduser() / filename
            def confirmed(yes):
                if yes:
                    self.place(tid, destination, rename)
            self.push_screen(ConfirmPlace(self.items[tid]["name"], str(target)), confirmed)

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected):
        self.query_one("#destination", Input).value = str(event.path)
        self.query_one("#rename", Input).focus()

    @work(thread=True, group="placement", exclusive=True)
    def place(self, tid, destination, rename):
        self.placing = True
        self.call_from_thread(self.query_one("#place", Button).__setattr__, "disabled", True)
        try:
            # Lazy import avoids a cycle when admin.main dispatches to this UI.
            from .admin import place as admin_place
            (self.place_file or admin_place)(tid, destination, rename=rename, force=False)
            self.call_from_thread(self.action_refresh)
            self.call_from_thread(self._message, "Placed safely. Your parcel is home.")
        except (Exception, SystemExit) as exc:
            self.call_from_thread(self._message, "Couldn’t place file: " + str(exc).splitlines()[0][:240])
        finally:
            self.placing = False
            self.call_from_thread(self.query_one("#place", Button).__setattr__, "disabled", not bool(self.selected_id and self.roots))


def run():
    ServerInboxApp().run()
