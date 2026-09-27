# ConnectLife API and SwitchBot humidity controller

This customized fork combines a Python ConnectLife client with a production
SwitchBot BLE → MQTT → ConnectLife dehumidifier controller. It supports
ConnectLife appliances from brands including Hisense, Gorenje, ASKO, ATAG, and
ETNA Connect.

Production appliance reads and writes use ConnectLife's signed HijuConn EU
gateway directly. The controller no longer depends on the third-party
`connectlife.bapi.ovh` relay.

## Repository layout

- `connectlife/`: authentication, gateway client, appliance model, tests, and
  development test server.
- `capture/switchbot_mqtt.py`: SwitchBot BLE scanner and MQTT publisher.
- `capture/switchbot_control.py`: MQTT-driven dehumidifier automation and
  Telegram control.
- `capture/README.md`: runtime configuration, safety behavior, validation, and
  deployment.
- `dumps/`: development appliance fixtures.

The root `humidity_control.py` and its setup/run shell scripts are a legacy cron
implementation. They are not the active production service.

## Development

Use Python 3.11 or newer and install this checkout, not the published upstream
package:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[controller]"
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -v
```

See [`DEVELOPMENT.md`](DEVELOPMENT.md) for the local test server and full checks.
The `capture/` daemons additionally require `paho-mqtt` and `bleak`, declared
as the `controller` extra. A library-only install (`pip install -e .`) omits
them.

Do not pass real passwords on a command line or run live appliance writes as a
test. Keep credentials in an owner-only environment file and use mocks or the
local test server for development.

## Agent contributors

Coding agents must follow [`AGENTS.md`](AGENTS.md). ConnectLife API, controller,
incident, and deployment work also uses the repository skill at
`.agents/skills/maintain-connectlife-controller/SKILL.md`.

Licensed under [GPLv3](LICENSE). This project is provided without warranty; use
care when controlling physical appliances.

The library code is based on the
[ConnectLife API proxy / MQTT Home Assistant integration](https://github.com/bilan/connectlife-api-connector)
and its [MIT-licensed source](https://github.com/bilan/connectlife-api-connector/blob/51c6b8e4562205e1c343d0cba19354f411bd5e77/composer.json#L2-L6).
