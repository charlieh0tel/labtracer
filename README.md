# labtracer

A Python driver for Jeio Tech laboratory ovens over RS-232, using the
LabTracer protocol that Jeio Tech's own PC software speaks. It reads the
process temperature, setpoint, heater duty cycle and run state, and can
change the setpoint and start or stop the oven.

The protocol reference is in
[`third_party/Lab_Tracer_Commands.pdf`](third_party/Lab_Tracer_Commands.pdf).
It also covers Jeio Tech incubators and water baths, which may work but
haven't been tried.

## Hookup

Connect the oven's RS-232 port to the computer with a straight-through
cable (pins 2, 3 and 5). The link runs at 19200 baud, 8N1.

## Usage

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run labtracer-oven /dev/ttyUSB0
```

That demo sets 40 °C and turns the oven on, raises the setpoint to 45 °C,
turns it off, and prints readings throughout.

From Python:

```python
import serial
from labtracer.oven import Oven

with serial.Serial("/dev/ttyUSB0", baudrate=19200) as port, Oven(port) as oven:
    oven.SetSetpointC(60.0)
    oven.SetOn(True)
    ...
    print(oven.GetCurrentProcessValueC(), oven.GetRunning())
```

## Safety behavior

The protocol has no explicit "on" or "off" command, only a run/stop toggle,
so the driver is careful about when it sends one:

- The oven isn't turned on until the controller reports back the setpoint
  you set, so it never starts heating toward a stale one.
- Only one toggle is in flight at a time; the driver waits for the status
  to confirm it before sending another.
- When the `with` block exits for any reason, including Ctrl-C, SIGTERM or
  an exception, the driver sets the setpoint to 0 °C and turns the oven
  off, then confirms it. A second Ctrl-C doesn't cut that short.
- If the oven reports overtemp, the driver logs an error and turns it off.
- If comms drop, the driver keeps retrying, and the getters raise
  `ConnectionError` instead of returning stale readings.

This has been checked against a simulated controller, not every oven model.
Watch the first run on real hardware.

## Development

```sh
uv run pytest
uvx ruff check --preview . && uvx ruff format --check .
```

See [AGENTS.md](AGENTS.md) for project conventions.

## License

MIT; see [LICENSE](LICENSE). The protocol manual in `third_party/` belongs to
Jeio Tech and isn't covered by it; see [third_party/NOTICE](third_party/NOTICE).
