"""Datasette command line and first-run enrollment."""
import argparse
import getpass
import json
import socket

import httpx

from .api import DsetteAPI,enroll
from . import __version__
from .keys import config_directory,load_config


def _enroll(args):
    url=args.url or input("Server HTTPS URL: ").strip()
    invite=args.invite or getpass.getpass("One-time invite token (hidden): ").strip()
    name=args.name or input(f"Device name [{socket.gethostname()}]: ").strip() or socket.gethostname()
    fingerprint=args.fingerprint
    if not fingerprint and not args.ca_cert:
        print("For a self-signed server, compare its SHA-256 fingerprint with dsetd invite output.")
        fingerprint=input("Expected certificate fingerprint (Enter for public CA trust): ").strip() or None
    passphrase=None
    if args.encrypt_key:
        passphrase=getpass.getpass("New local device-key passphrase: ")
        if not passphrase: raise ValueError("Passphrase cannot be empty")
        if passphrase!=getpass.getpass("Repeat passphrase: "): raise ValueError("Passphrases do not match")
    cfg=enroll(url,invite,name,args.config_dir,args.ca_cert,fingerprint,passphrase,
               checkout=getattr(args,"checkout",None),trusted_signers=getattr(args,"trusted_signers",None))
    print("Enrolled",cfg["device_id"],"as",cfg["device_name"])


def build_parser():
    parser=argparse.ArgumentParser(prog="dset",description="Datasette secure clipboard and file transfer")
    parser.add_argument("--version",action="version",version="dset "+__version__)
    parser.add_argument("--config-dir",help="Directory holding this device identity")
    parser.add_argument("--no-animation",action="store_true",help="Skip the startup animation")
    sub=parser.add_subparsers(dest="cmd")
    command=sub.add_parser("enroll"); command.add_argument("url",nargs="?"); command.add_argument("invite",nargs="?",help="Optional; prompt keeps the token out of shell history")
    command.add_argument("--name"); command.add_argument("--ca-cert"); command.add_argument("--fingerprint"); command.add_argument("--encrypt-key",action="store_true")
    command.add_argument("--checkout",help="Git checkout used for signed client updates (auto-detected when available)")
    command.add_argument("--trusted-signers",help="SSH allowed-signers file (defaults to deploy/release-signers in the checkout)")
    tui=sub.add_parser("tui"); tui.add_argument("--no-animation",action="store_true",default=argparse.SUPPRESS)
    sub.add_parser("push"); sub.add_parser("pull"); sub.add_parser("inbox"); sub.add_parser("devices"); sub.add_parser("version"); sub.add_parser("live-list")
    command=sub.add_parser("wait"); command.add_argument("--after",type=int,default=0); command.add_argument("--copy",action="store_true")
    command=sub.add_parser("watch",help="Wait continuously for clipboard arrivals; Ctrl+C stops")
    command.add_argument("--after",type=int,default=0); command.add_argument("--copy",action="store_true",help="Copy each received item to the local clipboard")
    command=sub.add_parser("ack"); command.add_argument("transfer_id"); command.add_argument("--failed",action="store_true",help="Record a failed receiver verification")
    command=sub.add_parser("delete"); command.add_argument("transfer_id")
    for name in ("send","live-send"):
        command=sub.add_parser(name); command.add_argument("path")
    for name in ("receive","live-receive"):
        command=sub.add_parser(name); command.add_argument("transfer_id"); command.add_argument("destination")
    command=sub.add_parser("update"); command.add_argument("--ref",default="main"); command.add_argument("--watch",action="store_true")
    command=sub.add_parser("self-update"); command.add_argument("--checkout",default=".")
    return parser


def main(argv=None):
    args=build_parser().parse_args(argv)
    try:
        if args.cmd=="enroll": return _enroll(args)
        if not (config_directory(args.config_dir)/"config.json").exists():
            print("Welcome to Datasette. Enroll this device once to get started.")
            setup=argparse.Namespace(url=None,invite=None,name=None,ca_cert=None,fingerprint=None,encrypt_key=False,config_dir=args.config_dir)
            _enroll(setup)
        if args.cmd=="self-update":
            from .selfupdate import update_checkout
            cfg=load_config(args.config_dir)
            print("Updated to",update_checkout(args.checkout,branch=cfg.get("update_branch","main"),remote=cfg.get("update_remote","origin"),trusted_signers=cfg.get("trusted_signers")))
            print("Restart dset to run the updated version."); return
        try: api=DsetteAPI(config_dir=args.config_dir)
        except TypeError: api=DsetteAPI(config_dir=args.config_dir,passphrase=getpass.getpass("Device key passphrase: "))
        with api:
            if args.cmd in (None,"tui"):
                from .tui import run
                run(api,startup_animation=not args.no_animation); return
            if args.cmd=="push":
                from .clipboard import read_text
                result=api.push_clipboard(read_text())
            elif args.cmd=="pull":
                from .clipboard import write_text
                result=api.latest_clipboard()
                if not result: print("No unexpired clipboard item."); return
                write_text(result["data"]); print("Clipboard received."); return
            elif args.cmd=="wait":
                result=api.wait_clipboard(after=args.after)
                if result and args.copy:
                    from .clipboard import write_text
                    write_text(result["data"])
                if result and not args.copy: print(result["data"])
                elif not result: print("No new clipboard item.")
                return
            elif args.cmd=="watch":
                after=args.after
                print("Watching for clipboard arrivals. Press Ctrl+C to stop.",flush=True)
                while True:
                    result=api.wait_clipboard(after=after)
                    if result:
                        after=max(after,int(result["clip_id"]))
                        if args.copy:
                            from .clipboard import write_text
                            write_text(result["data"])
                        print(result["data"],flush=True)
            elif args.cmd=="ack": result=api.ack_file(args.transfer_id,verified=not args.failed)
            elif args.cmd=="delete": result=api.delete_file(args.transfer_id)
            elif args.cmd=="send": result=api.send_file(args.path)
            elif args.cmd=="receive": print(api.receive_file(args.transfer_id,args.destination)); return
            elif args.cmd=="live-send": result=api.send_live(args.path,announced=lambda tid:print("Live transfer ID:",tid,flush=True))
            elif args.cmd=="live-receive": print(api.receive_live(args.transfer_id,args.destination)); return
            elif args.cmd=="inbox": result=api.inbox()
            elif args.cmd=="live-list": result=api.live_list()
            elif args.cmd=="devices": result=api.devices()
            elif args.cmd=="version": result=api.version()
            elif args.cmd=="update":
                result=api.update(args.ref)
                if args.watch and result.get("job_id"):
                    import time
                    while True:
                        status=api.update_status(result["job_id"])
                        print(status.get("log_tail",status))
                        if status.get("state") in ("complete","failed","rolled_back","rejected"): break
                        time.sleep(2)
            else: return
            print(json.dumps(result,indent=2))
    except KeyboardInterrupt:
        print("Cancelled.")
    except (httpx.HTTPError,OSError,ValueError,RuntimeError,EOFError) as exc:
        raise SystemExit("Datasette: "+str(exc)) from None


if __name__=="__main__": main()
