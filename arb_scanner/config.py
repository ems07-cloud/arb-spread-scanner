"""Загрузка и валидация YAML-конфига."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml


@dataclass(slots=True)
class Fees:
    taker: float = 0.001          # доля, напр. 0.001 = 0.1%
    maker: float = 0.001
    p2p: float = 0.0              # сбор P2P-площадки (доля)
    payment_cost: float = 0.0     # издержки платёжного метода (доля)


@dataclass(slots=True)
class VenueKeys:
    """READ-ONLY ключи. Два способа задать (ключи без прав trade/withdraw!):

    1) Безопасно: *_env — ИМЕНА переменных окружения, где лежат ключи.
    2) Просто: api_key/api_secret — сами значения прямо в конфиге
       (тогда НЕ коммить config.yaml в git — это секреты).
    """
    api_key_env: str | None = None
    api_secret_env: str | None = None
    passphrase_env: str | None = None  # нужен только для OKX
    api_key: str | None = None         # прямое значение (вместо api_key_env)
    api_secret: str | None = None      # прямое значение (вместо api_secret_env)
    passphrase: str | None = None      # прямое значение (OKX)


@dataclass(slots=True)
class Venue:
    name: str
    adapter: str                  # ключ в реестре адаптеров
    kind: str                     # 'spot' | 'p2p'
    endpoint: str | None = None   # переопределение базового URL (опционально)
    rate_min_interval: float = 0.2  # минимальный интервал между запросами к хосту, сек
    fees: Fees = field(default_factory=Fees)
    keys: VenueKeys | None = None   # read-only ключи для подтягивания живых комиссий
    fee_provider: str | None = None  # 'binance' | 'okx' | 'bybit' (по умолчанию = из adapter)
    # Поддерживаемые сети перевода: либо список (для всех монет), либо {coin: [сети]}.
    # Нужно, чтобы отсеивать неисполнимые маршруты (вывод по сети, которую приёмник не
    # принимает). Не задано -> берётся встроенная таблица netcompat (иначе «не подтверждено»).
    networks: dict | list | None = None


@dataclass(slots=True)
class Leg:
    venue: str                    # имя площадки
    side: str                     # 'buy' | 'sell' (проставляется маршрутом)
    params: dict = field(default_factory=dict)


@dataclass(slots=True)
class ChainLeg:
    """Шаг многоногого маршрута: конвертация give -> get на площадке."""
    venue: str
    op: str                          # 'buy' (трачу give=quote, получаю get=base) | 'sell' (наоборот)
    give: str                        # валюта на входе шага
    get: str                         # валюта на выходе шага
    params: dict = field(default_factory=dict)
    transfer_fee_coin: float = 0.0   # сетевой сбор за перевод get-актива (в его единицах)
    network: str | None = None       # сеть для live-комиссии вывода get-актива


@dataclass(slots=True)
class Route:
    name: str
    base: str
    quote: str
    size_base: float
    buy: Leg | None = None
    sell: Leg | None = None
    network_fee_coin: float = 0.0  # запасное значение сетевой комиссии, если live недоступна
    network: str | None = None     # сеть/чейн вывода базы (напр. 'BTC', 'TRX', 'BSC'); None = дефолтная
    # Многоногий вариант (вместо buy/sell): цепочка-цикл, начинается и кончается в одной валюте.
    legs: list[ChainLeg] | None = None
    start_currency: str | None = None
    start_amount: float = 0.0
    poll_interval: float | None = None  # своя частота опроса маршрута (None = глобальная)
    max_size: float | None = None       # верхняя граница авто-подбора объёма (None = только по глубине)

    @property
    def is_chain(self) -> bool:
        return bool(self.legs)


@dataclass(slots=True)
class Thresholds:
    min_net_spread_pct: float = 1.0
    min_abs_profit: float = 10.0
    cooldown_seconds: float = 900.0
    realert_improve_pct: float = 0.5  # повторить алерт раньше кулдауна, если спред вырос на столько п.п.
    confirmations: int = 1            # окно должно пройти пороги N опросов подряд (анти-фликер)
    max_net_spread_pct: float = 0.0   # потолок «разумного» спреда: выше = выброс данных, не алертим (0 = выкл)
    p2p_outlier_pct: float = 0.0      # отбрасывать P2P-офферы с ценой дальше N от медианы стороны (0 = выкл)
    # --- режим «железобетон»: слать ТОЛЬКО достоверные, реально исполнимые окна ---
    reliable_only: bool = True        # True = слать в Telegram только окна, прошедшие все проверки достоверности
    solid_max_spread_pct: float = 8.0     # выше этого спред для RU-P2P почти всегда иллюзорен -> не «железобетон»
    solid_min_depth_ratio: float = 1.5    # доступная глубина должна покрывать объём минимум в N раз
    solid_min_completion: float = 0.97    # минимальный % исполнения у мерчанта P2P-ноги
    solid_min_orders: float = 50.0        # минимум завершённых сделок у мерчанта P2P-ноги
    solid_max_merchants: int = 3          # макс. число отдельных P2P-сделок на ногу (больше = не исполнить)
    allow_no_depth: bool = False          # пускать окна с непроверяемой глубиной (напр. Rapira) с пометкой


@dataclass(slots=True)
class Telegram:
    enabled: bool = True
    bot_token: str = ""
    chat_id: str = ""


@dataclass(slots=True)
class HttpCfg:
    timeout: float = 10.0
    max_retries: int = 3
    concurrency: int = 8
    user_agent: str = "arb-scanner/0.1 (+read-only)"


@dataclass(slots=True)
class AppConfig:
    poll_interval: float
    log_file: str
    db_path: str
    http: HttpCfg
    thresholds: Thresholds
    telegram: Telegram
    venues: dict[str, Venue]
    routes: list[Route]
    fee_refresh_seconds: float = 1800.0  # как часто обновлять живые комиссии по ключам
    heartbeat_seconds: float = 0.0       # период «я жив» в Telegram (0 = выкл)
    health_fail_threshold: int = 5       # сколько ошибок подряд до алерта «площадка недоступна»
    history_enabled: bool = True         # писать ВСЕ замеры спреда в SQLite (для --report)
    history_retention_days: float = 14.0  # сколько хранить историю спредов
    autosize: bool = False               # подбирать макс. объём при спреде >= порога (показывать в алерте)
    deposit_rub: float = 0.0             # размер депозита в RUB: объём считается под него (0 = без ограничения)
    pay_methods: list = field(default_factory=list)  # банки/способы оплаты пользователя (фильтр всех P2P-ног)
    daily_summary_seconds: float = 0.0   # период сводки из истории в TG (0 = выкл; 86400 = раз в сутки)


def _secret(direct, env_name: str | None, default_env: str) -> str:
    """Значение секрета: прямое из конфига -> иначе из переменной окружения.

    direct       — значение прямо в YAML (не рекомендуется: попадёт в файл/git).
    env_name     — явное имя переменной окружения из YAML (*_env).
    default_env  — имя по умолчанию, если в YAML ничего не задано.
    """
    if direct:
        return str(direct)
    return os.environ.get(env_name or default_env, "")


def _fees(d: dict) -> Fees:
    d = d or {}
    return Fees(
        taker=float(d.get("taker", 0.001)),
        maker=float(d.get("maker", 0.001)),
        p2p=float(d.get("p2p", 0.0)),
        payment_cost=float(d.get("payment_cost", 0.0)),
    )


def _keys(d: dict) -> VenueKeys | None:
    if not d:
        return None
    return VenueKeys(
        api_key_env=d.get("api_key_env"),
        api_secret_env=d.get("api_secret_env"),
        passphrase_env=d.get("passphrase_env"),
        api_key=d.get("api_key"),
        api_secret=d.get("api_secret"),
        passphrase=d.get("passphrase"),
    )


def _leg(d: dict, side: str) -> Leg:
    if not d or "venue" not in d:
        raise ValueError(f"в ноге '{side}' нет обязательного поля 'venue'")
    params = {k: v for k, v in d.items() if k != "venue"}
    return Leg(venue=str(d["venue"]), side=side, params=params)


_CHAIN_RESERVED = {"venue", "op", "give", "get", "transfer_fee_coin", "network"}


def _chain_leg(d: dict, idx: int) -> ChainLeg:
    for f in ("venue", "op", "give", "get"):
        if f not in d:
            raise ValueError(f"шаг #{idx} цепочки: нет обязательного поля {f!r}")
    op = str(d["op"])
    if op not in ("buy", "sell"):
        raise ValueError(f"шаг #{idx}: op должен быть 'buy' или 'sell', а не {op!r}")
    params = {k: v for k, v in d.items() if k not in _CHAIN_RESERVED}
    return ChainLeg(
        venue=str(d["venue"]), op=op, give=str(d["give"]), get=str(d["get"]),
        params=params,
        transfer_fee_coin=float(d.get("transfer_fee_coin", 0.0)),
        network=d.get("network"),
    )


def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    venues: dict[str, Venue] = {}
    for name, v in (raw.get("venues") or {}).items():
        kind = str(v.get("kind", "spot"))
        if kind not in ("spot", "p2p"):
            raise ValueError(f"площадка {name}: kind должен быть 'spot' или 'p2p', а не {kind!r}")
        venues[name] = Venue(
            name=name,
            adapter=str(v["adapter"]),
            kind=kind,
            endpoint=v.get("endpoint"),
            rate_min_interval=float(v.get("rate_min_interval", 0.2)),
            fees=_fees(v.get("fees")),
            keys=_keys(v.get("keys")),
            fee_provider=v.get("fee_provider"),
            networks=v.get("networks"),
        )
    if not venues:
        raise ValueError("в конфиге не задано ни одной площадки (venues)")

    routes: list[Route] = []
    for r in (raw.get("routes") or []):
        name = str(r["name"])
        rpi = r.get("poll_interval")
        rpi = float(rpi) if rpi is not None else None
        rms = r.get("max_size")
        rms = float(rms) if rms is not None else None
        if r.get("legs"):
            legs = [_chain_leg(d, i) for i, d in enumerate(r["legs"])]
            start_cur = str(r.get("start_currency") or legs[0].give)
            route = Route(
                name=name,
                base=start_cur, quote=start_cur,
                size_base=float(r.get("start_amount", r.get("size_base", 0.0))),
                legs=legs,
                start_currency=start_cur,
                start_amount=float(r.get("start_amount", r.get("size_base", 0.0))),
                poll_interval=rpi,
            )
            # валидация цепочки: непрерывность валют и цикл
            cur = start_cur
            for i, lg in enumerate(legs):
                if lg.venue not in venues:
                    raise ValueError(f"маршрут {name}: неизвестная площадка {lg.venue!r}")
                if lg.give != cur:
                    raise ValueError(
                        f"маршрут {name}, шаг #{i}: вход {lg.give!r} != выход предыдущего {cur!r}")
                cur = lg.get
            if cur != start_cur:
                raise ValueError(
                    f"маршрут {name}: цепочка не цикл — кончается на {cur!r}, а старт {start_cur!r}")
            if route.start_amount <= 0:
                raise ValueError(f"маршрут {name}: задай start_amount > 0")
        else:
            route = Route(
                name=name,
                base=str(r["base"]),
                quote=str(r["quote"]),
                size_base=float(r["size_base"]),
                buy=_leg(r.get("buy"), "buy"),
                sell=_leg(r.get("sell"), "sell"),
                network_fee_coin=float(r.get("network_fee_coin", 0.0)),
                network=r.get("network"),
                poll_interval=rpi,
                max_size=rms,
            )
            for leg in (route.buy, route.sell):
                if leg.venue not in venues:
                    raise ValueError(f"маршрут {name}: неизвестная площадка {leg.venue!r}")
        routes.append(route)

    # Авто-генерация маршрутов из блока `auto` (если включён).
    auto = raw.get("auto")
    if auto and auto.get("enabled"):
        from .autoroutes import generate as _gen_auto
        existing = {r.name for r in routes}
        gen = _gen_auto(auto, venues, existing)
        for r in gen:
            for leg in (r.buy, r.sell):
                if leg.venue not in venues:
                    raise ValueError(f"авто-маршрут {r.name}: неизвестная площадка {leg.venue!r}")
                if venues[leg.venue].kind == "spot" and not leg.params.get("symbol"):
                    raise ValueError(f"авто-маршрут {r.name}: не задан символ спот-пары у {leg.venue}")
        routes.extend(gen)

    if not routes:
        raise ValueError("в конфиге не задано ни одного маршрута (routes)")

    # Глобальный фильтр банков пользователя: прокидываем во ВСЕ P2P-ноги, где не задан свой.
    # Так бот считает прибыль и шлёт окна только по офферам, оплачиваемым банками клиента.
    pay_methods = raw.get("pay_methods") or []
    if pay_methods:
        def _inject(leg):
            v = venues.get(leg.venue)
            if v and v.kind == "p2p" and "pay_methods" not in leg.params:
                leg.params["pay_methods"] = list(pay_methods)
        for r in routes:
            if r.is_chain:
                for lg in r.legs:
                    _inject(lg)
            else:
                _inject(r.buy)
                _inject(r.sell)

    th = raw.get("thresholds") or {}
    tg = raw.get("telegram") or {}
    http = raw.get("http") or {}

    return AppConfig(
        poll_interval=float(raw.get("poll_interval", 30.0)),
        log_file=str(raw.get("log_file", "arb_scanner.log")),
        db_path=str(raw.get("db_path", "arb_state.sqlite")),
        http=HttpCfg(
            timeout=float(http.get("timeout", 10.0)),
            max_retries=int(http.get("max_retries", 3)),
            concurrency=int(http.get("concurrency", 8)),
            user_agent=str(http.get("user_agent", "arb-scanner/0.1 (+read-only)")),
        ),
        thresholds=Thresholds(
            min_net_spread_pct=float(th.get("min_net_spread_pct", 1.0)),
            min_abs_profit=float(th.get("min_abs_profit", 10.0)),
            cooldown_seconds=float(th.get("cooldown_seconds", 900.0)),
            realert_improve_pct=float(th.get("realert_improve_pct", 0.5)),
            confirmations=int(th.get("confirmations", 1)),
            max_net_spread_pct=float(th.get("max_net_spread_pct", 0.0)),
            p2p_outlier_pct=float(th.get("p2p_outlier_pct", 0.0)),
            reliable_only=bool(th.get("reliable_only", True)),
            solid_max_spread_pct=float(th.get("solid_max_spread_pct", 8.0)),
            solid_min_depth_ratio=float(th.get("solid_min_depth_ratio", 1.5)),
            solid_min_completion=float(th.get("solid_min_completion", 0.97)),
            solid_min_orders=float(th.get("solid_min_orders", 50.0)),
            solid_max_merchants=int(th.get("solid_max_merchants", 3)),
            allow_no_depth=bool(th.get("allow_no_depth", False)),
        ),
        telegram=Telegram(
            enabled=bool(tg.get("enabled", True)),
            bot_token=_secret(tg.get("bot_token"), tg.get("bot_token_env"), "ARB_TG_BOT_TOKEN"),
            chat_id=_secret(tg.get("chat_id"), tg.get("chat_id_env"), "ARB_TG_CHAT_ID"),
        ),
        venues=venues,
        routes=routes,
        fee_refresh_seconds=float(raw.get("fee_refresh_seconds", 1800.0)),
        heartbeat_seconds=float(raw.get("heartbeat_seconds", 0.0)),
        health_fail_threshold=int(raw.get("health_fail_threshold", 5)),
        history_enabled=bool(raw.get("history_enabled", True)),
        history_retention_days=float(raw.get("history_retention_days", 14.0)),
        autosize=bool(raw.get("autosize", False)),
        deposit_rub=float(raw.get("deposit_rub", 0.0)),
        pay_methods=list(pay_methods),
        daily_summary_seconds=float(raw.get("daily_summary_seconds", 0.0)),
    )
