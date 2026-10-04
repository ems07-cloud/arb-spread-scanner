"""Тесты ядра расчёта спреда: набор по уровням, VWAP, цепочки, объём."""
import unittest

from arb_scanner.config import ChainLeg, Leg, Route
from arb_scanner.models import EffectiveFees, Ladder, Level
from arb_scanner import spread


def _zero_fees(**kw) -> EffectiveFees:
    base = dict(buy_taker=0.0, sell_taker=0.0, buy_p2p=0.0, sell_p2p=0.0,
                buy_payment=0.0, sell_payment=0.0, network_fee_coin=0.0)
    base.update(kw)
    return EffectiveFees(**base)


class TestFill(unittest.TestCase):
    def test_vwap_partial(self):
        levels = [Level(90, 5), Level(91, 5), Level(92, 10)]
        avg, filled, cap = spread.fill(levels, 8)
        self.assertAlmostEqual(avg, (90 * 5 + 91 * 3) / 8, places=9)
        self.assertAlmostEqual(filled, 8.0, places=9)
        self.assertAlmostEqual(cap, 20.0, places=9)

    def test_not_enough_depth(self):
        levels = [Level(90, 2)]
        avg, filled, cap = spread.fill(levels, 5)
        self.assertAlmostEqual(filled, 2.0, places=9)   # набрали меньше, чем нужно

    def test_min_qty_skips_level(self):
        # остаток 1 меньше min_qty=3 второго уровня -> уровень пропускается
        levels = [Level(90, 4), Level(91, 10, min_qty=3)]
        avg, filled, cap = spread.fill(levels, 5)
        self.assertAlmostEqual(filled, 4.0, places=9)   # добрали только 4 с первого

    def test_fill_by_quote(self):
        levels = [Level(90, 5), Level(91, 5)]
        got, spent, ok = spread.fill_by_quote(levels, 900)
        # 5 по 90 = 450, остаток 450 по 91 = 4.945...
        self.assertTrue(ok)
        self.assertAlmostEqual(spent, 900.0, places=6)
        self.assertAlmostEqual(got, 5 + 450 / 91, places=6)

    def test_fill_by_quote_insufficient(self):
        levels = [Level(90, 1)]
        got, spent, ok = spread.fill_by_quote(levels, 900)
        self.assertFalse(ok)


class TestUsedOffers(unittest.TestCase):
    def test_collects_touched_merchants(self):
        levels = [Level(90, 5, ref={"nick": "a"}),
                  Level(91, 5, ref={"nick": "b"}),
                  Level(92, 5, ref={"nick": "c"})]
        offers = spread.used_offers(levels, 8)   # затронуты a (5) и b (3), c — нет
        self.assertEqual([o["nick"] for o in offers], ["a", "b"])

    def test_by_quote_variant(self):
        levels = [Level(90, 5, ref={"nick": "a"}), Level(91, 5, ref={"nick": "b"})]
        offers = spread.used_offers(levels, 900, by_quote=True)
        self.assertEqual([o["nick"] for o in offers], ["a", "b"])


class TestOutliers(unittest.TestCase):
    def test_drops_single_anomaly(self):
        lad = Ladder("sell", [Level(100, 1), Level(101, 1), Level(102, 1), Level(300, 1)])
        dropped = spread.drop_price_outliers(lad, 0.25)
        self.assertEqual(dropped, 1)
        self.assertTrue(all(lv.price < 200 for lv in lad.levels))

    def test_keeps_when_few_levels(self):
        lad = Ladder("sell", [Level(100, 1), Level(300, 1)])
        self.assertEqual(spread.drop_price_outliers(lad, 0.25), 0)


class TestEvaluate(unittest.TestCase):
    def _route(self, size=1.0):
        return Route(name="t", base="BTC", quote="USDT", size_base=size,
                     buy=Leg("ex", "buy", {}), sell=Leg("ex2", "sell", {}))

    def test_positive_window(self):
        buy = Ladder("buy", [Level(100, 10)], venue="ex")
        sell = Ladder("sell", [Level(110, 10)], venue="ex2")
        opp, avail = spread.evaluate(self._route(), _zero_fees(), buy, sell)
        self.assertIsNotNone(opp)
        self.assertAlmostEqual(opp.net_profit, 10.0, places=6)      # (110-100)*1
        self.assertAlmostEqual(opp.net_spread_pct, 10.0, places=6)

    def test_fees_eat_profit(self):
        buy = Ladder("buy", [Level(100, 10)], venue="ex")
        sell = Ladder("sell", [Level(110, 10)], venue="ex2")
        eff = _zero_fees(buy_taker=0.06, sell_taker=0.06)  # 6%+6% издержек > 10% спреда
        opp, _ = spread.evaluate(self._route(), eff, buy, sell)
        self.assertLess(opp.net_profit, 0)

    def test_none_when_no_depth(self):
        buy = Ladder("buy", [Level(100, 0.5)], venue="ex")
        sell = Ladder("sell", [Level(110, 10)], venue="ex2")
        opp, _ = spread.evaluate(self._route(size=1.0), _zero_fees(), buy, sell)
        self.assertIsNone(opp)

    def test_collects_all_offers(self):
        buy = Ladder("buy", [Level(100, 0.6, ref={"nick": "a"}),
                             Level(101, 1, ref={"nick": "b"})], venue="ex")
        sell = Ladder("sell", [Level(110, 10, ref={"nick": "c"})], venue="ex2")
        opp, _ = spread.evaluate(self._route(size=1.0), _zero_fees(), buy, sell)
        nicks = [o["nick"] for o in opp.breakdown["buy_offers"]]
        self.assertEqual(nicks, ["a", "b"])   # оба мерчанта, через которых идёт объём


class TestEvaluateChain(unittest.TestCase):
    def test_round_trip_profit(self):
        # RUB ->(buy USDT @100)-> USDT ->(sell USDT @110)-> RUB, без комиссий
        legs = [ChainLeg("v", "buy", "RUB", "USDT"),
                ChainLeg("v", "sell", "USDT", "RUB")]
        route = Route(name="c", base="RUB", quote="RUB", size_base=10000,
                      legs=legs, start_currency="RUB", start_amount=10000)
        ladders = [Ladder("buy", [Level(100, 1000)]),
                   Ladder("sell", [Level(110, 1000)])]
        costs = [{"fee_frac": 0.0, "transfer_fee_coin": 0.0},
                 {"fee_frac": 0.0, "transfer_fee_coin": 0.0}]
        opp, start = spread.evaluate_chain(route, ladders, costs, "config")
        self.assertIsNotNone(opp)
        # 10000 RUB -> 100 USDT -> 11000 RUB => +1000
        self.assertAlmostEqual(opp.net_profit, 1000.0, places=4)
        self.assertAlmostEqual(opp.net_spread_pct, 10.0, places=4)


class TestOptimalSize(unittest.TestCase):
    def test_finds_max_size_above_threshold(self):
        # глубокий хороший уровень + дорогой хвост: оптимум упрётся в хороший объём
        buy = Ladder("buy", [Level(100, 5), Level(130, 100)])
        sell = Ladder("sell", [Level(110, 1000)])
        opt = spread.optimal_size(_zero_fees(), buy, sell,
                                  min_pct=1.0, size_min=1.0, size_max=50.0)
        self.assertIsNotNone(opt)
        self.assertGreaterEqual(opt["net_pct"], 1.0)


if __name__ == "__main__":
    unittest.main()
