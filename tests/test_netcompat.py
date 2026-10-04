"""Тесты совместимости сетей перевода между площадками."""
import unittest

from arb_scanner.config import Venue
from arb_scanner import netcompat


def _v(name, adapter, networks=None):
    return Venue(name=name, adapter=adapter, kind="spot", networks=networks)


class TestCanon(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(netcompat.canon("TRC20"), "TRX")
        self.assertEqual(netcompat.canon("tron"), "TRX")
        self.assertEqual(netcompat.canon("BEP20"), "BSC")
        self.assertIsNone(netcompat.canon(None))


class TestRouteNetworkOk(unittest.TestCase):
    def test_compatible_default(self):
        exmo = _v("exmo_spot", "exmo_spot")
        bybit = _v("bybit_p2p_rub", "bybit_p2p")
        ok, _ = netcompat.route_network_ok(exmo, bybit, "USDT", "TRX")
        self.assertTrue(ok)

    def test_incompatible_blocks(self):
        # Exmo USDT по умолчанию {TRX, ERC20} — SOL не поддерживает
        exmo = _v("exmo_spot", "exmo_spot")
        bybit = _v("bybit_p2p_rub", "bybit_p2p")
        ok, reason = netcompat.route_network_ok(exmo, bybit, "USDT", "SOL")
        self.assertFalse(ok)
        self.assertIn("не выводит", reason)

    def test_unknown_when_no_data(self):
        a = _v("x", "unknown_spot")
        b = _v("y", "unknown_p2p")
        ok, _ = netcompat.route_network_ok(a, b, "USDT", "TRX")
        self.assertIsNone(ok)

    def test_config_override(self):
        a = _v("a", "unknown_spot", networks={"USDT": ["TRX"]})
        b = _v("b", "unknown_p2p", networks=["TRX", "ERC20"])
        ok, _ = netcompat.route_network_ok(a, b, "USDT", "TRX")
        self.assertTrue(ok)
        ok2, _ = netcompat.route_network_ok(a, b, "USDT", "ERC20")
        self.assertFalse(ok2)   # a не поддерживает ERC20 по конфигу


if __name__ == "__main__":
    unittest.main()
