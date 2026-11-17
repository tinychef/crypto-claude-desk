"""Optional API keys.

Keys are read from environment variables ``DESK_<PROVIDER>_KEY`` (e.g.
``DESK_COINALYZE_KEY``). In Claude Code cloud sessions, prefer network secrets
(the proxy injects the header), in which case no env var is needed: providers
that accept header auth are called anyway and the proxy adds the credential.
"""
import os
from typing import Optional


def get_key(provider: str) -> Optional[str]:
    provider = provider.upper().replace("-", "_")
    for name in (f"DESK_{provider}_KEY", f"{provider}_API_KEY", f"{provider}_KEY"):
        val = os.environ.get(name)
        if val:
            return val.strip()
    return None


def unavailable(provider: str, reason: str = "missing API key", **extra) -> dict:
    """Uniform 'data source not available' result — agents treat it as a data gap."""
    out = {
        "status": "unavailable",
        "provider": provider,
        "reason": reason,
        "hint": f"Set DESK_{provider.upper().replace('-', '_')}_KEY or add a network secret for this host.",
    }
    out.update(extra)
    return out
