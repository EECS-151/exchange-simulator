
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, IntFlag

MSG_BYTES = 8
MSG_BITS = 8 * MSG_BYTES
MIN_PAYLOAD = 48
MAX_MSGS_PER_FRAME = 8
ETHERTYPE_TO_FPGA = 0x88B5
ETHERTYPE_TO_EXCHANGE = 0x88B6

PX_MIN, PX_MAX = 1, (1 << 14) - 1
SEQ_MOD = 256


class T(IntEnum):
    PAD = 0x0
    SESSION_START = 0x1
    HEARTBEAT = 0x2
    BOOK_UPDATE = 0x3
    TRADE = 0x4
    ACK = 0x8
    REJECT = 0x9
    FILL = 0xA
    CANCEL_ACK = 0xB
    NEW = 0xC
    CANCEL = 0xD
    REPLACE = 0xE
    CANCEL_ALL = 0xF
    END = 0x7


PUBLIC_TYPES = frozenset((T.SESSION_START, T.HEARTBEAT, T.BOOK_UPDATE, T.TRADE))
PRIVATE_TYPES = frozenset((T.ACK, T.REJECT, T.FILL, T.CANCEL_ACK))
ENTRY_TYPES = frozenset((T.NEW, T.CANCEL, T.REPLACE, T.CANCEL_ALL))
TELEMETRY_TYPES = frozenset((T.END,))


class RejectReason(IntEnum):
    BAD_PID = 1
    DUP_ID = 2
    BAND = 3
    LIMIT = 4
    RATE = 5
    WOULD_CROSS = 6
    CLOSED = 7
    UNKNOWN = 8
    BAD_MSG = 9


class EndStatus(IntFlag):
    CPU_TIMEOUT = 1
    LOG_OVERFLOW = 2
    SATURATED = 4
    UNSOLICITED = 8
    MULTI_FRAME = 16
    CPU_WAIT_OFF = 32
    SWAP_ON_COMMIT = 64


LAYOUT: dict[int, list[tuple[str, int, int, bool]]] = {
    T.SESSION_START: [("n_hb_total", 2, 2, False), ("max_pos", 4, 1, False),
                      ("max_order_qty", 5, 1, False), ("max_msgs_per_hb", 6, 1, False)],
    T.HEARTBEAT: [("hb_count", 2, 2, False)],
    T.BOOK_UPDATE: [("px", 2, 2, False), ("level_qty", 4, 2, False)],
    T.TRADE: [("px", 2, 2, False), ("qty", 4, 2, False)],
    T.ACK: [("clordid", 2, 3, False), ("leaves", 5, 1, False)],
    T.REJECT: [("clordid", 2, 3, False), ("reason", 5, 1, False)],
    T.FILL: [("clordid", 2, 3, False), ("qty", 5, 1, False), ("position_after", 6, 1, True)],
    T.CANCEL_ACK: [("clordid", 2, 3, False)],
    T.NEW: [("clordid", 2, 3, False), ("px", 5, 2, False), ("qty", 7, 1, False)],
    T.CANCEL: [("clordid", 2, 3, False)],
    T.REPLACE: [("clordid", 2, 3, False), ("px", 5, 2, False), ("total_qty", 7, 1, False)],
    T.CANCEL_ALL: [],
    T.END: [("n_orders", 2, 1, False), ("status", 3, 1, False), ("tag", 4, 2, False), ("busy", 6, 2, False)],
}

MAX_RESPONSE_ORDERS = MAX_MSGS_PER_FRAME - 1
CYCLES_MAX = (1 << 16) - 1


@dataclass
class Msg:
    type: int
    seq: int = 0
    side: int = 0
    eoe: bool = False
    f: dict = field(default_factory=dict)

    def __getitem__(self, name):
        return self.f.get(name, 0)

    @property
    def flags(self) -> int:
        return (self.side & 1) | (int(self.eoe) << 1)


def encode(m: Msg) -> bytes:
    b = bytearray(MSG_BYTES)
    b[0] = ((int(m.type) & 0xF) << 4) | m.flags
    b[1] = m.seq % SEQ_MOD
    for name, off, size, signed in LAYOUT.get(int(m.type), []):
        v = int(m.f.get(name, 0))
        if signed:
            lo, hi = -(1 << (8 * size - 1)), (1 << (8 * size - 1)) - 1
        else:
            lo, hi = 0, (1 << (8 * size)) - 1
        if not lo <= v <= hi:
            raise OverflowError(f"{T(m.type).name}.{name}={v} out of range")
        b[off:off + size] = v.to_bytes(size, "big", signed=signed)
    return bytes(b)


def decode(b: bytes) -> Msg:
    assert len(b) == MSG_BYTES
    t = b[0] >> 4
    m = Msg(type=t, seq=b[1], side=b[0] & 1, eoe=bool(b[0] & 2))
    for name, off, size, signed in LAYOUT.get(t, []):
        m.f[name] = int.from_bytes(b[off:off + size], "big", signed=signed)
    return m


def pack(msgs: list[Msg]) -> bytes:
    assert 1 <= len(msgs) <= MAX_MSGS_PER_FRAME
    p = b"".join(encode(m) for m in msgs)
    return p + bytes(max(0, MIN_PAYLOAD - len(p)))


def split(payload: bytes) -> list[bytes]:
    out = []
    for i in range(0, len(payload) - MSG_BYTES + 1, MSG_BYTES):
        chunk = payload[i:i + MSG_BYTES]
        if chunk[0] >> 4 == T.PAD:
            break
        out.append(bytes(chunk))
    return out



def make_end(rx_seq: int, n_orders: int = 0, tag: int = 0, busy: int = 0, status: int = 0) -> Msg:
    if tag > CYCLES_MAX or busy > CYCLES_MAX:
        status |= EndStatus.SATURATED
    return Msg(T.END, seq=rx_seq, eoe=True,
               f={"n_orders": n_orders, "status": int(status), "tag": min(tag, CYCLES_MAX),
                  "busy": min(busy, CYCLES_MAX)})


def split_response(payload: bytes) -> tuple[list[bytes], Msg]:
    msgs = split(payload)
    if not msgs or msgs[-1][0] >> 4 != T.END:
        raise ValueError("response frame does not end with END")
    end, orders = decode(msgs[-1]), msgs[:-1]
    if not end.eoe:
        raise ValueError("END without EOE")
    if any(m[0] >> 4 not in ENTRY_TYPES for m in orders):
        raise ValueError("response frame carries a non-order message before END")
    if end["n_orders"] != len(orders):
        raise ValueError(f"END.n_orders={end['n_orders']} but the frame carries {len(orders)} orders")
    return orders, end


PID_BITS, SLOT_BITS, GEN_BITS = 5, 3, 16
GEN_MAX = (1 << GEN_BITS) - 1


def make_clordid(pid: int, slot: int, gen: int) -> int:
    assert 0 <= pid < 32 and 0 <= slot < 8 and 0 <= gen <= GEN_MAX
    return (pid << 19) | (slot << 16) | gen


def split_clordid(c: int) -> tuple[int, int, int]:
    return (c >> 19) & 0x1F, (c >> 16) & 0x7, c & GEN_MAX
