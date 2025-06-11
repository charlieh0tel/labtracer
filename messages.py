import collections
import dataclasses
import enum
import struct


class Command(enum.IntEnum):
    Update = 0x30               # To controller
    Graph = 0x31                # From controller
    Ack = 0x01                  # To controller
    Status = 0x32               # From controller
    Parameter = 0x33            # From controller

########################################################################

_Prolog = struct.Struct(">"
                        "L"        # header
                        "B"        # length
                        "H"        # extension
                        "B"        # command
                        )
_PrologLength = _Prolog.size
assert(_PrologLength == 8)
_PrologTuple = collections.namedtuple(
    'PrologTuple',
    ['header', 'length', 'extension', 'command'])

_HEADER_TO_CONTROLLER = 0x01aa00aa
_HEADER_FROM_CONTROLLER = 0x00aa01aa

def _MakeProlog(length: int, extension: int, command: Command):
    return _Prolog.pack(_HEADER_TO_CONTROLLER, length, extension, command)

def _DecodeProlog(b: bytes):
    return  _PrologTuple._make(_Prolog.unpack(b))

########################################################################

_Update = struct.Struct(">"
                        "B"     # reserved = 0xff
                        "h"     # temp (100ths C, 2's complement)
                        "b"     # reserved = 0
                        )
_UpdateLength = _Update.size
assert(_UpdateLength == 4)


def _MakeUpdate(setpoint_C: float | None = None):
    extension = 0 if setpoint_C is None else 2
    prolog = _MakeProlog(_UpdateLength, extension, Command.Update)
    encoded_temp = 0 if setpoint_C is None else _EncodeTempC(setpoint_C)
    data = _Update.pack(0xff,    # reserved
                        encoded_temp,
                        0)       # data (reserved)
    return _AppendChecksum(prolog + data)


_Ack = struct.Struct(">"
                     "B"        # toggle
                     )
_AckLength = _Ack.size
assert(_AckLength == 1)

def _MakeAck(toggle_stopstart: bool = False):
    extension = 1 if toggle_stopstart else 0
    prolog = _MakeProlog(_AckLength, extension, Command.Ack)
    toggle = 0x7f if toggle_stopstart else 0xff
    data = _Ack.pack(toggle)
    return _AppendChecksum(prolog + data)


########################################################################

_Graph = struct.Struct(">"
                       "L"      # pvfnd = 0xf96392FF
                       "L"      # svfnd = 0xc00280ff
                       "B"      # led = 0xfc
                       "B"      # reserved = 0
                       "B"      # buzzer = 0x64
                       "B"      # reserved = 0
                       "B"      # model
                       "B"      # reserved = 0
                       "B"      # pv instance = 0
                       "H"      # pv value
                       "L"      # pv reserved = 0
                       "B"      # heat instance = 1
                       "H"      # heat value
                       "L"      # heat reserved = 0
                       "B"      # reserved a instance = 2
                       "H"      # reserved a value
                       "L"      # reserved a reserved = 0
                       "B"      # reserved b instance = 3
                       "H"      # reserved b value
                       "L"      # reserved b reserved = 0
                       )
assert(_Graph.size == 0x2a)

_GraphTuple = collections.namedtuple(
    'GraphTuble',
    ['pvfnd', 'svfnd',
     'led', 'reserved0',
     'buzzer', 'reserved1',
     'model', 'reserved2',
     'pv_instance', 'pv_value', 'pv_reserved',
     'heat_instance', 'heat_value', 'heat_reserved',
     'reservred_a_instance', 'reserved_a_value', 'reserved_a_reserved',
     'reservred_b_instance', 'reserved_b_value', 'reserved_b_reserved'])

def _DecodeGraphData(b: bytes):
    g = _GraphTuple._make(_Graph.unpack(b))
    return {
        'process_value_C': _DecodeTempC(g.pv_value),
        'heat_value_percent':  _DecodeHeatPercent(g.heat_value)
    }


_Status = struct.Struct(">"
                        "L"      # pvfnd = 0xa46392ff
                        "L"      # svfnd = 0xc019c0f9
                        "B"      # led = 0xfc
                        "B"      # reserved = 0
                        "B"      # buzzer = 0x64
                        "B"      # reserved = 0
                        "B"      # model
                        "B"      # reserved = 0
                        "B"      # run instance
                        "B"      # run data
                        "B"      # autotune instance
                        "B"      # autotune datab
                        "B"      # status instance
                        "B"      # status data
                        "B"      # overtmep instance
                        "B"      # overtemp data
                        "B"      # water level instance
                        "B"      # water level data
                        "B"      # cooler instance
                        "B"      # cooler data
                       )
assert(_Status.size == 0x1a)

_StatusTuple = collections.namedtuple(
    'StatusTuble',
    ['pvfnd', 'svfnd',
     'led', 'reserved0',
     'buzzer', 'reserved1',
     'model', 'reserved2',
     'run_instance', 'run_data',
     'autotune_instance', 'autotune_data',
     'status_instance', 'status_data',
     'overtemp_instance', 'overtemp_data',
     'water_level_instance', 'water_level_data',
     'cooler_instance', 'cooler_data'])

def _DecodeStatusData(b: bytes):
    s = _StatusTuple._make(_Status.unpack(b))
    # Make sure these come in the expected order.
    assert(s.run_instance == 4)
    assert(s.autotune_instance == 5)
    assert(s.status_instance == 6)
    assert(s.overtemp_instance == 7)
    assert(s.water_level_instance == 8)
    assert(s.cooler_instance == 9)
    return {
        'led': s.led,
        'buzzer': s.buzzer,
        'model': s.model,
        'run': bool(s.run_data),
        'overtemp': bool(s.overtemp_data)
    }


_Parameter = struct.Struct(">"
                           "L"      # pvfnd = 0xa46392ff
                           "L"      # svfnd = 0xc019c0f9
                           "B"      # led = 0xfc
                           "B"      # reserved = 0
                           "B"      # buzzer = 0x64
                           "B"      # reserved = 0
                           "B"      # model
                           "B"      # reserved = 0
                           "B"      # instance
                           "L"      # data
                           "B"      # instance
                           "L"      # data
                           "B"      # instance
                           "L"      # data
                           "B"      # instance
                           "L"      # data
                           "B"      # instance
                           "L"      # data
                           "B"      # instance
                           "L"      # data
                           "B"      # instance
                           "L"      # data
                           "B"      # instance
                           "L"      # data
                           "B"      # instance
                           "L"      # data
                           "B"      # instance
                           "L"      # data
                           )
assert(_Parameter.size == 0x40)

_ParameterTuple = collections.namedtuple(
    'ParameterTuble',
    ['pvfnd', 'svfnd',
     'led', 'reserved0',
     'buzzer', 'reserved1',
     'model', 'reserved2',
     'pv_instance', 'pv_value',
     'sv_instance', 'sv_value',
     'heat_instance', 'heat_value',
     'runtime_instance', 'runtime_value',
     'on_time_count_instance', 'on_time_count_value',
     'off_time_count_instance', 'off_time_count_value',
     'power_frequency_instance', 'power_frequency_value',
     'reserved0_instance', 'reserved0_value',
     'reserved1_instance', 'reserved1_value',
     'reserved2_instance', 'reserved2_value'])
     
def _DecodeParameterData(b: bytes):
    p = _ParameterTuple._make(_Parameter.unpack(b))
    # Make sure these come in the expected order.
    assert(p.pv_instance == 0x0a)
    assert(p.sv_instance == 0x0b)
    assert(p.heat_instance == 0x0c)
    assert(p.runtime_instance == 0x0d)
    assert(p.on_time_count_instance == 0x0e)
    assert(p.off_time_count_instance == 0x0f)
    assert(p.power_frequency_instance == 0x10)
    return {
        'led': p.led,
        'buzzer': p.buzzer,
        'model': p.model,
        'process_value_C': _DecodeTempC(p.pv_value),
        'setpoint_C': _DecodeTempC(p.sv_value),
        'heater_duty_cycle_percent':  _DecodeHeatPercent(p.heat_value),
        'runtime_seconds': p.runtime_value,
        #'on_time_count': p.on_time_count_value,
        #'off_time_count': p.off_time_count_value,
        'power_frequency_hz': float(p.power_frequency_value)/ 100.
    }

########################################################################

_Checksum = struct.Struct(">H")

def _AppendChecksum(b: bytes):
    checksum = sum(b) & 0xffff
    return b + _Checksum.pack(checksum)

def _GetChecksum(message: bytes):
    checksum, = _Checksum.unpack(message[-2:])
    return checksum

def _ComputeChecksum(message: bytes):
    return sum(message[:-2]) & 0xffff


def _EncodeTempC(t: float):
    hundredths = int(float(t) * 100.)
    if hundredths > 32767 or hundredths < -32768:
        raise ValueError(f"Temperature {t} C cannot be encoded.")
    return hundredths

def _DecodeTempC(value: int):
    return float(value) / 100.

def _DecodeHeatPercent(value: int):
    return float(value) / 100.

########################################################################

def _TestEncode():
    assert(MakeUpdate() == b'\x01\xAA\x00\xAA\x04\x00\x00\x30\xFF\x00\x00\x00\x02\x88')
    assert(MakeUpdate(99.85) == b'\x01\xAA\x00\xAA\x04\x00\x02\x30\xFF\x27\x01\x00\x02\xB2')
    assert(MakeAck(True) == b'\x01\xaa\x00\xaa\x01\x00\x01\x01\x7f\x01\xd7')
    assert(MakeAck() == b'\x01\xaa\x00\xaa\x01\x00\x00\x01\xff\x02\x56')

def _TestDecode():
    def data(b):
        d = b[8:-2]
        print(len(d))
        return d
    print("graph")
    print(_DecodeGraphData(data(_GRAPH_TEST_MESSAGE)))
    print("status")
    print(_DecodeStatusData(data(_STATUS_TEST_MESSAGE)))
    print("parameter")
    print(_DecodeParameterData(data(_PARAMETER_TEST_MESSAGE)))
          

           
_GRAPH_TEST_MESSAGE=(
    b'\x00\xAA\x01\xAA'
    b'\x2A'
    b'\xF4\x61'
    b'\x31'
    b'\xF9\x63\x92\xFF'
    b'\xC0\x02\x80\xFF'
    b'\xFC'
    b'\x00'
    b'\x64'
    b'\x00'
    b'\x29'
    b'\x00'
    b'\x00\x27\x10\x00\x00\x00\x00'
    b'\x01\x27\x10\x00\x00\x00\x00'
    b'\x02\x00\x00\x00\x00\x00\x00'
    b'\x03\x00\x01\x00\x00\x00\x00'
    b'\x0A\x31')

_STATUS_TEST_MESSAGE=(
    b'\x00\xAA\x01\xAA'
    b'\x1A'
    b'\xF4\x61'
    b'\x32'
    b'\xA4\x63\x92\xFF'
    b'\xC0\x19\xC0\xF9'
    b'\xFC'
    b'\x00'
    b'\x64'
    b'\x00'
    b'\x29'
    b'\x00'
    b'\x04\x01'
    b'\x05\x00'
    b'\x06\x00'
    b'\x07\x00'
    b'\x08\x00'
    b'\x09\x00'
    b'\x09\xD1')

_PARAMETER_TEST_MESSAGE=(
    b'\x00\xAA\x01\xAA'
    b'\x40'
    b'\xF4\xE1'
    b'\x33'
    b'\xB0\x63\x92\x9C'
    b'\xC0\x24\xA4\xF9'
    b'\xFC'
    b'\x00'
    b'\x64'
    b'\x00'
    b'\x29'
    b'\x00'
    b'\x0A\x00\x00\x27\x10'
    b'\x0B\x00\x00\x75\x3A'
    b'\x0C\x00\x00\x27\x10'
    b'\x0D\x00\x00\x08\xE0'
    b'\x0E\x00\x00\x00\x00'
    b'\x0F\x00\x00\x00\x00'
    b'\x10\x00\x00\x17\x6F'
    b'\x11\x00\x00\x00\x00'
    b'\x12\x00\x00\x00\x00'
    b'\x13\x00\x00\x00\x00'
    b'\x0D\x04'
    )
