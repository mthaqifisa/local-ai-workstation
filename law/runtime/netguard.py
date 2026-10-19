"""Request guard for the gateway and the dashboard — "trusted network" instead of a login.

There is deliberately no password: this is a personal workstation. What the guard stops is
everyone who is NOT you reaching the services:

  1. client address   only this Mac (loopback) and private networks (home LAN 10/8, 172.16/12,
                      192.168/16, Tailscale 100.64/10, IPv6 ULA / link-local) are served.
                      Override with LAW_TRUSTED_NETS="192.168.100.0/24,100.64.0.0/10" in
                      ~/.law/.env (loopback is always allowed).
  2. Host header      must be an IP, localhost, a dot-less name (mymac), a *.local / *.lan /
                      *.home.arpa / *.internal / *.ts.net name, or one listed in
                      LAW_ALLOWED_HOSTS. Stops DNS-rebinding (a web page re-pointing its own
                      domain at 127.0.0.1 to talk to your services).
  3. Origin header    a browser request coming from another site (Origin ≠ Host) is refused.
                      Stops CSRF: a web page you visit can no longer POST /api/task (which runs
                      an agent with shell access) or read your chats through the gateway.
                      Non-browser clients (curl, OpenCode, Open WebUI's backend) send no Origin.
  4. JSON only        state-changing dashboard requests must be Content-Type: application/json,
                      which a cross-site HTML form cannot send without a CORS preflight.
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

DEFAULT_NETS = ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                "100.64.0.0/10", "169.254.0.0/16", "fc00::/7", "fe80::/10")
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".ts.net", ".localhost")


def _setting(key: str) -> str:
    v = (os.environ.get(key) or "").strip()
    if v:
        return v
    try:
        from ..paths import get_paths
        from ..util import read_env
        return (read_env(get_paths().env_file).get(key) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


class Guard:
    def __init__(self, trusted_nets: str | None = None, allowed_hosts: str | None = None):
        nets = trusted_nets if trusted_nets is not None else _setting("LAW_TRUSTED_NETS")
        raw = [n.strip() for n in nets.split(",") if n.strip()] if nets else list(DEFAULT_NETS)
        raw += ["127.0.0.0/8", "::1/128"]
        self.nets = [ipaddress.ip_network(n, strict=False) for n in raw]
        hosts = allowed_hosts if allowed_hosts is not None else _setting("LAW_ALLOWED_HOSTS")
        self.hosts = {h.strip().lower() for h in hosts.split(",") if h.strip()}

    # ── individual checks ───────────────────────────────────────────────
    def client_ok(self, ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip.split("%")[0])
        except ValueError:
            return False
        if getattr(a, "ipv4_mapped", None):
            a = a.ipv4_mapped
        return any(a in n for n in self.nets)

    def host_ok(self, host: str | None) -> bool:
        if not host:
            return True                                   # HTTP/1.0 clients, curl --http1.0
        h = _hostname(host)
        if not h or h in self.hosts or h == "localhost" or "." not in h:
            return True
        try:
            ipaddress.ip_address(h)
            return True
        except ValueError:
            pass
        return h.endswith(LOCAL_SUFFIXES)

    def origin_ok(self, origin: str | None, host: str | None) -> bool:
        if not origin:
            return True
        if origin == "null":
            return False                                  # sandboxed iframe / file:// page
        o = _hostname(urlsplit(origin).netloc)
        if not o:
            return False
        if host and o == _hostname(host):
            return True
        return o in self.hosts or o in ("localhost", "127.0.0.1", "::1")

    # ── one call per request ────────────────────────────────────────────
    def check(self, client_ip: str, headers, *, write: bool, json_only: bool = False) -> str | None:
        """None = allowed, else the reason it was refused."""
        if not self.client_ok(client_ip):
            return f"client {client_ip} is not on a trusted network (set LAW_TRUSTED_NETS)"
        host = headers.get("Host")
        if not self.host_ok(host):
            return f"Host '{host}' is not allowed (add it to LAW_ALLOWED_HOSTS)"
        if not self.origin_ok(headers.get("Origin"), host):
            return f"cross-site request from Origin '{headers.get('Origin')}' refused"
        if write and json_only:
            ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return "requests that change something must be Content-Type: application/json"
        return None


def _hostname(netloc: str) -> str:
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):                            # [::1]:8800
        return netloc[1:netloc.find("]")] if "]" in netloc else netloc
    if netloc.count(":") == 1:
        netloc = netloc.split(":")[0]
    return netloc.rstrip(".")
