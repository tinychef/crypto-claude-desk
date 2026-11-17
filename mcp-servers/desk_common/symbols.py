"""Symbol normalization: BTC, btc, BTC/USDT, BTC-USDT, BTC/USDT:USDT, BTCUSDT -> BTC."""
import re

_QUOTES = ("USDT", "USDC", "USD", "BUSD", "EUR")


def base_symbol(symbol: str) -> str:
    if not symbol or not isinstance(symbol, str):
        raise ValueError("Symbol must be a non-empty string")
    s = symbol.strip().upper()
    s = s.split(":")[0]
    for sep in ("/", "-", "_"):
        if sep in s:
            s = s.split(sep)[0]
            break
    else:
        for q in _QUOTES:
            if s.endswith(q) and len(s) > len(q):
                s = s[: -len(q)]
                break
    if not re.fullmatch(r"[A-Z0-9]{1,15}", s):
        raise ValueError(f"Invalid symbol '{symbol}'")
    return s


def spot_pair(symbol: str, quote: str = "USDT") -> str:
    return f"{base_symbol(symbol)}/{quote}"


def perp_pair(symbol: str, quote: str = "USDT") -> str:
    return f"{base_symbol(symbol)}/{quote}:{quote}"
