"""Wire format for the Jeio Tech oven controller's serial protocol.

Every frame is a big-endian prolog (header, length, extension, command),
`length` bytes of data, and a 16-bit checksum that is the sum of all
preceding bytes.
"""

import collections
import enum
import struct


class ProtocolError(Exception):
    """A frame from the controller was short, corrupt, or unexpected."""


class Command(enum.IntEnum):
    Update = 0x30  # To controller
    Graph = 0x31  # From controller
    Ack = 0x01  # To controller
    Status = 0x32  # From controller
    Parameter = 0x33  # From controller


########################################################################

_Prolog = struct.Struct(
    ">"
    "L"  # header
    "B"  # length
    "H"  # extension
    "B"  # command
)
PROLOG_LENGTH = _Prolog.size
assert PROLOG_LENGTH == 8
Prolog = collections.namedtuple("Prolog", ["header", "length", "extension", "command"])

HEADER_TO_CONTROLLER = 0x01AA00AA
HEADER_FROM_CONTROLLER = 0x00AA01AA

CHECKSUM_LENGTH = 2


def _MakeProlog(length: int, extension: int, command: Command) -> bytes:
    return _Prolog.pack(HEADER_TO_CONTROLLER, length, extension, command)


def DecodeProlog(b: bytes) -> Prolog:
    if len(b) != PROLOG_LENGTH:
        raise ProtocolError(f"short prolog: got {len(b)} of {PROLOG_LENGTH} bytes")
    prolog = Prolog._make(_Prolog.unpack(b))
    if prolog.header != HEADER_FROM_CONTROLLER:
        raise ProtocolError(f"bad header 0x{prolog.header:08x}; out of sync?")
    return prolog


########################################################################

_Update = struct.Struct(
    ">"
    "B"  # reserved = 0xff
    "h"  # temp (100ths C, 2's complement)
    "b"  # reserved = 0
)
assert _Update.size == 4


def MakeUpdate(setpoint_C: float | None = None) -> bytes:
    extension = 0 if setpoint_C is None else 2
    prolog = _MakeProlog(_Update.size, extension, Command.Update)
    encoded_temp = 0 if setpoint_C is None else EncodeTempC(setpoint_C)
    data = _Update.pack(
        0xFF,  # reserved
        encoded_temp,
        0,
    )  # reserved
    return _AppendChecksum(prolog + data)


_Ack = struct.Struct(
    ">B"  # toggle
)
assert _Ack.size == 1


def MakeAck(toggle_stopstart: bool = False) -> bytes:
    extension = 1 if toggle_stopstart else 0
    prolog = _MakeProlog(_Ack.size, extension, Command.Ack)
    toggle = 0x7F if toggle_stopstart else 0xFF
    data = _Ack.pack(toggle)
    return _AppendChecksum(prolog + data)


########################################################################

_Graph = struct.Struct(
    ">"
    "L"  # pvfnd = 0xf96392FF
    "L"  # svfnd = 0xc00280ff
    "B"  # led = 0xfc
    "B"  # reserved = 0
    "B"  # buzzer = 0x64
    "B"  # reserved = 0
    "B"  # model
    "B"  # reserved = 0
    "B"  # pv instance = 0
    "h"  # pv value (100ths C, 2's complement)
    "L"  # pv reserved = 0
    "B"  # heat instance = 1
    "H"  # heat value
    "L"  # heat reserved = 0
    "B"  # reserved a instance = 2
    "H"  # reserved a value
    "L"  # reserved a reserved = 0
    "B"  # reserved b instance = 3
    "H"  # reserved b value
    "L"  # reserved b reserved = 0
)
assert _Graph.size == 0x2A

_GraphTuple = collections.namedtuple(
    "GraphTuple",
    [
        "pvfnd",
        "svfnd",
        "led",
        "reserved0",
        "buzzer",
        "reserved1",
        "model",
        "reserved2",
        "pv_instance",
        "pv_value",
        "pv_reserved",
        "heat_instance",
        "heat_value",
        "heat_reserved",
        "reserved_a_instance",
        "reserved_a_value",
        "reserved_a_reserved",
        "reserved_b_instance",
        "reserved_b_value",
        "reserved_b_reserved",
    ],
)


def DecodeGraphData(b: bytes) -> dict:
    g = _GraphTuple._make(_Graph.unpack(b))
    return {
        "process_value_C": DecodeTempC(g.pv_value),
        "heat_value_percent": _DecodeHeatPercent(g.heat_value),
    }


_Status = struct.Struct(
    ">"
    "L"  # pvfnd = 0xa46392ff
    "L"  # svfnd = 0xc019c0f9
    "B"  # led = 0xfc
    "B"  # reserved = 0
    "B"  # buzzer = 0x64
    "B"  # reserved = 0
    "B"  # model
    "B"  # reserved = 0
    "B"  # run instance
    "B"  # run data
    "B"  # autotune instance
    "B"  # autotune data
    "B"  # status instance
    "B"  # status data
    "B"  # overtemp instance
    "B"  # overtemp data
    "B"  # water level instance
    "B"  # water level data
    "B"  # cooler instance
    "B"  # cooler data
)
assert _Status.size == 0x1A

_StatusTuple = collections.namedtuple(
    "StatusTuple",
    [
        "pvfnd",
        "svfnd",
        "led",
        "reserved0",
        "buzzer",
        "reserved1",
        "model",
        "reserved2",
        "run_instance",
        "run_data",
        "autotune_instance",
        "autotune_data",
        "status_instance",
        "status_data",
        "overtemp_instance",
        "overtemp_data",
        "water_level_instance",
        "water_level_data",
        "cooler_instance",
        "cooler_data",
    ],
)


def _CheckInstance(name: str, actual: int, expected: int):
    if actual != expected:
        raise ProtocolError(
            f"{name} instance is 0x{actual:02x}, expected 0x{expected:02x}"
        )


def DecodeStatusData(b: bytes) -> dict:
    s = _StatusTuple._make(_Status.unpack(b))
    # Make sure these come in the expected order.
    _CheckInstance("run", s.run_instance, 4)
    _CheckInstance("autotune", s.autotune_instance, 5)
    _CheckInstance("status", s.status_instance, 6)
    _CheckInstance("overtemp", s.overtemp_instance, 7)
    _CheckInstance("water level", s.water_level_instance, 8)
    _CheckInstance("cooler", s.cooler_instance, 9)
    return {
        "led": s.led,
        "buzzer": s.buzzer,
        "model": s.model,
        "run": bool(s.run_data),
        "overtemp": bool(s.overtemp_data),
    }


_Parameter = struct.Struct(
    ">"
    "L"  # pvfnd = 0xa46392ff
    "L"  # svfnd = 0xc019c0f9
    "B"  # led = 0xfc
    "B"  # reserved = 0
    "B"  # buzzer = 0x64
    "B"  # reserved = 0
    "B"  # model
    "B"  # reserved = 0
    "B"  # pv instance
    "l"  # pv value (100ths C, 2's complement)
    "B"  # sv instance
    "l"  # sv value (100ths C, 2's complement)
    "B"  # heat instance
    "L"  # heat value
    "B"  # runtime instance
    "L"  # runtime value
    "B"  # on time count instance
    "L"  # on time count value
    "B"  # off time count instance
    "L"  # off time count value
    "B"  # power frequency instance
    "L"  # power frequency value
    "B"  # reserved instance
    "L"  # reserved value
    "B"  # reserved instance
    "L"  # reserved value
    "B"  # reserved instance
    "L"  # reserved value
)
assert _Parameter.size == 0x40

_ParameterTuple = collections.namedtuple(
    "ParameterTuple",
    [
        "pvfnd",
        "svfnd",
        "led",
        "reserved0",
        "buzzer",
        "reserved1",
        "model",
        "reserved2",
        "pv_instance",
        "pv_value",
        "sv_instance",
        "sv_value",
        "heat_instance",
        "heat_value",
        "runtime_instance",
        "runtime_value",
        "on_time_count_instance",
        "on_time_count_value",
        "off_time_count_instance",
        "off_time_count_value",
        "power_frequency_instance",
        "power_frequency_value",
        "reserved3_instance",
        "reserved3_value",
        "reserved4_instance",
        "reserved4_value",
        "reserved5_instance",
        "reserved5_value",
    ],
)


def DecodeParameterData(b: bytes) -> dict:
    p = _ParameterTuple._make(_Parameter.unpack(b))
    # Make sure these come in the expected order.
    _CheckInstance("pv", p.pv_instance, 0x0A)
    _CheckInstance("sv", p.sv_instance, 0x0B)
    _CheckInstance("heat", p.heat_instance, 0x0C)
    _CheckInstance("runtime", p.runtime_instance, 0x0D)
    _CheckInstance("on time count", p.on_time_count_instance, 0x0E)
    _CheckInstance("off time count", p.off_time_count_instance, 0x0F)
    _CheckInstance("power frequency", p.power_frequency_instance, 0x10)
    return {
        "led": p.led,
        "buzzer": p.buzzer,
        "model": p.model,
        "process_value_C": DecodeTempC(p.pv_value),
        "setpoint_C": DecodeTempC(p.sv_value),
        "heater_duty_cycle_percent": _DecodeHeatPercent(p.heat_value),
        "runtime_seconds": p.runtime_value,
        "power_frequency_hz": p.power_frequency_value / 100.0,
    }


########################################################################

_DECODERS = {
    Command.Graph: (_Graph.size, DecodeGraphData),
    Command.Status: (_Status.size, DecodeStatusData),
    Command.Parameter: (_Parameter.size, DecodeParameterData),
}


def DecodeResponse(prolog: Prolog, body: bytes) -> tuple[Command, dict]:
    """Decodes a controller frame given its prolog and the bytes after it.

    `body` is the data plus the trailing checksum.
    """
    try:
        command = Command(prolog.command)
        expected_length, decoder = _DECODERS[command]
    except (ValueError, KeyError):
        raise ProtocolError(
            f"unknown command from controller: 0x{prolog.command:02x}"
        ) from None
    if prolog.length != expected_length:
        raise ProtocolError(
            f"{command.name} length is {prolog.length}, expected {expected_length}"
        )
    if len(body) != prolog.length + CHECKSUM_LENGTH:
        raise ProtocolError(
            f"short {command.name} frame: got {len(body)} of "
            f"{prolog.length + CHECKSUM_LENGTH} bytes"
        )
    whole = _Prolog.pack(*prolog) + body
    message_checksum = _GetChecksum(whole)
    computed_checksum = _ComputeChecksum(whole)
    if computed_checksum != message_checksum:
        raise ProtocolError(
            f"bad checksum: computed=0x{computed_checksum:04x}, message=0x{message_checksum:04x}"
        )
    return command, decoder(body[:-CHECKSUM_LENGTH])


########################################################################

_Checksum = struct.Struct(">H")


def _AppendChecksum(b: bytes) -> bytes:
    checksum = sum(b) & 0xFFFF
    return b + _Checksum.pack(checksum)


def _GetChecksum(message: bytes) -> int:
    (checksum,) = _Checksum.unpack(message[-2:])
    return checksum


def _ComputeChecksum(message: bytes) -> int:
    return sum(message[:-2]) & 0xFFFF


def EncodeTempC(t: float) -> int:
    # round(), not int(): int(0.29 * 100.) == 28.
    hundredths = round(float(t) * 100.0)
    if hundredths > 32767 or hundredths < -32768:
        raise ValueError(f"Temperature {t} C cannot be encoded.")
    return hundredths


def DecodeTempC(value: int) -> float:
    return value / 100.0


def _DecodeHeatPercent(value: int) -> float:
    return value / 100.0
