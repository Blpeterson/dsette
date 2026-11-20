import pytest

from dsette_client import __version__,clipboard,main


def test_version_needs_no_enrollment(capsys):
    with pytest.raises(SystemExit) as result: main.main(["--version"])
    assert result.value.code==0
    assert capsys.readouterr().out.strip()=="dset "+__version__


@pytest.mark.parametrize("copy",[False,True])
def test_watch_repeats_and_copies_only_when_requested(tmp_path,monkeypatch,capsys,copy):
    (tmp_path/"config.json").write_text("{}")
    copied=[]; after_values=[]
    class API:
        def __init__(self,**kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def wait_clipboard(self,after):
            after_values.append(after)
            if len(after_values)>2: raise KeyboardInterrupt
            return {"clip_id":len(after_values),"data":"arrival "+str(len(after_values))}
    monkeypatch.setattr(main,"config_directory",lambda _:tmp_path)
    monkeypatch.setattr(main,"DsetteAPI",API)
    monkeypatch.setattr(clipboard,"write_text",copied.append)
    args=["watch"]+(["--copy"] if copy else [])
    main.main(args)
    assert after_values==[0,1,2]
    assert copied==(["arrival 1","arrival 2"] if copy else [])
    output=capsys.readouterr().out
    assert "arrival 1" in output and "arrival 2" in output and "Cancelled" in output
