import asyncio
from pathlib import Path

import pytest
from textual.widgets import Button, Input, ProgressBar, Static

from dsette_client.animation import SignalGarden, WORDMARK_GLYPHS, COMPACT_WORDMARK_GLYPHS, wordmark_lines
from dsette_client.tui import BootScreen, DatasetteApp


class FakeAPI:
    config = {"device_name": "Little laptop", "url": "https://localhost:8223"}
    server_url = "https://localhost:8223"
    fingerprint = "ab:cd:ef:01:23:45"

    def __init__(self):
        self.sent = []

    def send_live(self, path, progress=None, announced=None):
        self.sent.append(path)
        announced("parcel-1")
        progress(42, 100)
        progress(100, 100)
        return {"status": "verified"}

    def live_list(self):
        return [{"transfer_id": "parcel-1", "name": "letter.txt", "size": 100}]

    def inbox(self): return []

    def devices(self):
        return [{"id": "device-1", "name": "Little laptop", "admin": True}]

    def version(self):
        return {"server_version": "0.1.0", "git_commit": "test"}

    def receive_live(self, transfer_id, destination, progress=None):
        self.received = (transfer_id, destination)
        progress(42, 100)
        progress(100, 100)
        return destination


@pytest.mark.parametrize("compact,width,height,glyphs", [(False, 5, 5, WORDMARK_GLYPHS), (True, 3, 3, COMPACT_WORDMARK_GLYPHS)])
def test_boot_wordmark_uses_aligned_cells_and_identical_repeated_letters(compact, width, height, glyphs):
    rows = wordmark_lines(compact)
    assert len(rows) == height
    assert {len(row) for row in rows} == {9 * width + 8}
    for index, letter in enumerate("DATASETTE"):
        start = index * (width + 1)
        assert tuple(row[start:start + width] for row in rows) == glyphs[letter]
        if index < 8:
            assert all(row[start + width] == " " for row in rows)
    # Regression: the second A's lower rows used to merge with neighboring
    # letters and shift relative to the first A.
    first_a = width + 1
    second_a = 3 * (width + 1)
    assert [row[first_a:first_a + width] for row in rows] == [row[second_a:second_a + width] for row in rows]
    assert rows[-2][first_a] == rows[-2][second_a]
    assert rows[-1][first_a] == rows[-1][second_a] == "|"


@pytest.mark.parametrize("size", [(36, 18), (48, 20), (80, 24), (120, 40)])
def test_keyboard_navigation_and_responsive_layout(size):
    async def check():
        app = DatasetteApp(FakeAPI(), reduced_motion=True)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            assert app.has_class("narrow") == (size[0] < 70)
            assert app.screen.virtual_size.width <= size[0]
            for shortcut, panel in (("3", "send"), ("4", "receive"), ("5", "devices"), ("6", "update"), ("2", "wait"), ("1", "push")):
                await pilot.press("escape")
                await pilot.press(shortcut)
                assert app.current_panel == panel
                assert app.query_one("#panel-" + panel).has_class("active")
                assert [button.id for button in app.query("#dock Button.selected")] == ["nav-"+panel]
                assert app.focused.id != "nav-push"
            await pilot.press("3")
            app.query_one("#send-path", Input).focus()
            await pilot.press("x", "1", "2", "3")
            assert app.query_one("#send-path", Input).value == "x123"
            assert app.current_panel == "send"
            await pilot.press("escape")
            assert app.focused.id == "nav-send"
    asyncio.run(check())


def test_resize_and_reduced_motion():
    async def check():
        app = DatasetteApp(FakeAPI(), reduced_motion=True)
        async with app.run_test(size=(120, 40)) as pilot:
            signal = app.query_one(SignalGarden)
            signal.set_transfer(42, 100)
            start = signal.frame
            await pilot.pause(.4)
            assert signal.frame == start
            assert signal.progress == .42
            assert "42%" in signal.render().plain
            await pilot.resize_terminal(48, 20)
            assert app.has_class("narrow")
            await pilot.resize_terminal(80, 24)
            assert not app.has_class("narrow")
            await pilot.press("m")
            assert not signal.reduced_motion
    asyncio.run(check())


def test_file_send_uses_actual_progress_and_shows_verification():
    async def check():
        api = FakeAPI()
        app = DatasetteApp(api, reduced_motion=True)
        async with app.run_test(size=(80, 24)) as pilot:
            app.action_panel("send")
            app.query_one("#send-path", Input).value = "letter.txt"
            app.query_one("#send-file", Button).press()
            await asyncio.wait_for(pilot.pause(.2), 5)
            await asyncio.wait_for(app.workers.wait_for_complete([w for w in app.workers if w.group == "operations"]), 5)
            assert api.sent == [Path("letter.txt")]
            assert app.query_one(ProgressBar).progress == 100
            assert app.query_one(SignalGarden).state == "success"
            assert "SHA-256 verified" in str(app.query_one("#activity", Static).render())
            assert not app.busy
    asyncio.run(check())


def test_error_is_readable_and_does_not_crash():
    async def check():
        app = DatasetteApp(FakeAPI(), reduced_motion=True)
        async with app.run_test(size=(48, 20)) as pilot:
            app.action_panel("send")
            app.query_one("#send-file", Button).press()
            await asyncio.wait_for(pilot.pause(.2), 5)
            await asyncio.wait_for(app.workers.wait_for_complete([w for w in app.workers if w.group == "operations"]), 5)
            assert app.query_one(SignalGarden).state == "error"
            assert "choose a file" in str(app.query_one("#activity", Static).render())
            assert not app.busy
    asyncio.run(check())


def test_phone_boot_can_be_skipped_with_any_key():
    async def check():
        app = DatasetteApp(FakeAPI(), reduced_motion=False)
        async with app.run_test(size=(36, 18)) as pilot:
            assert isinstance(app.screen, BootScreen)
            assert app.screen.has_class("compact")
            wordmark = app.screen.query_one("#boot-wordmark", Static).render()
            assert len(str(wordmark).splitlines()) >= 3
            await pilot.press("space")
            assert not isinstance(app.screen, BootScreen)
            assert app.current_panel == "push"
    asyncio.run(check())


def test_boot_auto_dismisses_and_explicit_setting_disables_it():
    async def check():
        app = DatasetteApp(FakeAPI(), reduced_motion=False, startup_animation=False)
        async with app.run_test(size=(80, 24)) as pilot:
            assert not isinstance(app.screen, BootScreen)
            app.push_screen(BootScreen(duration=.1))
            await pilot.pause(.2)
            assert not isinstance(app.screen, BootScreen)
    asyncio.run(check())


def test_reduced_motion_skips_boot_entirely():
    async def check():
        app = DatasetteApp(FakeAPI(), reduced_motion=True)
        async with app.run_test(size=(48, 20)):
            assert not isinstance(app.screen, BootScreen)
    asyncio.run(check())


def test_phone_receive_action_is_reachable_with_keyboard_and_scrolls_into_view():
    async def check():
        api = FakeAPI()
        app = DatasetteApp(api, reduced_motion=True)
        async with app.run_test(size=(36, 18)) as pilot:
            await pilot.press("4")
            app._show_inbox(api.live_list())
            for _ in range(15):
                if app.focused and app.focused.id == "inbox-list": break
                await pilot.press("tab")
            assert app.focused.id == "inbox-list"
            await pilot.press("enter")
            assert app.selected_transfer == "parcel-1"
            for _ in range(10):
                if app.focused and app.focused.id == "receive-file": break
                await pilot.press("tab")
            assert app.focused.id == "receive-file"
            await pilot.pause()
            button = app.query_one("#receive-file", Button)
            assert button.region.y >= app.query_one("#room").region.y
            assert button.region.bottom <= app.screen.size.height - 1
            await pilot.press("enter")
            await asyncio.wait_for(pilot.pause(.2), 5)
            await asyncio.wait_for(app.workers.wait_for_complete([w for w in app.workers if w.group == "operations"]), 5)
            assert api.received[0] == "parcel-1"
            assert app.query_one(SignalGarden).state == "success"
    asyncio.run(check())


def test_client_update_offer_requires_a_configured_trusted_signer():
    async def check():
        api = FakeAPI()
        api.config = dict(api.config)
        app = DatasetteApp(api, reduced_motion=True)
        async with app.run_test(size=(80, 24)):
            app.action_panel("update")
            app._show_version({"server_version": "1.0.0", "min_client_version": "99.0.0"})
            button = app.query_one("#update-client", Button)
            assert button.display and button.disabled
            assert "trusted_signers" in str(app.query_one("#version-results", Static).render())
            api.config.update(client_checkout="checkout", trusted_signers="allowed-signers")
            app._busy(False)
            assert not button.disabled
            app._show_version({"server_version": "1.0.0", "min_client_version": "0.0.1"})
            assert not button.display
    asyncio.run(check())


def test_client_relaunch_happens_after_terminal_app_returns(monkeypatch):
    from dsette_client import tui
    order = []
    config_path = Path("test-config").resolve()
    class FinishedApp:
        def __init__(self, *args, **kwargs): pass
        def run(self):
            order.append("terminal-restored")
            return "restart-client"
    class API:
        config_dir = config_path
        def close(self): order.append("api-closed")
    monkeypatch.setattr(tui, "DatasetteApp", FinishedApp)
    def execute(executable, args):
        order.append("exec")
        assert args[1:3] == ["-m", "dsette_client.main"]
        assert args[-1] == "--no-animation"
        assert str(config_path) in args
    monkeypatch.setattr(tui.os, "execv", execute)
    tui.run(API())
    assert order == ["terminal-restored", "api-closed", "exec"]
