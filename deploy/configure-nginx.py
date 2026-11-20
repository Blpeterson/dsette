#!/usr/bin/env python3
"""Install one isolated name-based nginx site; leave all other sites intact."""
import argparse
import os
import re
import shutil
import subprocess
from pathlib import Path


def run(*args):
    subprocess.run(args,check=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("domain")
    args=parser.parse_args()
    domain=args.domain.lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,249}[a-z0-9])?",domain) or "." not in domain or ".." in domain:
        parser.error("expected a DNS hostname")
    if os.geteuid()!=0: parser.error("run with sudo")
    site=Path("/etc/nginx/sites-available/dsette")
    enabled=Path("/etc/nginx/sites-enabled/dsette")
    old=site.read_text() if site.exists() else None
    if old is not None and not old.startswith("# Managed by dsette"):
        raise SystemExit("Refusing to replace an unmanaged nginx site")
    root=Path("/var/lib/dsette-acme")
    root.mkdir(mode=0o755,exist_ok=True)
    acme=f"location ^~ /.well-known/acme-challenge/ {{ root {root}; }}"
    http=f"""# Managed by dsette — independent virtual host
server {{
    listen 80;
    listen [::]:80;
    server_name {domain};
    access_log off;
    {acme}
    location / {{ return 308 https://{domain}$request_uri; }}
}}
"""
    try:
        site.write_text(http)
        if not enabled.exists(): enabled.symlink_to(site)
        run("nginx","-t")
        run("systemctl","reload","nginx")
        run("certbot","certonly","--webroot","-w",str(root),"-d",domain,"--cert-name",domain,
            "--non-interactive","--agree-tos","--register-unsafely-without-email","--keep-until-expiring",
            "--deploy-hook","nginx -t && systemctl reload nginx")
        trust=Path("/etc/dsette")
        trust.mkdir(mode=0o755,exist_ok=True)
        shutil.copyfile("/var/lib/dsette/tls/server.crt",trust/"upstream.crt")
        (trust/"upstream.crt").chmod(0o644)
        https=f"""
server {{
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {domain};
    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    access_log off;
    error_log /var/log/nginx/dsette-error.log warn;
    client_max_body_size 2100m;
    add_header X-Content-Type-Options nosniff always;
    location = / {{
        default_type text/plain;
        return 200 "Datasette - your little signal station. Connect with dset enroll https://{domain}\\n";
    }}
    location /api/ {{
        proxy_pass https://127.0.0.1:8223;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/dsette/upstream.crt;
        proxy_ssl_server_name on;
        proxy_ssl_name localhost;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_read_timeout 150s;
        proxy_send_timeout 150s;
        add_header X-Accel-Buffering no;
    }}
    location / {{ return 404; }}
}}
"""
        site.write_text(http+https)
        run("nginx","-t")
        run("systemctl","reload","nginx")
        print("HTTPS configured:","https://"+domain)
    except BaseException:
        if old is not None: site.write_text(old)
        else:
            enabled.unlink(missing_ok=True)
            site.unlink(missing_ok=True)
        run("nginx","-t")
        run("systemctl","reload","nginx")
        raise


if __name__=="__main__": main()
