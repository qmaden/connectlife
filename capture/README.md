# SwitchBot humidity controller

`switchbot_mqtt.py` publishes the outdoor sensor to MQTT.
`switchbot_control.py` consumes those readings and controls the ConnectLife
dehumidifier.

## Runtime configuration

Secrets remain in `capture/.env` and must be readable only by the service user:

```text
CONNECTLIFE_USERNAME=...
CONNECTLIFE_PASSWORD=...
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
```

The default thresholds are ON at 60% and OFF at 55%. Telegram changes are
written atomically to `capture/settings.json`. Override that path with
`SWITCHBOT_SETTINGS_FILE`.

An MQTT `offline` event must persist for 30 seconds before an alert is sent.
Set `SENSOR_OFFLINE_CONFIRM_SECONDS` to change that confirmation window.

## Validation

These checks do not start MQTT or issue appliance commands:

```bash
venv/bin/python -m compileall -q connectlife capture
venv/bin/python -m unittest discover -v
```

The regression suite covers stale cached state, GET timeouts, recovery without
`/status`, overlapping slow reads, API retries, and timeout diagnostics.

## Deployment

Before restarting either systemd unit:

1. Confirm `capture/switchbot_control.py`, `capture/switchbot_mqtt.py`, and
   `capture/.env` exist.
2. Run the validation commands above.
3. Confirm `capture/settings.json`, if present, has the intended thresholds.
4. Rotate any Telegram token previously exposed in logs and update `.env`.
5. Restart `switchbot-scanner.service`, verify sensor messages, then restart
   `switchbot-control.service`.
6. Watch `journalctl -u switchbot-control.service -f` for API connection,
   state reconciliation, and one complete ON/OFF cycle.

The controller sends idempotent writes without requiring a successful
appliance-list GET first. Background reads never hold the write lock, so a slow
state refresh cannot delay an OFF command.
