import hashlib
from pathlib import Path

GOLDEN_MODES = ("accelerator", "legacy-full", "legacy-ckpt2")
MODES = GOLDEN_MODES + ("cycle", "fpga")
Participant = None


def source_digest(model_path):
    digest = hashlib.sha256()
    root = Path(model_path) / "mm_model"
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def split_sealed_response(payload, wire):
    messages = wire.split(payload)
    if len(messages) < 2 or messages[-1][0] >> 4 != 6:
        raise ValueError("sealed response is missing AUTH")
    auth = wire.decode(messages[-1])
    if messages[-1][0] != 0x62:
        raise ValueError("malformed AUTH header")
    if messages[-2][0] >> 4 != wire.T.END:
        raise ValueError("AUTH must follow END")
    end = wire.decode(messages[-2])
    if auth.seq != end.seq:
        raise ValueError("AUTH seq does not match END")
    orders = messages[:-2]
    if not end.eoe or end["n_orders"] != len(orders):
        raise ValueError("END framing or order count is invalid")
    if any(m[0] >> 4 not in wire.ENTRY_TYPES for m in orders):
        raise ValueError("response carries a non-order message before END")
    return orders, end


class BoardParticipant:
    def __init__(self, link, pid, wire, swap_on_commit=False):
        self.link, self.pid, self.wire = link, pid, wire
        self.frames = 0
        self.rx_seq = 0
        self.hb_count = 0
        self.cpu_commits = []
        self.board_mode = dict(cpuWaitOff=True, swapOnCommit=bool(swap_on_commit))

    def respond(self, payload):
        messages = self.wire.split(payload)
        if any(m[0] >> 4 == self.wire.T.SESSION_START for m in messages):
            self.rx_seq = 0
            self.hb_count = 0
        if any(m[0] >> 4 == self.wire.T.HEARTBEAT for m in messages):
            self.hb_count += 1
        response = self.link.exchange(payload)
        _, end = split_sealed_response(response, self.wire)
        if end.seq != self.rx_seq:
            raise RuntimeError(f"Board response sequence {end.seq} does not match expected {self.rx_seq}")
        self.rx_seq = (self.rx_seq + 1) % self.wire.SEQ_MOD
        self.frames += 1
        return [], [], response.hex(), dict(end.f, rxSeq=end.seq)

    def diagnostics(self):
        return {}
