"""История спредов в SQLite: пишем КАЖДОЕ оценённое окно (в т.ч. минусовое).

Зачем: алерты показывают только сработавшие окна, а понять, какие маршруты
реально «дышат» у нуля и в какие часы — нельзя. История копит все замеры, по ним
команда `--report` строит статистику: частота плюса, лучший/средний спред, лучшие
часы. Это подсказывает, куда докручивать пороги и какие связки добавлять.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone

log = logging.getLogger("arb.history")


class SpreadHistory:
    """Лог всех замеров спреда. Лёгкая обёртка над отдельной таблицей в той же БД."""

    def __init__(self, db_path: str, *, enabled: bool = True, retention_days: float = 14.0):
        self.enabled = enabled
        self.retention_seconds = retention_days * 86400.0
        self._db = sqlite3.connect(db_path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS spread_history ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts REAL NOT NULL,"
            " route TEXT NOT NULL,"
            " net_spread_pct REAL NOT NULL,"
            " net_profit REAL NOT NULL,"
            " available_base REAL NOT NULL,"
            " size_base REAL NOT NULL,"
            " fee_source TEXT,"
            " passed INTEGER NOT NULL DEFAULT 0)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS ix_hist_route_ts ON spread_history(route, ts)"
        )
        self._db.commit()
        self._last_prune = 0.0

    def record(self, route: str, net_spread_pct: float, net_profit: float,
               available_base: float, size_base: float,
               fee_source: str, passed: bool) -> None:
        if not self.enabled:
            return
        try:
            self._db.execute(
                "INSERT INTO spread_history"
                "(ts, route, net_spread_pct, net_profit, available_base, size_base,"
                " fee_source, passed) VALUES(?,?,?,?,?,?,?,?)",
                (time.time(), route, net_spread_pct, net_profit, available_base,
                 size_base, fee_source, 1 if passed else 0),
            )
            self._db.commit()
            self._maybe_prune()
        except Exception as e:  # история не должна ронять сканер
            log.debug("не удалось записать историю спреда %s: %s", route, e)

    def streak_minutes(self, route: str) -> float:
        """Сколько минут окно маршрута держится подряд (непрерывная серия passed=1).

        Берём последние замеры от свежего к старому и идём, пока passed=1. Возврат —
        (сейчас − начало серии) в минутах; 0 если окно только появилось или истории нет.
        """
        if not self.enabled:
            return 0.0
        try:
            rows = self._db.execute(
                "SELECT ts, passed FROM spread_history WHERE route=? ORDER BY ts DESC LIMIT 500",
                (route,),
            ).fetchall()
        except Exception:
            return 0.0
        start_ts = None
        for ts, passed in rows:
            if not passed:
                break
            start_ts = ts
        if start_ts is None:
            return 0.0
        return max(0.0, (time.time() - start_ts) / 60.0)

    def _maybe_prune(self) -> None:
        now = time.time()
        if self.retention_seconds <= 0 or now - self._last_prune < 3600.0:
            return
        self._last_prune = now
        self._db.execute("DELETE FROM spread_history WHERE ts < ?",
                         (now - self.retention_seconds,))
        self._db.commit()

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:  # pragma: no cover
            pass


def build_summary(db_path: str, hours: float = 24.0) -> str | None:
    """Компактная сводка для Telegram: сколько окон прошло порог, лучшее и частое.

    Возврат None, если за период не было ни одного прошедшего порог окна (нечего слать).
    """
    db = sqlite3.connect(db_path)
    try:
        has = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='spread_history'"
        ).fetchone()
        if not has:
            return None
        since = time.time() - hours * 3600.0
        agg = db.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT route) r FROM spread_history"
            " WHERE ts >= ? AND passed = 1", (since,)
        ).fetchone()
        total, n_routes = (agg or (0, 0))
        if not total:
            return None
        best = db.execute(
            "SELECT route, MAX(net_spread_pct) mx, net_profit FROM spread_history"
            " WHERE ts >= ? AND passed = 1 GROUP BY route ORDER BY mx DESC LIMIT 1",
            (since,)
        ).fetchone()
        freq = db.execute(
            "SELECT route, COUNT(*) c, AVG(net_spread_pct) a FROM spread_history"
            " WHERE ts >= ? AND passed = 1 GROUP BY route ORDER BY c DESC LIMIT 1",
            (since,)
        ).fetchone()
        lines = [f"📊 <b>Сводка за {hours:g} ч</b>",
                 f"✅ Прошло порог: <b>{total}</b> окон по {n_routes} маршрутам"]
        if best:
            lines.append(f"🏆 Лучшее: {best[0]} <b>{best[1]:+.2f}%</b> ({best[2]:+.0f} ₽)")
        if freq:
            lines.append(f"📈 Чаще всего: {freq[0]} ({freq[1]} окон, средн {freq[2]:+.2f}%)")
        return "\n".join(lines)
    finally:
        db.close()


def build_report(db_path: str, hours: float = 24.0) -> str:
    """Текстовый отчёт по истории за последние `hours` часов."""
    db = sqlite3.connect(db_path)
    try:
        # есть ли вообще таблица
        has = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='spread_history'"
        ).fetchone()
        if not has:
            return "История пуста: таблицы spread_history ещё нет (сканер не запускался)."
        since = time.time() - hours * 3600.0
        rows = db.execute(
            "SELECT route,"
            " COUNT(*) n,"
            " SUM(CASE WHEN net_spread_pct > 0 THEN 1 ELSE 0 END) pos,"
            " SUM(passed) passed,"
            " MAX(net_spread_pct) mx,"
            " AVG(net_spread_pct) av,"
            " MAX(net_profit) mxp"
            " FROM spread_history WHERE ts >= ?"
            " GROUP BY route ORDER BY mx DESC",
            (since,),
        ).fetchall()
        if not rows:
            return f"За последние {hours:g} ч замеров нет."

        lines = [f"📊 Отчёт по спредам за последние {hours:g} ч:", ""]
        for route, n, pos, passed, mx, av, mxp in rows:
            pos_pct = (pos / n * 100.0) if n else 0.0
            lines.append(
                f"• {route}\n"
                f"    замеров: {n}, в плюсе: {pos} ({pos_pct:.0f}%), прошло порог: {passed}"
                f" (фактич. отправок меньше — режутся кулдауном)\n"
                f"    спред: макс {mx:+.2f}%, средн {av:+.2f}%, лучшая прибыль {mxp:+.2f}"
            )
            # лучшие 3 часа суток по среднему спреду для этого маршрута
            best = db.execute(
                "SELECT CAST(strftime('%H', ts, 'unixepoch') AS INT) h,"
                " AVG(net_spread_pct) a, COUNT(*) c"
                " FROM spread_history WHERE route=? AND ts>=?"
                " GROUP BY h HAVING c >= 2 ORDER BY a DESC LIMIT 3",
                (route, since),
            ).fetchall()
            if best:
                hrs = ", ".join(f"{int(h):02d}:00 UTC ({a:+.2f}%)" for h, a, _ in best)
                lines.append(f"    лучшие часы: {hrs}")
            lines.append("")
        return "\n".join(lines)
    finally:
        db.close()
