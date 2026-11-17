"""IP whitelist for the API.

Configuration (environment):
  SPSE_ALLOWED_IPS       comma/space separated IPs or CIDRs. Empty or unset =
                         no whitelist (every IP allowed).
  SPSE_PROXY_HOPS        reverse proxies in front of the app that append to
                         X-Forwarded-For (default 1). The client is the entry
                         that many places from the right; entries further left
                         were sent by the client and can be forged. Use 0 when
                         the app is reached directly, without a proxy.
  SPSE_CLIENT_IP_HEADER  optional header set by a trusted proxy that holds the
                         client IP on its own (e.g. CF-Connecting-IP,
                         X-Real-IP). Takes precedence over X-Forwarded-For.
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# Reachable from anywhere: health checks, the caller's own IP for debugging,
# and the SPSE link bounce page (clicked from Excel/chat by people whose IP is
# not on the list; it can only send the browser to spse.inaproc.id).
PUBLIC_PATHS = frozenset({"/", "/health", "/whoami"})
PUBLIC_PREFIXES = ("/spse/",)


def parse_networks(value: str | None) -> list[Network]:
    """'1.2.3.4, 10.0.0.0/8' -> networks. Raises ValueError on a bad entry so a
    typo in the env fails at startup instead of silently locking people out."""
    networks = []
    for item in re.split(r"[,\s]+", value or ""):
        if item:
            try:
                networks.append(ipaddress.ip_network(item, strict=False))
            except ValueError as err:
                raise ValueError(f"SPSE_ALLOWED_IPS: entri tidak valid '{item}'") from err
    return networks


@dataclass
class AccessPolicy:
    networks: list[Network]
    proxy_hops: int = 1
    client_ip_header: str | None = None

    @classmethod
    def from_env(cls, allowed_ips: str | None = None, proxy_hops: int | None = None,
                 client_ip_header: str | None = None) -> "AccessPolicy":
        allowed = allowed_ips if allowed_ips is not None else os.environ.get("SPSE_ALLOWED_IPS")
        hops = proxy_hops if proxy_hops is not None else int(os.environ.get("SPSE_PROXY_HOPS", "1"))
        header = client_ip_header if client_ip_header is not None \
            else os.environ.get("SPSE_CLIENT_IP_HEADER")
        return cls(parse_networks(allowed), max(hops, 0), (header or None))

    @property
    def enabled(self) -> bool:
        return bool(self.networks)

    def client_ip(self, peer: str | None, headers) -> str:
        if self.client_ip_header:
            value = (headers.get(self.client_ip_header) or "").split(",")[0].strip()
            if value:
                return value
        if self.proxy_hops:
            forwarded = [p.strip() for p in (headers.get("x-forwarded-for") or "").split(",")
                         if p.strip()]
            if forwarded:
                return forwarded[-min(self.proxy_hops, len(forwarded))]
        return peer or ""

    def allows(self, ip: str) -> bool:
        if not self.enabled:
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        return any(addr in net for net in self.networks)

    @staticmethod
    def is_public(path: str) -> bool:
        return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)

    def describe(self) -> str:
        if not self.enabled:
            return "whitelist IP nonaktif (SPSE_ALLOWED_IPS kosong): semua IP diizinkan"
        return (f"whitelist IP aktif: {', '.join(str(n) for n in self.networks)} "
                f"(proxy_hops={self.proxy_hops}"
                f"{', header=' + self.client_ip_header if self.client_ip_header else ''})")
