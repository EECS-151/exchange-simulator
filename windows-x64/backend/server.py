import sys
sys.dont_write_bytecode = True

import argparse
import atexit
from contextlib import contextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import urlsplit

import scenarios
from build import ROOT, build
from fpga_link import BoardGone
from board_relay import GOLDEN_MODES, MODES, BoardParticipant, Participant, source_digest

SHELL_NS = 1800
BOT_STRATEGIES = ("sniper", "maker", "momentum", "mean_reversion", "fpga", "institution", "informed_institution")
MAX_BOTS, MAX_FPGA_BOTS, BOARD_PID = 64, 30, 3
ROUND_MS, PRICE_MS, HEARTBEAT_MS, ORDER_BUDGET, DOWNLINK_PCT = 60000, 400, 100, 64, 30
CYCLE_CLOCK_MHZ = 227.7
MAX_HEARTBEATS, MAX_LEGACY_HEARTBEATS = 65535, 4095


def pace_ns(name, value, low_ms, high_ms):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not low_ms <= value <= high_ms:
        raise ValueError(f"{name} must be a number of ms from {low_ms} through {high_ms}")
    ns = round(value * 1_000_000)
    if ns % 1000:
        raise ValueError(f"{name} must be whole microseconds")
    return ns


def copy_pids(n, taken=()):
    return [p for p in list(range(4, 32)) + [1, 2] if p not in taken and p != BOARD_PID][:n]


def normalize_boards(boards):
    if not isinstance(boards, list) or len(boards) > MAX_FPGA_BOTS:
        raise ValueError(f"boards must be a list of at most {MAX_FPGA_BOTS} boards")
    out, pids = [], {BOARD_PID}
    for board in boards:
        if not isinstance(board, dict):
            raise ValueError("Each board is {name, pid, link}")
        name, pid, link = board.get("name"), board.get("pid"), board.get("link")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
            raise ValueError("A board's name must be 1 to 40 characters")
        if type(pid) is not int or not 1 <= pid <= 31 or pid in pids:
            raise ValueError("Board pids must be distinct integers 1..31, other than the FPGA's 3")
        if not isinstance(link, str) or not (link.startswith("pcap:") or link.startswith("udp:")) or len(link) < 6 or " " in link:
            raise ValueError("A board's link is pcap:<interface> or udp:<host:port>")
        pids.add(pid)
        out.append(dict(name=name.strip(), pid=pid, link=link))
    return out


MAKER_HALF_SPREAD = 40


def normalize_bots(bots, defaults):
    if not isinstance(bots, list) or len(bots) > MAX_BOTS + MAX_FPGA_BOTS:
        raise ValueError(f"bots must be a list of at most {MAX_BOTS} bots and {MAX_FPGA_BOTS} FPGA-strategy copies")
    out = []
    for bot in bots:
        if not isinstance(bot, dict) or bot.get("strategy") not in BOT_STRATEGIES:
            raise ValueError(f"Each bot needs a strategy: one of {', '.join(BOT_STRATEGIES)}")
        default_param = {"maker": defaults["makerHalfSpreadCents"] if defaults else MAKER_HALF_SPREAD, "momentum": 1,
                         "institution": 200, "informed_institution": 200}.get(bot["strategy"], 0)
        latency = bot.get("latency_ns", defaults["reactionNs"] if defaults else None)
        if latency is None:
            raise ValueError("Each bot needs its latency_ns")
        b = dict(strategy=bot["strategy"], latency_ns=latency,
                 jitter_ns=bot.get("jitter_ns", 0), param=bot.get("param", default_param))
        for key in ("latency_ns", "jitter_ns", "param"):
            if type(b[key]) is not int or not 0 <= b[key] <= 100_000_000:
                raise ValueError(f"A bot's {key} must be an integer from 0 through 100000000")
        if b["jitter_ns"] > b["latency_ns"]:
            raise ValueError("A bot's jitter cannot exceed its latency")
        out.append(b)
    if sum(b["strategy"] == "fpga" for b in out) > MAX_FPGA_BOTS:
        raise ValueError(f"At most {MAX_FPGA_BOTS} FPGA-strategy bots")
    if sum(b["strategy"] != "fpga" for b in out) > MAX_BOTS:
        raise ValueError(f"At most {MAX_BOTS} bots besides FPGA-strategy copies")
    return out


class Bridge:
    def __init__(self, model_path, binary=None, bot_defaults=None, preset_profile=False):
        model_path = Path(model_path).resolve()
        model_marker = "params.py" if preset_profile else "golden/txn.py"
        if not (model_path / "mm_model" / model_marker).is_file():
            raise RuntimeError(f"Simulator model support not found in {model_path}")
        sys.path.insert(0, str(model_path))
        import mm_model
        if Path(mm_model.__file__).resolve().parent != model_path / "mm_model":
            raise RuntimeError("A different model checkout is already loaded. Restart the server to change model paths.")
        from mm_model import params, wire
        if preset_profile:
            self.Golden, self.Params, self.CKPT2, self.wire = None, params.Params, None, wire
        self.STOCK151 = getattr(params, "STOCK151", None)
        self.preset_profile = preset_profile
        self.model_path = model_path
        self.model_digest = source_digest(model_path)
        packaged_binary = ROOT / "build" / ("exchange.exe" if os.name == "nt" else "exchange")
        self.binary = binary or (packaged_binary if packaged_binary.is_file() else build(model=model_path))
        self.bot_defaults = bot_defaults
        self.process = None
        self.model = None
        self.link = None
        self.state = None
        self.settings = None
        self._trace = []


    def close(self):
        if self.link:
            self.link.close()
            self.link = None
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=5)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
            self.process = None

    def start(self, *, seed=1, sigma=None, processing_ns=None, propagation_ns=1000, scenario=None,
              model_mode=None, ckpt2=None, bots=None, sniper=None, sniper_ns=None, sniper_jitter_ns=None,
              clock_mhz=CYCLE_CLOCK_MHZ, fpga_transport="pcap", fpga_address=None, fpga_pid=3, pace=True,
              seeded_ties=False, defer=False, sync_flush=False, golden_impl="cpp", boards=(), board_link_impl=None,
              realtime_window_us=None, swap_on_commit=False, price_interval_ms=PRICE_MS, customer_interval_ms=None,
              heartbeat_interval_ms=HEARTBEAT_MS, order_budget=ORDER_BUDGET, downlink_pct=DOWNLINK_PCT, feed_queue=False,
              board_heartbeats_16bit=False, fair_snipers=False, adaptive_link=False):
        if type(seed) is not int or not 0 <= seed <= 0xffffffff:
            raise ValueError("Seed must be an integer from 0 through 4294967295")
        if scenario is not None:
            if not isinstance(scenario, str):
                raise ValueError("scenario must be a preset's name")
            preset = scenarios.find(scenario)
            if bots is not None or (sniper, sniper_ns, sniper_jitter_ns) != (None, None, None):
                raise ValueError("Use a scenario or your own bots, not both")
            bots = scenarios.expand(preset)
            if sigma is None:
                sigma = preset["sigma"]
        if sigma is None:
            sigma = 0.5
        if isinstance(sigma, bool) or not isinstance(sigma, (int, float)) or not math.isfinite(sigma) or not 0 <= sigma <= 10:
            raise ValueError("Standard deviation must be between 0 and 10")
        if model_mode is not None and model_mode not in MODES:
            raise ValueError(f"model_mode must be one of {', '.join(MODES)}")
        tagged = model_mode in ("cycle", "fpga")
        if processing_ns is None:
            processing_ns = SHELL_NS if tagged else 1000
        if bots is not None and (sniper, sniper_ns, sniper_jitter_ns) != (None, None, None):
            raise ValueError("Use bots or the sniper options, not both")
        if bots is None:
            if sniper is None:
                sniper = tagged
            if type(sniper) is not bool:
                raise ValueError("sniper must be true or false")
            if sniper and sniper_ns is None and not self.bot_defaults:
                raise ValueError("Choose a market preset")
            latency = sniper_ns if sniper_ns is not None else self.bot_defaults["reactionNs"] if self.bot_defaults else 0
            legacy = normalize_bots([dict(strategy="sniper", latency_ns=latency,
                                          jitter_ns=0 if sniper_jitter_ns is None else sniper_jitter_ns)], self.bot_defaults)
            bots = legacy if sniper else []
        bots = normalize_bots(bots, self.bot_defaults)
        for delay in (processing_ns, propagation_ns):
            if type(delay) is not int or not 0 <= delay <= 100_000_000:
                raise ValueError("Delays must be integer nanoseconds between 0 and 100000000")
        if golden_impl not in ("cpp", "python"):
            raise ValueError("golden_impl must be cpp or python")
        if board_link_impl is None:
            board_link_impl = "python" if sys.platform == "win32" and fpga_transport == "udp" else "cpp"
        if board_link_impl not in ("cpp", "python"):
            raise ValueError("board_link_impl must be cpp or python")
        boards = normalize_boards(list(boards) if isinstance(boards, (list, tuple)) else boards)
        for name, flag in (("pace", pace), ("seeded_ties", seeded_ties), ("defer", defer), ("sync_flush", sync_flush),
                           ("swap_on_commit", swap_on_commit)):
            if type(flag) is not bool:
                raise ValueError(f"{name} must be true or false")
        if isinstance(clock_mhz, bool) or not isinstance(clock_mhz, (int, float)) or not math.isfinite(clock_mhz) \
                or not 1 <= clock_mhz <= 1000:
            raise ValueError("clock_mhz must be a number of MHz from 1 through 1000")
        if ckpt2 is not None and type(ckpt2) is not bool:
            raise ValueError("ckpt2 must be true or false")
        if model_mode is not None and ckpt2 is not None:
            raise ValueError("Use model_mode or the legacy ckpt2 option, not both")
        mode = model_mode if model_mode is not None else (
            "accelerator" if ckpt2 is None else "legacy-ckpt2" if ckpt2 else "legacy-full")
        if source_digest(self.model_path) != self.model_digest:
            raise RuntimeError("The FPGA model checkout changed while the server was running. Restart backend/server.py to load the new branch.")
        if mode == "fpga":
            if fpga_transport not in ("pcap", "udp"):
                raise ValueError("fpga_transport must be pcap or udp")
            if not isinstance(fpga_address, str) or not fpga_address:
                raise ValueError("fpga mode needs fpga_address: a network interface (pcap) or host:port (udp)")
            if fpga_pid != 3:
                raise ValueError("The exchange trades as pid 3: set the board's switches to 3")
        if mode in ("accelerator", "cycle", "fpga") and self.STOCK151 is None:
            raise RuntimeError("This model checkout has no STOCK151 CPU preset. Update fpga-hft-pipeline-model.")
        p = self.STOCK151 if mode in ("accelerator", "cycle", "fpga") else self.CKPT2 if mode == "legacy-ckpt2" else self.Params()
        copies = sum(b["strategy"] == "fpga" for b in bots)
        if 1 + copies + len(boards) > 31:
            raise ValueError("At most 31 FPGA participants: the FPGA, its copies and the other boards")
        cpp_link = mode == "fpga" and board_link_impl == "cpp"
        window_ns = None
        if realtime_window_us is not None:
            if realtime_window_us == "inf":
                window_ns = -1
            elif isinstance(realtime_window_us, bool) or not isinstance(realtime_window_us, (int, float)) \
                    or not math.isfinite(realtime_window_us) or not 0 <= realtime_window_us <= 10_000_000:
                raise ValueError('realtime_window_us must be a number of µs from 0 through 10000000, or "inf"')
            else:
                window_ns = int(round(realtime_window_us * 1000))
            if not cpp_link and not boards:
                raise ValueError("The real-time mode needs a real board: fpga mode with the C++ link, or boards")
            if not pace or defer or sync_flush:
                raise ValueError("The real-time mode is paced by the wall clock: no pace=false, defer or sync_flush")
        price_ns = pace_ns("price_interval_ms", price_interval_ms, 1, ROUND_MS)
        if price_ns % 2000:
            raise ValueError("price_interval_ms must be an even number of microseconds")
        customer_ns = 0
        if customer_interval_ms is not None:
            customer_ns = pace_ns("customer_interval_ms", customer_interval_ms, 1, ROUND_MS)
            if customer_ns % price_ns:
                raise ValueError("customer_interval_ms must be a multiple of price_interval_ms")
        heartbeat_ns = pace_ns("heartbeat_interval_ms", heartbeat_interval_ms, 1, ROUND_MS)
        heartbeats = ROUND_MS * 1_000_000 // heartbeat_ns + 1
        if (ROUND_MS * 1_000_000) % heartbeat_ns:
            raise ValueError("heartbeat_interval_ms must divide the 60 s round")
        if heartbeats > MAX_HEARTBEATS:
            raise ValueError(f"At most {MAX_HEARTBEATS} heartbeats a round: heartbeat_interval_ms of at least 1")
        if mode.startswith("legacy") and heartbeats > MAX_LEGACY_HEARTBEATS:
            raise ValueError(f"The legacy models count at most {MAX_LEGACY_HEARTBEATS} heartbeats a round")
        if type(order_budget) is not int or not 0 <= order_budget <= 255:
            raise ValueError("order_budget must be an integer from 0 (no limit) through 255")
        if type(downlink_pct) is not int or not 1 <= downlink_pct <= 100:
            raise ValueError("downlink_pct must be an integer from 1 through 100")
        if type(feed_queue) is not bool:
            raise ValueError("feed_queue must be true or false")
        if type(board_heartbeats_16bit) is not bool:
            raise ValueError("board_heartbeats_16bit must be true or false")
        if type(fair_snipers) is not bool:
            raise ValueError("fair_snipers must be true or false")
        if type(adaptive_link) is not bool:
            raise ValueError("adaptive_link must be true or false")
        if adaptive_link and realtime_window_us is None:
            raise ValueError("adaptive_link is for the real-time mode")
        if (mode == "fpga" or boards) and heartbeats > MAX_LEGACY_HEARTBEATS and not board_heartbeats_16bit:
            raise ValueError(f"This pace has {heartbeats} heartbeats. A board built before the 16-bit heartbeat count closes "
                             f"its session at {MAX_LEGACY_HEARTBEATS}, about {MAX_LEGACY_HEARTBEATS * heartbeat_ns // 1_000_000_000} s in, "
                             f"and stops trading. If every board has the 16-bit count, confirm it (board_heartbeats_16bit; "
                             f"the page's \"Boards count 16-bit heartbeats\"); otherwise use heartbeats of 15 ms or more.")
        if cpp_link:
            model = None
            self.close()
        elif mode == "fpga":
            from fpga_link import open_link
            self.close()
            self.link = open_link(fpga_transport, fpga_address, fpga_pid)
            self.link.drain()
            model = (BoardParticipant(link=self.link, pid=fpga_pid, wire=self.wire, swap_on_commit=swap_on_commit)
                     if self.preset_profile else Participant(mode, p, link=self.link, swap_on_commit=swap_on_commit))
        elif mode == "accelerator" and golden_impl == "cpp":
            model = None
            self.close()
        else:
            model = Participant(mode, p)
            self.close()
        self.model, self.params = model, p
        clone_mode = mode if mode in GOLDEN_MODES else "accelerator"
        pids = copy_pids(copies, {b["pid"] for b in boards})
        self.clones = {} if clone_mode == "accelerator" else {
            1 + i: Participant(clone_mode, p, pid=pids[i]) for i in range(copies)}
        self.cpp_boards = [1 + i for i in range(copies)] if clone_mode == "accelerator" else []
        self.settings = dict(seed=seed, sigma=sigma, processing_ns=processing_ns,
                             propagation_ns=propagation_ns, model_mode=mode, bots=bots,
                             sniper=any(b["strategy"] == "sniper" for b in bots), clock_mhz=clock_mhz, pace=pace,
                             seeded_ties=seeded_ties)
        if realtime_window_us is not None:
            self.settings["realtime_window_us"] = realtime_window_us
        if swap_on_commit:
            self.settings["swap_on_commit"] = True
        for key, value, standard in (("price_interval_ms", price_interval_ms, PRICE_MS),
                                     ("customer_interval_ms", customer_interval_ms, None),
                                     ("heartbeat_interval_ms", heartbeat_interval_ms, HEARTBEAT_MS),
                                     ("order_budget", order_budget, ORDER_BUDGET), ("downlink_pct", downlink_pct, DOWNLINK_PCT),
                                     ("feed_queue", feed_queue, False), ("board_heartbeats_16bit", board_heartbeats_16bit, False),
                                     ("fair_snipers", fair_snipers, False), ("adaptive_link", adaptive_link, False)):
            if value != standard:
                self.settings[key] = value
        if scenario is not None:
            self.settings["scenario"] = scenario
        if mode == "accelerator":
            self.settings["golden_impl"] = golden_impl
        if mode == "fpga":
            self.settings.update(fpga_transport=fpga_transport, fpga_address=fpga_address, fpga_pid=fpga_pid,
                                 board_link_impl=board_link_impl)
        if boards:
            self.settings["boards"] = boards
        self._trace = []
        self.board_gone = None
        self.latency = {k: [] for k in ("tag", "busy", "observed", "observedCapture", "heartbeatObserved")}
        self.process = subprocess.Popen([str(self.binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        options = [name for name, on in (("seeded_ties", seeded_ties), ("defer", defer), ("sync_flush", sync_flush),
                                         ("swap_on_commit", swap_on_commit)) if on]
        if mode == "fpga":
            options.append("auth_any")
        if window_ns is not None:
            options.append(f"realtime {window_ns}")
        if (price_ns, customer_ns, heartbeat_ns) != (PRICE_MS * 1_000_000, 0, HEARTBEAT_MS * 1_000_000):
            options.append(f"pace {price_ns} {customer_ns} {heartbeat_ns}")
        if order_budget != ORDER_BUDGET:
            options.append(f"budget {order_budget}")
        if downlink_pct != DOWNLINK_PCT:
            options.append(f"downlink {downlink_pct}")
        if feed_queue:
            options.append("feed_queue")
        if fair_snipers:
            options.append("fair_snipers")
        if adaptive_link:
            options.append("adaptive_link")
        remote = {0: f"{fpga_transport}:{fpga_address}"} if cpp_link else {}
        remote.update({1 + copies + i: b["link"] for i, b in enumerate(boards)})
        cpp = ([0] if model is None and not cpp_link else []) + self.cpp_boards
        options += [f"board {b['pid']} {b['name'].encode().hex()}" for b in boards]
        if cpp or remote:
            options.append("params " + " ".join(str(v) for v in (
                p.HARD_POSITION_LIMIT, p.MAX_ORDER_QTY, p.V0_Q8, p.VOL_SHIFT, p.VOL_CLAMP_HALF_TICKS, p.GAMMA_M,
                p.GAMMA_E, p.LIQ_Q8, p.H_SIGMA_Q8, p.BASE_ORDER_SIZE, p.SOFT_INVENTORY_BAND)))
            options += [f"cpp {k}" for k in cpp] + [f"remote {k} {link}" for k, link in remote.items()]
        return self.command(f"START {seed} {sigma} {processing_ns} {propagation_ns} {clock_mhz} {len(bots)}"
                            + "".join(f" {b['strategy']} {b['latency_ns']} {b['jitter_ns']} {b['param']}" for b in bots)
                            + "".join(f" {o}" for o in options))

    def command(self, command):
        if not self.process or self.process.poll() is not None:
            raise RuntimeError("Exchange is not running")
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        while True:
            line = self.process.stdout.readline()
            if not line:
                error = self.process.stderr.read()
                raise RuntimeError("C++ exchange stopped: " + error)
            if line.startswith("RX "):
                _, index, timestamp, payload = line.split()
                if index != "0":
                    _, _, response, _ = self.clones[int(index)].respond(bytes.fromhex(payload))
                    self.process.stdin.write("TX " + response + "\n")
                    self.process.stdin.flush()
                    continue
                try:
                    records, outputs, response, end = self.model.respond(bytes.fromhex(payload))
                except BoardGone as exc:
                    if self.model.frames == 0:
                        self.process.kill()
                        raise
                    self.board_gone = dict(timeNs=int(timestamp), frame=self.model.frames, reason=str(exc))
                    self._trace.append(dict(timeNs=int(timestamp), rx=payload, boardGone=str(exc)))
                    self.process.stdin.write("TX GONE\n")
                    self.process.stdin.flush()
                    continue
                except Exception:
                    self.process.kill()
                    raise
                observed = self.link.last_timing if self.settings["model_mode"] == "fpga" else None
                self._trace.append(dict(timeNs=int(timestamp), rx=payload, tx=outputs, records=records,
                                       cpuCommits=self.model.cpu_commits, end=end, response=response,
                                       observed=observed))
                self._sample_latency(payload, end, observed)
                self.process.stdin.write("TX " + response + "\n")
                self.process.stdin.flush()
            elif line.startswith("STATE "):
                state = json.loads(line[6:])
                if self.model is None:
                    status = state["participants"][0]["status"]
                    state["golden"] = {k: status[k] for k in ("inventory", "cash", "halted", "haltReason", "heartbeat")}
                elif self.preset_profile and self.settings["model_mode"] == "fpga":
                    player = next(p for p in state["book"]["players"] if p["name"] == "FPGA Golden")
                    state["golden"] = dict(inventory=player["inventory"], cash=player["cash"], halted=False,
                                           haltReason=0, heartbeat=self.model.hb_count)
                else:
                    state["golden"] = dict(inventory=self.model.inventory, cash=self.model.cash,
                                           halted=self.model.halted, haltReason=int(self.model.halt_reason),
                                           heartbeat=self.model.hb_count)
                state["settings"] = self.settings
                state["latency"] = self.latency_summary()
                if self.board_gone:
                    state["board"]["reason"] = self.board_gone["reason"]
                if self.model is None and self.settings["model_mode"] == "fpga":
                    status = state["participants"][0]["status"]
                    state["latency"] = dict(status["latency"], clockMhz=400, tagTickNs=2.5)
                    if status["goneReason"]:
                        state["board"]["reason"] = status["goneReason"]
                elif self.preset_profile and self.settings["model_mode"] == "fpga":
                    state["latency"] = self.latency_summary()
                    state["participants"][0]["status"] = dict(
                        boardMode=self.model.board_mode, divergence=None, goneReason=None,
                        latency=state["latency"])
                if self.settings.get("model_mode") == "fpga":
                    for player in state["book"]["players"]:
                        if player["name"] == "FPGA Golden":
                            player["name"] = "FPGA board"
                    for order in state["book"]["orders"]:
                        if order["bot"] == "FPGA Golden":
                            order["bot"] = "FPGA board"
                    for event in state["book"]["events"]:
                        event["details"] = event["details"].replace("FPGA Golden", "FPGA board")
                if command == "LOG":
                    state.setdefault("trace", self._trace)
                    state["modelParams"] = asdict(self.params)
                    state["modelSource"] = {"sha256": self.model_digest, "path": str(self.model_path)}
                    if self.model is None:
                        status = state["participants"][0]["status"]
                        state["cpuDiagnostics"] = {k: status[k] for k in ("activeEpoch", "activeValid", "missedCommits", "staleCommits")}
                        if "divergence" in status:
                            state["cpuDiagnostics"]["divergence"] = status["divergence"]
                    else:
                        state["cpuDiagnostics"] = self.model.diagnostics()
                else:
                    self.state = state
                return state
            else:
                raise RuntimeError("Unexpected exchange response")

    def _sample_latency(self, payload, end, observed):
        if self.settings["model_mode"] not in ("cycle", "fpga"):
            return
        ns = lambda ticks: ticks * 2.5 if self.settings["model_mode"] == "fpga" else ticks * 1000 / self.settings["clock_mhz"]
        wire = self.model.wire
        waits_for_cpu = any(m[0] >> 4 in (wire.T.HEARTBEAT, wire.T.SESSION_START)
                            for m in wire.split(bytes.fromhex(payload)))
        self.latency["busy"].append(ns(end["busy"]))
        if end["n_orders"]:
            self.latency["tag"].append(ns(end["tag"]))
        if observed:
            self.latency["heartbeatObserved" if waits_for_cpu else "observed"].append(observed["hostNs"])
            if observed["captureNs"] is not None and not waits_for_cpu:
                self.latency["observedCapture"].append(observed["captureNs"])

    def latency_summary(self):
        if self.settings.get("model_mode") not in ("cycle", "fpga"):
            return None

        def summary(values):
            if not values:
                return None
            v = sorted(values)
            return dict(n=len(v), median=v[len(v) // 2], p99=v[min(len(v) - 1, (len(v) * 99) // 100)], max=v[-1])
        out = dict(clockMhz=400, tagTickNs=2.5) if self.settings["model_mode"] == "fpga" else dict(clockMhz=self.settings["clock_mhz"])
        out.update({f"{k}Ns": summary(v) for k, v in self.latency.items()})
        return out

    @property
    def trace(self):
        if self.model is None and self.process is not None:
            return self.log().get("trace", [])
        return self._trace

    def advance(self, elapsed_ms, budget_ms=None):
        if not math.isfinite(elapsed_ms) or elapsed_ms < 0:
            raise ValueError("Invalid elapsed time")
        budget = f" {budget_ms}" if budget_ms else ""
        return self.command(f"ADVANCE {int(min(elapsed_ms, 60000) * 1_000_000)}{budget}")

    def log(self):
        return self.command("LOG")


ADVANCE_BUDGET_MS = 50


class Application:
    def __init__(self, model_path, bot_defaults=None, role="preset"):
        self.lock = threading.RLock()
        self.turnstile = threading.Lock()
        self.bridge = Bridge(model_path, bot_defaults=bot_defaults, preset_profile=role == "preset")
        self.role = role
        self.started = None
        self.stopped = False
        self.error = None
        self.stop = threading.Event()
        self.bridge.start()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        busy = False
        while not self.stop.wait(0 if busy else 0.025):
            with self.turnstile:
                pass
            with self.lock:
                busy = (self.bridge.settings.get("realtime_window_us") is not None and self.started is not None
                        and not self.bridge.state["complete"] and not self.error)
                if self.started is not None and not self.bridge.state["complete"] and not self.error:
                    elapsed = (time.monotonic() - self.started) * 1000 if self.bridge.settings["pace"] else 60000
                    try:
                        self.bridge.advance(elapsed, ADVANCE_BUDGET_MS)
                    except Exception as exc:
                        self.error = str(exc)

    @contextmanager
    def exclusive(self):
        with self.turnstile:
            self.lock.acquire()
        try:
            yield
        finally:
            self.lock.release()

    def snapshot(self):
        error, state, started = self.error, self.bridge.state, self.started
        if error:
            return None, error
        state = dict(state)
        state["running"] = started is not None and not state["complete"]
        if self.stopped:
            state["stopped"] = True
        if state["running"] and self.bridge.settings["pace"]:
            wall = min(60000.0, (time.monotonic() - started) * 1000)
            state["lagMs"] = max(0.0, wall - state["elapsedMs"])
        return state, None

    def stop_round(self):
        with self.exclusive():
            if self.started is not None and not self.bridge.state["complete"]:
                self.started = None
                self.stopped = True
        return self.snapshot()

    def close(self):
        self.stop.set()
        with self.exclusive():
            self.bridge.close()


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, data, content_type="application/json"):
            if not isinstance(data, bytes):
                data = json.dumps(data, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/config":
                return self.send(200, {"role": app.role})
            if path == "/api/scenarios":
                return self.send(200, scenarios.catalog(describe=app.role != "preset"))
            if path == "/api/state":
                state, error = app.snapshot()
                return self.send(500, {"error": error}) if error else self.send(200, state)
            if path == "/api/log":
                with app.exclusive():
                    if app.error:
                        return self.send(500, {"error": app.error})
                    state = app.bridge.log()
                    state["running"] = app.started is not None and not state["complete"]
                    return self.send(200, state)
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/index.html": ("index.html", "text/html; charset=utf-8"),
                      "/style.css": ("style.css", "text/css"),
                      "/app.js": ("app.js", "text/javascript"),
                      "/market-ui.js": ("market-ui.js", "text/javascript")}
            if path not in assets:
                return self.send(404, {"error": "Not found"})
            file, mime = assets[path]
            return self.send(200, (ROOT / file).read_bytes(), mime)

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in ("/api/start", "/api/stop"):
                return self.send(404, {"error": "Not found"})
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers.get('Host')}":
                return self.send(403, {"error": "Use the local simulator page"})
            if path == "/api/stop":
                state, error = app.stop_round()
                return self.send(500, {"error": error}) if error else self.send(200, state)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > 65536:
                    raise ValueError("Invalid request size")
                settings = json.loads(self.rfile.read(length))
                if not isinstance(settings, dict):
                    raise ValueError("Expected settings object")
                if app.role == "preset":
                    scenario = settings.get("scenario")
                    if not isinstance(scenario, str):
                        raise ValueError("Choose a market preset before starting a preset round")
                    preset = scenarios.find(scenario)
                    address = settings.get("fpga_address")
                    transport = settings.get("fpga_transport", "pcap")
                    if not isinstance(address, str) or not address.strip():
                        raise ValueError("Set the board link before starting")
                    if transport not in ("pcap", "udp"):
                        raise ValueError("fpga_transport must be pcap or udp")
                    settings = dict(seed=settings.get("seed", 1), scenario=scenario, sigma=preset["sigma"],
                                    model_mode="fpga", fpga_transport=transport, fpga_address=address.strip(),
                                    processing_ns=SHELL_NS,
                                    propagation_ns=settings.get("propagation_ns", 500_000),
                                    price_interval_ms=20, heartbeat_interval_ms=10, order_budget=64,
                                    downlink_pct=100, feed_queue=True, board_heartbeats_16bit=True,
                                    fair_snipers=True, realtime_window_us="inf")
                else:
                    settings.pop("clock_mhz", None)
                with app.exclusive():
                    state = app.bridge.start(**settings)
                    app.started = time.monotonic()
                    app.stopped = False
                    app.error = None
                    state["running"] = True
                self.send(200, state)
            except (ValueError, TypeError) as exc:
                self.send(400, {"error": str(exc)})
            except Exception as exc:
                app.error = str(exc)
                self.send(500, {"error": str(exc)})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--role", choices=("preset",), default="preset",
                        help="use the fixed simulator controls")
    bundled_model = ROOT / "model"
    parser.add_argument("--model", type=Path,
                        default=bundled_model if (bundled_model / "mm_model" / "params.py").is_file()
                        else ROOT.parent / "fpga-hft-pipeline-model")
    args = parser.parse_args()
    bot_defaults = None
    app = Application(args.model, bot_defaults, role=args.role)
    atexit.register(app.close)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(app))
    print(f"Trading simulator ({args.role}): http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
