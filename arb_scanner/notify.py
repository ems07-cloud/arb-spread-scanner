"""Уведомления в Telegram (только отправка сообщений ботом)."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from .config import Telegram
from .http_client import HttpClient
from .models import Opportunity

log = logging.getLogger("arb.notify")


_FEE_SRC = {"live": "🟩 live", "mixed": "🟨 part-live", "config": "🟧 config"}

_VENUE_LABEL = {
    "bybit_p2p_rub": "Bybit P2P",
    "htx_p2p_rub": "HTX P2P",
    "exmo_spot": "Exmo (спот)",
    "rapira_spot": "Rapira (спот)",
    "bybit_spot": "Bybit (спот)",
}


def _esc(s) -> str:
    """Экранирование под Telegram HTML (иначе & в ссылках и < в никах ломают сообщение)."""
    return html.escape(str(s), quote=True)


def _venue(name: str) -> str:
    return _VENUE_LABEL.get(name, name)


def _p(x: float) -> str:
    """Цена без научной нотации: крупные — с разделителем тысяч, мелкие — как есть."""
    if abs(x) >= 1000:
        return f"{x:,.0f}".replace(",", " ")
    return f"{x:.6g}"


def _a(url: str, text: str = "открыть") -> str:
    return f'🔗 <a href="{_esc(url)}">{_esc(text)}</a>'


def _merchant(offer: dict | None) -> str:
    """Безопасность мерчанта в строку: ник · % исполнения · число сделок · онлайн."""
    o = offer or {}
    parts = []
    if o.get("nick"):
        parts.append(f"👤 {_esc(o['nick'])}")
    c = o.get("completion")
    if c is not None:
        parts.append(f"✅{c * 100:.0f}%")
    n = o.get("orders")
    if n is not None:
        parts.append(f"{n:.0f} сд")
    on = o.get("online")
    if on is True:
        parts.append("🟢")
    elif on is False:
        parts.append("🔴офлайн")
    return " · ".join(parts)


def _fmt_limits(offer: dict | None, quote: str) -> str:
    """Лимиты сделки мерчанта в фиате: '5 000–50 000 ₽'."""
    o = offer or {}
    lo, hi = o.get("min_limit"), o.get("max_limit")
    if lo and hi:
        return f"📑 лимит {_p(lo)}–{_p(hi)} {_esc(quote)}"
    if hi:
        return f"📑 лимит до {_p(hi)} {_esc(quote)}"
    return ""


def _leg_block(label: str, emoji: str, venue: str, price: float, quote: str,
               offer: dict | None, offers: list | None = None, banks: dict | None = None) -> str:
    """Блок одной ноги: цена · мерчант/банк · лимит · сколько мерчантов · лучшие банки."""
    o = offer or {}
    lines = [f"{emoji} <b>{label}</b> · {_esc(_venue(venue))} · <b>{_p(price)} {_esc(quote)}</b>"]
    sub = _merchant(offer)
    if o.get("pay"):
        sub = (sub + " · " if sub else "") + f"💳 {_esc(o['pay'])}"
    if sub:
        lines.append("   " + sub)
    # лимит топ-оффера + сколько отдельных сделок понадобится на объём
    info = []
    lim = _fmt_limits(offer, quote)
    if lim:
        info.append(lim)
    n = len([x for x in (offers or []) if x])
    if n >= 2:
        info.append(f"наберёшь с {n} мерчантов")
    if info:
        lines.append("   " + " · ".join(info))
    # лучшая цена по банкам прямо сейчас
    if banks:
        bstr = " · ".join(f"{_esc(b)} {_p(p)}" for b, p in list(banks.items())[:3])
        if bstr:
            lines.append(f"   🏦 {bstr}")
    return "\n".join(lines)


def _format_chain(opp: Opportunity, ts: str, fee_src: str) -> str:
    steps = opp.breakdown.get("chain", [])
    r = opp.breakdown.get("risk")
    head = f"Спред: <b>{opp.net_spread_pct:+.2f}%</b>   {fee_src}"
    if r:
        head += (f"\nДостоверность: {r.get('conf_emoji', '')} <b>{r.get('confidence', '')}</b>"
                 f"   ·   риск {r['emoji']} {r['label']}")
    parts = [
        f"🔗 <b>Цепочка: {_esc(opp.route)}</b>",
        head,
        f"{opp.size_base:g} {_esc(opp.base)} → {opp.breakdown.get('end_amount', 0):g} {_esc(opp.base)}",
        "",
    ]
    for i, s in enumerate(steps, 1):
        act = "Купить" if s["op"] == "buy" else "Продать"
        offer = s.get("offer") or {}
        parts.append(f"{i}. <b>{act}</b> · {_esc(_venue(s['venue']))} · @ {_p(s['price'])}")
        parts.append(f"   {s['in']:g} {_esc(s['give'])} → {s['out']:g} {_esc(s['get'])}")
        sub = _merchant(offer)
        if offer.get("pay"):
            sub = (sub + " · " if sub else "") + f"💳 {_esc(offer['pay'])}"
        if sub:
            parts.append("   " + sub)
        parts.append("")
    parts.append(f"💰 Прибыль: <b>{opp.net_profit:+.0f} {_esc(opp.base)}</b>")
    r = opp.breakdown.get("risk")
    if r and r.get("reasons"):
        parts.append(f"⚖️ Риск {r['emoji']} {r['label']}: {_esc(', '.join(r['reasons']))}")
    held = opp.breakdown.get("held_min")
    parts.append(f"🕒 держится ~{held:.0f} мин · {ts}" if held and held >= 1 else f"🕒 {ts}")
    return "\n".join(parts)


def format_message(opp: Opportunity) -> str:
    ts = datetime.fromtimestamp(opp.ts, tz=timezone.utc).strftime("%H:%M:%S UTC")
    b = opp.breakdown
    fee_src = _FEE_SRC.get(b.get("fee_source", "config"), "")
    if "chain" in b:
        return _format_chain(opp, ts, fee_src)

    r = b.get("risk") or {}
    # Заголовок одной строкой: пара · спред · достоверность.
    head = f"💹 <b>{_esc(opp.base)} → {_esc(opp.quote)}</b>  <b>{opp.net_spread_pct:+.2f}%</b>"
    conf = f"{r.get('conf_emoji', '')} {r.get('confidence', '')}".strip()
    if conf:
        head += f"  {conf}"

    parts = [head]
    if b.get("no_depth"):
        parts.append("⚠️ глубина одной площадки не проверена")
    parts += [
        "",
        _leg_block("Купить", "🟢", opp.buy_venue, opp.avg_buy, opp.quote,
                   b.get("buy_offer"), b.get("buy_offers"), b.get("buy_banks")),
        "",
        _leg_block("Продать", "🔴", opp.sell_venue, opp.avg_sell, opp.quote,
                   b.get("sell_offer"), b.get("sell_offers"), b.get("sell_banks")),
        "",
    ]
    # Прибыль и объём — одной строкой.
    prof = f"💰 <b>{opp.net_profit:+.0f} {_esc(opp.quote)}</b>"
    adj = r.get("adj_profit")
    if adj is not None and abs(adj - opp.net_profit) >= 1:
        prof += f" (по мерчанту {adj:+.0f})"
    vol = f"📦 {opp.size_base:.4g} {_esc(opp.base)}"
    if b.get("deposit"):
        vol += f" (деп {_p(b['deposit'])} ₽)"
    parts.append(f"{prof}   ·   {vol}   ·   {fee_src}")
    if b.get("opt_size") is not None:
        parts.append(
            f"📈 оптимум {b['opt_size']:.4g} {_esc(opp.base)} → "
            f"<b>{b['opt_profit']:+.0f} {_esc(opp.quote)}</b> ({b['opt_spread']:.2f}%)")
    # причины риска (кроме глубины — её уже показали вверху)
    reasons = [x for x in r.get("reasons", []) if "глубина" not in x]
    if reasons:
        parts.append(f"⚠️ {_esc(', '.join(reasons))}")
    held = b.get("held_min")
    parts.append(f"🕒 держится ~{held:.0f} мин · {ts}" if held and held >= 1 else f"🕒 {ts}")
    return "\n".join(parts)


def build_markup(opp: Opportunity) -> dict | None:
    """Inline-кнопки «открыть площадку» под сообщением (исполнение в один тап)."""
    b = opp.breakdown
    rows: list = []
    if "chain" in b:
        for i, s in enumerate(b.get("chain", []), 1):
            url = (s.get("offer") or {}).get("url") or s.get("url")
            if url:
                act = "Купить" if s["op"] == "buy" else "Продать"
                rows.append([{"text": f"{i}. {act} · {_venue(s['venue'])}", "url": url}])
    else:
        bu = (b.get("buy_offer") or {}).get("url") or b.get("buy_url")
        su = (b.get("sell_offer") or {}).get("url") or b.get("sell_url")
        row = []
        if bu:
            row.append({"text": f"🟢 Купить · {_venue(opp.buy_venue)}", "url": bu})
        if su:
            row.append({"text": f"🔴 Продать · {_venue(opp.sell_venue)}", "url": su})
        if row:
            rows.append(row)
    return {"inline_keyboard": rows} if rows else None


class Notifier:
    def __init__(self, cfg: Telegram, http: HttpClient, dry_run: bool = False):
        self.cfg = cfg
        self.http = http
        self.dry_run = dry_run

    async def send(self, opp: Opportunity) -> bool:
        ok = await self.send_text(format_message(opp), reply_markup=build_markup(opp))
        if ok:
            log.info("отправлен алерт в Telegram: %s (%.2f%%)", opp.route, opp.net_spread_pct)
        return ok

    async def send_text(self, text: str, reply_markup: dict | None = None) -> bool:
        """Шлёт произвольное сообщение. Возврат: отправлено ли реально."""
        if self.dry_run or not self.cfg.enabled:
            log.info("[DRY-RUN] сообщение: %s", text.splitlines()[0] if text else "")
            return False
        if not self.cfg.bot_token or not self.cfg.chat_id:
            log.warning("Telegram включён, но bot_token/chat_id не заданы — пропускаю отправку")
            return False
        url = f"https://api.telegram.org/bot{self.cfg.bot_token}/sendMessage"
        body = {
            "chat_id": self.cfg.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            body["reply_markup"] = reply_markup
        try:
            await self.http.request_json("POST", url, json_body=body)
            return True
        except Exception as e:
            log.error("не удалось отправить в Telegram: %s", e)
            return False
