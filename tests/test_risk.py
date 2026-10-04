"""Тесты вердикта достоверности («железобетон») и скоринга риска."""
import unittest

from arb_scanner.config import Thresholds
from arb_scanner.models import Opportunity
from arb_scanner import risk


def _opp(*, spread_pct=2.0, profit=1800.0, avail=3000.0, size=1000.0,
         network="TRX", buy="exmo_spot", sell="bybit_p2p_rub",
         no_depth=False, buy_offers=None, sell_offers=None, fee_source="live"):
    bd = {
        "network": network, "network_fee": 1.0, "fee_source": fee_source,
        "no_depth": no_depth,
        "buy_offer": (buy_offers or [None])[0],
        "sell_offer": (sell_offers or [None])[0],
        "buy_offers": buy_offers or [],
        "sell_offers": sell_offers or [],
    }
    return Opportunity(route="t", base="USDT", quote="RUB", size_base=size,
                       avg_buy=90, avg_sell=92, net_profit=profit,
                       net_spread_pct=spread_pct, gross_spread_pct=spread_pct,
                       available_base=avail, buy_venue=buy, sell_venue=sell,
                       breakdown=bd)


GOOD = {"completion": 0.99, "orders": 500, "online": True}


class TestSolidVerdict(unittest.TestCase):
    def setUp(self):
        self.th = Thresholds()   # дефолты = железобетон

    def test_solid_pass(self):
        r = risk.assess(_opp(sell_offers=[GOOD]), self.th)
        self.assertTrue(r["solid"])
        self.assertEqual(r["confidence"], "высокая")

    def test_no_depth_blocks(self):
        r = risk.assess(_opp(no_depth=True, sell_offers=[GOOD]), self.th)
        self.assertFalse(r["solid"])

    def test_no_depth_allowed_passes_with_medium_confidence(self):
        th = Thresholds(allow_no_depth=True)
        r = risk.assess(_opp(no_depth=True, sell_offers=[GOOD]), th)
        self.assertTrue(r["solid"])                 # прошло
        self.assertEqual(r["confidence"], "средняя")  # но достоверность не «высокая»
        self.assertIn("глубина не проверена", r["reasons"])

    def test_thin_depth_blocks(self):
        r = risk.assess(_opp(avail=1100, sell_offers=[GOOD]), self.th)
        self.assertFalse(r["solid"])

    def test_high_spread_blocks(self):
        r = risk.assess(_opp(spread_pct=18.0, profit=16000, sell_offers=[GOOD]), self.th)
        self.assertFalse(r["solid"])

    def test_weak_merchant_blocks(self):
        bad = {"completion": 0.92, "orders": 500, "online": True}
        r = risk.assess(_opp(sell_offers=[bad]), self.th)
        self.assertFalse(r["solid"])

    def test_offline_merchant_blocks(self):
        off = {"completion": 0.99, "orders": 500, "online": False}
        r = risk.assess(_opp(sell_offers=[off]), self.th)
        self.assertFalse(r["solid"])
        self.assertTrue(any("оффлайн" in b for b in r["blockers"]))

    def test_slow_network_blocks(self):
        r = risk.assess(_opp(network="BTC", spread_pct=3.0, sell_offers=[GOOD]), self.th)
        self.assertFalse(r["solid"])

    def test_unknown_network_blocks(self):
        r = risk.assess(_opp(network=None, sell_offers=[GOOD]), self.th)
        self.assertFalse(r["solid"])

    def test_worst_merchant_across_fill(self):
        # топ-мерчант отличный, но глубокий — слабый: окно НЕ железобетон
        weak = {"completion": 0.80, "orders": 500, "online": True}
        r = risk.assess(_opp(sell_offers=[GOOD, weak]), self.th)
        self.assertFalse(r["solid"])

    def test_adj_profit_discounts_by_completion(self):
        mid = {"completion": 0.95, "orders": 500, "online": True}
        r = risk.assess(_opp(profit=1000.0, sell_offers=[mid]), self.th)
        self.assertAlmostEqual(r["adj_profit"], 950.0, places=6)

    def test_too_many_merchants_blocks(self):
        many = [dict(GOOD) for _ in range(5)]   # 5 отдельных сделок > порога 3
        r = risk.assess(_opp(sell_offers=many), self.th)
        self.assertFalse(r["solid"])
        self.assertTrue(any("мерчант" in b for b in r["blockers"]))

    def test_few_merchants_ok(self):
        few = [dict(GOOD), dict(GOOD)]
        r = risk.assess(_opp(sell_offers=few), self.th)
        self.assertTrue(r["solid"])

    def test_net_unverified_is_soft(self):
        # «сеть не подтверждена» — мягкий риск, НЕ блокер
        o = _opp(sell_offers=[GOOD])
        o.breakdown["net_unverified"] = True
        r = risk.assess(o, self.th)
        self.assertTrue(r["solid"])
        self.assertIn("сеть приёма не подтверждена", r["reasons"])


if __name__ == "__main__":
    unittest.main()
