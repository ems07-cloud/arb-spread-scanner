"""Тест адаптивного троттлинга HTTP-клиента (логика _penalize/_reward без сети)."""
import unittest

from arb_scanner.http_client import HttpClient


class _FakeClient(HttpClient):
    def __init__(self):
        # не зовём super().__init__ (не нужен aiohttp-сеанс) — заводим только поля рейта
        self._host_interval = {}
        self._host_base = {}
        self._host_ok = {}


class TestBackoff(unittest.TestCase):
    def setUp(self):
        self.c = _FakeClient()
        self.c.set_rate("h", 0.5)

    def test_penalize_inflates(self):
        self.c._penalize("h")
        self.assertGreater(self.c._host_interval["h"], 0.5)

    def test_penalize_capped(self):
        for _ in range(20):
            self.c._penalize("h")
        self.assertLessEqual(self.c._host_interval["h"], HttpClient._BACKOFF_CAP)

    def test_recover_after_streak(self):
        self.c._penalize("h")
        self.c._penalize("h")
        inflated = self.c._host_interval["h"]
        for _ in range(HttpClient._RECOVER_AFTER):
            self.c._reward("h")
        self.assertLess(self.c._host_interval["h"], inflated)

    def test_reward_noop_at_base(self):
        for _ in range(HttpClient._RECOVER_AFTER + 5):
            self.c._reward("h")
        self.assertEqual(self.c._host_interval["h"], 0.5)   # не опускаемся ниже базы


if __name__ == "__main__":
    unittest.main()
