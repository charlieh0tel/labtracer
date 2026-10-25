"""Runs Oven against a simulated controller."""

import threading
import time
import types

import pytest

from labtracer import messages, oven


def _Frame(command: messages.Command, data: bytes) -> bytes:
    prolog = messages._Prolog.pack(
        messages.HEADER_FROM_CONTROLLER, len(data), 0, command
    )
    return messages._AppendChecksum(prolog + data)


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
        # How long a run/stop change takes to show up in Status.
        self.status_lag_s = 0.0
        self.reported_run = False
        self.run_changed_at = 0.0
        # Setpoints above this are clamped.
        self.max_setpoint_hundredths = 32767

    def _Graph(self):
        data = messages._Graph.pack(
            0, 0, 0xFC, 0, 0x64, 0, 0x29, 0,
            0, 2500, 0, 1, 0, 0, 2, 0, 0, 3, 0, 0,
        )  # fmt: skip
        return _Frame(messages.Command.Graph, data)

    def _Status(self):
        if time.monotonic() - self.run_changed_at >= self.status_lag_s:
            self.reported_run = self.run
        data = messages._Status.pack(
            0, 0, 0xFC, 0, 0x64, 0, 0x29, 0,
            4, int(self.reported_run), 5, 0, 6, 0, 7, 0, 8, 0, 9, 0,
        )  # fmt: skip
        return _Frame(messages.Command.Status, data)

    def _Parameter(self):
        data = messages._Parameter.pack(
            0, 0, 0xFC, 0, 0x64, 0, 0x29, 0,
            0x0A, 2500, 0x0B, self.setpoint_hundredths, 0x0C, 0, 0x0D, 0,
            0x0E, 0, 0x0F, 0, 0x10, 6000, 0x11, 0, 0x12, 0, 0x13, 0,
        )  # fmt: skip
        return _Frame(messages.Command.Parameter, data)

    def write(self, b: bytes):
        with self.lock:
            if self.dead:
                return
            command = b[7]
            if command == messages.Command.Update:
                if b[6] == 2:  # extension: setpoint present
                    requested = int.from_bytes(b[9:11], "big", signed=True)
                    self.setpoint_hundredths = min(
                        requested, self.max_setpoint_hundredths
                    )
                    self.events.append(("setpoint", requested / 100.0))
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
                        self.run_changed_at = time.monotonic()
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
    # Shorten the oven module's sleeps only; the tests' own sleeps stay real.
    monkeypatch.setattr(oven, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(
        oven,
        "time",
        types.SimpleNamespace(
            monotonic=time.monotonic, sleep=lambda s: time.sleep(min(s, 0.01))
        ),
    )


def WaitFor(condition, timeout_s: float = 2.0):
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_setpoint_sent_before_turning_on_and_off_on_exit():
    port = FakeController()
    with oven.Oven(port) as o:
        o.SetSetpointC(40.0)
        o.SetOn(True)
        WaitFor(o.GetRunning)
    assert port.events == [
        ("setpoint", 40.0),
        ("toggle", True),
        ("setpoint", 0.0),
        ("toggle", False),
    ]
    assert not port.run


def test_does_not_turn_on_until_setpoint_held():
    port = FakeController()
    port.setpoint_hundredths = 25000  # Left over from an earlier run.
    with oven.Oven(port) as o:
        o.SetOn(True)
        time.sleep(0.2)
        assert not port.run
        o.SetSetpointC(40.0)
        WaitFor(o.GetRunning)
    assert port.events[:2] == [("setpoint", 40.0), ("toggle", True)]


def test_setpoint_not_resent():
    port = FakeController()
    with oven.Oven(port) as o:
        o.SetSetpointC(40.004)
        time.sleep(0.3)
    assert port.events == [("setpoint", 40.0)]


def test_clamped_setpoint_not_resent_and_not_turned_on():
    port = FakeController()
    port.max_setpoint_hundredths = 20000
    with oven.Oven(port) as o:
        o.SetSetpointC(250.0)
        o.SetOn(True)
        time.sleep(0.3)
        assert not port.run
    assert port.events == [("setpoint", 250.0)]


def test_lagging_status_never_double_toggles(monkeypatch):
    monkeypatch.setattr(oven, "_TOGGLE_TIMEOUT_S", 0.1)
    port = FakeController()
    port.status_lag_s = 0.5
    with oven.Oven(port) as o:
        o.SetSetpointC(40.0)
        o.SetOn(True)
        time.sleep(0.3)  # Past the toggle timeout, before status catches up.
        assert port.events == [("setpoint", 40.0), ("toggle", True)]
        o.SetOn(False)
        WaitFor(lambda: not o.GetRunning() and port.events[-1] == ("toggle", False))
        time.sleep(0.3)
    assert port.events == [("setpoint", 40.0), ("toggle", True), ("toggle", False)]
    assert not port.run


def test_exit_with_toggle_unconfirmed_turns_off():
    port = FakeController()
    port.status_lag_s = 0.3
    with oven.Oven(port) as o:
        o.SetSetpointC(40.0)
        o.SetOn(True)
        WaitFor(lambda: port.run)
    assert not port.run
    assert port.events[-1] == ("toggle", False)


def test_recovers_from_corrupt_frame():
    port = FakeController()
    with oven.Oven(port) as o:
        o.SetSetpointC(40.0)
        port.corrupt_next = 1
        o.SetOn(True)
        WaitFor(o.GetRunning)
    assert not port.run


def test_dead_controller_times_out(monkeypatch):
    monkeypatch.setattr(oven, "_COMMS_TIMEOUT_S", 0.2)
    port = FakeController()
    port.dead = True
    with pytest.raises(TimeoutError), oven.Oven(port):
        pass


def test_getters_raise_while_comms_lost_and_recover(monkeypatch):
    monkeypatch.setattr(oven, "_MAX_CONSECUTIVE_ERRORS", 2)
    port = FakeController()
    with oven.Oven(port) as o:
        port.dead = True

        def CommsLost():
            try:
                o.GetParameters()
            except ConnectionError:
                return True
            return False

        WaitFor(CommsLost)
        port.dead = False
        WaitFor(lambda: not CommsLost())
