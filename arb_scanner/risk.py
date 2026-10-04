"""Скоринг риска и вердикт достоверности окна.

Задача: отличить надёжный, реально исполнимый +2% от рискового/иллюзорного +18%.
Очки риска копятся за: непроверенную/тонкую глубину, подозрительно высокий спред
(реально в данных, но обычно не реализуется), слабого мерчанта (низкий % исполнения /
мало сделок), комиссии не из live, медленный он-чейн перевод монеты (ценовой риск за
время перевода).

Дополнительно считается ВЕРДИКТ «железобетон» (`solid`) — окно проходит, только если
ВСЕ условия достоверности выполнены: проверенная глубина с запасом, надёжный мерчант,
спред в реалистичном коридоре, перевод по быстрой сети (а не медленный BTC/ETH).
В режиме reliable_only сканер шлёт в Telegram только такие окна.
"""
from __future__ import annotations

from .config import Thresholds
from .models import Opportunity

_EMOJI = {"низкий": "🟢", "средний": "🟡", "высокий": "🔴"}
_CONF_EMOJI = {"высокая": "✅", "средняя": "🟡", "низкая": "⛔"}

# Сети по скорости/риску перевода между площадками. Быстрые и дешёвые -> ценовой риск
# за время перевода ничтожен. Медленные/дорогие -> монета «в пути» минуты-десятки минут,
# цена успевает уйти -> окно недостоверно.
_FAST_NETWORKS = {
    "TRX", "TRC20", "BSC", "BEP20", "SOL", "SOLANA", "TON", "MATIC", "POLYGON",
    "ARBITRUM", "ARB", "OP", "OPTIMISM", "XLM", "ALGO", "AVAX", "APT",
}
_SLOW_NETWORKS = {"BTC", "ETH", "ERC20", "BCH", "LTC", "DOGE"}


def _transfer_risk(network, has_transfer: bool) -> str:
    """Классифицирует ценовой риск за время перевода: none|fast|slow|unknown."""
    if not has_transfer:
        return "none"
    if not network:
        return "unknown"
    n = str(network).upper()
    if n in _FAST_NETWORKS:
        return "fast"
    if n in _SLOW_NETWORKS:
        return "slow"
    return "unknown"


def _offers(b: dict, is_chain: bool) -> list[dict]:
    """Все мерчанты, реально участвующие в наборе объёма (не только топ-оффер)."""
    out: list[dict] = []
    if is_chain:
        for st in b.get("chain", []):
            offs = st.get("offers")
            if offs:
                out.extend(offs)
            elif st.get("offer"):
                out.append(st.get("offer"))
        return out
    for key_list, key_single in (("buy_offers", "buy_offer"), ("sell_offers", "sell_offer")):
        offs = b.get(key_list)
        if offs:
            out.extend(offs)
        elif b.get(key_single):
            out.append(b.get(key_single))
    return out


def _max_merchants(b: dict, is_chain: bool) -> int:
    """Макс. число отдельных P2P-офферов на одну ногу/шаг (сколько сделок реально нужно)."""
    if is_chain:
        groups = [len([o for o in (st.get("offers") or []) if o]) for st in b.get("chain", [])]
    else:
        groups = [len([o for o in (b.get(k) or []) if o]) for k in ("buy_offers", "sell_offers")]
    return max(groups) if groups else 0


def _min_completion(offers: list[dict]) -> float | None:
    comps = [o["completion"] for o in offers if o and o.get("completion") is not None]
    return min(comps) if comps else None


def _min_orders(offers: list[dict]) -> float | None:
    ords = [o["orders"] for o in offers if o and o.get("orders") is not None]
    return min(ords) if ords else None


def _has_transfer(opp: Opportunity, b: dict, is_chain: bool) -> tuple[bool, object]:
    """Есть ли реальный он-чейн перевод монеты + по какой сети (худшей = медленной)."""
    if is_chain:
        # перевод есть только на шагах с ненулевым сетевым сбором (внутрибиржевые = 0)
        nets = [st.get("network") for st in b.get("chain", [])
                if (st.get("transfer_fee_coin") or 0) > 0]
        if not nets:
            return False, None
        for n in nets:                       # медленная сеть на любом шаге = худший случай
            if n and str(n).upper() in _SLOW_NETWORKS:
                return True, n
        return True, nets[0]
    has = opp.buy_venue != opp.sell_venue and (b.get("network_fee", 0) or 0) > 0
    return has, b.get("network")


def assess(opp: Opportunity, th: Thresholds | None = None) -> dict:
    """Скоринг риска + вердикт достоверности. th управляет порогами «железобетона»."""
    th = th or Thresholds()
    b = opp.breakdown
    is_chain = "chain" in b
    pts = 0
    reasons: list[str] = []
    blockers: list[str] = []   # причины, по которым окно НЕ «железобетон»

    # 1) глубина под объём
    if b.get("no_depth"):
        pts += 3
        reasons.append("глубина не проверена")
        if not th.allow_no_depth:           # разрешено -> не блокер, но риск + пометка остаются
            blockers.append("глубина одной площадки не проверена")
    elif not is_chain and opp.size_base > 0:
        ratio = opp.available_base / opp.size_base
        if ratio < 1.2:
            pts += 2
            reasons.append(f"тонкая глубина ×{ratio:.1f}")
        elif ratio < 2.0:
            pts += 1
            reasons.append(f"глубина ×{ratio:.1f}")
        if ratio < th.solid_min_depth_ratio:
            blockers.append(f"глубина покрывает объём лишь ×{ratio:.1f}")

    # 2) «слишком хороший» спред — обычно не реализуется
    s = opp.net_spread_pct
    if s > 15:
        pts += 3
        reasons.append(f"спред {s:.0f}% подозрительно высок")
    elif s > 8:
        pts += 2
        reasons.append(f"спред {s:.0f}% высок")
    if th.solid_max_spread_pct > 0 and s > th.solid_max_spread_pct:
        blockers.append(f"спред {s:.1f}% выше реалистичного потолка {th.solid_max_spread_pct:.0f}%")

    # 3) качество мерчанта (берём худшего из участвующих офферов)
    offers = _offers(b, is_chain)
    mc = _min_completion(offers)
    mo = _min_orders(offers)
    if mc is not None:
        if mc < 0.90:
            pts += 2
            reasons.append(f"мерчант {mc * 100:.0f}%")
        elif mc < 0.97:
            pts += 1
            reasons.append(f"мерчант {mc * 100:.0f}%")
        if mc < th.solid_min_completion:
            blockers.append(f"мерчант исполняет {mc * 100:.0f}% (< {th.solid_min_completion * 100:.0f}%)")
    if mo is not None:
        if mo < 50:
            pts += 1
            reasons.append(f"мало сделок ({mo:.0f})")
        if mo < th.solid_min_orders:
            blockers.append(f"мало сделок у мерчанта ({mo:.0f})")
    # оффлайн-мерчант = протухший оффер: сделку сейчас не заключить
    if any(o.get("online") is False for o in offers if o):
        pts += 2
        reasons.append("мерчант оффлайн")
        blockers.append("мерчант оффлайн — оффер протух")

    # число отдельных P2P-сделок: чем больше мерчантов в наборе, тем труднее исполнить
    n_merch = _max_merchants(b, is_chain)
    if n_merch > th.solid_max_merchants:
        pts += 1
        reasons.append(f"нужно {n_merch} сделок")
        blockers.append(f"объём набирается с {n_merch} мерчантов (> {th.solid_max_merchants})")

    # 4) комиссии не live (мягкий риск — конфиг даёт консервативную оценку, не блокер)
    if b.get("fee_source") == "config":
        pts += 1
        reasons.append("комиссии не live")

    # приём по сети не подтверждён (нет данных в netcompat) — мягкий риск, не блокер,
    # чтобы маршруты без таблицы сетей не молчали (явная несовместимость отсекается в scanner)
    if b.get("net_unverified"):
        pts += 1
        reasons.append("сеть приёма не подтверждена")

    # 5) перевод монеты: ценовой риск зависит от скорости сети
    has_transfer, network = _has_transfer(opp, b, is_chain)
    trisk = _transfer_risk(network, has_transfer)
    if trisk == "slow":
        pts += 2
        reasons.append(f"медленный перевод ({network})")
        blockers.append(f"перевод по медленной сети {network} — цена успеет уйти")
    elif trisk == "unknown":
        pts += 1
        reasons.append("сеть перевода неизвестна")
        blockers.append("сеть перевода не задана — риск по времени не оценить")
    # trisk == "fast" / "none" — риска нет, в причины не пишем

    label = "низкий" if pts <= 1 else ("средний" if pts <= 3 else "высокий")

    # Честная прибыль с поправкой на надёжность мерчанта (вероятность завершения сделки).
    comp_factor = mc if mc is not None else 1.0
    adj_profit = opp.net_profit * comp_factor

    solid = not blockers
    if solid and b.get("no_depth"):
        confidence = "средняя"   # прошло, но глубину одной площадки проверить нельзя
    elif solid:
        confidence = "высокая"
    else:
        confidence = "средняя" if pts <= 3 else "низкая"

    return {
        "label": label,
        "emoji": _EMOJI[label],
        "score": pts,
        "reasons": reasons,
        "solid": solid,
        "confidence": confidence,
        "conf_emoji": _CONF_EMOJI[confidence],
        "blockers": blockers,
        "adj_profit": adj_profit,
        "transfer_risk": trisk,
    }
