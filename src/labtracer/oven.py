"""Drives a Jeio Tech oven controller over a serial port."""

import argparse
import logging
import sys
import threading
import time

import serial

from labtracer import messages

# How long to wait for the controller to report a run/stop toggle.
_TOGGLE_TIMEOUT_S = 5.0
# How many consecutive bad comms cycles before giving up.
_MAX_CONSECUTIVE_ERRORS = 10
# How long __enter__ waits for the first good comms cycle.
_COMMS_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.1

logger = logging.getLogger(__name__)


class Oven:
    def __init__(self, port: serial.Serial):
        self._port = port
        port.timeout = 1.0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._comms_established = threading.Event()
        self._thread = None
        self._setpoint_C = None
        self._on = False
        self._graph = None
        self._status = None
        self._parameters = None

    def _ReadControllerResponse(self, expected: messages.Command) -> dict:
        prolog = messages.DecodeProlog(self._port.read(messages.PROLOG_LENGTH))
        body = self._port.read(prolog.length + messages.CHECKSUM_LENGTH)
        command, data = messages.DecodeResponse(prolog, body)
        if command != expected:
            raise messages.ProtocolError(
                f"expected {expected.name}, got {command.name}"
            )
        return data

    def SetSetpointC(self, setpoint_C: float):
        messages.EncodeTempC(setpoint_C)  # Raise now if out of range.
        logger.info(f"Setting setpoint to {setpoint_C:.2f}")
        with self._lock:
            self._setpoint_C = setpoint_C

    def SetOn(self, on: bool):
        logger.info(f"Setting oven to {self._OnOff(on)}")
        with self._lock:
            self._on = on

    def Stop(self):
        logger.info("Stopping oven thread.")
        self._stop_event.set()

    def _CheckAlive(self):
        # Don't hand out stale readings once the comms thread has died.
        if self._ThreadDied():
            raise RuntimeError("oven thread failed; see log")

    def _ThreadDied(self) -> bool:
        return (
            self._thread is not None
            and not self._thread.is_alive()
            and not self._stop_event.is_set()
        )

    def GetStatus(self) -> dict:
        self._CheckAlive()
        with self._lock:
            return self._status

    def GetGraph(self) -> dict:
        self._CheckAlive()
        with self._lock:
            return self._graph

    def GetParameters(self) -> dict:
        self._CheckAlive()
        with self._lock:
            return self._parameters

    def GetRunning(self) -> bool:
        return self.GetStatus()["run"]

    def GetCurrentSetpointValueC(self) -> float:
        return self.GetParameters()["setpoint_C"]

    def GetCurrentProcessValueC(self) -> float:
        return self.GetParameters()["process_value_C"]

    def GetCurrentHeaterDutyCyclePercent(self) -> float:
        return self.GetParameters()["heater_duty_cycle_percent"]

    def _CommsCycle(self, setpoint_C: float | None = None, toggle_onoff: bool = False):
        """One exchange: Update, then three responses each answered by an Ack.

        A new setpoint goes out in the Update, ahead of any run/stop toggle in
        the final Ack, so the oven never starts with a stale setpoint.
        """
        if setpoint_C is not None:
            logger.info(f"Sending new setpoint {setpoint_C:.2f}")
        try:
            self._port.write(messages.MakeUpdate(setpoint_C))
            graph = self._ReadControllerResponse(messages.Command.Graph)
            self._port.write(messages.MakeAck())
            status = self._ReadControllerResponse(messages.Command.Status)
            self._port.write(messages.MakeAck())
            parameters = self._ReadControllerResponse(messages.Command.Parameter)
            if toggle_onoff:
                logger.info("Sending oven on/off toggle")
            self._port.write(messages.MakeAck(toggle_onoff))
        except messages.ProtocolError:
            # Drop whatever is left of the exchange so the next one starts clean.
            time.sleep(self._port.timeout)
            self._port.reset_input_buffer()
            raise
        with self._lock:
            self._graph = graph
            self._status = status
            self._parameters = parameters

    def _RetryingCommsCycle(self, *args, **kwargs):
        for attempt in range(1, _MAX_CONSECUTIVE_ERRORS + 1):
            try:
                return self._CommsCycle(*args, **kwargs)
            except messages.ProtocolError as e:
                logger.warning(f"Comms error (attempt {attempt}): {e}")
        raise messages.ProtocolError(
            f"giving up after {_MAX_CONSECUTIVE_ERRORS} consecutive comms errors"
        )

    @staticmethod
    def _OnOff(v: bool):
        return "on" if v else "off"

    def _WaitForRunning(self, want_running: bool) -> bool:
        deadline = time.monotonic() + _TOGGLE_TIMEOUT_S
        while time.monotonic() < deadline:
            self._RetryingCommsCycle()
            if self.GetRunning() == want_running:
                logger.info(f"Oven now {self._OnOff(want_running)}")
                return True
            time.sleep(_POLL_INTERVAL_S)
        logger.warning(
            f"Oven did not turn {self._OnOff(want_running)} "
            f"within {_TOGGLE_TIMEOUT_S:.0f} s"
        )
        return False

    def _Loop(self):
        while not self._stop_event.is_set():
            with self._lock:
                setpoint_C = self._setpoint_C
                current_setpoint_C = self._parameters["setpoint_C"]
                # Compare in wire units so a setpoint that isn't a whole
                # number of hundredths doesn't get resent forever.
                new_setpoint_C = (
                    setpoint_C
                    if setpoint_C is not None
                    and messages.EncodeTempC(setpoint_C)
                    != messages.EncodeTempC(current_setpoint_C)
                    else None
                )
                want_running = self._on
                needs_toggle = self._status["run"] != want_running
            self._RetryingCommsCycle(new_setpoint_C, needs_toggle)
            if needs_toggle:
                # The toggle takes a moment to show up in the status.  If it
                # never does, the next pass toggles again.
                self._WaitForRunning(want_running)
            time.sleep(_POLL_INTERVAL_S)

    def _TurnOff(self):
        if not self.GetRunning():
            return
        logger.info("Oven is still running; turning it off.")
        for _ in range(3):
            self._RetryingCommsCycle(0.0, True)
            if self._WaitForRunning(False):
                return
        logger.error("Could not turn the oven off!")

    def _Thread(self):
        # An exception here ends the thread and is reported to stderr by
        # threading.excepthook; _ThreadDied() then tells the caller.
        # The first exchange after opening the port is discarded.
        self._RetryingCommsCycle()
        time.sleep(1.0)
        self._RetryingCommsCycle()
        self._comms_established.set()
        try:
            self._Loop()
        finally:
            self._TurnOff()

    def Start(self):
        self._thread = threading.Thread(target=self._Thread, name="oven")
        self._thread.start()

    def Join(self):
        if self._thread is not None:
            self._thread.join()

    def __enter__(self):
        self.Start()
        established = False
        try:
            logger.info("Waiting for comms...")
            deadline = time.monotonic() + _COMMS_TIMEOUT_S
            while not self._comms_established.wait(_POLL_INTERVAL_S):
                self._CheckAlive()
                if time.monotonic() > deadline:
                    raise TimeoutError("no response from oven controller")
            logger.info("Comms established.")
            established = True
        finally:
            # Also covers KeyboardInterrupt, which skips __exit__ here.
            if not established:
                self.Stop()
                self.Join()
        return self

    def __exit__(self, *args):
        self.Stop()
        self.Join()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exercise a Jeio Tech oven.")
    parser.add_argument("port", help="serial device, e.g. /dev/ttyUSB0")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )

    def DelayPrintingValues(oven, howlong: float, interval: float = 0.5):
        start = time.monotonic()
        while time.monotonic() - start < howlong:
            print(
                f"{oven.GetCurrentSetpointValueC():.2f} C, "
                f"{oven.GetCurrentProcessValueC():.2f} C, "
                f"{oven.GetCurrentHeaterDutyCyclePercent():5.1f} %, "
                f"{Oven._OnOff(oven.GetRunning())}"
            )
            time.sleep(interval)

    with serial.Serial(args.port, baudrate=19200) as port, Oven(port) as oven:
        oven.SetSetpointC(40.0)
        oven.SetOn(True)
        DelayPrintingValues(oven, 10.0)
        oven.SetSetpointC(45.0)
        DelayPrintingValues(oven, 10.0)
        oven.SetOn(False)
        DelayPrintingValues(oven, 10.0)

    return 0


if __name__ == "__main__":
    sys.exit(main())
