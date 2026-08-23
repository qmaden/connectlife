# Development environment

Use Python 3.11 or newer. `pyenv` is optional; any compatible Python can create
the virtual environment.

## Install the checkout

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

The production programs and full test suite also import `paho-mqtt` and
`bleak`. They are installed in the deployed environment but are not currently
declared as library dependencies. Install them explicitly before developing or
validating the BLE/MQTT services:

```bash
python -m pip install paho-mqtt bleak
```

## Validation

Run all checks from the repository root:

```bash
PYTHONPYCACHEPREFIX=/tmp/connectlife-pycache python -m compileall -q connectlife capture
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -v
git diff --check
git status --short
```

The integration test opens a localhost socket. A restricted sandbox may skip
it, but it must pass on the deployment host. Tests must use mocks or the local
test server and must not read real credentials or control a real appliance.

## Test server

Test server that mocks the ConnectLife API. Runs on `http://localhost:8080`.

The server reads JSON files in the current directory and serves them as
appliances. Property updates are not persisted. It validates only that the
`puid` and property exist, and assumes all known properties and values are
writable.

```bash
cd dumps
python -m test_server
```

To use the test server, provide its URL to the client:

```python
from connectlife.api import ConnectLifeApi
api = ConnectLifeApi(username="user@example.com", password="password", test_server="http://localhost:8080")
```

The explicit `test_server` mode retains the legacy local `/appliances` route.
Production uses the signed direct HijuConn gateway. Gateway signing, retry, and
error behavior are covered by mocked tests in `connectlife/tests/test_api.py`.

Close the API session when finished:

```python
await api.close()
```

For repository-wide coding rules, see [`AGENTS.md`](AGENTS.md). For API,
controller, incident, and deployment work, also follow
`.agents/skills/maintain-connectlife-controller/SKILL.md`.
