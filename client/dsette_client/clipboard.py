"""Native clipboard access with common terminal/desktop fallbacks."""
import os
import shutil
import subprocess
import sys
import time


def _commands(write=False):
    if shutil.which("termux-clipboard-set" if write else "termux-clipboard-get"):
        yield ["termux-clipboard-set" if write else "termux-clipboard-get"]
    if sys.platform=="darwin": yield ["pbcopy" if write else "pbpaste"]
    elif sys.platform=="win32":
        yield ["powershell.exe","-NoProfile","-NonInteractive","-Command","$input | Set-Clipboard" if write else "Get-Clipboard -Raw"]
    else:
        if os.getenv("WAYLAND_DISPLAY"): yield ["wl-copy"] if write else ["wl-paste","--no-newline"]
        yield ["xclip","-selection","clipboard"] if write else ["xclip","-selection","clipboard","-o"]
        yield ["xsel","--clipboard","--input" if write else "--output"]


def read_text():
    try:
        import pyperclip
        return pyperclip.paste()
    except Exception: pass
    for command in _commands():
        if not shutil.which(command[0]): continue
        try:
            result=subprocess.run(command,check=True,capture_output=True,text=True,encoding="utf-8",timeout=5)
            return result.stdout
        except (OSError,subprocess.SubprocessError): continue
    raise RuntimeError("Clipboard unavailable. Install wl-clipboard/xclip, or Termux:API on Android.")


def write_text(text):
    try:
        import pyperclip
        pyperclip.copy(text); return
    except Exception: pass
    for command in _commands(write=True):
        if not shutil.which(command[0]): continue
        try:
            subprocess.run(command,input=text,check=True,capture_output=True,text=True,encoding="utf-8",timeout=5); return
        except (OSError,subprocess.SubprocessError): continue
    raise RuntimeError("Clipboard unavailable. Install wl-clipboard/xclip, or Termux:API on Android.")


def watch(callback,stop_event,interval=.5):
    previous=read_text()
    while not stop_event.wait(interval):
        current=read_text()
        if current!=previous: callback(current); previous=current
