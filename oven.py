#!/usr/bin/python3

import logging
import struct
import sys
import threading
import time

import serial

import messages


def _Dump(data):
    print(' '.join(f'{b:02X}' for b in data))

class Oven:
    def __init__(self, port):
        self._port = port
        port.timeout = 1.
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._comms_established = threading.Event()
        self._setpoint_C = None
        self._on = False
        self._graph = None
        self._status = None
        self._parameters = None
        
    def _ReadControllerResponse(self):
        prolog = self._port.read(messages._PrologLength)
        decoded_prolog = messages._DecodeProlog(prolog)
        if decoded_prolog.header != messages._HEADER_FROM_CONTROLLER:
            logging.info(f"bad header 0x{decoded_prolog.header:0x}; out of sync?")
            return None
        remainder = self._port.read(decoded_prolog.length + 2) # data + 2-byte checksum
        whole_response = prolog + remainder
        message_checksum = messages._GetChecksum(whole_response)
        computed_checksum = messages._ComputeChecksum(whole_response)
        if computed_checksum != message_checksum:
            logging.warning(f"bad checksum: computed=0x{computed_checksum:0x}, message=0x{message_checksum}")
            return None
        data = remainder[:-2]

        command = decoded_prolog.command
        if command == messages.Command.Graph:
            return messages._DecodeGraphData(data)
        elif command == messages.Command.Status:
            return messages._DecodeStatusData(data)
        elif command == messages.Command.Parameter:
            return  messages._DecodeParameterData(data)
        else:
            logging.error(f"Unknown command from controller: 0x{command:0x}")
            return None

    def _SendUpdate(self, new_setpoint_C: float | None = None):
        message = messages._MakeUpdate(new_setpoint_C)
        self._port.write(message)

    def _SendAck(self, toggle_stopstart: bool = False):
        message = messages._MakeAck(toggle_stopstart)
        self._port.write(message)

    def SetSetpointC(self, setpoint_C: float):
        logging.info(f"Setting setpoint to {setpoint_C:.3f}")
        with self._lock:
            self._setpoint_C = setpoint_C

    def SetOn(self, on: bool):
        logging.info(f"Setting stove to {self._OnOff(on)}")
        with self._lock:
            self._on = on

    def Stop(self):
        logging.info("Stopping stove thread.")
        self._stop_event.set()

    def GetStatus(self) -> dict:
        with self._lock:
            return self._status

    def GetGraph(self) -> dict:
        with self._lock:
            return self._graph

    def GetParameters(self) -> dict:
        with self._lock:
            return self._parameters

    def GetRunning(self) -> bool:
        return self.GetStatus()['run']

    def GetCurrentSetpointValueC(self) -> float:
        return self.GetParameters()['setpoint_C']

    def GetCurrentProcessValueC(self) -> float:
        return self.GetParameters()['process_value_C']

    def GetCurrentHeaterDutyCyclePercent(self) -> float:
        return self.GetParameters()['heater_duty_cycle_percent']
        
    def _CommsCycle(self,
                    setpoint_C : float | None = None,
                    toggle_onoff: bool = False):
        if setpoint_C:
            logging.info(f"Sending new setpoint {setpoint_C:.3f}")
        self._SendUpdate(setpoint_C)
        graph = self._ReadControllerResponse()
        assert(graph)
        self._SendAck()
        status = self._ReadControllerResponse()
        assert(status)
        self._SendAck()
        parameters = self._ReadControllerResponse()
        assert(parameters)
        if toggle_onoff:
            logging.info("Sending stove on/off toggle")
        self._SendAck(toggle_onoff)
        return (graph, status, parameters)

    def _UpdatingCommsCycle(self, *args, **kwargs):
        (graph, status, parameters) = self._CommsCycle(*args, **kwargs)
        with self._lock:
            self._graph = graph
            self._status = status
            self._parameters = parameters
        
    @staticmethod
    def _OnOff(v: bool):
        return "on" if v else "off"

    @staticmethod
    def _CloseEnough(x: float, y: float):
        return abs(x - y) < 0.001

    def Start(self):
        def thread():
            _ = self._CommsCycle()
            time.sleep(1.)
            self._UpdatingCommsCycle()
            self._comms_established.set()
            try:
                while not self._stop_event.is_set():
                    with self._lock:
                        setpoint_C = self._setpoint_C
                        current_setpoint_C = self._parameters['setpoint_C']
                        maybe_new_setpoint_C = (
                            setpoint_C if setpoint_C is not None and not self._CloseEnough(setpoint_C, current_setpoint_C) else None)

                        running = self._status['run']
                        needs_toggle = running != self._on
                        new_running = not running
                    if needs_toggle:
                        self._UpdatingCommsCycle(None, True)
                        # We have to wait for this to take effect.
                        while not self._stop_event.is_set():
                            self._UpdatingCommsCycle()
                            if self.GetRunning() == new_running:
                                logging.info(f"Stove now in {self._OnOff(new_running)} state")
                                break
                            time.sleep(0.25)
                    self._UpdatingCommsCycle(maybe_new_setpoint_C)
                    time.sleep(0.25)
            finally:                    
                if self.GetRunning():
                    logging.info("Stove is still running; turning it off.")
                    _ = self._CommsCycle(0., True)
                
        threading.Thread(target=thread).start()
        
    def __enter__(self):
        self.Start()
        logging.info("Waiting for comms...")
        self._comms_established.wait()
        logging.info("Comms established.")
        return self

    def __exit__(self, *args):
        self.Stop()


def main(argv):
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    port = serial.Serial(argv[1], baudrate=19200)

    def DelayPrintingValues(oven, howlong: float, interval: float = 1.):
        now = time.time()
        while time.time() - now < howlong:
            print(f"{oven.GetCurrentSetpointValueC():.3f} C, "
                  f"{oven.GetCurrentProcessValueC():.3f} C, "
                  f"{oven.GetCurrentHeaterDutyCyclePercent():5.1f} %")
            time.sleep(interval)

    with Oven(port) as oven:
        oven.SetSetpointC(40.)
        oven.SetOn(True)
        DelayPrintingValues(oven, 10.)
        oven.SetSetpointC(45.0)
        DelayPrintingValues(oven, 10.)
        oven.SetOn(False)
        DelayPrintingValues(oven, 10.)

    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
    
