"""Живые комиссии с бирж по READ-ONLY ключам.

Тянем ровно две вещи, которые нельзя знать точно «на глаз»:
  * твою настоящую торговую комиссию maker/taker (зависит от VIP/скидок);
  * текущую сетевую комиссию за вывод монеты (плавает во времени).

Все запросы — только чтение (account/asset info). Ключи берутся из переменных
окружения по именам из конфига; сами ключи в файлах не хранятся. Если ключей нет
или запрос упал — молча откатываемся на значения из config.yaml (скрипт не падает).

ВАЖНО: ключи должны быть выпущены БЕЗ прав на торговлю и вывод (read-only).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from .config import AppConfig, Venue
from .http_client import HttpClient

log = logging.getLogger("arb.fees")


class _Creds:
    __slots__ = ("key", "secret", "passphrase")

    def __init__(self, key: str, secret: str, passphrase: str | None):
        self.key = key
        self.secret = secret
        self.passphrase = passphrase


def _resolve_creds(venue: Venue) -> _Creds | None:
    k = venue.keys
    if not k:
        return None
    # приоритет — прямое значение в конфиге; иначе берём из переменной окружения
    key = k.api_key or (os.environ.get(k.api_key_env, "") if k.api_key_env else "")
    secret = k.api_secret or (os.environ.get(k.api_secret_env, "") if k.api_secret_env else "")
    if k.passphrase:
        passphrase = k.passphrase
    elif k.passphrase_env:
        passphrase = os.environ.get(k.passphrase_env, "")
    else:
        passphrase = None
    if not key or not secret:
        return None
    return _Creds(key, secret, passphrase)


# --------------------------------------------------------------------------- #
#  Провайдеры комиссий по биржам
# --------------------------------------------------------------------------- #
class FeeFetcher:
    """База. Возвращает (maker, taker) как доли и сетевую комиссию в монете."""
    provider = "base"

    async def trading_fee(self, http, base_url, creds, symbol) -> tuple[float, float] | None:
        raise NotImplementedError

    async def network_fee(self, http, base_url, creds, coin, network) -> tuple[float, bool] | None:
        """Возврат: (сетевая комиссия в монете, включён ли вывод) или None."""
        raise NotImplementedError


class BinanceFees(FeeFetcher):
    provider = "binance"
    base_url = "https://api.binance.com"

    def _signed_url(self, base_url, path, creds, params: dict) -> str:
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query = urlencode(params)
        sig = hmac.new(creds.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return f"{base_url or self.base_url}{path}?{query}&signature={sig}"

    async def trading_fee(self, http, base_url, creds, symbol):
        url = self._signed_url(base_url, "/sapi/v1/asset/tradeFee", creds, {"symbol": symbol})
        data = await http.request_json("GET", url, headers={"X-MBX-APIKEY": creds.key})
        row = data[0] if isinstance(data, list) and data else data
        return float(row["makerCommission"]), float(row["takerCommission"])

    async def network_fee(self, http, base_url, creds, coin, network):
        url = self._signed_url(base_url, "/sapi/v1/capital/config/getall", creds, {})
        data = await http.request_json("GET", url, headers={"X-MBX-APIKEY": creds.key})
        for c in data:
            if c.get("coin") != coin:
                continue
            nets = c.get("networkList") or []
            chosen = None
            for n in nets:
                if network and n.get("network") == network:
                    chosen = n
                    break
                if not network and n.get("isDefault"):
                    chosen = n
            if chosen is None and nets:
                chosen = nets[0]
            if chosen:
                enabled = bool(chosen.get("withdrawEnable", True))
                return float(chosen["withdrawFee"]), enabled
        return None


class BybitFees(FeeFetcher):
    provider = "bybit"
    base_url = "https://api.bybit.com"

    async def _signed_get(self, http, base_url, creds, path, params: dict):
        ts = str(int(time.time() * 1000))
        recv = "5000"
        query = urlencode(params)
        payload = ts + creds.key + recv + query
        sign = hmac.new(creds.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": creds.key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv,
            "X-BAPI-SIGN": sign,
        }
        url = f"{base_url or self.base_url}{path}?{query}"
        return await http.request_json("GET", url, headers=headers)

    async def trading_fee(self, http, base_url, creds, symbol):
        data = await self._signed_get(http, base_url, creds, "/v5/account/fee-rate",
                                       {"category": "spot", "symbol": symbol})
        row = (data.get("result") or {}).get("list") or []
        if not row:
            return None
        return float(row[0]["makerFeeRate"]), float(row[0]["takerFeeRate"])

    async def network_fee(self, http, base_url, creds, coin, network):
        data = await self._signed_get(http, base_url, creds, "/v5/asset/coin/query-info",
                                       {"coin": coin})
        rows = (data.get("result") or {}).get("rows") or []
        for r in rows:
            if r.get("coin") != coin:
                continue
            chains = r.get("chains") or []
            chosen = None
            for ch in chains:
                if network and ch.get("chain") == network:
                    chosen = ch
                    break
            if chosen is None and chains:
                chosen = chains[0]
            if chosen and chosen.get("withdrawFee") not in (None, ""):
                enabled = str(chosen.get("chainWithdraw", "1")) == "1"
                return float(chosen["withdrawFee"]), enabled
        return None


class OkxFees(FeeFetcher):
    provider = "okx"
    base_url = "https://www.okx.com"

    async def _signed_get(self, http, base_url, creds, path_with_query):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
            f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
        prehash = ts + "GET" + path_with_query
        sign = base64.b64encode(
            hmac.new(creds.secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        headers = {
            "OK-ACCESS-KEY": creds.key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": creds.passphrase or "",
        }
        url = f"{base_url or self.base_url}{path_with_query}"
        return await http.request_json("GET", url, headers=headers)

    async def trading_fee(self, http, base_url, creds, symbol):
        path = f"/api/v5/account/trade-fee?instType=SPOT&instId={symbol}"
        data = await self._signed_get(http, base_url, creds, path)
        rows = data.get("data") or []
        if not rows:
            return None
        # OKX отдаёт комиссии как отрицательные строки ("-0.0008") — берём модуль
        maker = abs(float(rows[0]["maker"]))
        taker = abs(float(rows[0]["taker"]))
        return maker, taker

    async def network_fee(self, http, base_url, creds, coin, network):
        path = f"/api/v5/asset/currencies?ccy={coin}"
        data = await self._signed_get(http, base_url, creds, path)
        rows = data.get("data") or []
        chosen = None
        for r in rows:
            if r.get("ccy") != coin:
                continue
            chain = r.get("chain", "")  # вид "BTC-Bitcoin"
            if network and network.lower() in chain.lower():
                chosen = r
                break
            if chosen is None:
                chosen = r
        if chosen and chosen.get("minFee") not in (None, ""):
            enabled = bool(chosen.get("canWd", True))
            return float(chosen["minFee"]), enabled
        return None


_FETCHERS: dict[str, FeeFetcher] = {
    f.provider: f for f in (BinanceFees(), BybitFees(), OkxFees())
}


def _provider_for(venue: Venue) -> str | None:
    if venue.fee_provider:
        return venue.fee_provider
    # выводим из имени адаптера: binance_spot -> binance
    for p in _FETCHERS:
        if venue.adapter.startswith(p):
            return p
    return None


# --------------------------------------------------------------------------- #
#  Менеджер: кэш + периодическое обновление
# --------------------------------------------------------------------------- #
class FeeManager:
    """Подтягивает и кэширует живые комиссии для площадок, у которых есть ключи."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._trading: dict[tuple[str, str], tuple[float, float]] = {}
        self._network: dict[tuple[str, str, str | None], float] = {}
        self._withdraw_ok: dict[tuple[str, str, str | None], bool] = {}
        # какие символы/монеты вообще нужны (из маршрутов)
        self._symbols: dict[str, set[str]] = {}        # venue -> {symbol}
        self._coins: dict[str, set[tuple[str, str | None]]] = {}  # venue -> {(coin, network)}
        for r in cfg.routes:
            if r.is_chain:
                for leg in r.legs:
                    v = cfg.venues[leg.venue]
                    if v.kind == "spot" and leg.params.get("symbol"):
                        self._symbols.setdefault(leg.venue, set()).add(leg.params["symbol"])
                    if leg.network:   # сетевой сбор за перевод get-актива
                        self._coins.setdefault(leg.venue, set()).add((leg.get, leg.network))
                continue
            for leg in (r.buy, r.sell):
                v = cfg.venues[leg.venue]
                if v.kind == "spot" and leg.params.get("symbol"):
                    self._symbols.setdefault(leg.venue, set()).add(leg.params["symbol"])
            # сетевой сбор берём с площадки, ОТКУДА выводим базу — это buy-нога
            bv = cfg.venues[r.buy.venue]
            if bv.kind == "spot":
                self._coins.setdefault(r.buy.venue, set()).add((r.base, r.network))

    def any_keys(self) -> bool:
        return any(_resolve_creds(v) for v in self.cfg.venues.values())

    async def refresh(self, http: HttpClient) -> None:
        for name, venue in self.cfg.venues.items():
            creds = _resolve_creds(venue)
            if not creds:
                continue
            provider = _provider_for(venue)
            fetcher = _FETCHERS.get(provider) if provider else None
            if not fetcher:
                log.warning("площадка %s: неизвестный провайдер комиссий — пропуск live", name)
                continue
            base_url = venue.endpoint
            for symbol in self._symbols.get(name, ()):  # торговые комиссии
                try:
                    res = await fetcher.trading_fee(http, base_url, creds, symbol)
                    if res:
                        self._trading[(name, symbol)] = res
                        log.info("live комиссия %s %s: maker=%.5f taker=%.5f",
                                 name, symbol, res[0], res[1])
                except Exception as e:
                    log.warning("площадка %s: не удалось получить торговую комиссию %s: %s",
                                name, symbol, e)
            for coin, network in self._coins.get(name, ()):  # сетевые комиссии
                try:
                    res = await fetcher.network_fee(http, base_url, creds, coin, network)
                    if res is not None:
                        fee, enabled = res
                        self._network[(name, coin, network)] = fee
                        self._withdraw_ok[(name, coin, network)] = enabled
                        log.info("live сетевая комиссия %s %s (%s): %s, вывод=%s",
                                 name, coin, network or "default", fee,
                                 "вкл" if enabled else "ВЫКЛ")
                except Exception as e:
                    log.warning("площадка %s: не удалось получить сетевую комиссию %s: %s",
                                name, coin, e)

    def trading_taker(self, venue: str, symbol: str | None) -> tuple[float, float] | None:
        if not symbol:
            return None
        return self._trading.get((venue, symbol))

    def network_fee(self, venue: str, coin: str, network: str | None) -> float | None:
        return self._network.get((venue, coin, network))

    def withdraw_enabled(self, venue: str, coin: str, network: str | None) -> bool | None:
        """True/False если известно по live-данным, иначе None (не проверяли)."""
        return self._withdraw_ok.get((venue, coin, network))
