import re

SCENARIOS = [
    dict(name="calm", title="Calm blue chip", sigma=0.2,
         bots=[("maker", "10us", 4), ("maker", "25us", 8), ("maker", "60us", 16), ("sniper", "3us"),
               ("mean_reversion", "200us"), ("mean_reversion", "1ms"), ("institution", "2ms"), ("institution", "2ms")]),
    dict(name="busy", title="Busy large-cap", sigma=0.5,
         bots=[("maker", "2us", 1), ("maker", "4us", 2), ("maker", "8us", 4), ("maker", "20us", 8), ("sniper", "2.2us"), ("sniper", "2.4us"),
               ("momentum", "20us"), ("momentum", "100us"), ("mean_reversion", "50us"), ("mean_reversion", "500us"),
               ("institution", "2ms"), ("institution", "2ms"), ("institution", "2ms"), ("informed_institution", "2ms")]),
    dict(name="hft", title="HFT arms race", sigma=0.5,
         bots=[("maker", "1.6us", 1), ("maker", "2us", 2), ("maker", "2.4us", 3), ("maker", "3us", 4), ("sniper", "2.2us"),
               ("sniper", "2.2us"), ("sniper", "2.2us"), ("sniper", "2.4us"), ("momentum", "4us")]),
    dict(name="news", title="News shock", sigma=1.5,
         bots=[("maker", "4us", 5), ("maker", "15us", 10), ("sniper", "2.2us"), ("sniper", "2.2us"), ("sniper", "2.6us"),
               ("momentum", "10us"), ("momentum", "30us"), ("momentum", "100us"), ("informed_institution", "2ms"),
               ("informed_institution", "2ms"), ("informed_institution", "2ms"), ("institution", "2ms")]),
    dict(name="rebalance", title="Index rebalance", sigma=0.3,
         bots=[("maker", "3us", 2), ("maker", "10us", 5), ("maker", "40us", 10), ("sniper", "2.2us")]
              + [("institution", "3ms", 300)] * 6),
    dict(name="thin", title="Thin small-cap", sigma=0.8,
         bots=[("maker", "50us", 20), ("momentum", "1ms"), ("mean_reversion", "2ms"), ("institution", "2ms", 100)]),
]


def latency_ns(text):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ns|us|ms)", text)
    if not m:
        raise ValueError(f"bad latency {text!r}")
    return round(float(m.group(1)) * {"ns": 1, "us": 1_000, "ms": 1_000_000}[m.group(2)])


TAKER_FLOOR_NS = 2200
TAKES = ("maker", "momentum", "mean_reversion")


def expand(scenario):
    out = []
    for bot in scenario["bots"]:
        strategy, latency, *param = bot
        b = dict(strategy=strategy, latency_ns=latency_ns(latency))
        if param:
            b["param"] = param[0]
        out.append(b)
    return out + [dict(strategy="sniper", latency_ns=max(b["latency_ns"], TAKER_FLOOR_NS)) for b in out if b["strategy"] in TAKES]


def catalog(describe=False):
    notes = {}
    if describe:
        try:
            from scenario_notes import DESCRIPTIONS as notes
        except ImportError:
            pass
    out = []
    for s in SCENARIOS:
        preset = dict(name=s["name"], title=s["title"], sigma=s["sigma"], bots=expand(s))
        if s["name"] in notes:
            preset["description"] = notes[s["name"]]
        out.append(preset)
    return out


def find(name):
    for s in SCENARIOS:
        if s["name"] == name:
            return s
    raise ValueError(f"scenario must be one of {', '.join(s['name'] for s in SCENARIOS)}")
