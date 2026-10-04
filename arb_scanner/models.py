"""Нормализованные структуры данных, общие для всех источников."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Level:
    """Один уровень книги / одно P2P-объявление.

    price   — цена за единицу базовой монеты в валюте котировки.
    qty     — доступный/максимальный объём базовой монеты на этом уровне.
    min_qty — минимальный объём сделки на этом уровне (для P2P; 0 для стакана).
    """
    price: float
    qty: float
    min_qty: float = 0.0
    ref: dict | None = None   # для P2P: {nick, url, ...} конкретного объявления/мерчанта


@dataclass(slots=True)
class Ladder:
    """Односторонняя «лестница» офферов, против которых я могу торговать.

    side='buy'  -> уровни, у которых я ПОКУПАЮ базу (asks), сортировка по возрастанию цены.
    side='sell' -> уровни, у которых я ПРОДАЮ базу (bids), сортировка по убыванию цены.
    """
    side: str
    levels: list[Level]
    venue: str = ""
    raw_count: int = 0
    no_depth: bool = False   # True = глубина недоступна (только топ-цена, напр. Rapira)


@dataclass(slots=True)
class EffectiveFees:
    """Комиссии, фактически применённые к маршруту (live с биржи или из конфига)."""
    buy_taker: float
    sell_taker: float
    buy_p2p: float
    sell_p2p: float
    buy_payment: float
    sell_payment: float
    network_fee_coin: float
    source: str = "config"          # 'live' | 'config' | 'mixed'
    details: dict = field(default_factory=dict)  # пояснение по каждому компоненту


@dataclass(slots=True)
class Opportunity:
    """Найденное окно после честного расчёта."""
    route: str
    base: str
    quote: str
    size_base: float
    avg_buy: float
    avg_sell: float
    net_profit: float
    net_spread_pct: float
    gross_spread_pct: float
    available_base: float
    buy_venue: str
    sell_venue: str
    breakdown: dict = field(default_factory=dict)
    ts: float = 0.0
