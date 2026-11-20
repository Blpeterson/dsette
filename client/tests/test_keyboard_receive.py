import asyncio
from pathlib import Path
import pytest
from textual.widgets import Button,Input,Checkbox,Static
from dsette_client.tui import DatasetteApp


class API:
    config={"admin":True,"device_name":"keyboard station"}
    server_url="https://demo.example"
    device_id="local"
    def __init__(self): self.items=[]; self.calls=0; self.received=[]
    def live_list(self): self.calls+=1; return list(self.items)
    def inbox(self): return []
    def receive_live(self,tid,destination,progress=None):
        self.received.append((tid,destination)); progress(5,5); return destination


@pytest.mark.parametrize("size",[(36,18),(48,20),(80,24),(120,40)])
def test_every_panel_button_is_in_keyboard_focus_cycle(size):
    async def check():
        api=API(); app=DatasetteApp(api,reduced_motion=True)
        async with app.run_test(size=size) as pilot:
            app._incoming({"data":"test note","clip_id":1})
            for n,panel in enumerate(("push","wait","send","receive","devices","update"),1):
                await pilot.press(f"alt+{n}")
                await pilot.pause(.1)
                assert app.current_panel==panel
                visible=[b for b in app.query(f"#panel-{panel} Button") if b.display and not b.disabled]
                seen=set()
                for _ in range(30):
                    if app.focused:
                        seen.add(app.focused.id)
                        if isinstance(app.focused,Button) and app.focused in visible:
                            await pilot.pause()
                            assert app.focused.region.y>=0
                            assert app.focused.region.bottom<=size[1]-1
                    if {b.id for b in visible} <= seen: break
                    await pilot.press("tab")
                assert {b.id for b in visible} <= seen
                await pilot.press("shift+tab")
                assert app.focused is not None
    asyncio.run(check())


def test_arrivals_refresh_automatically_without_losing_selection_or_save_path():
    async def check():
        api=API(); app=DatasetteApp(api,reduced_motion=True)
        async with app.run_test(size=(48,20)) as pilot:
            await pilot.press("4")
            await pilot.pause(.2)
            api.items=[{"transfer_id":"parcel","name":"letter.txt","size":5}]
            await pilot.pause(2.2)
            assert "parcel" in app.inbox_items
            assert app.focused.id=="inbox-list"
            await pilot.press("enter")
            assert app.focused.id=="receive-path"
            path=app.query_one("#receive-path",Input)
            path.value="/tmp/my-choice.txt"
            await pilot.pause(2.2)
            assert app.selected_transfer=="parcel" and path.value=="/tmp/my-choice.txt"
            await pilot.press("tab")
            assert app.focused.id=="receive-file"
            await pilot.press("enter")
            await pilot.pause(.2)
            assert api.received==[("parcel",Path("/tmp/my-choice.txt"))]
            if app.focused.id!="refresh-inbox": await pilot.press("tab")
            assert app.focused.id=="refresh-inbox"
            before=api.calls
            await pilot.press("enter")
            await pilot.pause(.2)
            assert api.calls>before
            before=api.calls
            await pilot.press("f5")
            await pilot.pause(.2)
            assert api.calls>before
    asyncio.run(check())


def test_refresh_does_not_block_buttons_and_old_mode_result_is_ignored():
    async def check():
        app=DatasetteApp(API(),reduced_motion=True)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.press("4"); await pilot.pause(.2)
            assert not app.busy
            assert not app.query_one("#refresh-inbox",Button).disabled
            app.inbox_generation+=1
            app._discovered_inbox([{"transfer_id":"stale","name":"old","size":0}],True,app.inbox_generation-1)
            assert "stale" not in app.inbox_items
    asyncio.run(check())


def test_live_and_stored_files_share_shelf_and_select_correct_download_method():
    async def check():
        api=API(); api.items=[{"transfer_id":"live","name":"live.txt","size":5}]
        api.inbox=lambda:[{"transfer_id":"stored","name":"stored.txt","size":5}]
        api.receive_file=lambda tid,path,progress=None:api.received.append(("stored",tid)) or path
        app=DatasetteApp(api,reduced_motion=True)
        async with app.run_test(size=(80,24)) as pilot:
            await pilot.press("4"); await pilot.pause(.2)
            assert set(app.inbox_items)=={"live","stored"}
            await pilot.press("down","enter","tab","enter")
            await pilot.pause(.2)
            assert api.received==[("stored","stored")]
    asyncio.run(check())


def test_tab_can_skip_boot_before_entering_normal_focus_cycle():
    async def check():
        from dsette_client.tui import BootScreen
        app=DatasetteApp(API(),reduced_motion=False)
        async with app.run_test(size=(48,20)) as pilot:
            assert isinstance(app.screen,BootScreen)
            await pilot.press("tab")
            assert not isinstance(app.screen,BootScreen)
    asyncio.run(check())


@pytest.mark.parametrize("size",[(36,18),(80,24)])
def test_buttons_activate_using_only_keyboard(size):
    async def check():
        api=API(); api.config={**api.config,"client_checkout":"checkout","trusted_signers":"signers"}
        api.items=[{"transfer_id":"parcel","name":"letter.txt","size":5}]
        app=DatasetteApp(api,reduced_motion=True)
        calls=[]
        async with app.run_test(size=size) as pilot:
            app.perform=lambda operation,*args:calls.append(operation)
            app.listen=lambda stop:calls.append("listen")
            app._incoming({"data":"a note","clip_id":1})
            async def activate(panel,button):
                await pilot.press(f"alt+{panel}")
                await pilot.pause(.1)
                for _ in range(35):
                    if app.focused and app.focused.id==button: break
                    await pilot.press("tab")
                assert app.focused.id==button
                await pilot.press("enter")
                await pilot.pause(.1)
            for panel,button,operation in [(1,"push-clipboard","push"),(2,"start-wait","listen"),(2,"copy-clipboard","copy"),(3,"send-file","send"),(5,"refresh-devices","devices"),(6,"check-version","version"),(6,"update-server","update")]:
                await activate(panel,button)
                assert calls[-1]==operation
            await activate(3,"browse-file")
            assert app.query_one("#file-tree").display
            await pilot.press("alt+4"); await pilot.pause(.2)
            app.selected_transfer="parcel"
            await activate(4,"receive-file")
            assert calls[-1]=="receive"
            count=api.calls
            await activate(4,"refresh-inbox")
            assert api.calls>count
            await pilot.press("alt+6")
            app._show_version({"min_client_version":"99.0.0"})
            app._busy(False)
            await activate(6,"update-client")
            assert calls[-1]=="client-update"
    asyncio.run(check())
