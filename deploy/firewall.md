# Network setup

The application binds only to `127.0.0.1:8223`. Expose HTTPS through Caddy on port 443; do not expose 8223 publicly. In the Oracle VCN ingress rules allow TCP 443 from the desired source CIDRs. On the VM, after inspecting the active firewall, allow the same CIDRs to TCP 443. Do not run firewall commands blindly: Oracle Ubuntu images may use iptables rules that are easy to disrupt.

