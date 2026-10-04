"""Точка входа сканера арбитражных спредов (read-only).

Запуск:
    python main.py --config config.yaml
    python main.py --config config.yaml --once --dry-run   # один прогон без отправки
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

from arb_scanner.config import load_config
from arb_scanner.scanner import Scanner


def setup_logging(log_file: str, level: int) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read-only сканер арбитражных спредов крипты")
    p.add_argument("--config", "-c", default="config.yaml", help="путь к YAML-конфигу")
    p.add_argument("--once", action="store_true", help="один прогон и выход")
    p.add_argument("--dry-run", action="store_true", help="не слать в Telegram, только логировать")
    p.add_argument("--test-telegram", action="store_true",
                   help="отправить одно тестовое сообщение в Telegram и выйти")
    p.add_argument("--report", nargs="?", const=24.0, type=float, metavar="ЧАСЫ",
                   help="показать статистику спредов из истории за N часов (по умолч. 24) и выйти")
    p.add_argument("--deposit", "-d", type=float, default=None, metavar="RUB",
                   help="размер депозита в рублях (объём считается под него). Не задан — спросит при запуске")
    p.add_argument("--verbose", "-v", action="store_true", help="подробный лог (DEBUG)")
    return p.parse_args()


def resolve_deposit(args, cfg) -> float:
    """Берёт депозит из флага, иначе из конфига, иначе спрашивает интерактивно."""
    if args.deposit is not None:
        return max(args.deposit, 0.0)
    if cfg.deposit_rub > 0:
        return cfg.deposit_rub
    try:
        raw = input("💰 Сколько у тебя депозит в рублях? (Enter — без ограничения): ").strip()
    except EOFError:
        return 0.0
    if not raw:
        return 0.0
    raw = raw.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        val = max(float(raw), 0.0)
        print(f"✅ Считаю объёмы под депозит {val:,.0f} ₽".replace(",", " "))
        return val
    except ValueError:
        print("⚠️ Не понял сумму — запускаю без ограничения по депозиту.")
        return 0.0


def main() -> int:
    args = parse_args()
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"Ошибка конфига: {e}", file=sys.stderr)
        return 2

    if args.report is not None:
        from arb_scanner.history import build_report
        print(build_report(cfg.db_path, hours=args.report))
        return 0

    setup_logging(cfg.log_file, logging.DEBUG if args.verbose else logging.INFO)

    if not args.test_telegram:
        cfg.deposit_rub = resolve_deposit(args, cfg)

    scanner = Scanner(cfg, dry_run=args.dry_run)

    if args.test_telegram:
        ok = asyncio.run(scanner.test_telegram())
        return 0 if ok else 1

    try:
        asyncio.run(scanner.run(once=args.once))
    except KeyboardInterrupt:
        logging.getLogger("arb").info("остановлено пользователем")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
