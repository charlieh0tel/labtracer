"""Drives a Jeio Tech oven controller over a serial port."""

import argparse
import logging
import signal
import sys
import threading
import time

import serial

from labtracer import messages

# How long a run/stop toggle may go unconfirmed before we complain.  We
# never send a second toggle while one is unconfirmed: the protocol has no
# explicit on/off, so a late-arriving status would make it flip back.
_TOGGLE_TIMEOUT_S = 5.0
# Consecutive bad exchanges before getters report comms as lost.
_MAX_CONSECUTIVE_ERRORS = 10
# How long __enter__ waits for the first good exchanges.
_COMMS_TIMEOUT_S = 15.0
# How long shutdown keeps trying to confirm the oven is off.
_SHUTDOWN_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.1

logger = logging.getLogger(__name__)


def _OnOff(v: bool) -> str:
    return "on" if v else "off"


class Oven:
    def __init__(self, port: serial.Serial):
        self._port = port
        port.timeout = 1.0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._comms_established = threading.Event()
        self._thread = None
        # Requested state, set by the caller.
        self._setpoint_C = None
        self._on = False
        # Latest readings from the controller.
        self._graph = None
        self._status = None
        self._parameters = None
        # Comms thread bookkeeping.
        self._sent_setpoint = None  # Last setpoint sent, in wire units.
        self._warned_setpoint = None
        self._toggle_expect = None  # Run state an unconfirmed toggle should give.
        self._toggle_sent_at = None
        self._toggle_warned = False
        self._consecutive_errors = 0
        self._comms_lost = False

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
        self._CheckThread()
        logger.info(f"Setting setpoint to {setpoint_C:.2f}")
        with self._lock:
            self._setpoint_C = setpoint_C

    def SetOn(self, on: bool):
        """Requests the oven on or off.

        The oven is only turned on once the controller reports the setpoint
        given to SetSetpointC, so it never heats toward a stale one.
        """
        self._CheckThread()
        logger.info(f"Setting oven to {_OnOff(on)}")
        with self._lock:
            self._on = on
            if on and self._setpoint_C is None:
                logger.warning("No setpoint set; oven will stay off until one is.")

    def Stop(self):
        logger.info("Stopping oven thread.")
        self._stop_event.set()

    def _CheckThread(self):
        if (
            self._thread is not None
            and not self._thread.is_alive()
            and not self._stop_event.is_set()
        ):
            raise RuntimeError("oven thread failed; see log")

    def _CheckAlive(self):
        # Don't hand out stale readings once comms are gone.
        self._CheckThread()
        if self._comms_lost:
            raise ConnectionError("lost comms with oven controller")

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

    def _ShouldToggle(
        self, want_running: bool | None, setpoint_C: float | None, status, parameters
    ) -> bool:
        """Decides the run/stop toggle from this exchange's fresh readings."""
        run = status["run"]
        if self._toggle_expect is not None:
            if run != self._toggle_expect:
                elapsed = time.monotonic() - self._toggle_sent_at
                if elapsed > _TOGGLE_TIMEOUT_S and not self._toggle_warned:
                    logger.error(
                        f"Oven has not turned {_OnOff(self._toggle_expect)} "
                        f"{elapsed:.0f} s after the toggle; not toggling again."
                    )
                    self._toggle_warned = True
                return False
            logger.info(f"Oven now {_OnOff(run)}")
            self._toggle_expect = None
        if want_running is None or run == want_running:
            return False
        if not want_running:
            return True
        # Only start heating once the controller holds our setpoint.
        return setpoint_C is not None and messages.EncodeTempC(
            setpoint_C
        ) == messages.EncodeTempC(parameters["setpoint_C"])

    def _CheckSetpointTaken(self, setpoint_C: float, parameters):
        encoded = messages.EncodeTempC(setpoint_C)
        held = messages.EncodeTempC(parameters["setpoint_C"])
        if held != encoded and self._warned_setpoint != encoded:
            logger.warning(
                f"Controller holds {parameters['setpoint_C']:.2f} C, "
                f"not the {setpoint_C:.2f} C sent; out of range?"
            )
            self._warned_setpoint = encoded

    def _Exchange(self, setpoint_C: float | None, want_running: bool | None):
        """One exchange: Update, then three responses each answered by an Ack.

        A changed setpoint goes out in the Update; a run/stop toggle, if any,
        goes in the final Ack, decided from the Status read in this exchange.
        """
        new_setpoint_C = None
        if (
            setpoint_C is not None
            and messages.EncodeTempC(setpoint_C) != self._sent_setpoint
        ):
            new_setpoint_C = setpoint_C
            logger.info(f"Sending new setpoint {setpoint_C:.2f}")
        self._port.write(messages.MakeUpdate(new_setpoint_C))
        graph = self._ReadControllerResponse(messages.Command.Graph)
        self._port.write(messages.MakeAck())
        status = self._ReadControllerResponse(messages.Command.Status)
        self._port.write(messages.MakeAck())
        parameters = self._ReadControllerResponse(messages.Command.Parameter)
        if new_setpoint_C is not None:
            # The controller answered the Update, so it has the setpoint.
            self._sent_setpoint = messages.EncodeTempC(new_setpoint_C)
        elif setpoint_C is not None:
            self._CheckSetpointTaken(setpoint_C, parameters)
        toggle = self._ShouldToggle(want_running, setpoint_C, status, parameters)
        if toggle:
            logger.info(f"Sending toggle to turn oven {_OnOff(want_running)}")
            # Mark it pending before writing: if the write fails partway the
            # controller may still have acted on it.
            self._toggle_expect = want_running
            self._toggle_sent_at = time.monotonic()
            self._toggle_warned = False
        self._port.write(messages.MakeAck(toggle))
        with self._lock:
            self._graph = graph
            self._status = status
            self._parameters = parameters

    def _TryExchange(
        self, setpoint_C: float | None = None, want_running: bool | None = None
    ) -> bool:
        try:
            self._Exchange(setpoint_C, want_running)
        except (messages.ProtocolError, serial.SerialException) as e:
            self._consecutive_errors += 1
            logger.warning(f"Comms error ({self._consecutive_errors} in a row): {e}")
            if (
                self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS
                and not self._comms_lost
            ):
                logger.error("Lost comms with oven controller; still retrying.")
                self._comms_lost = True
            # Let the rest of a broken exchange arrive, then drop it so the
            # next one starts clean.
            time.sleep(self._port.timeout)
            if isinstance(e, messages.ProtocolError):
                self._port.reset_input_buffer()
            return False
        if self._comms_lost:
            logger.info("Comms with oven controller restored.")
        self._consecutive_errors = 0
        self._comms_lost = False
        return True

    def _Startup(self) -> bool:
        # The first exchange after opening the port is discarded.
        good = 0
        while good < 2 and not self._stop_event.is_set():
            if self._TryExchange():
                good += 1
                if good == 1:
                    time.sleep(1.0)
        return good == 2

    def _Loop(self):
        while not self._stop_event.is_set():
            with self._lock:
                setpoint_C = self._setpoint_C
                want_running = self._on
            self._TryExchange(setpoint_C, want_running)
            time.sleep(_POLL_INTERVAL_S)

    def _Shutdown(self):
        if self._status is None:
            return  # Never heard from the controller, so never changed it.
        deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_S
        while True:
            if (
                self._TryExchange()
                and not self._status["run"]
                and self._toggle_expect is None
            ):
                return
            if time.monotonic() > deadline:
                break
            logger.info("Oven may still be running; turning it off.")
            # Setpoint 0 stops the heating even if the toggle goes astray.
            self._TryExchange(0.0, False)
            time.sleep(_POLL_INTERVAL_S)
        logger.error("Could not confirm the oven is off!")

    def _Thread(self):
        try:
            if self._Startup():
                self._comms_established.set()
                self._Loop()
        finally:
            self._Shutdown()

    def Start(self):
        self._thread = threading.Thread(target=self._Thread, name="oven")
        self._thread.start()

    def Join(self):
        # Shutdown is bounded, and abandoning it could leave the oven heating,
        # so a second Ctrl-C doesn't cut it short.
        while self._thread is not None and self._thread.is_alive():
            try:
                self._thread.join()
            except KeyboardInterrupt:
                logger.warning("Still turning the oven off; please wait.")

    def __enter__(self):
        established = False
        try:
            self.Start()
            logger.info("Waiting for comms...")
            deadline = time.monotonic() + _COMMS_TIMEOUT_S
            while not self._comms_established.wait(_POLL_INTERVAL_S):
                self._CheckThread()
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

    # Make SIGTERM and SIGHUP unwind like Ctrl-C so the oven gets turned off.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    signal.signal(signal.SIGHUP, signal.default_int_handler)

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
                f"{_OnOff(oven.GetRunning())}"
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
