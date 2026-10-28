
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fmt:
    signed: bool
    ibits: int
    fbits: int = 0

    @property
    def width(self) -> int:
        return self.ibits + self.fbits

    @property
    def min(self) -> int:
        return -(1 << (self.width - 1)) if self.signed else 0

    @property
    def max(self) -> int:
        return (1 << (self.width - 1)) - 1 if self.signed else (1 << self.width) - 1

    def fits(self, x: int) -> bool:
        return self.min <= x <= self.max

    def check(self, x: int, what: str = "value") -> int:
        if isinstance(x, bool):
            x = int(x)
        if not isinstance(x, int):
            raise TypeError(f"{what}: expected int in {self}, got {type(x).__name__}")
        if not self.fits(x):
            raise OverflowError(f"{what}={x} does not fit {self}")
        return x

    def __str__(self) -> str:
        kind = "SQ" if self.signed else "UQ"
        return f"{kind}{self.ibits}.{self.fbits}"


def UQ(ibits: int, fbits: int = 0) -> Fmt:
    return Fmt(False, ibits, fbits)


def SQ(ibits: int, fbits: int = 0) -> Fmt:
    return Fmt(True, ibits, fbits)


BIT = UQ(1)

Q8 = SQ(16, 8)


def sat(x: int, fmt: Fmt) -> int:
    return max(fmt.min, min(fmt.max, x))


def wrap(x: int, fmt: Fmt) -> int:
    x &= (1 << fmt.width) - 1
    if fmt.signed and x >> (fmt.width - 1):
        x -= 1 << fmt.width
    return x


def shr_floor(x: int, n: int) -> int:
    return x >> n


def rnd_half_up(x: int, n: int) -> int:
    if n == 0:
        return x
    return (x + (1 << (n - 1))) >> n


def ceil_shift(x: int, n: int) -> int:
    return (x + (1 << n) - 1) >> n


def bits(x: int, hi: int, lo: int) -> int:
    return (x >> lo) & ((1 << (hi - lo + 1)) - 1)
