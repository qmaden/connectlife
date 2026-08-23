# Repository agent instructions

These instructions apply to the entire repository. Treat this file as the
canonical rulebook for coding agents. For API, controller, incident, or
deployment work, also load
[`.agents/skills/maintain-connectlife-controller/SKILL.md`](.agents/skills/maintain-connectlife-controller/SKILL.md).

## Repository identity

- This is a customized fork of `oyvindwe/connectlife`, not a mirror of current
  upstream.
- The fork combines a ConnectLife Python client with a production SwitchBot BLE
  → MQTT → ConnectLife dehumidifier controller.
- The production branch diverged from the v0.5.4-era API and has fork-specific
  state, notification, session, and deployment behavior. Evaluate upstream
  commits individually; do not merge upstream wholesale.

## Code map

- `connectlife/api.py`: Gigya login/JWT, HijuConn OAuth, token cache, signed
  direct-gateway reads/writes, retry and error classification.
- `connectlife/appliance.py`: appliance model, status conversion, refresh, and
  property updates.
- `connectlife/tests/`: API, signing, retry, appliance, and local integration
  tests.
- `capture/switchbot_mqtt.py`: production BLE scanner and MQTT publisher.
- `capture/switchbot_control.py`: production MQTT consumer, hysteresis,
  ConnectLife control, reconciliation, and Telegram UI.
- `capture/tests/`: controller state and concurrency regression tests.
- `dumps/` and `connectlife/test_server.py`: development fixtures and local test
  server.
- `humidity_control.py`, `setup_humidity_control.sh`, and
  `run_humidity_control.sh`: legacy cron implementation. Do not use or extend
  them for production changes.
- `control_example.py` and `analyze_device.py`: manual utilities that may access
  or mutate a real appliance. Never use them as automated validation.

## Runtime and dependencies

- Require Python 3.11 or newer.
- Prefer `.venv/` for local work. The deployed checkout uses `venv/`.
- Install the library in editable mode for development: `python -m pip install
  -e .`.
- The production `capture/` programs also import `paho-mqtt` and `bleak`. These
  runtime dependencies are currently installed on the server but are not yet
  declared in `pyproject.toml`; do not assume a clean library install provides
  them.

## Non-negotiable invariants

### ConnectLife gateway and authentication

- Send production appliance reads and writes directly to the signed EU
  HijuConn gateway. Never restore `connectlife.bapi.ovh` or another third-party
  relay.
- Treat `test_server` and its `/appliances` route as local-test compatibility
  only.
- Generate a new `randStr` and signature for every request attempt, including
  transport and HTTP 5xx retries.
- On gateway error `100026`, re-authenticate once. On `101005`, retry once with
  a fresh nonce. Do not retry permanent client errors indefinitely.
- Preserve the distinction between `LifeConnectAuthError` and other
  `LifeConnectError` failures.
- Keep the token cache owner-only (`0600`). Never print or commit credentials,
  access/refresh tokens, Telegram tokens, `.env` files, settings, or raw live
  payloads. Treat PUIDs and device IDs as sensitive: redact them from new logs,
  reports, issues, and prompts.
- Close `ConnectLifeApi` sessions on every normal or exceptional exit.

### Appliance and controller state

- Reject an appliance-list snapshot atomically if any `statusList` is missing
  or is not a mapping. Never drop/reindex devices or mark cached state fresh
  from an incomplete response.
- Select a production target by exact `CONNECTLIFE_APPLIANCE_PUID` when an
  account has multiple appliances. Never control by list position. Automatic
  selection is allowed only when exactly one appliance exists.
- Treat failed reads and commands as unknown device state. Do not infer success
  from an attempted request.
- Keep writes idempotent. Once the target appliance has been discovered and
  cached, do not require a fresh preceding GET, especially for OFF commands.
  Do not make a safety command wait for a background refresh. A startup outage
  can still block writes until initial appliance discovery succeeds.
- Do not hold the command lock during a network read. Discard slow refresh
  results that were superseded by a successful command.
- Preserve hysteresis: ON at or above the configured upper threshold, OFF at or
  below the lower threshold, and always require the lower threshold to be less
  than the upper threshold.
- Write controller settings atomically.

### Live-system safety

- Start diagnosis with logs and read-only probes. Do not send a live appliance
  command without the user's explicit authorization.
- Do not expose secrets while inspecting systemd, process environments, token
  caches, or logs.
- Do not deploy over a dirty server checkout or discard existing server changes.
- Restart only the affected service after tests pass. API/controller changes
  require `switchbot-control.service`; scanner changes require
  `switchbot-scanner.service`.

## Change workflow

1. Read the relevant source, tests, [`DEVELOPMENT.md`](DEVELOPMENT.md), and
   [`capture/README.md`](capture/README.md).
2. Inspect `git status`, the current branch, and configured remotes before
   editing. Preserve unrelated changes and generated runtime files.
3. Make the smallest coherent change that preserves the invariants above.
4. Add focused regression tests beside the affected code. Mock network and
   device writes; tests must not need real credentials or hardware.
5. Run the checks below and inspect the final diff for secrets and accidental
   generated files.
6. Update this file, the maintenance skill, and the relevant human-facing doc
   whenever an invariant, command, environment variable, or deployment path
   changes.

## Required checks

Run from the repository root with the active development environment:

```bash
PYTHONPYCACHEPREFIX=/tmp/connectlife-pycache python -m compileall -q connectlife capture
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -v
git diff --check
git status --short
```

The integration test opens a localhost socket and may skip only when a sandbox
prohibits socket creation. It must run on the deployment host. Do not replace
the standard-library `unittest` workflow with `pytest` unless the project is
deliberately migrated.

## Upstream work

- Keep `https://github.com/oyvindwe/connectlife.git` as the conceptual upstream
  and `https://github.com/qmaden/connectlife.git` as the fork.
- Compare upstream changes by behavior and tests, then backport only what the
  production controller needs.
- Preserve fork-only `fetch_status()`, `update_properties()`, persistent-session,
  fail-closed snapshot, device-code `007`, and controller concurrency contracts.
- Treat TRIR, energy statistics, dump/capability tooling, and release-only
  changes as optional unless the task explicitly needs them.

## Production deployment

- Host: `maden@192.168.2.75`
- Checkout: `/home/maden/connectlife`
- Python: `/home/maden/connectlife/venv/bin/python`
- Controller service: `switchbot-control.service`
- Scanner service: `switchbot-scanner.service`
- Controller environment: `/home/maden/connectlife/capture/.env`

Deployment is a separate, authorized step. Follow the maintenance skill:
verify a clean checkout, deploy an identified commit with fast-forward Git
operations, install dependencies only when needed, run the full suite on the
host, restart the affected unit with `sudo systemctl restart`, inspect its
journal, and monitor at least two controller refresh intervals.

## Agent documentation

- Keep this file authoritative.
- Keep `CLAUDE.md` and `.github/copilot-instructions.md` as short compatibility
  pointers; do not copy the full rulebook into them.
- Keep reusable operational procedure in the repository skill, not in new
  one-off agent README files.
