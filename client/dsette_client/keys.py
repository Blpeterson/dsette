"""Device keys and private local configuration."""
import base64
import json
import os
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from platformdirs import user_config_dir


@lru_cache(maxsize=1)
def _windows_sid():
    result=subprocess.run(["whoami","/user","/fo","csv","/nh"],capture_output=True,text=True,check=True,timeout=10)
    match=re.search(r"S-1-\d+(?:-\d+)+",result.stdout)
    if not match: raise OSError("Could not determine the Windows user SID for private key protection")
    return match.group(0)


def protect_private(path,directory=False):
    """Set owner-only permissions, using a protected Windows DACL atomically."""
    path=Path(path)
    if os.name!="nt":
        path.chmod(0o700 if directory else 0o600); return
    import ctypes
    from ctypes import wintypes
    advapi=ctypes.WinDLL("advapi32",use_last_error=True)
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    convert=advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(wintypes.DWORD)]
    convert.restype=wintypes.BOOL
    set_security=advapi.SetFileSecurityW
    set_security.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.c_void_p]
    set_security.restype=wintypes.BOOL
    kernel.LocalFree.argtypes=[ctypes.c_void_p]; kernel.LocalFree.restype=ctypes.c_void_p
    descriptor=ctypes.c_void_p()
    flags="OICI" if directory else ""
    sddl=f"D:P(A;{flags};FA;;;{_windows_sid()})"
    if not convert(sddl,1,ctypes.byref(descriptor),None): raise ctypes.WinError(ctypes.get_last_error())
    try:
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION.
        if not set_security(str(path.resolve()),0x80000004,descriptor): raise ctypes.WinError(ctypes.get_last_error())
    finally: kernel.LocalFree(descriptor)


_protected_directories=set()


def config_directory(path=None):
    directory=(Path(path) if path is not None else Path(user_config_dir("dsette"))).expanduser().resolve()
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    if directory not in _protected_directories:
        marker=directory/".dsette-private"
        known={"device.key","config.json","server-cert.pem","uploads.json","allowed_signers","trusted_signers","release-signers"}
        if not marker.exists() and any(entry.name not in known for entry in directory.iterdir()):
            raise ValueError("Use a dedicated empty directory for --config-dir; shared directories will not be restricted")
        protect_private(directory,directory=True)
        marker.touch(exist_ok=True); protect_private(marker)
        _protected_directories.add(directory)
    return directory


def write_private(path,data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(prefix=".dsette-",dir=path.parent)
    try:
        protect_private(name)
        with os.fdopen(fd,"wb") as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        os.replace(name,path)
        protect_private(path)
    finally:
        Path(name).unlink(missing_ok=True)


def save_config(config,config_dir=None):
    write_private(config_directory(config_dir)/"config.json",json.dumps(config,indent=2).encode())


def load_config(config_dir=None):
    path=config_directory(config_dir)/"config.json"
    if not path.exists(): raise FileNotFoundError("This device is not enrolled. Run dset enroll first.")
    protect_private(path)
    return json.loads(path.read_text(encoding="utf-8"))


def load_key(config_dir=None,passphrase=None,create=True):
    path=config_directory(config_dir)/"device.key"
    password=passphrase.encode() if isinstance(passphrase,str) else passphrase
    if not path.exists():
        if not create: raise FileNotFoundError("Device private key is missing")
        key=Ed25519PrivateKey.generate()
        protection=serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
        write_private(path,key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,protection))
        return key
    protect_private(path)
    data=path.read_bytes()
    # Read keys produced by the initial prototype as well as PKCS8 keys.
    if len(data)==32: return Ed25519PrivateKey.from_private_bytes(data)
    key=serialization.load_pem_private_key(data,password=password)
    if not isinstance(key,Ed25519PrivateKey): raise ValueError("Expected an Ed25519 device key")
    return key


def public_key(key):
    return base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode()
