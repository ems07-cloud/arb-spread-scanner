"""Ядро: честный расчёт спреда с учётом всех издержек и глубины стакана.

Денежная математика (набор по уровням, VWAP, компаундинг по шагам цепочки) считается
в Decimal — float накапливает ошибку округления при суммировании многих уровней и
перемножении шагов. На границах (вход/выход функций) конвертируем в Decimal/float,
чтобы остальной код продолжал работать с обычными числами.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from statistics import median

from .config import Route
from .models import EffectiveFees, Ladder, Level, Opportunity

log = logging.getLogger("arb.spread")

_EPS = 1e-9
_DEPS = Decimal("1e-9")
_ONE = Decimal(1)


def _D(x) -> Decimal:
    """Безопасная конвертация в Decimal через str (без двоичного шума float)."""
    return x if isinstance(x, Decimal) else Decimal(str(x))


def drop_price_outliers(ladder: Ladder, pct: float) -> int:
    """Убирает из P2P-лестницы офферы с ценой дальше pct (доля) от медианы стороны.

    Чистит ОДИНОЧНЫЕ аномалии (один мерчант с дикой ценой среди нормальных), которые
    иначе исказили бы fill. Не трогает, если уровней мало (<3) или фильтр выключен.
    Возврат: сколько уровней отброшено.
    """
    if pct <= 0 or ladder.no_depth or len(ladder.levels) < 3:
        return 0
    med = median(lv.price for lv in ladder.levels)
    if med <= 0:
        return 0
    kept = [lv for lv in ladder.levels if abs(lv.price / med - 1.0) <= pct]
    dropped = len(ladder.levels) - len(kept)
    if kept and dropped:
        ladder.levels = kept
    return dropped if kept else 0


def bank_prices(ladder: Ladder, top: int = 4) -> dict:
    """Лучшая цена по каждому банку из P2P-лестницы.

    Лестница уже отсортирована «лучшая цена сверху» (buy=дешевле, sell=дороже), поэтому
    первая встреча банка = его лучшая цена. Возврат: {банк: цена} (до top штук).
    """
    out: dict = {}
    for lv in ladder.levels:
        pay = (lv.ref or {}).get("pay") or ""
        for bank in (b.strip() for b in pay.split(",")):
            if bank and bank not in out:
                out[bank] = lv.price
    return dict(list(out.items())[:top])


def fill(levels: list[Level], target: float) -> tuple[float, float, float]:
    """Прохожу по уровням, набирая target базы.

    Возврат: (средневзвешенная цена, реально набранный объём, суммарная ёмкость).
    Если набрать target не удалось — filled < target (значит глубины не хватает).
    VWAP по уровням автоматически учитывает проскальзывание под объём.
    """
    remaining = _D(target)
    cost = Decimal(0)
    capacity = Decimal(0)
    for lv in levels:
        # Нельзя взять с оффера меньше его минимального лимита: если остаток меньше
        # минимума этого уровня — пропускаем его (сделку на меньший объём не заключить).
        if remaining < _D(lv.min_qty) - _DEPS:
            continue
        qty = _D(lv.qty)
        capacity += qty
        take = remaining if remaining < qty else qty
        if take > 0:
            cost += take * _D(lv.price)
            remaining -= take
    filled = _D(target) - (remaining if remaining > 0 else Decimal(0))
    avg = cost / filled if filled > _DEPS else Decimal(0)
    return float(avg), float(filled), float(capacity)


def used_offers(levels: list[Level], target: float, *, by_quote: bool = False) -> list[dict]:
    """Ссылки на офферы, РЕАЛЬНО затронутые при наборе target (база) или budget (котировка).

    Нужно, чтобы оценивать надёжность ВСЕХ мерчантов, через которых пройдёт объём, а не
    только лучшего: глубокий «слабый» оффер так же влияет на исполнимость окна.
    """
    refs: list[dict] = []
    if by_quote:
        spent = 0.0
        for lv in levels:
            rem = target - spent
            if rem <= _EPS:
                break
            affordable = rem / lv.price if lv.price > 0 else 0.0
            take = min(affordable, lv.qty)
            if take < lv.min_qty - _EPS:
                continue
            if take > 0 and lv.ref:
                refs.append(lv.ref)
            spent += take * lv.price
    else:
        remaining = target
        for lv in levels:
            if remaining < lv.min_qty - _EPS:
                continue
            take = min(remaining, lv.qty)
            if take > 0:
                if lv.ref:
                    refs.append(lv.ref)
                remaining -= take
            if remaining <= _EPS:
                break
    return refs


def fill_by_quote(levels: list[Level], budget: float) -> tuple[float, float, bool]:
    """Тратим `budget` валюты котировки, набирая базу по уровням asks.

    Возврат: (набрано базы, потрачено котировки, хватило ли глубины на весь бюджет).
    Учитывает минимальный лимит уровня (min_qty) — оффер пропускается, если объём
    под остаток бюджета меньше его минимума.
    """
    budget_d = _D(budget)
    spent = Decimal(0)
    got = Decimal(0)
    for lv in levels:
        rem = budget_d - spent
        if rem <= _DEPS:
            break
        price = _D(lv.price)
        affordable = rem / price if price > 0 else Decimal(0)
        take = affordable if affordable < _D(lv.qty) else _D(lv.qty)
        if take < _D(lv.min_qty) - _DEPS:
            continue
        got += take
        spent += take * price
    ok = spent >= budget_d - Decimal("1e-6")
    return float(got), float(spent), ok


def evaluate_chain(route: Route, ladders: list[Ladder], leg_costs: list[dict],
                   fee_source: str, start_override: float | None = None
                   ) -> tuple[Opportunity | None, float]:
    """Считает многоногий маршрут-цикл. leg_costs[i] = {fee_frac, transfer_fee_coin}.

    start_override — стартовая сумма под депозит (если цепочка стартует в той же валюте).
    """
    start_amount = _D(start_override if start_override and start_override > 0
                      else route.start_amount)
    amount = start_amount
    steps: list[dict] = []
    for leg, ladder, cost in zip(route.legs, ladders, leg_costs):
        fee_frac = _D(cost.get("fee_frac", 0.0))
        amt_f = float(amount)
        if leg.op == "buy":
            got, spent, ok = fill_by_quote(ladder.levels, amt_f)
            if not ok or got <= 0:
                log.debug("маршрут %s: шаг buy %s не набрал глубину", route.name, leg.venue)
                return None, 0.0
            got_d = _D(got)
            price = _D(spent) / got_d if got_d > 0 else Decimal(0)
            out = got_d * (_ONE - fee_frac)
            offers = used_offers(ladder.levels, amt_f, by_quote=True)
        else:  # sell
            avg, filled, _ = fill(ladder.levels, amt_f)
            if filled < amt_f - _EPS or avg <= 0:
                log.debug("маршрут %s: шаг sell %s не набрал глубину", route.name, leg.venue)
                return None, 0.0
            price = _D(avg)
            out = price * amount * (_ONE - fee_frac)
            offers = used_offers(ladder.levels, amt_f)
        out -= _D(cost.get("transfer_fee_coin", 0.0))
        steps.append({
            "venue": leg.venue, "op": leg.op, "give": leg.give, "get": leg.get,
            "in": round(amt_f, 6), "out": round(float(out), 6),
            "price": round(float(price), 8), "fee_frac": float(fee_frac),
            "offer": ladder.levels[0].ref if ladder.levels else None,
            "offers": offers,   # все мерчанты, затронутые на этом шаге
            "transfer_fee_coin": cost.get("transfer_fee_coin", 0.0),
            "network": leg.network,
        })
        amount = out

    profit = amount - start_amount
    pct = float(profit / start_amount * Decimal(100)) if start_amount > 0 else 0.0
    start_f = float(start_amount)
    opp = Opportunity(
        route=route.name,
        base=route.start_currency, quote=route.start_currency,
        size_base=start_f,
        avg_buy=0.0, avg_sell=0.0,
        net_profit=float(profit), net_spread_pct=pct, gross_spread_pct=pct,
        available_base=start_f,
        buy_venue=route.legs[0].venue, sell_venue=route.legs[-1].venue,
        breakdown={"chain": steps, "fee_source": fee_source,
                   "end_amount": round(float(amount), 6)},
        ts=time.time(),
    )
    return opp, start_f


def _metrics(eff: EffectiveFees, buy_ladder: Ladder, sell_ladder: Ladder,
             size: float) -> dict | None:
    """Метрики маршрута под заданный объём size (None если глубины не хватило)."""
    avg_buy, buy_filled, _ = fill(buy_ladder.levels, size)
    avg_sell, sell_filled, _ = fill(sell_ladder.levels, size)
    if buy_filled < size - _EPS or sell_filled < size - _EPS:
        return None
    if avg_buy <= 0 or avg_sell <= 0:
        return None

    sz = _D(size)
    ab, as_ = _D(avg_buy), _D(avg_sell)
    buy_notional = ab * sz
    sell_notional = as_ * sz
    buy_fee = buy_notional * _D(eff.buy_taker)
    sell_fee = sell_notional * _D(eff.sell_taker)
    p2p_fee = buy_notional * _D(eff.buy_p2p) + sell_notional * _D(eff.sell_p2p)
    pay_cost = buy_notional * _D(eff.buy_payment) + sell_notional * _D(eff.sell_payment)
    network_quote = _D(eff.network_fee_coin) * ab
    costs = buy_fee + sell_fee + p2p_fee + pay_cost + network_quote
    gross = (as_ - ab) * sz
    net = gross - costs
    notional = buy_notional
    hundred = Decimal(100)
    return {
        "avg_buy": avg_buy, "avg_sell": avg_sell,
        "buy_fee": float(buy_fee), "sell_fee": float(sell_fee), "p2p_fee": float(p2p_fee),
        "payment_cost": float(pay_cost), "network_fee": float(network_quote),
        "total_costs": float(costs), "gross": float(gross), "net": float(net),
        "net_pct": float(net / notional * hundred) if notional > 0 else 0.0,
        "gross_pct": float(gross / notional * hundred) if notional > 0 else 0.0,
    }


def optimal_size(eff: EffectiveFees, buy_ladder: Ladder, sell_ladder: Ladder,
                 *, min_pct: float, size_min: float, size_max: float) -> dict | None:
    """Ищет максимальный объём в [size_min, size_max], при котором net-спред >= min_pct.

    Спред падает с ростом объёма (глубже в стакан = хуже VWAP), поэтому крупнейший
    объём с спредом >= порога даёт и максимальную абсолютную прибыль. None если даже
    на size_min не проходит. Бинарный поиск по объёму.
    """
    if size_max <= size_min:
        return None
    base = _metrics(eff, buy_ladder, sell_ladder, size_min)
    if base is None or base["net_pct"] < min_pct:
        return None
    lo, hi, best = size_min, size_max, size_min
    for _ in range(24):  # ~1e-7 относительной точности
        mid = (lo + hi) / 2.0
        m = _metrics(eff, buy_ladder, sell_ladder, mid)
        if m is not None and m["net_pct"] >= min_pct:
            best, lo = mid, mid
        else:
            hi = mid
    m = _metrics(eff, buy_ladder, sell_ladder, best)
    if m is None:
        return None
    return {"size": best, "net_profit": m["net"], "net_pct": m["net_pct"]}


def affordable_size(deposit_quote: float, buy_ladder: Ladder, sell_ladder: Ladder) -> float:
    """Сколько базы можно купить на депозит (в валюте котировки), с поправкой на глубину."""
    if deposit_quote <= 0 or not buy_ladder.levels:
        return 0.0
    top = buy_ladder.levels[0].price
    if top <= 0:
        return 0.0
    size = deposit_quote / top
    caps = [sum(lv.qty for lv in lad.levels)
            for lad in (buy_ladder, sell_ladder) if not lad.no_depth]
    if caps:
        size = min(size, min(caps))
    return size


def evaluate(route: Route, eff: EffectiveFees,
             buy_ladder: Ladder, sell_ladder: Ladder,
             size_override: float | None = None) -> tuple[Opportunity | None, float]:
    """Считает чистый спред маршрута по ЭФФЕКТИВНЫМ комиссиям (live или конфиг).

    size_override — если задан (напр. под депозит), считаем на этот объём вместо size_base.
    Возврат: (Opportunity или None если глубины не хватило, доступный объём базы).
    """
    size = size_override if size_override and size_override > 0 else route.size_base
    _, buy_filled, buy_cap = fill(buy_ladder.levels, size)
    _, sell_filled, sell_cap = fill(sell_ladder.levels, size)
    no_depth = buy_ladder.no_depth or sell_ladder.no_depth
    # доступный объём считаем только по сторонам с реальным стаканом
    caps = [c for c, lad in ((buy_cap, buy_ladder), (sell_cap, sell_ladder)) if not lad.no_depth]
    available = min(caps) if caps else float("inf")

    m = _metrics(eff, buy_ladder, sell_ladder, size)
    # Проверка глубины: под мой объём должно хватить с обеих сторон.
    if buy_filled < size - _EPS or sell_filled < size - _EPS or m is None:
        log.debug("маршрут %s: не хватает глубины (нужно %.6f, доступно %.6f)",
                  route.name, size, available)
        return None, available

    avg_buy = m["avg_buy"]
    avg_sell = m["avg_sell"]
    buy_fee, sell_fee = m["buy_fee"], m["sell_fee"]
    p2p_fee, pay_cost = m["p2p_fee"], m["payment_cost"]
    network_quote = m["network_fee"]
    costs, gross, net = m["total_costs"], m["gross"], m["net"]
    net_pct, gross_pct = m["net_pct"], m["gross_pct"]

    opp = Opportunity(
        route=route.name,
        base=route.base,
        quote=route.quote,
        size_base=size,
        avg_buy=avg_buy,
        avg_sell=avg_sell,
        net_profit=net,
        net_spread_pct=net_pct,
        gross_spread_pct=gross_pct,
        available_base=available,
        buy_venue=route.buy.venue,
        sell_venue=route.sell.venue,
        breakdown={
            "gross": round(gross, 6),
            "buy_fee": round(buy_fee, 6),
            "sell_fee": round(sell_fee, 6),
            "p2p_fee": round(p2p_fee, 6),
            "payment_cost": round(pay_cost, 6),
            "network_fee": round(network_quote, 6),
            "total_costs": round(costs, 6),
            "fee_source": eff.source,
            "fee_details": eff.details,
            "no_depth": no_depth,
            "buy_offer": buy_ladder.levels[0].ref if buy_ladder.levels else None,
            "sell_offer": sell_ladder.levels[0].ref if sell_ladder.levels else None,
            # все мерчанты, через которых реально набирается объём (не только топ)
            "buy_offers": used_offers(buy_ladder.levels, size),
            "sell_offers": used_offers(sell_ladder.levels, size),
        },
        ts=time.time(),
    )
    return opp, available
