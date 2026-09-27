#!/usr/bin/env bash
# Validate this checkout, restart both SwitchBot services, and verify them.
#
# Usage: capture/restart_services.sh [monitor-seconds]
#   monitor-seconds  how long to follow the journal after the restart
#                    (default 130: two default 60 s controller refresh cycles)
#
# Runs on the deployment host. Needs sudo only for `systemctl restart`.
# Never reads capture/.env and never sends an appliance command itself.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$REPO_DIR/venv/bin/python"
SCANNER=switchbot-scanner.service
CONTROLLER=switchbot-control.service
MONITOR_SECONDS="${1:-130}"
LEGACY_LOG="$REPO_DIR/capture/switchbot_control.log"

cd "$REPO_DIR"

echo "==> Checkout: $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"
if [[ -n "$(git status --porcelain)" ]]; then
    echo "    Uncommitted changes will run:"
    git status --short | sed 's/^/      /'
fi

echo "==> Checking runtime imports"
"$PYTHON" -c "import aiohttp, cryptography, paho.mqtt.client, bleak"

echo "==> Running checks (nothing is restarted if these fail)"
PYTHONPYCACHEPREFIX=/tmp/connectlife-pycache "$PYTHON" -m compileall -q connectlife capture
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -m unittest discover
git diff --check

since="$(date '+%Y-%m-%d %H:%M:%S')"

# Scanner first so the controller starts against fresh sensor data.
for service in "$SCANNER" "$CONTROLLER"; do
    echo "==> Restarting $service"
    sudo systemctl restart "$service"
done

sleep 5
# Take baselines after the restart: the old processes still log their own
# shutdown lines while being stopped, and those must not count against the
# new code.
log_size_before="$(stat -c %s "$LEGACY_LOG" 2>/dev/null || echo 0)"
declare -A restarts_after_start pid_after_start
for service in "$SCANNER" "$CONTROLLER"; do
    restarts_after_start[$service]="$(systemctl show -p NRestarts --value "$service")"
    pid_after_start[$service]="$(systemctl show -p MainPID --value "$service")"
done

echo "==> Following the journal for ${MONITOR_SECONDS}s (Ctrl+C stops early)"
timeout "$MONITOR_SECONDS" \
    journalctl -u "$SCANNER" -u "$CONTROLLER" --since "$since" -f --no-pager \
    || true

echo
echo "==> Verification"
problems=0

ok() { echo "  [ok]   $1"; }
bad() { echo "  [FAIL] $1"; problems=1; }

for service in "$SCANNER" "$CONTROLLER"; do
    state="$(systemctl show -p ActiveState --value "$service")"
    sub="$(systemctl show -p SubState --value "$service")"
    restarts="$(systemctl show -p NRestarts --value "$service")"
    if [[ "$state" == active && "$sub" == running ]]; then
        ok "$service is $state/$sub"
    else
        bad "$service is $state/$sub"
    fi
    if [[ "$restarts" == "${restarts_after_start[$service]}" ]]; then
        ok "$service did not crash-restart while monitored"
    else
        bad "$service crash-restarted while monitored"
    fi
done

# Only the new processes' own lines count.
new_process_logs() {
    journalctl "_PID=${pid_after_start[$1]}" --since "$since" --no-pager -o cat
}
scanner_logs="$(new_process_logs "$SCANNER")"
controller_logs="$(new_process_logs "$CONTROLLER")"

expect() {
    local description="$1" logs="$2" pattern="$3"
    if grep -q -- "$pattern" <<<"$logs"; then ok "$description"; else bad "$description"; fi
}

expect "scanner connected to MQTT" "$scanner_logs" "MQTT connected"
expect "scanner sees the sensor" "$scanner_logs" "Sensor is ONLINE"
expect "controller connected to ConnectLife" "$controller_logs" "Connected to '"
expect "controller connected to MQTT" "$controller_logs" "MQTT connected"
expect "controller received sensor data" "$controller_logs" "First sensor data received"
expect "controller started Telegram polling" "$controller_logs" "Telegram bot polling started"

errors="$(grep -cE '\[ERROR\]|Traceback|Background task failed|MQTT message handling failed' \
    <<<"$scanner_logs"$'\n'"$controller_logs" || true)"
warnings="$(grep -c '\[WARNING\]' <<<"$scanner_logs"$'\n'"$controller_logs" || true)"
if [[ "$errors" -eq 0 ]]; then ok "no errors logged"; else bad "$errors error line(s) logged"; fi
echo "  [info] $warnings warning line(s) logged (transient API retries can be normal)"

log_size_after="$(stat -c %s "$LEGACY_LOG" 2>/dev/null || echo 0)"
if [[ "$log_size_after" == "$log_size_before" ]]; then
    ok "controller logs go to the journal only (log file unchanged)"
else
    bad "capture/switchbot_control.log is still growing (old code running?)"
fi

echo
if [[ "$problems" -eq 0 ]]; then
    echo "All checks passed."
else
    echo "Some checks failed. Full log since restart:"
    echo "  journalctl -u $SCANNER -u $CONTROLLER --since '$since' --no-pager"
    exit 1
fi
