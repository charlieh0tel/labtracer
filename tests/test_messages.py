import pytest

from labtracer import messages

# Example frames from third_party/Lab_Tracer_Commands.pdf, section 5.
GRAPH_FRAME = (
    b"\x00\xaa\x01\xaa"
    b"\x2a"
    b"\xf4\x61"
    b"\x31"
    b"\xf9\x63\x92\xff"
    b"\xc0\x02\x80\xff"
    b"\xfc"
    b"\x00"
    b"\x64"
    b"\x00"
    b"\x29"
    b"\x00"
    b"\x00\x27\x10\x00\x00\x00\x00"
    b"\x01\x27\x10\x00\x00\x00\x00"
    b"\x02\x00\x00\x00\x00\x00\x00"
    b"\x03\x00\x01\x00\x00\x00\x00"
    b"\x0a\x31"
)

STATUS_FRAME = (
    b"\x00\xaa\x01\xaa"
    b"\x1a"
    b"\xf4\x61"
    b"\x32"
    b"\xa4\x63\x92\xff"
    b"\xc0\x19\xc0\xf9"
    b"\xfc"
    b"\x00"
    b"\x64"
    b"\x00"
    b"\x29"
    b"\x00"
    b"\x04\x01"
    b"\x05\x00"
    b"\x06\x00"
    b"\x07\x00"
    b"\x08\x00"
    b"\x09\x00"
    b"\x09\xd1"
)

PARAMETER_FRAME = (
    b"\x00\xaa\x01\xaa"
    b"\x40"
    b"\xf4\xe1"
    b"\x33"
    b"\xb0\x63\x92\x9c"
    b"\xc0\x24\xa4\xf9"
    b"\xfc"
    b"\x00"
    b"\x64"
    b"\x00"
    b"\x29"
    b"\x00"
    b"\x0a\x00\x00\x27\x10"
    b"\x0b\x00\x00\x75\x3a"
    b"\x0c\x00\x00\x27\x10"
    b"\x0d\x00\x00\x08\xe0"
    b"\x0e\x00\x00\x00\x00"
    b"\x0f\x00\x00\x00\x00"
    b"\x10\x00\x00\x17\x6f"
    b"\x11\x00\x00\x00\x00"
    b"\x12\x00\x00\x00\x00"
    b"\x13\x00\x00\x00\x00"
    b"\x0d\x04"
)


def Decode(frame: bytes):
    prolog = messages.DecodeProlog(frame[: messages.PROLOG_LENGTH])
    return messages.DecodeResponse(prolog, frame[messages.PROLOG_LENGTH :])


def test_make_update():
    assert (
        messages.MakeUpdate()
        == b"\x01\xaa\x00\xaa\x04\x00\x00\x30\xff\x00\x00\x00\x02\x88"
    )
    assert (
        messages.MakeUpdate(99.85)
        == b"\x01\xaa\x00\xaa\x04\x00\x02\x30\xff\x27\x01\x00\x02\xb2"
    )


def test_make_ack():
    assert messages.MakeAck(True) == b"\x01\xaa\x00\xaa\x01\x00\x01\x01\x7f\x01\xd7"
    assert messages.MakeAck() == b"\x01\xaa\x00\xaa\x01\x00\x00\x01\xff\x02\x56"


def test_encode_temp_rounds():
    assert messages.EncodeTempC(0.29) == 29
    assert messages.EncodeTempC(-0.29) == -29
    assert messages.EncodeTempC(327.67) == 32767
    with pytest.raises(ValueError):
        messages.EncodeTempC(327.68)


def test_decode_graph():
    assert Decode(GRAPH_FRAME) == (
        messages.Command.Graph,
        {
            "process_value_C": 100.0,
            "heat_value_percent": 100.0,
        },
    )


def test_decode_status():
    assert Decode(STATUS_FRAME) == (
        messages.Command.Status,
        {
            "led": 0xFC,
            "buzzer": 0x64,
            "model": 0x29,
            "run": True,
            "overtemp": False,
        },
    )


def test_decode_parameter():
    assert Decode(PARAMETER_FRAME) == (
        messages.Command.Parameter,
        {
            "led": 0xFC,
            "buzzer": 0x64,
            "model": 0x29,
            "process_value_C": 100.0,
            "setpoint_C": 300.1,
            "heater_duty_cycle_percent": 100.0,
            "runtime_seconds": 2272,
            "power_frequency_hz": 59.99,
        },
    )


def test_bad_header():
    with pytest.raises(messages.ProtocolError, match="header"):
        Decode(b"\x01" + GRAPH_FRAME[1:])


def test_short_prolog():
    with pytest.raises(messages.ProtocolError, match="short prolog"):
        messages.DecodeProlog(GRAPH_FRAME[:3])


def test_short_body():
    with pytest.raises(messages.ProtocolError, match="short"):
        Decode(GRAPH_FRAME[:-5])


def test_bad_checksum():
    corrupt = bytearray(GRAPH_FRAME)
    corrupt[20] ^= 0x01
    with pytest.raises(messages.ProtocolError, match="checksum"):
        Decode(bytes(corrupt))


def test_length_mismatch():
    # A Status-sized frame claiming to be a Graph.
    frame = bytearray(STATUS_FRAME)
    frame[7] = messages.Command.Graph
    with pytest.raises(messages.ProtocolError, match="length"):
        Decode(bytes(frame))


def test_unknown_command():
    frame = bytearray(GRAPH_FRAME)
    frame[7] = 0x99
    with pytest.raises(messages.ProtocolError, match="unknown command"):
        Decode(bytes(frame))


def test_bad_instance():
    frame = bytearray(STATUS_FRAME[:-2])
    frame[8 + 14] = 0x05  # run instance
    frame = messages._AppendChecksum(bytes(frame))
    with pytest.raises(messages.ProtocolError, match="run instance"):
        Decode(frame)
