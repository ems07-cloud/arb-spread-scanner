"""Тесты продуктовых фич: внутрибиржевой P2P, сводка, фильтр банков."""
import os
import sqlite3
import tempfile
import time
import unittest

from arb_scanner import autoroutes, history
from arb_scanner.config import Venue, load_config


def _venue(name, adapter, kind):
    return Venue(name=name, adapter=adapter, kind=kind)


class TestIntraP2P(unittest.TestCase):
    def test_internal_route_generated(self):
        venues = {
            "bybit_p2p_rub": _venue("bybit_p2p_rub", "bybit_p2p", "p2p"),
            "htx_p2p_rub": _venue("htx_p2p_rub", "htx_p2p", "p2p"),
        }
        auto = {
            "enabled": True, "quote": "RUB", "types": ["p2p_internal"],
            "coins": {"USDT": {"size": 1000, "venues": {
                "bybit_p2p_rub": {"asset": "USDT", "fiat": "RUB"},
                "htx_p2p_rub": {"coin_id": 2, "currency_id": 11}}}},
        }
        routes = autoroutes.generate(auto, venues, set())
        intra = [r for r in routes if r.buy.venue == r.sell.venue]
        self.assertEqual(len(intra), 2)                      # по одному на каждую P2P-площадку
        r = intra[0]
        self.assertEqual(r.network, None)                   # внутри биржи перевода нет
        self.assertEqual(r.network_fee_coin, 0.0)


class TestSummary(unittest.TestCase):
    def _db(self, rows):
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE spread_history (id INTEGER PRIMARY KEY, ts REAL, route TEXT,"
                   " net_spread_pct REAL, net_profit REAL, available_base REAL, size_base REAL,"
                   " fee_source TEXT, passed INTEGER)")
        now = time.time()
        for route, spread, profit, passed in rows:
            db.execute("INSERT INTO spread_history(ts,route,net_spread_pct,net_profit,"
                       "available_base,size_base,fee_source,passed) VALUES(?,?,?,?,?,?,?,?)",
                       (now, route, spread, profit, 100, 100, "live", passed))
        db.commit(); db.close()
        return path

    def test_summary_counts_passed(self):
        path = self._db([
            ("exmo->bybit", 1.5, 200, 1), ("exmo->bybit", 1.7, 240, 1),
            ("exmo->rapira", 3.1, 460, 1), ("x->y", -2.0, -100, 0),
        ])
        try:
            s = history.build_summary(path, hours=24)
            self.assertIn("Прошло порог", s)
            self.assertIn("3", s)                # 3 прошедших окна
            self.assertIn("exmo->rapira", s)     # лучшее по спреду
        finally:
            os.remove(path)

    def test_summary_none_when_empty(self):
        path = self._db([("x->y", -1.0, -50, 0)])
        try:
            self.assertIsNone(history.build_summary(path, hours=24))
        finally:
            os.remove(path)


class TestPayMethodsInjection(unittest.TestCase):
    def test_global_banks_injected_into_p2p_legs(self):
        cfg_text = """
poll_interval: 10
pay_methods: [Tinkoff, "75"]
telegram: { enabled: false }
venues:
  exmo_spot: { adapter: exmo_spot, kind: spot }
  bybit_p2p_rub: { adapter: bybit_p2p, kind: p2p }
routes:
  - name: t
    base: USDT
    quote: RUB
    size_base: 100
    buy:  { venue: exmo_spot, symbol: USDT_RUB }
    sell: { venue: bybit_p2p_rub, asset: USDT, fiat: RUB }
"""
        fd, path = tempfile.mkstemp(suffix=".yaml")
        os.write(fd, cfg_text.encode("utf-8")); os.close(fd)
        try:
            cfg = load_config(path)
            r = cfg.routes[0]
            self.assertEqual(r.sell.params.get("pay_methods"), ["Tinkoff", "75"])  # P2P-нога
            self.assertNotIn("pay_methods", r.buy.params)                          # спот не трогаем
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
