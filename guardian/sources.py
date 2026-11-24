"""Reuse the desktop project's VLESS and subscription parsing, with URL checks."""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from legacy.src.sources import Target, ports_from_spec, subscription_links
from legacy.src.vless import outbound_from_url

MAX_SUBSCRIPTION_BYTES = 4_000_000
MAX_TARGETS = 1000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Subscription redirects are disabled; use its final HTTPS URL.")


def validate_subscription_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Subscription must be an HTTP(S) URL.")
    if parsed.username or parsed.password:
        raise ValueError("Userinfo in subscription URL is not allowed.")
    if len(url) > 2048 or parsed.port not in (None, 80, 443, 8080, 8443):
        raise ValueError("URL too long or unsupported subscription port.")
    # Defense-in-depth: block local, private, metadata, link-local destinations.
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or
                                       (443 if parsed.scheme == "https" else 80),
                                       type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Subscription host could not be resolved.") from exc
    if not addresses or any(
        not ipaddress.ip_address(addr[4][0]).is_global for addr in addresses
    ):
        raise ValueError("Subscription must resolve only to public IP addresses.")
    return url


def fetch_subscription(url: str) -> str:
    validate_subscription_url(url)
    opener = build_opener(NoRedirect)
    req = Request(url, headers={"User-Agent": "XrayGeminiGuardian/0.1", "Accept": "text/plain"})
    try:
        with opener.open(req, timeout=20) as response:
            size = response.headers.get("Content-Length")
            if size and int(size) > MAX_SUBSCRIPTION_BYTES:
                raise ValueError("Subscription is too large.")
            content = response.read(MAX_SUBSCRIPTION_BYTES + 1)
    except HTTPError as exc:
        raise ValueError(f"Subscription returned HTTP {exc.code}.") from exc
    if len(content) > MAX_SUBSCRIPTION_BYTES:
        raise ValueError("Subscription is too large.")
    return content.decode("utf-8-sig")


def parse_subscription(text: str) -> tuple[list[Target], int]:
    links = subscription_links(text)
    targets = []
    skipped = 0
    for idx, uri in enumerate(links, 1):
        if not uri.lower().startswith("vless://"):
            skipped += 1
            continue
        try:
            outbound_from_url(uri)
            parsed = urlparse(uri)
            if not parsed.port:
                raise ValueError("missing port")
            from urllib.parse import unquote
            name = unquote(parsed.fragment or f"Node_{idx}")[:80]
            # VLESS port is per-node, not a port range override.
            targets.append(Target(name, uri, parsed.port))
        except (ValueError, TypeError):
            skipped += 1
        if len(targets) > MAX_TARGETS:
            raise ValueError("Too many targets; maximum 1000.")
    return targets, skipped


def targets_for_run(store):
    mode = store.get("MODE")
    if mode == "subscription":
        targets = store.cached_targets()
        if not targets:
            raise ValueError("سابسکریپشن خالی است؛ ابتدا از منوی منابع Refresh بزنید.")
        return targets[:MAX_TARGETS]
    if mode != "vless":
        raise ValueError("Unknown mode.")
    uri = store.get("VLESS_URL").strip()
    if not uri.startswith("vless://"):
        raise ValueError("لینک VLESS ثبت نشده است.")
    outbound_from_url(uri)
    ports = [p for p, enabled in store.list_ports() if enabled]
    if not ports:
        raise ValueError("هیچ پورت فعالی در فهرست نیست.")
    return [Target(f"VLESS_{port}", uri, port) for port in ports[:MAX_TARGETS]]


def safe_source_label(url: str) -> str:
    parsed = urlparse(url)
    hostname = parsed.hostname or "?"
    path = re.sub(r"[^a-zA-Z0-9_/.-]", "", parsed.path)[:20]
    return hostname + (path if len(path) < 15 else "/…")
