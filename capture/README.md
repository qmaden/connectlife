# SwitchBot humidity controller

`switchbot_mqtt.py` publishes the outdoor sensor to MQTT.
`switchbot_control.py` consumes those readings and controls the ConnectLife
dehumidifier.

These are the active production programs, run by `switchbot-scanner.service`
and `switchbot-control.service`. The systemd unit files live on the deployment
host and are not versioned in this repository. The root `humidity_control.py`,
`setup_humidity_control.sh`, and `run_humidity_control.sh` are an obsolete cron
implementation and must not be used for production changes.

## Data flow

1. The scanner reads one SwitchBot BLE sensor and publishes retained values and
   JSON under `switchbot/outdoor/*`.
2. The controller consumes the JSON/status topics and applies humidity
   hysteresis.
3. The controller reads and writes the selected dehumidifier through the signed
   direct HijuConn gateway.
4. Telegram provides status, settings, mode, and manual-control actions when
   configured.

## Runtime configuration

Secrets remain in `capture/.env` and must be readable only by the service user:

```text
CONNECTLIFE_USERNAME=...
CONNECTLIFE_PASSWORD=...
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
```

`switchbot-control.service` loads this file directly. The root `.env` belongs
to the legacy cron implementation and is not used by the production service.
Do not commit either file or paste their values into logs, issues, or prompts.

Controller variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONNECTLIFE_USERNAME` | required | ConnectLife account login |
| `CONNECTLIFE_PASSWORD` | required | ConnectLife account password |
| `CONNECTLIFE_APPLIANCE_PUID` | single appliance only | Stable production target |
| `MQTT_HOST` | `localhost` | MQTT broker host |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `CONNECTLIFE_STATE_REFRESH_INTERVAL` | `60` | Device reconciliation interval, seconds |
| `CONNECTLIFE_AUTO_COMMAND_RETRY_INTERVAL` | `300` | Automatic-command retry interval, seconds |
| `SENSOR_OFFLINE_CONFIRM_SECONDS` | `30` | Delay before confirming a sensor outage |
| `SWITCHBOT_SETTINGS_FILE` | `capture/settings.json` | Persistent threshold settings |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | disabled | Telegram bot credentials |
| `TELEGRAM_NOTIFY_STARTUP` | `0` | Send startup notification when `1` |
| `TELEGRAM_NOTIFY_SENSOR_ONLINE` | `0` | Send sensor-online notification when `1` |
| `TELEGRAM_COOLDOWN_SECONDS` | `1800` | Duplicate notification cooldown |

Scanner variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SWITCHBOT_TARGET_MAC` | current deployment sensor | BLE sensor address |
| `MQTT_HOST` | `localhost` | MQTT broker host |
| `MQTT_PORT` | `1883` | MQTT broker port |

Accounts with more than one ConnectLife appliance must also set
`CONNECTLIFE_APPLIANCE_PUID` to the stable PUID of the appliance being
controlled. The controller refuses automatic writes when multiple appliances
exist and no stable target is configured.

The default thresholds are ON at 60% and OFF at 55%. Telegram changes are
written atomically to `capture/settings.json`. Override that path with
`SWITCHBOT_SETTINGS_FILE`.

An MQTT `offline` event must persist for 30 seconds before an alert is sent.
Set `SENSOR_OFFLINE_CONFIRM_SECONDS` to change that confirmation window.

## Safety behavior

- Accounts with one appliance may select it automatically. Accounts with more
  than one must configure the exact PUID; list indexes are never safe targets.
- Failed ConnectLife reads or writes make device state unknown. An incomplete
  appliance snapshot is rejected rather than treated as a fresh observation.
- ON/OFF writes are idempotent and, after the target has been discovered and
  cached, do not require a fresh successful GET. An outage during initial
  discovery can still block commands.
- Background reads do not hold the write lock and cannot overwrite a newer
  successful command.
- Do not use a live property write as a health check without explicit approval.

## Validation

These checks do not start MQTT or issue appliance commands:

```bash
PYTHONPYCACHEPREFIX=/tmp/connectlife-pycache venv/bin/python -m compileall -q connectlife capture
PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m unittest discover -v
git diff --check
```

The regression suite covers stale cached state, GET timeouts, recovery without
`/status`, overlapping slow reads, API retries, and timeout diagnostics.

## Deployment

Before restarting an affected systemd unit:

1. Confirm `capture/switchbot_control.py`, `capture/switchbot_mqtt.py`, and
   `capture/.env` exist.
2. Run the validation commands above.
3. Confirm `capture/settings.json`, if present, has the intended thresholds.
4. Rotate any Telegram token previously exposed in logs and update `.env`.
5. For API/controller changes, restart only `switchbot-control.service`. For
   scanner changes, restart `switchbot-scanner.service` and verify MQTT sensor
   messages before restarting the controller if needed.
6. Watch `journalctl -u switchbot-control.service -f` for API connection, MQTT
   connection, state reconciliation, and errors. Monitor at least two state
   refresh intervals; do not force an ON/OFF cycle solely as a deployment test.

After initial appliance discovery, the controller sends idempotent writes
without requiring a fresh appliance-list GET. Background reads never hold the
write lock, so a slow state refresh cannot delay an OFF command.

Coding agents must also follow [`../AGENTS.md`](../AGENTS.md) and the
`maintain-connectlife-controller` repository skill.
