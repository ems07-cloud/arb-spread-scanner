"""Базовый интерфейс адаптера."""
from __future__ import annotations

from ..config import Leg, Venue
from ..http_client import HttpClient
from ..models import Ladder


class Adapter:
    """Адаптер источника. Возвращает одностороннюю лестницу офферов.

    Реализации ТОЛЬКО читают публичные данные. Никаких приватных/торговых вызовов.
    Если для P2P-эндпоинта когда-либо понадобится ключ — он обязан быть read-only.
    """
    name: str = "base"
    kind: str = "spot"

    async def fetch_ladder(self, http: HttpClient, venue: Venue, leg: Leg) -> Ladder:
        raise NotImplementedError
