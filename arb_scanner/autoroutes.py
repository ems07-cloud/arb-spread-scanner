"""Авто-генерация маршрутов из компактного описания (монеты × площадки).

Вместо ручного перечисления каждой связки задаём в конфиге блок `auto`: для каждой
монеты — на каких площадках она торгуется и под каким символом. Генератор сам
строит все 2-ногие связки нужных типов:
  - spot_to_p2p  — купить на споте, продать в P2P (и наоборот);
  - p2p_to_p2p   — между двумя P2P-площадками;
  - spot_to_spot — между двумя спот-биржами.
Так ничего не упускается, а при добавлении площадки маршруты создаются сами.

Формат `auto` (пример):
  auto:
    enabled: true
    quote: RUB
    types: [spot_to_p2p, p2p_to_p2p, spot_to_spot]
    filters: { min_completion: 0.9, min_orders: 50, online_only: true, rows: 20 }
    coins:
      USDT:
        size: 1000
        network: { coin: 1.0, net: TRX }
        venues:
          exmo_spot: USDT_RUB
          rapira_spot: "USDT/RUB"
          bybit_p2p_rub: { asset: USDT, fiat: RUB }
          htx_p2p_rub: { coin_id: 2, currency_id: 11 }
"""
from __future__ import annotations

from .config import Leg, Route, Venue

_DEFAULT_TYPES = ("spot_to_p2p", "p2p_to_p2p", "spot_to_spot")


def _leg_params(venue: Venue, spec, filters: dict) -> dict:
    """Параметры ноги для площадки из её spec (строка-символ для спота, dict для P2P)."""
    if venue.kind == "spot":
        sym = spec if isinstance(spec, str) else spec.get("symbol")
        p = {"symbol": sym}
        if isinstance(spec, dict) and spec.get("depth"):
            p["depth"] = spec["depth"]
        else:
            p["depth"] = 100
        return p
    # p2p: spec — dict с asset/fiat (bybit) или coin_id/currency_id (htx) + фильтры
    p = dict(spec) if isinstance(spec, dict) else {}
    for k in ("min_completion", "min_orders", "online_only", "rows", "pay_methods"):
        if k in filters and k not in p:
            p[k] = filters[k]
    return p


def generate(auto: dict, venues: dict[str, Venue], existing_names: set[str]) -> list[Route]:
    if not auto or not auto.get("enabled"):
        return []
    quote = str(auto.get("quote", "RUB"))
    types = set(auto.get("types") or _DEFAULT_TYPES)
    filters = auto.get("filters") or {}
    coins = auto.get("coins") or {}
    g_poll = auto.get("poll_interval")
    g_poll = float(g_poll) if g_poll is not None else None

    routes: list[Route] = []
    for coin, cdef in coins.items():
        vmap = (cdef or {}).get("venues") or {}
        size = float((cdef or {}).get("size", 0) or 0)
        net = (cdef or {}).get("network") or {}
        net_coin = float(net.get("coin", 0.0))
        net_name = net.get("net")
        c_max = (cdef or {}).get("max_size")
        c_max = float(c_max) if c_max is not None else None
        c_poll = (cdef or {}).get("poll_interval")
        c_poll = float(c_poll) if c_poll is not None else g_poll
        if size <= 0 or len(vmap) < 2:
            continue

        # классифицируем площадки монеты
        spots = [v for v in vmap if venues.get(v) and venues[v].kind == "spot"]
        p2ps = [v for v in vmap if venues.get(v) and venues[v].kind == "p2p"]

        def mk(buy_v: str, sell_v: str, cross: bool) -> Route | None:
            name = (f"[auto] {coin} {buy_v} (внутри P2P)" if buy_v == sell_v
                    else f"[auto] {coin} {buy_v}->{sell_v}")
            if name in existing_names:
                return None
            existing_names.add(name)
            buy = Leg(buy_v, "buy", _leg_params(venues[buy_v], vmap[buy_v], filters))
            sell = Leg(sell_v, "sell", _leg_params(venues[sell_v], vmap[sell_v], filters))
            return Route(
                name=name, base=coin, quote=quote, size_base=size,
                buy=buy, sell=sell,
                network_fee_coin=net_coin if cross else 0.0,
                network=net_name if cross else None,
                poll_interval=c_poll, max_size=c_max,
            )

        pairs: list[tuple[str, str, bool]] = []
        if "spot_to_p2p" in types:
            for sp in spots:
                for pp in p2ps:
                    pairs.append((sp, pp, True))   # spot -> p2p
                    pairs.append((pp, sp, True))   # p2p -> spot
        if "p2p_to_p2p" in types:
            for i, a in enumerate(p2ps):
                for b in p2ps[i + 1:]:
                    pairs.append((a, b, True))
                    pairs.append((b, a, True))
        if "spot_to_spot" in types:
            for i, a in enumerate(spots):
                for b in spots[i + 1:]:
                    pairs.append((a, b, True))
                    pairs.append((b, a, True))
        if "p2p_internal" in types:
            # купить у дешёвого продавца и продать дорогому покупателю в P2P ОДНОЙ биржи
            # (без перевода, cross=False): ловит инвертированный стакан P2P.
            for pp in p2ps:
                pairs.append((pp, pp, False))

        for buy_v, sell_v, cross in pairs:
            r = mk(buy_v, sell_v, cross)
            if r is not None:
                routes.append(r)
    return routes
