"""P2P-адаптеры: публичные витрины объявлений (без ключей).

Объявления превращаем в «лестницу» уровней так же, как ордербук:
- нога 'buy' (я покупаю базу) -> объявления, где рекламодатель ПРОДАЁТ;
- нога 'sell' (я продаю базу) -> объявления, где рекламодатель ПОКУПАЕТ.

Объём уровня = доступный остаток объявления, ограниченный максимальным лимитом
сделки (maxAmount/цена). Минимальный лимит сделки пишем в Level.min_qty, чтобы
расчёт не «набирал» с оффера меньше, чем он разрешает. Дополнительно фильтруем
ненадёжных мерчантов (% исполнения, число сделок, онлайн).
"""
from __future__ import annotations

import logging

from .base import Adapter
from ..config import Leg, Venue
from ..http_client import HttpClient
from ..links import bybit_p2p_offer, htx_p2p_offer
from ..models import Ladder, Level

log = logging.getLogger("arb.p2p")


class _Filter:
    """Пороги надёжности мерчанта из leg.params (любой не задан -> не фильтруем)."""
    __slots__ = ("min_completion", "min_orders", "online_only")

    def __init__(self, params: dict):
        mc = params.get("min_completion")          # доля 0..1 (напр. 0.95)
        self.min_completion = float(mc) if mc is not None else None
        mo = params.get("min_orders")              # минимум сделок
        self.min_orders = int(mo) if mo is not None else None
        self.online_only = bool(params.get("online_only", False))

    def ok(self, *, completion: float | None, orders: int | None, online: bool | None) -> bool:
        if self.min_completion is not None and completion is not None and completion < self.min_completion:
            return False
        if self.min_orders is not None and orders is not None and orders < self.min_orders:
            return False
        if self.online_only and online is False:
            return False
        return True


def _norm_pay(spec) -> set[str] | None:
    """Нормализует список разрешённых способов оплаты в множество lower-строк."""
    if not spec:
        return None
    if isinstance(spec, (str, int)):
        spec = [spec]
    return {str(x).strip().lower() for x in spec if str(x).strip()}


def _pay_ok(ref: dict | None, allowed: set[str] | None) -> bool:
    """Оффер подходит, если его способы оплаты пересекаются с разрешёнными."""
    if not allowed:
        return True
    toks = (ref or {}).get("pay_tokens") or set()
    return bool(toks & allowed)


def _finish(levels: list[Level], side: str, venue: str, raw: int) -> Ladder:
    # buy -> дешевле сверху (asc); sell -> дороже сверху (desc)
    levels.sort(key=lambda lv: lv.price, reverse=(side == "sell"))
    return Ladder(side=side, levels=levels, venue=venue, raw_count=raw)


class BinanceP2P(Adapter):
    name = "binance_p2p"
    kind = "p2p"
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

    async def fetch_ladder(self, http: HttpClient, venue: Venue, leg: Leg) -> Ladder:
        p = leg.params
        flt = _Filter(p)
        trade_type = "SELL" if leg.side == "buy" else "BUY"
        body = {
            "page": 1,
            "rows": int(p.get("rows", 20)),
            "asset": p["asset"],
            "fiat": p["fiat"],
            "tradeType": trade_type,
            "payTypes": p.get("pay_types", []) or [],
        }
        if p.get("trans_amount") is not None:
            body["transAmount"] = str(p["trans_amount"])
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; arb-scanner/0.1; read-only)",
        }
        endpoint = venue.endpoint or self.url
        data = await http.request_json("POST", endpoint, headers=headers, json_body=body)

        rows = data.get("data") or []
        respect_limits = bool(p.get("respect_limits", True))
        levels: list[Level] = []
        for item in rows:
            adv = item.get("adv") or {}
            advr = item.get("advertiser") or {}
            try:
                price = float(adv["price"])
            except (KeyError, TypeError, ValueError):
                continue
            # фильтр мерчанта
            comp = advr.get("monthFinishRate")        # уже доля 0..1
            orders = advr.get("monthOrderCount")
            if not flt.ok(completion=float(comp) if comp is not None else None,
                          orders=int(orders) if orders is not None else None,
                          online=None):
                continue

            qty = float(adv.get("surplusAmount") or adv.get("tradableQuantity") or 0.0)
            min_qty = 0.0
            if respect_limits and price > 0:
                max_fiat = adv.get("dynamicMaxSingleTransAmount") or adv.get("maxSingleTransAmount")
                if max_fiat:
                    qty = min(qty, float(max_fiat) / price)
                min_fiat = adv.get("minSingleTransAmount")
                if min_fiat:
                    min_qty = float(min_fiat) / price
            if qty > 0 and qty >= min_qty:
                levels.append(Level(price, qty, min_qty))

        return _finish(levels, leg.side, venue.name, len(rows))


class BybitP2P(Adapter):
    """Bybit fiat-OTC (P2P). Публичная витрина объявлений, без ключей.

    side в API Bybit = МОЁ действие: '0' = я покупаю базу (ads-продавцы, ask),
    '1' = я продаю базу (ads-покупатели, bid). lastQuantity — доступный остаток
    базы; maxAmount/minAmount — лимиты сделки в фиате; recentExecuteRate — %
    исполнения, recentOrderNum — число последних сделок, isOnline — онлайн.
    """
    name = "bybit_p2p"
    kind = "p2p"
    url = "https://api2.bybit.com/fiat/otc/item/online"

    async def fetch_ladder(self, http: HttpClient, venue: Venue, leg: Leg) -> Ladder:
        p = leg.params
        flt = _Filter(p)
        allowed_pay = _norm_pay(p.get("pay_methods"))
        bybit_side = "0" if leg.side == "buy" else "1"
        body = {
            "tokenId": p["asset"],
            "currencyId": p["fiat"],
            "payment": [str(x) for x in (p.get("pay_ids") or [])],
            "side": bybit_side,
            "size": str(int(p.get("rows", 20))),
            "page": "1",
            "amount": str(p["amount"]) if p.get("amount") is not None else "",
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; arb-scanner/0.1; read-only)",
        }
        endpoint = venue.endpoint or self.url
        data = await http.request_json("POST", endpoint, headers=headers, json_body=body)

        items = ((data.get("result") or {}).get("items")) or []
        respect_limits = bool(p.get("respect_limits", True))
        levels: list[Level] = []
        for it in items:
            try:
                price = float(it["price"])
            except (KeyError, TypeError, ValueError):
                continue
            # фильтр мерчанта: recentExecuteRate в процентах (0..100) -> доля
            rate = it.get("recentExecuteRate")
            comp = float(rate) / 100.0 if rate is not None else None
            orders = it.get("recentOrderNum")
            if not flt.ok(completion=comp,
                          orders=int(orders) if orders is not None else None,
                          online=it.get("isOnline")):
                continue

            qty = float(it.get("lastQuantity") or it.get("quantity") or 0.0)
            min_qty = 0.0
            if respect_limits and price > 0:
                if it.get("maxAmount"):
                    qty = min(qty, float(it["maxAmount"]) / price)
                if it.get("minAmount"):
                    min_qty = float(it["minAmount"]) / price
            ref = bybit_p2p_offer(it)
            if not _pay_ok(ref, allowed_pay):
                continue
            if qty > 0 and qty >= min_qty:
                levels.append(Level(price, qty, min_qty, ref=ref))

        return _finish(levels, leg.side, venue.name, len(items))


class HtxP2P(Adapter):
    """HTX (бывш. Huobi) OTC/P2P. Публичная витрина, без ключей.

    ВАЖНО: основной домен htx.com за Cloudflare (403). Рабочий бэкенд —
    otc-akm.huobi.com. tradeType = МОЁ действие: 'buy' (объявления продавцов, ask),
    'sell' (объявления покупателей, bid). coinId/currency — числовые ID
    (USDT=2, RUB=11). Объём берём из лимитов сделки в фиате (min/maxTradeLimit),
    т.к. остаток рекламы в публичном ответе не отдаётся.
    """
    name = "htx_p2p"
    kind = "p2p"
    url = "https://otc-akm.huobi.com/v1/data/trade-market"

    async def fetch_ladder(self, http: HttpClient, venue: Venue, leg: Leg) -> Ladder:
        p = leg.params
        flt = _Filter(p)
        allowed_pay = _norm_pay(p.get("pay_methods"))
        params = {
            "coinId": int(p.get("coin_id", 2)),       # USDT
            "currency": int(p.get("currency_id", 11)),  # RUB
            "tradeType": "buy" if leg.side == "buy" else "sell",
            "currPage": 1,
            "payMethod": p.get("pay_method", 0),       # 0 = все способы
            "blockType": "general",
            "online": 1,
            "range": 0,
        }
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; arb-scanner/0.1; read-only)",
        }
        endpoint = venue.endpoint or self.url
        data = await http.request_json("GET", endpoint, headers=headers, params=params)

        rows = data.get("data") or []
        respect_limits = bool(p.get("respect_limits", True))
        levels: list[Level] = []
        for it in rows:
            try:
                price = float(it["price"])
            except (KeyError, TypeError, ValueError):
                continue
            # фильтр мерчанта: orderCompleteRate в процентах (строка) -> доля
            rate = it.get("orderCompleteRate")
            comp = float(rate) / 100.0 if rate not in (None, "") else None
            orders = it.get("tradeMonthTimes")
            if not flt.ok(completion=comp,
                          orders=int(orders) if orders is not None else None,
                          online=it.get("isOnline")):
                continue

            # объём = лимиты сделки в фиате / цена
            qty = 0.0
            min_qty = 0.0
            if price > 0:
                if it.get("maxTradeLimit"):
                    qty = float(it["maxTradeLimit"]) / price
                if respect_limits and it.get("minTradeLimit"):
                    min_qty = float(it["minTradeLimit"]) / price
            ref = htx_p2p_offer(it)
            if not _pay_ok(ref, allowed_pay):
                continue
            if qty > 0 and qty >= min_qty:
                levels.append(Level(price, qty, min_qty, ref=ref))

        return _finish(levels, leg.side, venue.name, len(rows))
