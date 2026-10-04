"""Спотовые адаптеры: публичные эндпоинты ордербука (без ключей)."""
from __future__ import annotations

from .base import Adapter
from ..config import Leg, Venue
from ..http_client import HttpClient
from ..models import Ladder, Level


def _side_key_pair(side: str) -> str:
    # 'buy' -> беру у тех, кто продаёт (asks); 'sell' -> отдаю тем, кто покупает (bids)
    return "asks" if side == "buy" else "bids"


class BinanceSpot(Adapter):
    name = "binance_spot"
    kind = "spot"
    base_url = "https://api.binance.com"

    async def fetch_ladder(self, http: HttpClient, venue: Venue, leg: Leg) -> Ladder:
        symbol = leg.params["symbol"]
        depth = int(leg.params.get("depth", 100))
        url = f"{venue.endpoint or self.base_url}/api/v3/depth"
        data = await http.request_json("GET", url, params={"symbol": symbol, "limit": depth})
        rows = data[_side_key_pair(leg.side)]
        levels = [Level(float(p), float(q)) for p, q in rows if float(q) > 0]
        return Ladder(side=leg.side, levels=levels, venue=venue.name, raw_count=len(rows))


class BybitSpot(Adapter):
    name = "bybit_spot"
    kind = "spot"
    base_url = "https://api.bybit.com"

    async def fetch_ladder(self, http: HttpClient, venue: Venue, leg: Leg) -> Ladder:
        symbol = leg.params["symbol"]
        depth = int(leg.params.get("depth", 50))
        url = f"{venue.endpoint or self.base_url}/v5/market/orderbook"
        data = await http.request_json(
            "GET", url, params={"category": "spot", "symbol": symbol, "limit": depth}
        )
        result = data["result"]
        rows = result["a"] if leg.side == "buy" else result["b"]
        levels = [Level(float(p), float(q)) for p, q in rows if float(q) > 0]
        return Ladder(side=leg.side, levels=levels, venue=venue.name, raw_count=len(rows))


class OkxSpot(Adapter):
    name = "okx_spot"
    kind = "spot"
    base_url = "https://www.okx.com"

    async def fetch_ladder(self, http: HttpClient, venue: Venue, leg: Leg) -> Ladder:
        inst = leg.params["symbol"]
        depth = int(leg.params.get("depth", 50))
        url = f"{venue.endpoint or self.base_url}/api/v5/market/books"
        data = await http.request_json("GET", url, params={"instId": inst, "sz": depth})
        book = data["data"][0]
        rows = book[_side_key_pair(leg.side)]
        # OKX уровень: [price, size, liquidated_orders, num_orders]
        levels = [Level(float(r[0]), float(r[1])) for r in rows if float(r[1]) > 0]
        return Ladder(side=leg.side, levels=levels, venue=venue.name, raw_count=len(rows))


class MexcSpot(Adapter):
    """MEXC спот. Формат ответа идентичен Binance: depth с asks/bids=[[price,qty]].
    ВАЖНО: у MEXC нет рублёвых пар на споте и нет публичного P2P — только котировки
    к USDT/USDC и т.п. Полезен для межбиржевого USDT-арбитража или как нога цепочки.
    """
    name = "mexc_spot"
    kind = "spot"
    base_url = "https://api.mexc.com"

    async def fetch_ladder(self, http: HttpClient, venue: Venue, leg: Leg) -> Ladder:
        symbol = leg.params["symbol"]
        depth = int(leg.params.get("depth", 100))
        url = f"{venue.endpoint or self.base_url}/api/v3/depth"
        data = await http.request_json("GET", url, params={"symbol": symbol, "limit": depth})
        rows = data[_side_key_pair(leg.side)]
        levels = [Level(float(p), float(q)) for p, q in rows if float(q) > 0]
        return Ladder(side=leg.side, levels=levels, venue=venue.name, raw_count=len(rows))


class ExmoSpot(Adapter):
    """Exmo спот. ВАЖНО: api.exmo.com геоблокирует РФ-пары (отдаёт пусто),
    поэтому базовый хост — api.exmo.me. Пары вида 'USDT_RUB', 'BTC_RUB'.
    order_book.ask/bid = [[price, quantity, amount], ...]; ask по возрастанию,
    bid по убыванию (то есть уже в нужном порядке для лестницы).
    """
    name = "exmo_spot"
    kind = "spot"
    base_url = "https://api.exmo.me"

    async def fetch_ladder(self, http: HttpClient, venue: Venue, leg: Leg) -> Ladder:
        pair = leg.params["symbol"]
        depth = int(leg.params.get("depth", 100))
        url = f"{venue.endpoint or self.base_url}/v1.1/order_book"
        data = await http.request_json("GET", url, params={"pair": pair, "limit": depth})
        book = data.get(pair)
        if not book:
            raise ValueError(f"Exmo: нет данных по паре {pair} (геоблок или неверная пара)")
        # Exmo: ключи в ЕДИНСТВЕННОМ числе — 'ask' (продают мне) / 'bid' (покупают у меня)
        rows = book["ask" if leg.side == "buy" else "bid"]
        # уровень Exmo: [price, quantity, amount]
        levels = [Level(float(r[0]), float(r[1])) for r in rows if float(r[1]) > 0]
        return Ladder(side=leg.side, levels=levels, venue=venue.name, raw_count=len(rows))


class RapiraSpot(Adapter):
    """Rapira — российская биржа со спотом USDT/RUB и др. RUB-парами.

    ВАЖНО: публичный стакан (order-book) у Rapira недоступен — отдаётся только
    ТОП-цена через /open/market/rates (askPrice/bidPrice). Поэтому глубину под
    объём проверить НЕЛЬЗЯ: возвращаем один уровень по топ-цене с «бесконечным»
    объёмом-заглушкой. Расчёт по такому маршруту считается без гарантии объёма —
    маршрут стоит помечать как no-depth (см. флаг no_depth в маршруте).
    Символ в формате 'USDT/RUB' (со слешем).
    """
    name = "rapira_spot"
    kind = "spot"
    base_url = "https://api.rapira.net"
    _NO_DEPTH_QTY = 1e12  # заглушка: глубина неизвестна, не блокируем маршрут

    async def fetch_ladder(self, http: HttpClient, venue: Venue, leg: Leg) -> Ladder:
        symbol = leg.params["symbol"]  # напр. 'USDT/RUB'
        url = f"{venue.endpoint or self.base_url}/open/market/rates"
        data = await http.request_json("GET", url)
        row = next((x for x in (data.get("data") or []) if x.get("symbol") == symbol), None)
        if not row:
            raise ValueError(f"Rapira: пара {symbol} не найдена в rates")
        # buy -> покупаю по ask; sell -> продаю по bid
        price = float(row["askPrice"] if leg.side == "buy" else row["bidPrice"])
        if price <= 0:
            raise ValueError(f"Rapira: нулевая цена по {symbol}")
        # один уровень, объём-заглушка (стакан недоступен)
        levels = [Level(price, self._NO_DEPTH_QTY)]
        return Ladder(side=leg.side, levels=levels, venue=venue.name, raw_count=1, no_depth=True)
