"""simply.com WAF proof-of-work solver.

The host serves a JS interstitial that asks the browser to find a nonce whose
SHA-256 has >= D leading zero *bits*, then POST it to /.sc-verify/ for a
clearance cookie. This is the same work every visitor's browser does; we do it
in pure Python so the runtime needs no browser.
"""

from __future__ import annotations

import hashlib
import re

from . import config


def leading_zero_bits(hex_digest: str) -> int:
    """Count leading zero bits of a hex string (matches the site's lz())."""
    bits = 0
    for ch in hex_digest:
        n = int(ch, 16)
        if n == 0:
            bits += 4
            continue
        if n < 2:
            bits += 3
        elif n < 4:
            bits += 2
        elif n < 8:
            bits += 1
        break
    return bits


def solve(token: str, difficulty: int, max_iter: int = 5_000_000) -> int:
    """Return the smallest nonce with >= difficulty leading zero bits."""
    n = 0
    while n < max_iter:
        h = hashlib.sha256(f"{token}:{n}".encode()).hexdigest()
        if leading_zero_bits(h) >= difficulty:
            return n
        n += 1
    raise RuntimeError(f"WAF PoW not solved within {max_iter} iterations")


def parse_challenge(html: str) -> tuple[str, str, int] | None:
    """Extract (token, ts, difficulty) from a challenge page, or None."""
    m = re.search(config.WAF_PARAM_RE, html)
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def is_challenge(html: str) -> bool:
    return any(marker in html for marker in config.WAF_CHALLENGE_MARKERS)
