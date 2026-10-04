"""Оркестрация: цикл опроса, расчёт, фильтр порогов, алерты."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from urllib.parse import urlparse

import aiohttp

from . import netcompat, risk, spread
from .adapters import get_adapter
from .config import AppConfig, Leg, Route
from .dedup import AlertState
from .fees import FeeManager
from .history import SpreadHistory
from .http_client import HttpClient
from .links import build_link
from .models import EffectiveFees, Ladder
from .notify import Notifier

log = logging.getLogger("arb.scanner")


class Scanner:
    def __init__(self, cfg: AppConfig, *, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.fee_mgr = FeeManager(cfg)
        self._last_fee_refresh = 0.0
        self._last_heartbeat = 0.0
        self._last_summary = 0.0
        self._confirm: dict[str, int] = {}        # маршрут -> сколько опросов подряд прошёл пороги
        self._fail: dict[str, int] = {}           # площадка -> ошибок подряд
        self._down_notified: set[str] = set()     # площадки, по которым уже слали «недоступна»
        self._history: SpreadHistory | None = None  # лог всех замеров (создаётся в run)
        self._validate_routes()

    def _validate_routes(self) -> None:
        """Жёсткая защита: нельзя сравнивать ноги с разной валютой котировки."""
        for r in self.cfg.routes:
            if r.is_chain:        # цепочки валидируются при загрузке конфига
                continue
            for leg in (r.buy, r.sell):
                v = self.cfg.venues[leg.venue]
                if v.kind == "p2p":
                    fiat = leg.params.get("fiat")
                    if fiat and fiat != r.quote:
                        raise ValueError(
                            f"маршрут {r.name}: P2P-нога fiat={fiat} != quote={r.quote}. "
                            f"Сравнение разных котировок запрещено (нужен многоногий маршрут)."
                        )

    def _effective_fees(self, route: Route) -> EffectiveFees:
        """Собирает фактические комиссии: live с биржи где есть, иначе из конфига."""
        bv = self.cfg.venues[route.buy.venue]
        sv = self.cfg.venues[route.sell.venue]
        details: dict[str, str] = {}
        live_flags: list[bool] = []

        def taker_for(leg_venue, venue) -> float:
            live = self.fee_mgr.trading_taker(venue.name, _symbol(leg_venue))
            if live is not None:
                live_flags.append(True)
                details[f"{venue.name}.taker"] = "live"
                return live[1]
            live_flags.append(False)
            details[f"{venue.name}.taker"] = "config"
            return venue.fees.taker

        def _symbol(leg):
            return leg.params.get("symbol")

        buy_taker = taker_for(route.buy, bv)
        sell_taker = taker_for(route.sell, sv)

        # сетевая комиссия — с площадки вывода (buy-нога)
        net_live = self.fee_mgr.network_fee(bv.name, route.base, route.network)
        if net_live is not None:
            network = net_live
            live_flags.append(True)
            details["network"] = "live"
        else:
            network = route.network_fee_coin
            live_flags.append(False)
            details["network"] = "config"

        if all(live_flags):
            source = "live"
        elif any(live_flags):
            source = "mixed"
        else:
            source = "config"

        return EffectiveFees(
            buy_taker=buy_taker,
            sell_taker=sell_taker,
            buy_p2p=bv.fees.p2p if bv.kind == "p2p" else 0.0,
            sell_p2p=sv.fees.p2p if sv.kind == "p2p" else 0.0,
            buy_payment=bv.fees.payment_cost,
            sell_payment=sv.fees.payment_cost,
            network_fee_coin=network,
            source=source,
            details=details,
        )

    def _filter_outliers(self, ladder, venue_name: str) -> None:
        """Чистит одиночные ценовые аномалии у P2P-ноги (по медиане стороны)."""
        pct = self.cfg.thresholds.p2p_outlier_pct
        if pct <= 0 or ladder is None:
            return
        if self.cfg.venues[venue_name].kind != "p2p":
            return
        n = spread.drop_price_outliers(ladder, pct)
        if n:
            log.debug("маршрут-нога %s: отброшено %d аномальных оффера (медианный фильтр)",
                      venue_name, n)

    def _attach_links(self, opp, route: Route) -> None:
        """Кладёт в opp прямые ссылки на витрины покупки/продажи."""
        bv = self.cfg.venues[route.buy.venue]
        sv = self.cfg.venues[route.sell.venue]
        opp.breakdown["buy_url"] = build_link(
            bv.adapter, "buy", route.buy.params, base=route.base, quote=route.quote)
        opp.breakdown["sell_url"] = build_link(
            sv.adapter, "sell", route.sell.params, base=route.base, quote=route.quote)

    def _attach_chain_links(self, opp, route: Route) -> None:
        for step, leg in zip(opp.breakdown.get("chain", []), route.legs):
            v = self.cfg.venues[leg.venue]
            step["url"] = build_link(v.adapter, leg.op, leg.params,
                                     base=leg.get, quote=leg.give)

    async def _maybe_refresh_fees(self, http: HttpClient) -> None:
        if not self.fee_mgr.any_keys():
            return
        now = time.monotonic()
        if now - self._last_fee_refresh < self.cfg.fee_refresh_seconds and self._last_fee_refresh:
            return
        self._last_fee_refresh = now
        try:
            await self.fee_mgr.refresh(http)
        except Exception as e:
            log.warning("обновление живых комиссий не удалось: %s", e)

    async def _fetch_leg(self, http: HttpClient, leg, notifier: Notifier) -> Ladder | None:
        """Тянет одну ногу, отслеживая здоровье площадки. None при ошибке."""
        venue = self.cfg.venues[leg.venue]
        adapter = get_adapter(venue.adapter)
        try:
            ladder = await adapter.fetch_ladder(http, venue, leg)
        except Exception as e:
            await self._mark_down(leg.venue, notifier, e)
            return None
        await self._mark_up(leg.venue, notifier)
        return ladder

    async def _mark_down(self, name: str, notifier: Notifier, err: Exception) -> None:
        n = self._fail.get(name, 0) + 1
        self._fail[name] = n
        log.warning("площадка %s: ошибка данных (%d подряд): %s", name, n, err)
        if n == self.cfg.health_fail_threshold and name not in self._down_notified:
            self._down_notified.add(name)
            await notifier.send_text(
                f"⚠️ Площадка <b>{name}</b>: {n} ошибок подряд — данные недоступны.")

    async def _mark_up(self, name: str, notifier: Notifier) -> None:
        if name in self._down_notified:
            self._down_notified.discard(name)
            await notifier.send_text(f"✅ Площадка <b>{name}</b> снова отвечает.")
        self._fail[name] = 0

    async def _maybe_heartbeat(self, notifier: Notifier) -> None:
        hs = self.cfg.heartbeat_seconds
        if hs <= 0:
            return
        now = time.monotonic()
        if self._last_heartbeat == 0.0:      # первый вызов — не шлём, просто заводим таймер
            self._last_heartbeat = now
            return
        if now - self._last_heartbeat < hs:
            return
        self._last_heartbeat = now
        await notifier.send_text(f"💚 Сканер жив. Маршрутов: {len(self.cfg.routes)}.")

    async def _maybe_daily_summary(self, notifier: Notifier) -> None:
        secs = self.cfg.daily_summary_seconds
        if secs <= 0:
            return
        now = time.monotonic()
        if self._last_summary == 0.0:      # первый вызов — заводим таймер, не шлём сразу
            self._last_summary = now
            return
        if now - self._last_summary < secs:
            return
        self._last_summary = now
        from .history import build_summary
        text = build_summary(self.cfg.db_path, hours=secs / 3600.0)
        if text:
            await notifier.send_text(text)

    def _chain_leg_costs(self, route: Route) -> tuple[list[dict], str]:
        """Комиссии по шагам цепочки: доля на конвертацию + сетевой сбор за перевод."""
        costs: list[dict] = []
        live: list[bool] = []
        for leg in route.legs:
            v = self.cfg.venues[leg.venue]
            taker = v.fees.taker
            lt = self.fee_mgr.trading_taker(v.name, leg.params.get("symbol"))
            if lt is not None:
                taker = lt[1]
                live.append(True)
            elif v.kind == "spot":
                live.append(False)
            fee_frac = taker + (v.fees.p2p if v.kind == "p2p" else 0.0) + v.fees.payment_cost
            tfee = leg.transfer_fee_coin
            nlive = self.fee_mgr.network_fee(v.name, leg.get, leg.network)
            if nlive is not None:
                tfee = nlive
                live.append(True)
            elif leg.transfer_fee_coin > 0:
                live.append(False)
            costs.append({"fee_frac": fee_frac, "transfer_fee_coin": tfee})
        source = "live" if live and all(live) else ("mixed" if any(live) else "config")
        return costs, source

    async def _process_chain(self, http: HttpClient, notifier: Notifier,
                             state: AlertState, route: Route) -> None:
        fetch_legs = [Leg(venue=l.venue, side=l.op, params=l.params) for l in route.legs]
        ladders = await asyncio.gather(*(
            self._fetch_leg(http, fl, notifier) for fl in fetch_legs))
        if any(lad is None for lad in ladders):
            self._confirm[route.name] = 0
            return

        for fl, lad in zip(fetch_legs, ladders):
            self._filter_outliers(lad, fl.venue)

        legs = route.legs
        chain_net_unverified = False
        for i, l in enumerate(legs):
            if not l.network:
                continue
            nxt = legs[i + 1] if i + 1 < len(legs) else legs[0]
            if nxt.venue == l.venue:
                continue                         # внутрибиржевой шаг — перевода нет
            # вывод get-актива отключён -> пропуск
            if self.fee_mgr.withdraw_enabled(l.venue, l.get, l.network) is False:
                log.warning("маршрут %s: вывод %s с %s отключён — пропуск",
                            route.name, l.get, l.venue)
                self._confirm[route.name] = 0
                return
            # совместимость сети перевода между шагами
            net_ok, net_reason = netcompat.route_network_ok(
                self.cfg.venues[l.venue], self.cfg.venues[nxt.venue], l.get, l.network)
            if net_ok is False:
                log.warning("маршрут %s: %s — неисполнимо, пропуск", route.name, net_reason)
                self._confirm[route.name] = 0
                return
            if net_ok is None:
                chain_net_unverified = True

        costs, source = self._chain_leg_costs(route)
        start_override = None
        if self.cfg.deposit_rub > 0 and route.start_currency == "RUB":
            start_override = min(route.start_amount, self.cfg.deposit_rub)
        opp, _ = spread.evaluate_chain(route, list(ladders), costs, source, start_override)
        if opp is not None and start_override:
            opp.breakdown["deposit"] = self.cfg.deposit_rub
        if opp is None:
            self._confirm[route.name] = 0
            return
        opp.breakdown["net_unverified"] = chain_net_unverified

        th = self.cfg.thresholds
        log.info("маршрут %s (цепочка): спред %.2f%%, прибыль %.2f %s",
                 route.name, opp.net_spread_pct, opp.net_profit, opp.quote)

        # Вердикт достоверности («железобетон») + скоринг риска.
        rinfo = risk.assess(opp, th)
        opp.breakdown["risk"] = rinfo

        suspicious = (th.max_net_spread_pct > 0
                      and opp.net_spread_pct > th.max_net_spread_pct)
        if suspicious:
            log.warning("маршрут %s (цепочка): спред %.2f%% выше потолка %.2f%% — "
                        "вероятно выброс данных, алерт подавлен",
                        route.name, opp.net_spread_pct, th.max_net_spread_pct)
        unreliable = th.reliable_only and not rinfo["solid"]
        if unreliable:
            log.info("маршрут %s (цепочка): окно недостоверно (%s) — не шлём",
                     route.name, "; ".join(rinfo["blockers"]))
        passes = (opp.net_spread_pct >= th.min_net_spread_pct
                  and rinfo["adj_profit"] >= th.min_abs_profit  # честная прибыль с поправкой на мерчанта
                  and not suspicious
                  and not unreliable)
        if self._history is not None:
            self._history.record(route.name, opp.net_spread_pct, opp.net_profit,
                                 opp.available_base, opp.size_base, source, passes)
        if not passes:
            self._confirm[route.name] = 0
            return
        cnt = self._confirm.get(route.name, 0) + 1
        self._confirm[route.name] = cnt
        if cnt < th.confirmations:
            return
        if not state.should_alert(route.name, opp.net_spread_pct):
            return
        self._attach_chain_links(opp, route)
        if self._history is not None:
            opp.breakdown["held_min"] = self._history.streak_minutes(route.name)
        if await notifier.send(opp):       # дедуп пишем ТОЛЬКО при реальной отправке
            state.record(route.name, opp.net_spread_pct)

    async def _process_route(self, http: HttpClient, notifier: Notifier,
                             state: AlertState, route: Route) -> None:
        if route.is_chain:
            return await self._process_chain(http, notifier, state, route)
        buy_ladder, sell_ladder = await asyncio.gather(
            self._fetch_leg(http, route.buy, notifier),
            self._fetch_leg(http, route.sell, notifier),
        )
        if buy_ladder is None or sell_ladder is None:
            self._confirm[route.name] = 0
            return

        self._filter_outliers(buy_ladder, route.buy.venue)
        self._filter_outliers(sell_ladder, route.sell.venue)

        # Если между площадками есть реальный перевод базы и вывод сейчас отключён —
        # окно «бумажное», пропускаем (проверка работает только по live-данным).
        net_unverified = False
        if route.buy.venue != route.sell.venue:
            wd = self.fee_mgr.withdraw_enabled(route.buy.venue, route.base, route.network)
            if wd is False:
                log.warning("маршрут %s: вывод %s с %s сейчас отключён — пропуск",
                            route.name, route.base, route.buy.venue)
                self._confirm[route.name] = 0
                return
            # Совместимость сети: вывод источника и приём получателя должны совпадать.
            net_ok, net_reason = netcompat.route_network_ok(
                self.cfg.venues[route.buy.venue], self.cfg.venues[route.sell.venue],
                route.base, route.network)
            if net_ok is False:
                log.warning("маршрут %s: %s — маршрут неисполним, пропуск", route.name, net_reason)
                self._confirm[route.name] = 0
                return
            net_unverified = net_ok is None

        eff = self._effective_fees(route)
        size_override = None
        if self.cfg.deposit_rub > 0 and route.quote == "RUB":
            size_override = spread.affordable_size(self.cfg.deposit_rub, buy_ladder, sell_ladder)
        opp, available = spread.evaluate(route, eff, buy_ladder, sell_ladder, size_override)
        if opp is not None and size_override:
            opp.breakdown["deposit"] = self.cfg.deposit_rub
        if opp is None:
            log.debug("маршрут %s: нет окна (глубина=%.6g)", route.name, available)
            self._confirm[route.name] = 0
            return
        opp.breakdown["network"] = route.network
        opp.breakdown["net_unverified"] = net_unverified
        # лучшая цена по банкам — для P2P-ног (трейдер сразу видит, чем брать выгоднее)
        if self.cfg.venues[route.buy.venue].kind == "p2p":
            opp.breakdown["buy_banks"] = spread.bank_prices(buy_ladder)
        if self.cfg.venues[route.sell.venue].kind == "p2p":
            opp.breakdown["sell_banks"] = spread.bank_prices(sell_ladder)

        th = self.cfg.thresholds
        log.info("маршрут %s: чистый спред %.2f%%, прибыль %.2f %s, объём %.6g %s",
                 route.name, opp.net_spread_pct, opp.net_profit, opp.quote,
                 opp.available_base, opp.base)

        # Вердикт достоверности («железобетон») + скоринг риска.
        rinfo = risk.assess(opp, th)
        opp.breakdown["risk"] = rinfo

        # Пороги ОДНОВРЕМЕННО: спред >= порога, прибыль >= минимума, объёма хватает.
        suspicious = (th.max_net_spread_pct > 0
                      and opp.net_spread_pct > th.max_net_spread_pct)
        if suspicious:
            log.warning("маршрут %s: спред %.2f%% выше потолка %.2f%% — вероятно выброс "
                        "данных (тонкий/мусорный оффер), алерт подавлен",
                        route.name, opp.net_spread_pct, th.max_net_spread_pct)
        # В режиме «железобетон» шлём только окна, прошедшие все проверки достоверности.
        unreliable = th.reliable_only and not rinfo["solid"]
        if unreliable:
            log.info("маршрут %s: окно недостоверно (%s) — не шлём",
                     route.name, "; ".join(rinfo["blockers"]))
        passes = (opp.net_spread_pct >= th.min_net_spread_pct
                  and rinfo["adj_profit"] >= th.min_abs_profit  # честная прибыль с поправкой на мерчанта
                  and opp.available_base >= opp.size_base  # глубины хватает на РЕАЛЬНЫЙ объём (учёт депозита)
                  and not suspicious
                  and not unreliable)
        if self._history is not None:
            self._history.record(route.name, opp.net_spread_pct, opp.net_profit,
                                 opp.available_base, opp.size_base,
                                 opp.breakdown.get("fee_source", "config"), passes)
        if not passes:
            self._confirm[route.name] = 0
            return

        # Анти-фликер: окно должно продержаться N опросов подряд.
        cnt = self._confirm.get(route.name, 0) + 1
        self._confirm[route.name] = cnt
        if cnt < th.confirmations:
            log.debug("маршрут %s: окно есть, ждём подтверждения %d/%d",
                      route.name, cnt, th.confirmations)
            return

        if not state.should_alert(route.name, opp.net_spread_pct):
            log.debug("маршрут %s: подавлено кулдауном/дедупом", route.name)
            return

        self._maybe_autosize(opp, route, eff, buy_ladder, sell_ladder)
        self._attach_links(opp, route)
        if self._history is not None:
            opp.breakdown["held_min"] = self._history.streak_minutes(route.name)
        if await notifier.send(opp):       # дедуп пишем ТОЛЬКО при реальной отправке
            state.record(route.name, opp.net_spread_pct)

    def _maybe_autosize(self, opp, route: Route, eff, buy_ladder, sell_ladder) -> None:
        """Если включено — ищет макс. объём при спреде >= порога и кладёт в breakdown."""
        if not self.cfg.autosize or self.cfg.deposit_rub > 0:
            return
        cap = opp.available_base
        size_max = route.max_size if route.max_size else cap
        if route.max_size and math.isfinite(cap):
            size_max = min(route.max_size, cap)
        if not math.isfinite(size_max) or size_max <= route.size_base:
            return
        opt = spread.optimal_size(
            eff, buy_ladder, sell_ladder,
            min_pct=self.cfg.thresholds.min_net_spread_pct,
            size_min=route.size_base, size_max=size_max)
        if opt and opt["size"] > route.size_base * 1.001:
            opp.breakdown["opt_size"] = opt["size"]
            opp.breakdown["opt_profit"] = opt["net_profit"]
            opp.breakdown["opt_spread"] = opt["net_pct"]

    async def run(self, *, once: bool = False) -> None:
        state = AlertState(
            self.cfg.db_path,
            self.cfg.thresholds.cooldown_seconds,
            self.cfg.thresholds.realert_improve_pct,
        )
        self._history = SpreadHistory(
            self.cfg.db_path,
            enabled=self.cfg.history_enabled,
            retention_days=self.cfg.history_retention_days,
        )
        connector = aiohttp.TCPConnector(limit=self.cfg.http.concurrency)
        headers = {"User-Agent": self.cfg.http.user_agent}
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            http = HttpClient(
                session,
                timeout=self.cfg.http.timeout,
                max_retries=self.cfg.http.max_retries,
                concurrency=self.cfg.http.concurrency,
            )
            # Настраиваем рейт-лимиты по хостам площадок.
            for v in self.cfg.venues.values():
                adapter = get_adapter(v.adapter)
                base = v.endpoint or getattr(adapter, "base_url", None) or getattr(adapter, "url", "")
                host = urlparse(base).netloc
                if host:
                    http.set_rate(host, v.rate_min_interval)

            notifier = Notifier(self.cfg.telegram, http, dry_run=self.dry_run)
            keys = self.fee_mgr.any_keys()
            log.info("сканер запущен: маршрутов=%d, интервал=%.0fс, dry_run=%s, live-комиссии=%s",
                     len(self.cfg.routes), self.cfg.poll_interval, self.dry_run,
                     "вкл" if keys else "выкл (нет ключей, беру из конфига)")

            try:
                await self._maybe_refresh_fees(http)   # первый прогон комиссий до старта циклов
                if once:
                    await asyncio.gather(*(
                        self._process_route(http, notifier, state, r)
                        for r in self.cfg.routes
                    ))
                else:
                    # Каждый маршрут крутится в своём цикле со своей частотой опроса:
                    # горячие связки можно опрашивать чаще, не дёргая остальные.
                    tasks = [asyncio.create_task(self._housekeeping(http, notifier))]
                    tasks += [asyncio.create_task(self._route_loop(http, notifier, state, r))
                              for r in self.cfg.routes]
                    try:
                        await asyncio.gather(*tasks)
                    finally:
                        for t in tasks:
                            t.cancel()
            finally:
                state.close()
                if self._history is not None:
                    self._history.close()

    async def _route_loop(self, http: HttpClient, notifier: Notifier,
                          state: AlertState, route: Route) -> None:
        """Бесконечный цикл одного маршрута со своей частотой. Ошибки не валят остальные."""
        interval = route.poll_interval or self.cfg.poll_interval
        while True:
            try:
                await self._process_route(http, notifier, state, route)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("маршрут %s: непредвиденная ошибка в цикле", route.name)
            await asyncio.sleep(interval)

    async def _housekeeping(self, http: HttpClient, notifier: Notifier) -> None:
        """Фоновое обслуживание: обновление живых комиссий и heartbeat."""
        while True:
            try:
                await self._maybe_refresh_fees(http)
                await self._maybe_heartbeat(notifier)
                await self._maybe_daily_summary(notifier)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ошибка в фоновом обслуживании")
            await asyncio.sleep(min(self.cfg.poll_interval, 30.0))

    async def test_telegram(self) -> bool:
        """Шлёт одно тестовое сообщение в Telegram. Возврат: отправлено ли."""
        headers = {"User-Agent": self.cfg.http.user_agent}
        async with aiohttp.ClientSession(headers=headers) as session:
            http = HttpClient(session, timeout=self.cfg.http.timeout,
                              max_retries=self.cfg.http.max_retries,
                              concurrency=self.cfg.http.concurrency)
            notifier = Notifier(self.cfg.telegram, http, dry_run=False)
            ok = await notifier.send_text(
                "✅ Тест связи: бот подключён, токен и chat_id рабочие.")
            if ok:
                log.info("тестовое сообщение отправлено")
            else:
                log.error("тест не прошёл: проверь enabled/bot_token/chat_id в конфиге")
            return ok
