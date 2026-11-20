#!/usr/bin/env python3
"""Create a fail-closed public source export without Git history."""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT_FILES = {"README.md", "SECURITY.md", "pyproject.toml", "requirements.lock", "LICENSE", ".gitignore", ".gitattributes", "run.sh", "update.sh", "run.command", "update.command", "run.cmd", "update.cmd"}
OPTIONAL_ROOT_FILES = {"LICENSE.md", "NOTICE"}
DEPLOY_FILES = {
    "Caddyfile.example", "dsette.service", "firewall.md", "updater-check.sh",
    "updater-supervisor.service", "updater-systemd.md", "updater-trigger.path",
    "install-oracle.sh", "server-setup.sh", "configure-nginx.py",
    "enable-updates.sh", "release-signers", "dsette-control.conf",
    "dsette-control-restart.path", "dsette-control-restart.service",
    "dsette-control-stop.path", "dsette-control-stop.service",
    "dsette-control-start.path", "dsette-control-start.service",
    "release-signers",
}
PRIVATE_NAMES = {
    "NOTES.md", "RESUME.md", "QUESTIONS.md", "oracle-server.crt",
}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".lock", ".service", ".path", ".sh", ".example"}
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
FORBIDDEN = (
    (re.compile(r"C:\\Users\\[^\\\s]+", re.I), "Windows user path"),
    (re.compile(r"/home/[A-Za-z0-9._-]+"), "home-directory user path"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "private key"),
    (re.compile(r"(?i)\b(brack(?:en)?|blpeterson)\b"), "private identity"),
)


class ExportError(RuntimeError): pass


def allowed(relative: PurePosixPath) -> bool:
    if relative.as_posix()==".github/workflows/tests.yml": return True
    if relative.as_posix() in {"docs/images/boot-preview.gif","docs/images/transfer-preview.gif"}: return True
    value = relative.as_posix()
    if len(relative.parts)==1 and relative.name in ROOT_FILES|OPTIONAL_ROOT_FILES: return True
    if relative.name in PRIVATE_NAMES or any(part.startswith(".") for part in relative.parts): return False
    if len(relative.parts) == 1: return False
    if relative.parts[0] in {"server", "client"}:
        return relative.suffix == ".py" and ("dsette_" in value or "/tests/" in value)
    if relative.parts[0] == "deploy": return len(relative.parts) == 2 and relative.name in DEPLOY_FILES
    if relative.parts[0] == "docs": return len(relative.parts) == 2 and relative.suffix == ".md"
    # The release-construction tool and its intentionally malicious scan
    # fixtures are not runtime source and are not copied into the artifact.
    if relative.parts[0] == "scripts":
        return (len(relative.parts)==2 and relative.name in {"export_public.py","acceptance_large.py","dsette_launcher.py","bootstrap-requirements.txt"}) or (len(relative.parts)==3 and relative.parts[1]=="tests" and relative.suffix==".py")
    return False


def violations(relative: PurePosixPath, data: bytes) -> list[str]:
    # Only these manually reviewed, no-network demo captures are release assets.
    if relative.as_posix() in {"docs/images/boot-preview.gif","docs/images/transfer-preview.gif"}:
        return [] if data.startswith((b"GIF87a",b"GIF89a")) else ["invalid preview image"]
    problems = []
    if relative.name in PRIVATE_NAMES: problems.append("private filename")
    if b"\x00" in data: return problems + ["binary content is not allowed"]
    text = data.decode("utf-8")
    # The scanner source necessarily contains the signatures it detects.
    if relative.as_posix() != "scripts/export_public.py":
        for pattern, label in FORBIDDEN:
            if pattern.search(text): problems.append(label)
    for match in IPV4.finditer(text):
        try: address = ipaddress.ip_address(match.group())
        except ValueError: continue
        documented = address in ipaddress.ip_network("192.0.2.0/24") or address in ipaddress.ip_network("198.51.100.0/24") or address in ipaddress.ip_network("203.0.113.0/24")
        if not (address.is_loopback or address.is_unspecified or documented):
            problems.append(f"non-documentation IP address {address}")
    return problems


def collect(source: Path) -> list[tuple[PurePosixPath, bytes]]:
    files = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.is_symlink(): continue
        relative = PurePosixPath(path.relative_to(source).as_posix())
        if not allowed(relative): continue
        data = path.read_bytes(); issues = violations(relative, data)
        if issues: raise ExportError(f"{relative}: " + ", ".join(issues))
        files.append((relative, data))
    present = {str(p) for p, _ in files}
    missing = sorted(ROOT_FILES - present)
    if missing: raise ExportError("required public files missing: " + ", ".join(missing))
    return files


def export(source: Path, destination: Path) -> int:
    source, destination = source.resolve(), destination.resolve()
    if destination.exists(): raise ExportError(f"refusing to overwrite existing destination: {destination}")
    files = collect(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() == ".zip":
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative, data in files:
                info = zipfile.ZipInfo(relative.as_posix(), (2020, 1, 1, 0, 0, 0))
                info.external_attr = (0o755 if relative.suffix == ".sh" else 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
    else:
        destination.mkdir(mode=0o755)
        for relative, data in files:
            target = destination / Path(*relative.parts); target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data); target.chmod(0o755 if relative.suffix == ".sh" else 0o644)
    return len(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="new .zip file or directory")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try: count = export(args.source, args.destination)
    except (ExportError, UnicodeDecodeError) as exc: parser.error(str(exc))
    print(f"Exported {count} allowlisted files to {args.destination}")
    return 0


if __name__ == "__main__": raise SystemExit(main())

