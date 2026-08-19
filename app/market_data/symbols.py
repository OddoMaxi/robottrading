"""Symbol translation between each exchange's native format and the common "BASE/QUOTE" form."""

# Longest first so e.g. "FDUSD" is matched before a hypothetical shorter overlap.
_QUOTE_ASSETS_BY_LENGTH = sorted(["USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH"], key=len, reverse=True)


def to_common_symbol(exchange: str, native_symbol: str) -> str:
    """Convert an exchange-native symbol to "BASE/QUOTE"."""
    if exchange == "okx":
        base, quote = native_symbol.split("-")[:2]
        return f"{base}/{quote}"

    # binance, bybit: concatenated form (e.g. "BTCUSDT") — split on known quote assets.
    for quote in _QUOTE_ASSETS_BY_LENGTH:
        if native_symbol.endswith(quote) and len(native_symbol) > len(quote):
            return f"{native_symbol[: -len(quote)]}/{quote}"
    raise ValueError(f"Could not split native symbol {native_symbol!r} for exchange={exchange!r}")


def to_native_symbol(exchange: str, common_symbol: str) -> str:
    """Convert "BTC/USDT" to the exchange-native spot symbol format."""
    base, quote = common_symbol.split("/")
    match exchange:
        case "binance" | "bybit":
            return f"{base}{quote}"
        case "okx":
            return f"{base}-{quote}"
        case _:
            raise NotImplementedError(f"No symbol mapping registered for exchange={exchange!r}")


def to_native_perp_symbol(exchange: str, common_symbol: str) -> str:
    """Convert "BTC/USDT" to the exchange-native perpetual instrument format."""
    base, quote = common_symbol.split("/")
    match exchange:
        case "binance" | "bybit":
            return f"{base}{quote}"
        case "okx":
            return f"{base}-{quote}-SWAP"
        case _:
            raise NotImplementedError(f"No perpetual symbol mapping registered for exchange={exchange!r}")
