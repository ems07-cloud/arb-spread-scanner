"""Совместимость сетей перевода между площадками.

Маршрут с переводом монеты исполним, ТОЛЬКО если сеть вывода у источника совпадает с
сетью, которую принимает получатель. Пример: купил USDT на Exmo, выводишь TRC20 — Bybit
обязан принимать USDT по TRC20. Если сети не пересекаются, окно «бумажное», даже если
спред отличный.

Публичного API «какие сети принимает площадка» у P2P-витрин нет, поэтому держим
встроенную таблицу для активных площадок (ключ — имя адаптера) с возможностью
переопределить через `networks` в конфиге площадки. Если данных нет — возвращаем
«не подтверждено» (None): маршрут не блокируем, но помечаем мягким риском.
"""
from __future__ import annotations

# Канонизация псевдонимов сетей к единому токену.
_ALIAS = {
    "TRC20": "TRX", "TRON": "TRX", "TRX": "TRX",
    "BEP20": "BSC", "BSC": "BSC", "BEP2": "BSC",
    "SOL": "SOL", "SOLANA": "SOL",
    "TON": "TON",
    "ERC20": "ERC20",
    "BTC": "BTC", "BITCOIN": "BTC",
    "ETH": "ETH", "ETHEREUM": "ETH",
}


def canon(net) -> str | None:
    if not net:
        return None
    n = str(net).strip().upper()
    return _ALIAS.get(n, n)


# Встроенная таблица: адаптер -> монета -> множество поддерживаемых сетей (канонично).
# Покрывает активные площадки для USDT (основной кросс). Дополняй по мере надобности.
_DEFAULTS: dict[str, dict[str, set[str]]] = {
    "exmo_spot":  {"USDT": {"TRX", "ERC20"}, "USDC": {"TRX", "ERC20"}},
    "rapira_spot": {"USDT": {"TRX", "ERC20"}},
    "bybit_spot": {"USDT": {"TRX", "ERC20", "BSC", "SOL"}, "USDC": {"TRX", "ERC20", "SOL"}},
    "bybit_p2p":  {"USDT": {"TRX", "ERC20", "BSC", "SOL"}, "USDC": {"TRX", "ERC20", "SOL"}},
    "htx_p2p":    {"USDT": {"TRX", "ERC20"}, "USDC": {"TRX", "ERC20"}},
}


def _from_config(networks, coin: str) -> set[str] | None:
    """Парсит venue.networks (список или {coin: [...]}) в множество канон-сетей."""
    if networks is None:
        return None
    if isinstance(networks, dict):
        lst = networks.get(coin)
        if lst is None:
            return None
    else:
        lst = networks
    if isinstance(lst, str):
        lst = [lst]
    out = {canon(x) for x in lst if canon(x)}
    return out or None


def supported(venue, coin: str) -> set[str] | None:
    """Сети, которыми площадка может слать/принимать монету. None = неизвестно."""
    cfg = _from_config(getattr(venue, "networks", None), coin)
    if cfg is not None:
        return cfg
    return _DEFAULTS.get(getattr(venue, "adapter", ""), {}).get(coin)


def route_network_ok(buy_venue, sell_venue, coin: str, network) -> tuple[bool | None, str]:
    """Совместима ли сеть перевода coin с buy_venue (вывод) и sell_venue (приём).

    Возврат: (True ок | False неисполнимо | None не подтверждено, причина).
    """
    net = canon(network)
    if net is None:
        return None, "сеть перевода не задана"
    s_out = supported(buy_venue, coin)
    s_in = supported(sell_venue, coin)
    if s_out is not None and net not in s_out:
        return False, f"{buy_venue.name} не выводит {coin} по сети {net}"
    if s_in is not None and net not in s_in:
        return False, f"{sell_venue.name} не принимает {coin} по сети {net}"
    if s_out is None or s_in is None:
        return None, f"приём {coin} по сети {net} не подтверждён"
    return True, ""
