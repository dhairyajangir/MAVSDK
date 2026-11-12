"""Deterministic localhost MAVLink FTP peer, based on the retained #3175 replay.

This is a synthetic peer. It advertises a PX4 autopilot identity for discovery;
it is not PX4 firmware and makes no physical-hardware or filesystem claim.
"""
import hashlib
import importlib
import socket
import struct
import threading
import time
from pathlib import Path

from pymavlink.dialects.v20 import common

HEADER = struct.Struct('<HBBBBBBI')
assert HEADER.size == 12
ACK = 128
NAK = 129
REMOVE_DIRECTORY = 10
FAIL_ERRNO = 2
# Literal Linux asm-generic/NuttX wire39: never the probe host's errno.ENOTEMPTY.
SCENARIOS = {
    '/nonempty': (NAK, bytes((FAIL_ERRNO, 39))),
    '/missing': (NAK, bytes((FAIL_ERRNO, 2))),
    '/empty': (ACK, b''),
    '/generic': (NAK, b'\x01'),
    '/empty-nak': (NAK, b''),
    '/short-errno': (NAK, bytes((FAIL_ERRNO,))),
    '/other-errno': (NAK, bytes((FAIL_ERRNO, 66))),
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reserve_port(kind):
    with socket.socket(socket.AF_INET, kind) as reservation:
        reservation.bind(('127.0.0.1', 0))
        return reservation.getsockname()[1]


class LoopbackPeer:
    def __init__(self, sdk_udp_port):
        self.address = ('127.0.0.1', sdk_udp_port)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('127.0.0.1', 0))
        self.socket.settimeout(0.03)
        self.peer_address = self.socket.getsockname()
        self.finished = threading.Event()
        self.errors = []
        self.requests = []
        self.frames = []
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name='ftp-deterministic-udp-peer')

    def write(self, value):
        self.frames.append({'direction': 'peer-to-sdk', 'bytes_hex': bytes(value).hex()})
        return self.socket.sendto(value, self.address)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.finished.set()
        self.thread.join(timeout=2)
        self.socket.close()
        assert not self.thread.is_alive(), 'UDP peer failed to stop'
        assert not self.errors, self.errors

    def snapshot(self):
        with self._lock:
            return {'sdk_udp_address': self.address, 'peer_udp_address': self.peer_address,
                    'requests': list(self.requests), 'frames': list(self.frames),
                    'errors': list(self.errors), 'hardware': False,
                    'identity': 'synthetic MAVLink system1/component1 PX4 heartbeat'}

    def mark(self):
        return len(self.requests)

    def assert_case_wire(self, mark, path):
        actual = self.requests[mark:]
        assert actual, ('No real FTP request observed', path)
        assert all(row['path'] == path and row['request_opcode'] == REMOVE_DIRECTORY
                   and row['target_system'] == 1 and row['target_component'] == 1 for row in actual), actual
        expected = SCENARIOS[path]
        assert all(row['response_opcode'] == expected[0]
                   and row['response_data_hex'] == expected[1].hex() for row in actual), actual
        # A retry is recorded rather than hidden; this probe does not redefine retry policy.
        return {'request_count': len(actual), 'request_start': mark,
                'request_end': len(self.requests)}

    def _run(self):
        vehicle = common.MAVLink(self, srcSystem=1, srcComponent=1)
        decoder = common.MAVLink(None)
        heartbeat_at = 0.0
        try:
            while not self.finished.is_set():
                if time.monotonic() >= heartbeat_at:
                    vehicle.heartbeat_send(common.MAV_TYPE_QUADROTOR, common.MAV_AUTOPILOT_PX4,
                                           0, 0, common.MAV_STATE_ACTIVE)
                    heartbeat_at = time.monotonic() + 0.1
                try:
                    packet, source = self.socket.recvfrom(4096)
                except TimeoutError:
                    continue
                self.frames.append({'direction': 'sdk-to-peer', 'source_address': source,
                                    'bytes_hex': packet.hex()})
                for request in decoder.parse_buffer(packet) or ():
                    if request.get_type() != 'FILE_TRANSFER_PROTOCOL':
                        continue
                    assert request.target_system == 1 and request.target_component == 1
                    payload = bytes(request.payload)
                    sequence, session, opcode, size, _, _, _, offset = HEADER.unpack_from(payload)
                    assert offset == 0
                    path = payload[HEADER.size:HEADER.size + size].rstrip(b'\0').decode()
                    if opcode == 1:  # TerminateSession cleanup, if a client sends it.
                        response, result = ACK, b''
                    else:
                        assert opcode == REMOVE_DIRECTORY, (opcode, path)
                        response, result = SCENARIOS[path]
                    data = HEADER.pack((sequence + 1) % 65536, session, response,
                                       len(result), opcode, 0, 0, 0) + result
                    data = data.ljust(251, b'\0')
                    with self._lock:
                        if opcode == REMOVE_DIRECTORY:
                            self.requests.append({'path': path, 'request_opcode': opcode,
                                'request_sequence': sequence, 'session': session,
                                'source_system': request.get_srcSystem(),
                                'source_component': request.get_srcComponent(),
                                'target_system': request.target_system,
                                'target_component': request.target_component,
                                'request_payload_hex': payload.hex(), 'response_opcode': response,
                                'response_data_hex': result.hex(), 'response_payload_hex': data.hex()})
                    vehicle.file_transfer_protocol_send(0, request.get_srcSystem(),
                                                       request.get_srcComponent(), list(data))
        except Exception as error:
            self.errors.append({'class': type(error).__name__, 'message': str(error)})
            self.finished.set()


def native_mappings(pid=None):
    maps = Path('/proc/self/maps' if pid is None else f'/proc/{pid}/maps')
    paths = sorted({line.split()[-1] for line in maps.read_text().splitlines()
                    if 'libmavsdk' in line or 'libcmavsdk' in line})
    return [{'path': str(Path(path).resolve()), 'sha256': sha(path)}
            for path in paths if Path(path).is_file()]


def module_origins(names, sdk_root):
    root = (Path(sdk_root) / 'py/mavsdk/mavsdk').resolve()
    result = {}
    for name in names:
        mod = importlib.import_module(name)
        path = Path(mod.__file__).resolve()
        assert path.is_relative_to(root), (name, str(path), str(root))
        result[name] = {'path': str(path), 'sha256': sha(path)}
    return result
