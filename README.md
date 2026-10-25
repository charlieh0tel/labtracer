# labtracer

Serial driver for Jeio Tech oven controllers.

```sh
uv sync
uv run labtracer-oven /dev/ttyUSB0   # demo: 40 C, then 45 C, then off
uv run pytest
```
