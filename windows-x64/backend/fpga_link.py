import sys
sys.dont_write_bytecode = True
import ctypes
import ctypes.util
import json
import socket
import subprocess
import time
from pathlib import Path

ETHERTYPE_TO_FPGA, ETHERTYPE_TO_EXCHANGE = 0x88B5, 0x88B6
BROADCAST = b"\xff" * 6
MIN_PAYLOAD = 48
PRIVATE_TYPES = range(0x8, 0xC)
DEFAULT_TIMEOUT = 0.25


def board_mac(pid):
    return bytes([2, 0, 0, 0, 0, pid])


def ethernet(dst, src, ethertype, payload):
    return dst + src + ethertype.to_bytes(2, "big") + payload


def messages(payload):
    out = []
    for i in range(0, len(payload) - 7, 8):
        if payload[i] >> 4 == 0:
            break
        out.append(payload[i:i + 8])
    return out


def repack(payload):
    body = b"".join(messages(payload))
    return body + bytes(max(0, MIN_PAYLOAD - len(body)))


class LinkError(RuntimeError):
    pass


class BoardGone(LinkError):
    pass


class PcapTransport:
    class _Header(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long),
                    ("caplen", ctypes.c_uint32), ("len", ctypes.c_uint32)]

    class _Program(ctypes.Structure):
        _fields_ = [("bf_len", ctypes.c_uint), ("bf_insns", ctypes.c_void_p)]

    def __init__(self, iface, pid):
        device = iface
        if sys.platform == "win32":
            device, self.mac = self._windows_adapter(iface)
        path = ctypes.util.find_library("wpcap" if sys.platform == "win32" else "pcap")
        if not path and sys.platform == "win32":
            path = "wpcap.dll"
        if not path:
            raise LinkError("libpcap not found (Linux: install libpcap0.8; Windows: install Npcap)")
        lib = self.lib = ctypes.WinDLL(path) if sys.platform == "win32" else ctypes.CDLL(path)
        lib.pcap_create.restype = ctypes.c_void_p
        lib.pcap_create.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        for name in ("pcap_set_snaplen", "pcap_set_promisc", "pcap_set_timeout", "pcap_set_immediate_mode",
                     "pcap_set_buffer_size"):
            getattr(lib, name).argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.pcap_activate.argtypes = [ctypes.c_void_p]
        lib.pcap_geterr.restype = ctypes.c_char_p
        lib.pcap_geterr.argtypes = [ctypes.c_void_p]
        lib.pcap_compile.argtypes = [ctypes.c_void_p, ctypes.POINTER(self._Program), ctypes.c_char_p, ctypes.c_int,
                                     ctypes.c_uint32]
        lib.pcap_setfilter.argtypes = [ctypes.c_void_p, ctypes.POINTER(self._Program)]
        lib.pcap_freecode.argtypes = [ctypes.POINTER(self._Program)]
        lib.pcap_inject.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
        lib.pcap_next_ex.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(self._Header)),
                                     ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
        lib.pcap_close.argtypes = [ctypes.c_void_p]
        errbuf = ctypes.create_string_buffer(512)
        handle = self.handle = lib.pcap_create(device.encode(), errbuf)
        if not handle:
            raise LinkError(f"pcap_create({iface}): {errbuf.value.decode()}")
        lib.pcap_set_snaplen(handle, 256)
        lib.pcap_set_promisc(handle, 1)
        lib.pcap_set_timeout(handle, 10)
        lib.pcap_set_immediate_mode(handle, 1)
        lib.pcap_set_buffer_size(handle, 4 << 20)
        if lib.pcap_activate(handle) < 0:
            raise LinkError(f"pcap_activate({iface}): {lib.pcap_geterr(handle).decode()} (on Windows, run as Administrator)")
        program = self._Program()
        expression = f"ether proto 0x{ETHERTYPE_TO_EXCHANGE:04x}".encode()
        if lib.pcap_compile(handle, ctypes.byref(program), expression, 1, 0xFFFFFFFF) < 0 or \
                lib.pcap_setfilter(handle, ctypes.byref(program)) < 0:
            raise LinkError(f"pcap filter: {lib.pcap_geterr(handle).decode()}")
        lib.pcap_freecode(ctypes.byref(program))
        if sys.platform != "win32":
            self.mac = self._interface_mac(iface)
        self.rx_stamp_ns = None

    @staticmethod
    def _windows_adapter(value):
        script = "Get-NetAdapter | Select-Object Name,InterfaceGuid,MacAddress | ConvertTo-Json -Compress"
        try:
            result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                    capture_output=True, text=True, check=True)
            adapters = json.loads(result.stdout)
            if isinstance(adapters, dict):
                adapters = [adapters]
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            raise LinkError("Could not list Windows adapters; install Npcap and PowerShell") from exc
        needle = value.strip().lower()
        for adapter in adapters:
            guid = str(adapter.get("InterfaceGuid", "")).strip("{}").lower()
            name = str(adapter.get("Name", ""))
            device = rf"\Device\NPF_{{{guid}}}"
            if needle in (name.lower(), guid, device.lower()):
                try:
                    mac = bytes(int(x, 16) for x in adapter["MacAddress"].split("-"))
                except (KeyError, ValueError):
                    raise LinkError(f"Could not read the MAC address for Windows adapter {name}") from None
                return device, mac
        raise LinkError(f"Windows adapter '{value}' not found; enter its name or InterfaceGuid")

    @staticmethod
    def _interface_mac(iface):
        sysfs = Path("/sys/class/net") / iface / "address"
        if sysfs.exists():
            text = sysfs.read_text()
        else:
            text = subprocess.run(["ifconfig", iface], capture_output=True, text=True).stdout
            text = next((line.split()[1] for line in text.splitlines() if line.strip().startswith("ether ")), "")
        try:
            return bytes(int(x, 16) for x in text.strip().split(":"))
        except ValueError:
            raise LinkError(f"can't read the MAC address of {iface}") from None

    def send(self, frame):
        if self.lib.pcap_inject(self.handle, frame, len(frame)) < 0:
            raise BoardGone(f"pcap_inject: {self.lib.pcap_geterr(self.handle).decode()}")

    def recv(self, deadline):
        header, data = ctypes.POINTER(self._Header)(), ctypes.POINTER(ctypes.c_ubyte)()
        while time.monotonic() < deadline:
            rc = self.lib.pcap_next_ex(self.handle, ctypes.byref(header), ctypes.byref(data))
            if rc == 1:
                h = header.contents
                self.rx_stamp_ns = h.tv_sec * 1_000_000_000 + h.tv_usec * 1000
                return ctypes.string_at(data, h.caplen)
            if rc < 0:
                raise BoardGone(f"pcap_next_ex: {self.lib.pcap_geterr(self.handle).decode()}")
        return None

    def close(self):
        if self.handle:
            self.lib.pcap_close(self.handle)
            self.handle = None


class UdpTransport:

    def __init__(self, address):
        host, port = address.rsplit(":", 1)
        self.peer = (host, int(port))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.mac = bytes([2, 0, 0, 0, 0xFE, 0x01])
        self.rx_stamp_ns = None

    def send(self, frame):
        try:
            self.sock.sendto(frame, self.peer)
        except OSError as exc:
            raise BoardGone(f"udp send: {exc}") from None

    def recv(self, deadline):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                frame, _ = self.sock.recvfrom(2048)
                return frame
            except socket.timeout:
                return None
            except OSError as exc:
                raise BoardGone(f"udp recv: {exc}") from None

    def close(self):
        self.sock.close()


class FpgaLink:
    def __init__(self, transport, pid=3, timeout=DEFAULT_TIMEOUT):
        self.transport, self.pid, self.timeout = transport, pid, timeout
        self.board = board_mac(pid)
        self.frames = 0
        self.last_timing = None

    def drain(self):
        while self.transport.recv(time.monotonic() + 0.02) is not None:
            pass

    def exchange(self, payload):
        msgs = messages(payload)
        private = bool(msgs) and msgs[0][0] >> 4 in PRIVATE_TYPES
        frame_out = ethernet(self.board if private else BROADCAST, self.transport.mac, ETHERTYPE_TO_FPGA, payload)
        sent_wall, sent = time.time_ns(), time.perf_counter_ns()
        self.transport.send(frame_out)
        deadline = time.monotonic() + self.timeout
        while True:
            frame = self.transport.recv(deadline)
            if frame is None:
                raise BoardGone(f"the board did not answer inbound frame {self.frames} within "
                                f"{self.timeout * 1000:.0f} ms (link down, wrong pid on the switches, or not lockstep?)")
            if len(frame) < 14 or int.from_bytes(frame[12:14], "big") != ETHERTYPE_TO_EXCHANGE:
                continue
            source = frame[6:12]
            if source != self.board:
                if source[:5] == self.board[:5]:
                    raise LinkError(f"a board answered inbound frame {self.frames} as pid {source[5]} "
                                    f"({':'.join(f'{b:02x}' for b in source)}), but the exchange trades as "
                                    f"pid {self.pid}: set the board's switches to {self.pid}")
                continue
            host_ns = time.perf_counter_ns() - sent
            stamp = self.transport.rx_stamp_ns
            self.last_timing = {"hostNs": host_ns, "captureNs": None if stamp is None else stamp - sent_wall}
            self.frames += 1
            return repack(frame[14:])

    def close(self):
        self.transport.close()


def open_link(transport, address, pid):
    if transport == "pcap":
        return FpgaLink(PcapTransport(address, pid), pid)
    if transport == "udp":
        return FpgaLink(UdpTransport(address), pid)
    raise ValueError("fpga_transport must be pcap or udp")
