---
name: maintain-connectlife-controller
description: Maintain and operate the customized ConnectLife dehumidifier controller, including direct HijuConn API authentication/signing/retries, appliance state, SwitchBot BLE and MQTT ingestion, Telegram control, upstream backports, incident diagnosis, tests, and systemd deployment. Use whenever changing `connectlife/` or `capture/`, investigating ConnectLife/controller outages, modifying gateway or token behavior, changing appliance selection or automation safety, or deploying the controller to its production server.
---

# Maintain ConnectLife Controller

## Establish context

1. Read [`AGENTS.md`](../../../AGENTS.md) completely.
2. Read [`DEVELOPMENT.md`](../../../DEVELOPMENT.md) for local setup and tests.
3. Read [`capture/README.md`](../../../capture/README.md) for controller runtime
   configuration and deployment behavior.
4. Inspect `git status --short --branch`, the active branch, remotes, and the
   relevant source/tests before changing anything.
5. Distinguish the active `capture/` systemd implementation from the obsolete
   root cron controller. Do not repair production through
   `humidity_control.py`, `setup_humidity_control.sh`, or
   `run_humidity_control.sh`.

## Diagnose before changing

1. Establish a timeline from `systemctl show` and `journalctl`; identify the
   last success, first failure, restarts, and recoveries.
2. Separate the failure domain:
   - BLE/scanner: no SwitchBot advertisements or scanner watchdog exits.
   - MQTT: connection or topic-flow failures.
   - ConnectLife authentication: Gigya/JWT/OAuth or token rejection.
   - HijuConn gateway: signed read/write, timeout, transport, HTTP, or gateway
     result-code failure.
   - Controller logic: target selection, stale state, hysteresis, locking, or
     notification behavior.
3. Inspect endpoint names, status codes, and sanitized error types. If a 530 or
   Cloudflare 1033 appears, confirm no code path has regressed to a third-party
   relay; production must use the direct HijuConn gateway.
4. Prefer a read-only authenticated probe using the deployed virtual
   environment. Report only the minimum result, such as appliance count/type
   and power state; never include raw payloads, PUIDs, device IDs, or tokens in
   user-facing output.
5. Do not send live ON/OFF/property writes unless the user explicitly authorizes
   that device mutation.

## Change the API safely

Preserve these protocol and state contracts:

- Use Gigya/JWT and HijuConn OAuth for authentication, then signed direct EU
  HijuConn requests for production reads and writes.
- Generate the complete signed envelope and a fresh `randStr` for each attempt.
- Retry transient transport and HTTP 5xx failures with a new signature. Re-auth
  once for gateway code `100026`; retry once with a new nonce for `101005`.
- Fail closed on malformed JSON, missing response envelopes, and incomplete
  appliance snapshots. Preserve typed `LifeConnectError` classification where
  the API boundary already provides it.
- Normalize and validate the complete device list before mutating the cached
  appliance collection.
- Preserve token-cache locking, refresh-to-full-login fallback, owner-only cache
  permissions, and session cleanup.
- Preserve the fork's public contracts used by the controller:
  `fetch_status()`, `refresh_status()`, `update_properties()`, and status
  conversion including decimal strings.

Add or update tests in `connectlife/tests/` for every auth, signing, retry,
payload, or cache behavior change. Mock HTTP; never use live credentials in the
suite.

## Change the controller safely

Preserve these automation contracts:

- Select the sole appliance automatically only when exactly one exists;
  otherwise require an exact `CONNECTLIFE_APPLIANCE_PUID` match.
- Set state to unknown after failed reads or writes. Never label an incomplete
  or stale observation as fresh.
- After the target appliance has been discovered and cached, keep safety writes
  independent of fresh reads and keep network reads outside the command lock.
  Do not claim writes can bypass initial appliance discovery.
- Discard refresh results superseded by a successful command, and ignore
  reads contradicting a successful command within
  `CONNECTLIFE_COMMAND_SETTLE_SECONDS`.
- Keep all controller state on the event-loop thread; MQTT callbacks only
  schedule work with `call_soon_threadsafe`.
- Ignore sensor readings older than `SENSOR_MAX_AGE_SECONDS` for control.
- Keep ON/OFF writes idempotent and preserve the configured hysteresis.
- Preserve atomic settings writes (thresholds and mode), the sensor-offline
  confirmation window, and the opt-in offline failsafe.
- Keep Telegram secrets redacted, avoid identifiers in new logs, preserve the
  existing notification cooldown behavior, accept updates only from
  `TELEGRAM_CHAT_ID`, and back off on every polling failure.

Add focused tests in `capture/tests/test_switchbot_control.py` for state,
concurrency, selection, notification, and retry changes. Mock MQTT, Telegram,
and appliance writes.

## Evaluate upstream changes

1. Fetch or inspect the maintained `oyvindwe/connectlife` history without
   overwriting local refs or work.
2. Compare the candidate commit's behavior with the fork's API and controller
   contracts.
3. Adapt or cherry-pick only relevant behavior and tests. Do not merge the
   current upstream branch wholesale into production.
4. Treat region-specific TRIR support, energy APIs, device dumps, capability
   probes, and packaging/release work as out of scope unless requested.
5. Re-run both library and controller tests after every backport.

## Validate locally

Use `.venv/bin/python` locally, or the active environment's `python`:

```bash
PYTHONPYCACHEPREFIX=/tmp/connectlife-pycache .venv/bin/python -m compileall -q connectlife capture
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -v
git diff --check
git status --short
```

Require the localhost integration test to run outside restricted sandboxes. Use
a read-only live API probe only when local mocks cannot answer an endpoint or
account-specific question. Never make a live write part of validation.

## Deploy deliberately

Deploy only when the user authorizes production changes.

1. Connect to `maden@192.168.2.75` and inspect
   `/home/maden/connectlife` status, branch, remotes, and current commit. Stop if
   the checkout has unrelated changes.
2. Record the current known-good commit. Deploy a reviewed, identified commit
   using non-destructive, fast-forward Git operations.
3. If `pyproject.toml` or `uv.lock` changed, update the existing `venv` without
   exposing credentials.
4. Run on the host:

   ```bash
   cd /home/maden/connectlife
   PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m unittest discover -v
   git diff --check
   ```

5. Restart only the affected unit:
   - API or controller: `sudo systemctl restart switchbot-control.service`
   - BLE scanner: `sudo systemctl restart switchbot-scanner.service`
6. Verify `ActiveState=active` and `SubState=running`, then inspect the journal
   from the restart time. Confirm controller connection, MQTT connection, and
   sensor data without logging secrets.
7. Monitor at least two state-refresh intervals (default 60 seconds each) for
   retries, unknown state, authentication failures, or restarts.
   `capture/restart_services.sh` performs steps 4-7 for both units; run it
   only when both services are affected and the user authorized the restart.
8. Report the deployed commit, tests, service state, and any branch not yet
   pushed to the GitHub fork.

For a rollback, preserve dirty work, use an explicit known-good commit, and get
authorization before any history-changing or service-changing action.
