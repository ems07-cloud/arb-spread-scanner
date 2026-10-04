"""Дедупликация и кулдаун алертов через SQLite.

Одно и то же окно не должно спамить: на каждый маршрут держим время и величину
последнего алерта. Новый алерт разрешён, если истёк кулдаун ИЛИ спред заметно
улучшился (на realert_improve_pct процентных пунктов).
"""
from __future__ import annotations

import logging
import sqlite3
import time

log = logging.getLogger("arb.dedup")


class AlertState:
    def __init__(self, db_path: str, cooldown: float, realert_improve_pct: float):
        self.cooldown = cooldown
        self.realert_improve_pct = realert_improve_pct
        self._db = sqlite3.connect(db_path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS alert_state ("
            " route TEXT PRIMARY KEY,"
            " last_ts REAL NOT NULL,"
            " last_spread REAL NOT NULL)"
        )
        self._db.commit()

    def should_alert(self, route: str, net_spread_pct: float) -> bool:
        row = self._db.execute(
            "SELECT last_ts, last_spread FROM alert_state WHERE route=?", (route,)
        ).fetchone()
        if row is None:
            return True
        last_ts, last_spread = row
        if (time.time() - last_ts) >= self.cooldown:
            return True
        if (net_spread_pct - last_spread) >= self.realert_improve_pct:
            return True
        return False

    def record(self, route: str, net_spread_pct: float) -> None:
        self._db.execute(
            "INSERT INTO alert_state(route, last_ts, last_spread) VALUES(?,?,?)"
            " ON CONFLICT(route) DO UPDATE SET last_ts=excluded.last_ts,"
            " last_spread=excluded.last_spread",
            (route, time.time(), net_spread_pct),
        )
        self._db.commit()

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:  # pragma: no cover
            pass
