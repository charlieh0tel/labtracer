"""Runs Oven against a simulated controller."""

import threading
import time

import pytest

from labtracer import messages, oven

_FROM = messages.HEADER_FROM_CONTROLLER


def _Frame(command: messages.Command, data: bytes) -> bytes:
    return messages._AppendChecksum(
        messages._Prolog.pack(_FROM, len(data), 0, command) + data
    )


class FakeController:
    """Stands in for serial.Serial, answering like the oven controller."""

    def __init__(self):
        self.timeout = None
        self.lock = threading.Lock()
        self.pending = bytearray()
        self.events = []  # ('setpoint', C) and ('toggle', run) in order
        self.setpoint_hundredths = 2500
        self.run = False
        self.acks_left = 0
        self.dead = False
        self.corrupt_next = 0

    def _Graph(self):
        return _Frame(
            messages.Command.Graph,
            messages._Graph.pack(
                0, 0, 0xFC, 0, 0x64, 0, 0x29, 0, 0, 2500, 0, 1, 0, 0, 2, 0, 0, 3, 0, 0
            ),
        )

    def _Status(self):
        return _Frame(
            messages.Command.Status,
            messages._Status.pack(
                0,
                0,
                0xFC,
                0,
                0x64,
                0,
                0x29,
                0,
                4,
                int(self.run),
                5,
                0,
                6,
                0,
                7,
                0,
                8,
                0,
                9,
                0,
            ),
        )

    def _Parameter(self):
        return _Frame(
            messages.Command.Parameter,
            messages._Parameter.pack(
                0,
                0,
                0xFC,
                0,
                0x64,
                0,
                0x29,
                0,
                0x0A,
                2500,
                0x0B,
                self.setpoint_hundredths,
                0x0C,
                0,
                0x0D,
                0,
                0x0E,
                0,
                0x0F,
                0,
                0x10,
                6000,
                0x11,
                0,
                0x12,
                0,
                0x13,
                0,
            ),
        )

    def write(self, b: bytes):
        with self.lock:
            if self.dead:
                return
            command = b[7]
            if command == messages.Command.Update:
                if b[6] == 2:  # extension: setpoint present
                    self.setpoint_hundredths = int.from_bytes(
                        b[9:11], "big", signed=True
                    )
                    self.events.append(("setpoint", self.setpoint_hundredths / 100.0))
                reply = self._Graph()
                self.acks_left = 3
            elif command == messages.Command.Ack:
                self.acks_left -= 1
                if self.acks_left == 2:
                    reply = self._Status()
                elif self.acks_left == 1:
                    reply = self._Parameter()
                else:
                    reply = b""
                    if b[8] == 0x7F:
                        self.run = not self.run
                        self.events.append(("toggle", self.run))
            if self.corrupt_next and reply:
                self.corrupt_next -= 1
                reply = bytes([reply[0] ^ 0xFF]) + reply[1:]
            self.pending += reply

    def read(self, n: int) -> bytes:
        with self.lock:
            out = bytes(self.pending[:n])
            del self.pending[:n]
            return out

    def reset_input_buffer(self):
        with self.lock:
            self.pending.clear()


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(oven, "_POLL_INTERVAL_S", 0.01)
    real_sleep = time.sleep
    monkeypatch.setattr(oven.time, "sleep", lambda s: real_sleep(min(s, 0.01)))


def test_setpoint_sent_before_turning_on_and_off_on_exit():
    port = FakeController()
    with oven.Oven(port) as o:
        o.SetSetpointC(40.0)
        o.SetOn(True)
        deadline = time.monotonic() + 2
        while not (
            o.GetRunning() and o.GetCurrentSetpointValueC() == pytest.approx(40.0)
        ):
            assert time.monotonic() < deadline
            time.sleep(0.01)
    assert port.events[:2] == [("setpoint", 40.0), ("toggle", True)]
    assert port.events[-1] == ("toggle", False)
    assert not port.run


def test_setpoint_not_resent_forever():
    port = FakeController()
    with oven.Oven(port) as o:
        o.SetSetpointC(40.004)
        time.sleep(0.3)
    assert port.events == [("setpoint", 40.0)]


def test_recovers_from_corrupt_frame():
    port = FakeController()
    with oven.Oven(port) as o:
        port.corrupt_next = 1
        o.SetOn(True)
        deadline = time.monotonic() + 2
        while not o.GetRunning():
            assert time.monotonic() < deadline
            time.sleep(0.01)
    assert not port.run


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_dead_controller_fails_fast(monkeypatch):
    monkeypatch.setattr(oven, "_MAX_CONSECUTIVE_ERRORS", 2)
    port = FakeController()
    port.dead = True
    with pytest.raises(RuntimeError, match="thread failed"), oven.Oven(port):
        pass


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_getters_raise_after_thread_dies(monkeypatch):
    monkeypatch.setattr(oven, "_MAX_CONSECUTIVE_ERRORS", 2)
    port = FakeController()
    with oven.Oven(port) as o:
        port.dead = True
        deadline = time.monotonic() + 2
        while o._thread.is_alive():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        with pytest.raises(RuntimeError):
            o.GetParameters()
