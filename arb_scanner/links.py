"""Глубокие ссылки на нужный рынок/сторону площадки для сообщений в Telegram.

Это ссылки на нужную ВИТРИНУ (пара + сторона buy/sell), а не на конкретное
объявление — публичных стабильных ссылок на отдельный оффер площадки не дают.
Открыв ссылку, попадаешь сразу в нужный раздел, где видно те же объявления.
"""
from __future__ import annotations


def _bybit_p2p(side: str, p: dict, **_) -> str:
    coin = p.get("asset", "USDT")
    fiat = p.get("fiat", "RUB")
    tab = "buy" if side == "buy" else "sell"
    return f"https://www.bybit.com/en/p2p/{tab}/{coin}/{fiat}"


def _htx_p2p(side: str, p: dict, **_) -> str:
    # HTX за Cloudflare — точный deep-link с query не проверить и он давал 404.
    # Ведём на витрину P2P без параметров (она открывается), монета/сторона — из текста.
    return "https://www.htx.com/en-us/fiat-crypto/"


def _exmo_spot(side: str, p: dict, **_) -> str:
    # .com геоблочит RUB-пары (редирект на USDT); .me сохраняет пару. Путь /trade/pro/{PAIR}.
    return f"https://exmo.me/trade/pro/{p.get('symbol', '')}"


def _rapira_spot(side: str, p: dict, **_) -> str:
    sym = str(p.get("symbol", "")).replace("/", "_")
    return f"https://rapira.net/exchange/{sym}" if sym else "https://rapira.net/"


def _bybit_spot(side: str, p: dict, *, base=None, quote=None) -> str:
    if base and quote:
        return f"https://www.bybit.com/en/trade/spot/{base}/{quote}"
    return "https://www.bybit.com/en/trade/spot"


def _binance_spot(side: str, p: dict, **_) -> str:
    return f"https://www.binance.com/en/trade/{p.get('symbol', '')}"


def _okx_spot(side: str, p: dict, **_) -> str:
    return f"https://www.okx.com/trade-spot/{str(p.get('symbol', '')).lower()}"


def _mexc_spot(side: str, p: dict, **_) -> str:
    return f"https://www.mexc.com/exchange/{p.get('symbol', '')}"


_BUILDERS = {
    "bybit_p2p": _bybit_p2p,
    "htx_p2p": _htx_p2p,
    "binance_p2p": _bybit_p2p,  # запасной; binance_p2p подключают редко
    "exmo_spot": _exmo_spot,
    "rapira_spot": _rapira_spot,
    "bybit_spot": _bybit_spot,
    "binance_spot": _binance_spot,
    "okx_spot": _okx_spot,
    "mexc_spot": _mexc_spot,
}


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _online(x) -> bool | None:
    """Нормализует isOnline (bool / 1-0 / '1'-'0' / None) в bool | None."""
    if x is None:
        return None
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return x != 0
    s = str(x).strip().lower()
    if s in ("1", "true", "online", "yes"):
        return True
    if s in ("0", "false", "offline", "no", ""):
        return False
    return None


# Беглый справочник самых частых Bybit RUB payment-ID -> банк (для подписи; проверяй).
_BYBIT_PAY = {
    "14": "Tinkoff", "75": "SBP", "377": "SBP", "585": "SBP", "64": "Raiffeisen",
    "90": "Rosbank", "40": "Local Card", "51": "QIWI", "62": "YooMoney", "18": "Sberbank",
}


def bybit_p2p_offer(item: dict) -> dict | None:
    """Ник, метрики, способы оплаты Bybit-оффера. Ссылку НЕ даём здесь (профиль по
    userMaskId в SPA часто 404) — в сообщении используется ссылка на рынок (нужная
    монета+сторона), а оффер опознаётся по нику+банку+цене."""
    nick = item.get("nickName")
    url = None
    rate = _num(item.get("recentExecuteRate"))
    ids = [str(x) for x in (item.get("payments") or [])]
    names = [_BYBIT_PAY.get(i, f"#{i}") for i in ids]
    tokens = {t.lower() for t in ids + names}
    return {
        "nick": nick,
        "url": url,
        "completion": rate / 100.0 if rate is not None else None,  # 0..1
        "orders": _num(item.get("recentOrderNum")) or _num(item.get("finishNum")),
        "online": _online(item.get("isOnline")),   # онлайн ли мерчант (свежесть оффера)
        "min_limit": _num(item.get("minAmount")),   # лимиты сделки в фиате
        "max_limit": _num(item.get("maxAmount")),
        "pay": ", ".join(dict.fromkeys(names)),   # подпись для сообщения
        "pay_tokens": tokens,                      # для фильтра (id + имя, lower)
    }


def htx_p2p_offer(item: dict) -> dict | None:
    """HTX: ник + метрики + способы оплаты (имена прямо из ответа)."""
    nick = item.get("userName")
    rate = _num(item.get("orderCompleteRate"))
    methods = item.get("payMethods") or []
    names = [m.get("name") for m in methods if m.get("name")]
    ids = [str(m.get("payMethodId")) for m in methods if m.get("payMethodId") is not None]
    tokens = {t.lower() for t in names + ids}
    return {
        "nick": nick,
        "url": None,
        "completion": rate / 100.0 if rate is not None else None,
        "orders": _num(item.get("tradeMonthTimes")),
        "online": _online(item.get("isOnline")),
        "min_limit": _num(item.get("minTradeLimit")),
        "max_limit": _num(item.get("maxTradeLimit")),
        "pay": ", ".join(dict.fromkeys(names)),
        "pay_tokens": tokens,
    }


def build_link(adapter: str, side: str, params: dict,
               *, base: str | None = None, quote: str | None = None) -> str | None:
    """Ссылка на витрину площадки для стороны side ('buy'|'sell'). None если неизвестно."""
    fn = _BUILDERS.get(adapter)
    if fn is None:
        return None
    try:
        return fn(side, params or {}, base=base, quote=quote)
    except Exception:
        return None
