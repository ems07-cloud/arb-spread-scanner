"""Реестр адаптеров источников данных (только чтение публичных эндпоинтов)."""
from __future__ import annotations

from .base import Adapter
from .spot import BinanceSpot, BybitSpot, ExmoSpot, MexcSpot, OkxSpot, RapiraSpot
from .p2p import BinanceP2P, BybitP2P, HtxP2P

_REGISTRY: dict[str, Adapter] = {
    a.name: a for a in (
        BinanceSpot(),
        BybitSpot(),
        OkxSpot(),
        ExmoSpot(),
        MexcSpot(),
        RapiraSpot(),
        BinanceP2P(),
        BybitP2P(),
        HtxP2P(),
    )
}


def get_adapter(name: str) -> Adapter:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"неизвестный адаптер {name!r}. Доступны: {', '.join(sorted(_REGISTRY))}"
        )


__all__ = ["Adapter", "get_adapter"]
