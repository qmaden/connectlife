#!/usr/bin/env python3
"""
SwitchBot MQTT to ConnectLife dehumidifier controller with Telegram UI.

The controller treats its appliance state as a time-limited observation. A
stale cached value is never sufficient reason to suppress a requested state
change, and writes do not depend on an appliance-list read succeeding first.
"""

import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path

import aiohttp
import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from connectlife.api import ConnectLifeApi


# Static configuration

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_CLIENT_ID = "switchbot-controller"
SENSOR_TOPIC = "switchbot/outdoor"
CONTROL_TOPIC = "switchbot/control"

CL_USERNAME = os.environ.get("CONNECTLIFE_USERNAME", "")
CL_PASSWORD = os.environ.get("CONNECTLIFE_PASSWORD", "")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DEVICE_POWER_KEY = "t_power"
DEVICE_POWER_ON = 1
DEVICE_POWER_OFF = 0
DEVICE_WORK_MODE_KEY = "t_work_mode"
DEVICE_WORK_MODE_MANUAL = 1
DEVICE_FAN_SPEED_KEY = "t_fan_speed"
DEVICE_FAN_SPEED_HIGH = 2
DEVICE_TARGET_HUMIDITY_KEY = "t_humidity"
DEVICE_TARGET_HUMIDITY = 30
TARGET_APPLIANCE_PUID = os.environ.get("CONNECTLIFE_APPLIANCE_PUID", "").strip()

STATE_REFRESH_INTERVAL = int(
    os.environ.get("CONNECTLIFE_STATE_REFRESH_INTERVAL", "60")
)
AUTO_COMMAND_RETRY_INTERVAL = int(
    os.environ.get("CONNECTLIFE_AUTO_COMMAND_RETRY_INTERVAL", "300")
)
TELEGRAM_NOTIFY_STARTUP = os.environ.get("TELEGRAM_NOTIFY_STARTUP", "0") == "1"
TELEGRAM_NOTIFY_SENSOR_ONLINE = (
    os.environ.get("TELEGRAM_NOTIFY_SENSOR_ONLINE", "0") == "1"
)
TG_COOLDOWN = int(os.environ.get("TELEGRAM_COOLDOWN_SECONDS", "1800"))
SENSOR_OFFLINE_CONFIRM_SECONDS = int(
    os.environ.get("SENSOR_OFFLINE_CONFIRM_SECONDS", "30")
)
# Readings older than this (e.g. a retained MQTT message left by a dead
# scanner) are displayed but never drive control or availability decisions.
SENSOR_MAX_AGE_SECONDS = int(os.environ.get("SENSOR_MAX_AGE_SECONDS", "180"))
# Turn the dehumidifier OFF after a confirmed sensor outage lasts this long in
# auto mode. 0 disables the failsafe and keeps the last commanded state.
SENSOR_OFFLINE_FAILSAFE_SECONDS = int(
    os.environ.get("SENSOR_OFFLINE_FAILSAFE_SECONDS", "0")
)
# The cloud can return the pre-command state for a short time after a write.
# Reads contradicting a successful command inside this window are ignored.
COMMAND_SETTLE_SECONDS = int(
    os.environ.get("CONNECTLIFE_COMMAND_SETTLE_SECONDS", "30")
)
TELEGRAM_POLL_BASE_BACKOFF = 5
TELEGRAM_POLL_MAX_BACKOFF = 60

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = Path(
    os.environ.get("SWITCHBOT_SETTINGS_FILE", str(BASE_DIR / "settings.json"))
)
LOG_FILE = BASE_DIR / "switchbot_control.log"


# Logging

log = logging.getLogger("switchbot-control")
if not log.handlers:
    # Under systemd, stdout already goes to the persistent journal with
    # timestamps. Writing a rotating file as well would store every line twice.
    under_journal = bool(os.environ.get("JOURNAL_STREAM"))
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if not under_journal:
        handlers.append(
            logging.handlers.RotatingFileHandler(
                LOG_FILE,
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
            )
        )
    formatter = logging.Formatter(
        "[%(levelname)s] %(message)s"
        if under_journal
        else "%(asctime)s [%(levelname)s] %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    for handler in handlers:
        handler.setFormatter(formatter)
        log.addHandler(handler)
log.setLevel(logging.INFO)
log.propagate = False

# Include API retry diagnostics in the same destinations.
api_log = logging.getLogger("connectlife.api")
api_log.setLevel(logging.INFO)
api_log.handlers = log.handlers
api_log.propagate = False


# Persistent settings

VALID_MODES = ("auto", "manual")
DEFAULT_SETTINGS = {
    "humidity_on_above": 60,
    "humidity_off_below": 55,
    "mode": "auto",
}


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with SETTINGS_FILE.open() as file:
                data = json.load(file)
            settings = {**DEFAULT_SETTINGS, **data}
            if settings["humidity_off_below"] >= settings["humidity_on_above"]:
                raise ValueError("OFF threshold must be below ON threshold")
            if settings["mode"] not in VALID_MODES:
                raise ValueError(f"mode must be one of {VALID_MODES}")
            return settings
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as err:
            log.error("Invalid settings file %s: %s", SETTINGS_FILE, err)
    return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(f"{SETTINGS_FILE.suffix}.tmp")
    with temporary.open("w") as file:
        json.dump(settings, file, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, SETTINGS_FILE)


_settings = load_settings()


# Runtime state

_mode = _settings["mode"]
_device_state: bool | None = None
_cl_api: ConnectLifeApi | None = None
_appliances = None
_loop = None
_last_sensor: dict = {}
_start_time = time.monotonic()
_first_data_seen = False
_sensor_offline = False
_sensor_offline_pending = False
_sensor_offline_generation = 0
_last_sensor_data_time = 0.0
STARTUP_GRACE = 15

_last_state_refresh = 0.0
_last_command_time = 0.0
_last_command_state: bool | None = None
_command_lock = asyncio.Lock()
_connect_lock = asyncio.Lock()
_last_auto_command_attempt = {
    True: 0.0,
    False: 0.0,
}

_api_outage_active = False
_api_failure_count = 0
_api_last_error = ""
_tg_last_sent: dict[str, float] = {}
_tg_session: aiohttp.ClientSession | None = None
_background_tasks: set[asyncio.Task] = set()


def spawn(coroutine) -> asyncio.Task:
    """Run a coroutine on the current loop and keep a reference until done."""
    task = asyncio.get_running_loop().create_task(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_task_done)
    return task


def _background_task_done(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.error(
            "Background task failed: %s",
            format_exception(task.exception()),
        )


# Telegram helpers

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def format_exception(err: BaseException) -> str:
    detail = str(err).strip() or repr(err)
    if TELEGRAM_TOKEN:
        detail = detail.replace(TELEGRAM_TOKEN, "<redacted>")
    if TG_API:
        detail = detail.replace(TG_API, "<telegram-api>")
    return f"{type(err).__name__}: {detail}"


class TelegramApiError(Exception):
    """The Bot API answered, but with ok=false."""

    def __init__(self, method: str, body):
        body = body if isinstance(body, dict) else {}
        parameters = body.get("parameters")
        self.description = str(body.get("description") or "no description")
        self.retry_after = (
            parameters.get("retry_after")
            if isinstance(parameters, dict)
            else None
        )
        super().__init__(
            f"{method} returned error {body.get('error_code')}: "
            f"{self.description}"
        )


async def _tg_request(method: str, **kwargs) -> dict:
    """Call the Bot API over one shared session; raise on any failure."""
    global _tg_session
    if _tg_session is None or _tg_session.closed:
        _tg_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=35, connect=10)
        )
    async with _tg_session.post(f"{TG_API}/{method}", json=kwargs) as response:
        body = await response.json(content_type=None)
    if not isinstance(body, dict) or not body.get("ok"):
        raise TelegramApiError(method, body)
    return body


async def close_telegram_session() -> None:
    global _tg_session
    if _tg_session is not None and not _tg_session.closed:
        await _tg_session.close()
    _tg_session = None


async def _tg(method: str, **kwargs):
    if not TELEGRAM_TOKEN:
        return {}
    try:
        return await _tg_request(method, **kwargs)
    except asyncio.CancelledError:
        raise
    except TelegramApiError as err:
        # Re-rendering an unchanged menu (e.g. Refresh) is expected.
        level = (
            logging.DEBUG
            if "message is not modified" in err.description
            else logging.WARNING
        )
        log.log(level, "Telegram %s failed: %s", method, format_exception(err))
        return {}
    except Exception as err:
        log.warning("Telegram %s failed: %s", method, format_exception(err))
        return {}


async def tg_send(text: str, reply_markup=None):
    kwargs = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    return await _tg("sendMessage", **kwargs)


async def tg_edit(chat_id, message_id: int, text: str, reply_markup=None):
    kwargs = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    return await _tg("editMessageText", **kwargs)


async def tg_answer(callback_query_id: str, text: str = ""):
    await _tg(
        "answerCallbackQuery",
        callback_query_id=callback_query_id,
        text=text,
    )


async def tg_notify(text: str, cooldown_key: str | None = None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    if cooldown_key:
        last = _tg_last_sent.get(cooldown_key, 0)
        if time.monotonic() - last < TG_COOLDOWN:
            log.debug("Telegram suppressed by cooldown: %s", cooldown_key)
            return
        _tg_last_sent[cooldown_key] = time.monotonic()
    await tg_send(text)


def tg_fire(text: str, cooldown_key: str | None = None):
    if _loop and _loop.is_running():
        spawn(tg_notify(text, cooldown_key))


# Menu builders

def _ik(buttons: list[list[dict]]) -> dict:
    return {"inline_keyboard": buttons}


def main_menu_kb() -> dict:
    return _ik(
        [
            [
                {"text": "📊 Status", "callback_data": "menu:status"},
                {"text": "🎛 Control", "callback_data": "menu:control"},
            ],
            [{"text": "⚙️ Settings", "callback_data": "menu:settings"}],
        ]
    )


def control_kb() -> dict:
    mode_button = (
        {"text": "🔄 Switch to Manual", "callback_data": "ctrl:mode:manual"}
        if _mode == "auto"
        else {"text": "🔄 Switch to Auto", "callback_data": "ctrl:mode:auto"}
    )
    rows = [[mode_button]]
    if _mode == "manual":
        rows.append(
            [
                {"text": "✅ Turn ON", "callback_data": "ctrl:on"},
                {"text": "⏹ Turn OFF", "callback_data": "ctrl:off"},
            ]
        )
    rows.append([{"text": "« Back", "callback_data": "menu:main"}])
    return _ik(rows)


def settings_kb() -> dict:
    on_threshold = _settings["humidity_on_above"]
    off_threshold = _settings["humidity_off_below"]
    return _ik(
        [
            [{"text": "💧 Humidity ON threshold", "callback_data": "noop"}],
            [
                {"text": "−5", "callback_data": "set:hum_on:-5"},
                {"text": f"{on_threshold}%", "callback_data": "noop"},
                {"text": "+5", "callback_data": "set:hum_on:+5"},
            ],
            [{"text": "💧 Humidity OFF threshold", "callback_data": "noop"}],
            [
                {"text": "−5", "callback_data": "set:hum_off:-5"},
                {"text": f"{off_threshold}%", "callback_data": "noop"},
                {"text": "+5", "callback_data": "set:hum_off:+5"},
            ],
            [{"text": "« Back", "callback_data": "menu:main"}],
        ]
    )


def state_age(now: float | None = None) -> float | None:
    if not _last_state_refresh:
        return None
    return (now if now is not None else time.monotonic()) - _last_state_refresh


def sensor_reading_age(data: dict, now: float | None = None) -> float | None:
    """Return the age of a scanner reading, or None if it has no timestamp."""
    timestamp = data.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return None
    return (now if now is not None else time.time()) - timestamp


def sensor_reading_is_fresh(data: dict, now: float | None = None) -> bool:
    if not data:
        return False
    age = sensor_reading_age(data, now)
    return age is None or age <= SENSOR_MAX_AGE_SECONDS


def state_is_fresh(now: float | None = None) -> bool:
    age = state_age(now)
    return (
        _device_state is not None
        and age is not None
        and age < STATE_REFRESH_INTERVAL
    )


def desired_state_is_fresh(turn_on: bool, now: float | None = None) -> bool:
    if not state_is_fresh(now) or _device_state != turn_on:
        return False
    return cached_state_matches_desired(turn_on)


def cached_state_matches_desired(turn_on: bool) -> bool:
    """Return whether the last known state already matches an auto decision.

    Auto control may use an old-but-known matching value because the background
    reconciler continuously refreshes it. A failed reconciliation invalidates
    the state to None, which re-enables safety enforcement.
    """
    if _device_state != turn_on:
        return False
    if not turn_on:
        return True
    try:
        appliance = select_target_appliance(_appliances)
    except RuntimeError:
        return False
    return bool(
        appliance
        and current_on_settings_match(appliance)
    )


def status_text() -> str:
    data = _last_sensor
    device = (
        "ON ✅"
        if _device_state is True
        else "OFF ⏹"
        if _device_state is False
        else "unknown ❓"
    )
    age = state_age()
    state_detail = "unverified" if age is None else f"checked {int(age)}s ago"
    if not data:
        return "⏳ No sensor data received yet."
    reading_age = sensor_reading_age(data)
    reading_detail = ""
    if reading_age is not None:
        reading_detail = f"🕒 Reading age: {max(0, int(reading_age))}s"
        if not sensor_reading_is_fresh(data):
            reading_detail += " (stale, ignored for control)"
        reading_detail += "\n"
    return (
        f"<b>Outdoor Sensor</b>\n"
        f"🌡 Temp: {data.get('temperature_c')}°C / "
        f"{data.get('temperature_f')}°F\n"
        f"💧 Humidity: {data.get('humidity')}%\n"
        f"🌫 Dew point: {data.get('dew_point')}°C\n"
        f"📊 Abs. humidity: {data.get('absolute_humidity')} g/m³\n"
        f"📉 VPD: {data.get('vpd')} kPa\n"
        f"🔋 Battery: {data.get('battery')}%\n"
        f"📶 RSSI: {data.get('rssi')} dBm\n"
        f"{reading_detail}\n"
        f"<b>Dehumidifier:</b> {device} ({state_detail})\n"
        f"<b>API:</b> {'degraded ❌' if _api_outage_active else 'available ✅'}\n"
        f"<b>Mode:</b> {_mode}\n"
        f"<b>Thresholds:</b> ON ≥{_settings['humidity_on_above']}%  "
        f"OFF ≤{_settings['humidity_off_below']}%"
    )


def control_text(result: str = "") -> str:
    label = "🤖 Auto" if _mode == "auto" else "🖐 Manual"
    text = f"🎛 <b>Control</b>\nCurrent mode: <b>{label}</b>"
    return f"{text}\n\n{result}" if result else text


def settings_text() -> str:
    return (
        "⚙️ <b>Settings</b>\n\n"
        f"Turn dehumidifier <b>ON</b> when humidity ≥ "
        f"{_settings['humidity_on_above']}%\n"
        f"Turn dehumidifier <b>OFF</b> when humidity ≤ "
        f"{_settings['humidity_off_below']}%"
    )


def sensor_summary() -> str:
    data = _last_sensor
    if not data:
        return ""
    return (
        "\n\n<b>Current readings:</b>\n"
        f"🌡 {data.get('temperature_c')}°C / {data.get('temperature_f')}°F  "
        f"💧 {data.get('humidity')}%  🔋 {data.get('battery')}%"
    )


# ConnectLife state and commands

def is_power_on(value) -> bool:
    return value in (
        DEVICE_POWER_ON,
        str(DEVICE_POWER_ON),
        "on",
        "ON",
        "running",
        "RUNNING",
        True,
    )


def select_target_appliance(appliances):
    """Select one appliance without relying on unstable list ordering."""
    if not appliances:
        raise RuntimeError("No appliances returned for this account")
    if TARGET_APPLIANCE_PUID:
        matches = [
            appliance
            for appliance in appliances
            if appliance.puid == TARGET_APPLIANCE_PUID
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "Configured CONNECTLIFE_APPLIANCE_PUID did not match exactly "
                "one appliance"
            )
        return matches[0]
    if len(appliances) == 1:
        return appliances[0]
    raise RuntimeError(
        "Multiple ConnectLife appliances found; configure "
        "CONNECTLIFE_APPLIANCE_PUID before automatic control"
    )


def desired_on_properties(appliance) -> dict:
    desired = {
        DEVICE_POWER_KEY: DEVICE_POWER_ON,
        DEVICE_WORK_MODE_KEY: DEVICE_WORK_MODE_MANUAL,
        DEVICE_FAN_SPEED_KEY: DEVICE_FAN_SPEED_HIGH,
        DEVICE_TARGET_HUMIDITY_KEY: DEVICE_TARGET_HUMIDITY,
    }
    available = appliance.status_list.keys()
    return {key: value for key, value in desired.items() if key in available}


def current_on_settings_match(appliance) -> bool:
    expected = desired_on_properties(appliance)
    expected.pop(DEVICE_POWER_KEY, None)
    return all(
        str(appliance.status_list.get(key)) == str(value)
        for key, value in expected.items()
    )


async def record_api_failure(operation: str, err: BaseException) -> None:
    global _api_outage_active, _api_failure_count, _api_last_error
    global _device_state, _last_state_refresh

    _device_state = None
    _last_state_refresh = 0.0
    _api_failure_count += 1
    _api_last_error = format_exception(err)

    if not _api_outage_active:
        _api_outage_active = True
        log.error("ConnectLife API outage during %s: %s", operation, _api_last_error)
        await tg_notify(
            "❌ <b>ConnectLife API unavailable</b>\n"
            f"Operation: {operation}\n"
            f"Error: {_api_last_error}\n"
            "Device state is now treated as unknown; automatic retries continue."
        )
    else:
        log.warning(
            "ConnectLife API still unavailable during %s "
            "(failure %d): %s",
            operation,
            _api_failure_count,
            _api_last_error,
        )


async def record_api_recovery(operation: str) -> None:
    global _api_outage_active, _api_failure_count, _api_last_error
    if not _api_outage_active:
        return
    failures = _api_failure_count
    _api_outage_active = False
    _api_failure_count = 0
    _api_last_error = ""
    # Commands throttled during the outage should go out on the next reading
    # instead of waiting for the rest of AUTO_COMMAND_RETRY_INTERVAL.
    for turn_on in _last_auto_command_attempt:
        _last_auto_command_attempt[turn_on] = 0.0
    log.info(
        "ConnectLife API recovered during %s after %d failures",
        operation,
        failures,
    )
    await tg_notify(
        "✅ <b>ConnectLife API recovered</b>\n"
        f"Operation: {operation}\n"
        f"Recorded failures: {failures}"
    )


async def connectlife_login() -> bool:
    global _cl_api, _appliances, _device_state, _last_state_refresh

    if not CL_USERNAME or not CL_PASSWORD:
        log.error("CONNECTLIFE_USERNAME or CONNECTLIFE_PASSWORD is not configured")
        return False

    async with _connect_lock:
        if _cl_api is None:
            _cl_api = ConnectLifeApi(CL_USERNAME, CL_PASSWORD)
        try:
            await _cl_api.login()
            appliances = await _cl_api.get_appliances()
            appliance = select_target_appliance(appliances)
            _appliances = appliances
            current_power = appliance.status_list.get(DEVICE_POWER_KEY)
            if current_power is None:
                raise RuntimeError(f"Appliance has no {DEVICE_POWER_KEY} property")
            _device_state = is_power_on(current_power)
            _last_state_refresh = time.monotonic()
            await record_api_recovery("connect")
            log.info(
                "Connected to '%s'; current state=%s",
                appliance.device_nickname,
                "ON" if _device_state else "OFF",
            )
            return True
        except Exception as err:
            _appliances = None
            await record_api_failure("connect", err)
            return False


async def refresh_device_state(force: bool = False):
    global _device_state, _last_state_refresh
    if not _cl_api or not _appliances:
        return _device_state
    if not force and state_is_fresh():
        return _device_state

    appliance = select_target_appliance(_appliances)
    request_started = time.monotonic()
    try:
        appliance_data = await appliance.fetch_status()
        if appliance_data is None:
            raise RuntimeError("Target appliance disappeared from API response")
        if _last_state_refresh > request_started:
            # A write completed while this GET was in flight. Its observation
            # is newer, so an older GET response must not overwrite it.
            log.debug("Discarding state refresh superseded by a command")
            return _device_state
        current_power = appliance_data["statusList"].get(DEVICE_POWER_KEY)
        if current_power is None:
            raise RuntimeError(f"Appliance has no {DEVICE_POWER_KEY} property")
        actual_state = is_power_on(current_power)
        since_command = request_started - _last_command_time
        if (
            _last_command_state is not None
            and actual_state != _last_command_state
            and since_command < COMMAND_SETTLE_SECONDS
        ):
            # The cloud can briefly serve the pre-command snapshot. Keep the
            # command's observation; the next reconciliation re-checks it.
            log.info(
                "Ignoring state refresh that contradicts the %s command "
                "sent %.0fs earlier",
                "ON" if _last_command_state else "OFF",
                since_command,
            )
            return _device_state
        appliance._update_status(appliance_data)
        previous_state = _device_state
        _device_state = actual_state
        _last_state_refresh = time.monotonic()
        await record_api_recovery("refresh state")
        if previous_state is not None and actual_state != previous_state:
            log.info(
                "Device state corrected from %s to %s after API refresh",
                "ON" if previous_state else "OFF",
                "ON" if actual_state else "OFF",
            )
        return _device_state
    except Exception as err:
        if _last_state_refresh <= request_started:
            await record_api_failure("refresh state", err)
        else:
            log.warning(
                "Ignoring failed state refresh superseded by a successful command: %s",
                format_exception(err),
            )
        raise


async def refresh_for_status() -> None:
    if not _cl_api or not _appliances:
        await connectlife_login()
    else:
        await refresh_device_state(force=True)


async def set_device_power(
    turn_on: bool,
    trigger: str = "auto",
    force: bool = False,
) -> bool:
    """Write the desired state directly, then update the local observation.

    Returns True only after a successful write. Manual commands pass
    ``force`` so an explicit user request is never skipped on cached state.
    """
    global _device_state, _last_state_refresh
    global _last_command_time, _last_command_state

    async with _command_lock:
        if not _cl_api or not _appliances:
            if not await connectlife_login():
                return False

        appliance = select_target_appliance(_appliances)
        action = "ON" if turn_on else "OFF"
        previous_state = _device_state

        if not force and desired_state_is_fresh(turn_on):
            log.debug("Skipping %s: fresh state already matches", action)
            return False

        properties = (
            desired_on_properties(appliance)
            if turn_on
            else {DEVICE_POWER_KEY: DEVICE_POWER_OFF}
        )
        if DEVICE_POWER_KEY not in properties:
            log.error("Cannot turn %s: appliance has no %s", action, DEVICE_POWER_KEY)
            return False

        try:
            # This idempotent write intentionally does not depend on a preceding
            # GET. A read outage must not prevent a safety-relevant OFF command.
            await appliance.update_properties(properties)
            appliance.status_list.update(properties)
            _device_state = turn_on
            _last_state_refresh = time.monotonic()
            _last_command_time = _last_state_refresh
            _last_command_state = turn_on
            await record_api_recovery(f"turn {action}")
            log.info(
                "Device '%s' turned %s (%s); properties=%s",
                appliance.device_nickname,
                action,
                trigger,
                properties,
            )
            manual_command = trigger in ("Telegram manual", "MQTT command")
            confirmed_transition = (
                previous_state is not None
                and previous_state != turn_on
            )
            # An OFF write from an unknown/already-OFF state is safety
            # enforcement, not evidence of a transition. Keep it silent.
            if manual_command or turn_on or confirmed_transition:
                emoji = "✅" if turn_on else "⏹"
                await tg_notify(
                    f"{emoji} <b>Dehumidifier turned {action}</b>\n"
                    f"Trigger: {trigger}"
                    + sensor_summary(),
                    cooldown_key=(
                        None
                        if manual_command
                        else f"device_{action.lower()}_success"
                    ),
                )
            return True
        except Exception as err:
            await record_api_failure(f"turn {action}", err)
            await tg_notify(
                f"❌ <b>Failed to turn {action} dehumidifier</b>\n"
                f"Error: {format_exception(err)}",
                cooldown_key=f"device_{action.lower()}_failed",
            )
            return False


async def reconcile_device_loop() -> None:
    """Continuously reconcile external/manual changes and recover API access."""
    while True:
        await asyncio.sleep(STATE_REFRESH_INTERVAL)
        try:
            # Reads intentionally do not hold the write lock. A slow GET must
            # never delay an idempotent safety-relevant OFF command.
            if not _cl_api or not _appliances:
                await connectlife_login()
            else:
                await refresh_device_state(force=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            # refresh_device_state records the classified failure.
            pass


# Automatic control

def queue_auto_command(turn_on: bool, trigger: str) -> bool:
    if not _loop:
        return False

    if cached_state_matches_desired(turn_on):
        return False

    now = time.monotonic()
    last_attempt = _last_auto_command_attempt[turn_on]
    if last_attempt and now - last_attempt < AUTO_COMMAND_RETRY_INTERVAL:
        return False

    _last_auto_command_attempt[turn_on] = now
    spawn(set_device_power(turn_on, trigger))
    return True


def evaluate_auto(data: dict) -> None:
    if _mode != "auto":
        return
    humidity = data.get("humidity")
    if humidity is None:
        return

    on_threshold = _settings["humidity_on_above"]
    off_threshold = _settings["humidity_off_below"]
    trigger = f"humidity={humidity}%"

    if humidity >= on_threshold:
        if queue_auto_command(True, trigger):
            log.info(
                "Auto threshold matched: %s >= %d%%; queued ON",
                trigger,
                on_threshold,
            )
    elif humidity <= off_threshold:
        if queue_auto_command(False, trigger):
            log.info(
                "Auto threshold matched: %s <= %d%%; queued OFF",
                trigger,
                off_threshold,
            )


def set_mode(mode: str, source: str) -> bool:
    """Change and persist the control mode; return whether it changed."""
    global _mode
    if mode not in VALID_MODES or mode == _mode:
        return False
    _mode = mode
    _settings["mode"] = mode
    try:
        save_settings(_settings)
    except OSError as err:
        log.error("Could not persist mode %s: %s", mode, err)
    log.info("%s mode changed to %s", source, mode)
    if mode == "auto" and sensor_reading_is_fresh(_last_sensor):
        # Apply the thresholds now instead of waiting for the next reading.
        evaluate_auto(_last_sensor)
    return True


# Sensor availability

async def confirm_sensor_offline(generation: int) -> None:
    global _sensor_offline, _sensor_offline_pending

    await asyncio.sleep(SENSOR_OFFLINE_CONFIRM_SECONDS)
    if (
        not _sensor_offline_pending
        or generation != _sensor_offline_generation
    ):
        return

    _sensor_offline_pending = False
    _sensor_offline = True
    age = (
        int(time.monotonic() - _last_sensor_data_time)
        if _last_sensor_data_time
        else None
    )
    age_text = f"{age}s ago" if age is not None else "unknown"
    log.warning(
        "Sensor OFFLINE confirmed after %ds; last data=%s",
        SENSOR_OFFLINE_CONFIRM_SECONDS,
        age_text,
    )
    failsafe_text = (
        f"\nFailsafe: dehumidifier turns OFF after "
        f"{SENSOR_OFFLINE_FAILSAFE_SECONDS}s more offline in auto mode."
        if SENSOR_OFFLINE_FAILSAFE_SECONDS > 0
        else ""
    )
    await tg_notify(
        "🚨 <b>ALARM: Outdoor sensor is OFFLINE</b>\n"
        f"Offline status persisted for {SENSOR_OFFLINE_CONFIRM_SECONDS} seconds.\n"
        f"Last BLE reading: {age_text}."
        + failsafe_text
    )

    if SENSOR_OFFLINE_FAILSAFE_SECONDS <= 0:
        return
    await asyncio.sleep(SENSOR_OFFLINE_FAILSAFE_SECONDS)
    if (
        not _sensor_offline
        or generation != _sensor_offline_generation
        or _mode != "auto"
    ):
        return
    log.warning(
        "Sensor offline for %ds after confirmation; applying OFF failsafe",
        SENSOR_OFFLINE_FAILSAFE_SECONDS,
    )
    await set_device_power(False, "sensor offline failsafe")


def schedule_sensor_offline_confirmation() -> None:
    global _sensor_offline_pending, _sensor_offline_generation

    if _sensor_offline or _sensor_offline_pending:
        return
    if time.monotonic() - _start_time < STARTUP_GRACE:
        log.info("Ignoring retained offline status during startup grace")
        return
    if not _loop or not _loop.is_running():
        log.warning("Cannot confirm sensor offline status: event loop unavailable")
        return

    _sensor_offline_pending = True
    _sensor_offline_generation += 1
    generation = _sensor_offline_generation
    log.info(
        "Sensor offline status received; confirming for %ds",
        SENSOR_OFFLINE_CONFIRM_SECONDS,
    )
    spawn(confirm_sensor_offline(generation))


def mark_sensor_online() -> bool:
    """Cancel a pending alarm and return whether a confirmed outage recovered."""
    global _sensor_offline, _sensor_offline_pending
    global _sensor_offline_generation, _last_sensor_data_time

    _last_sensor_data_time = time.monotonic()
    if _sensor_offline_pending:
        _sensor_offline_generation += 1
        _sensor_offline_pending = False
        log.info("Sensor returned before offline confirmation; alarm cancelled")

    if not _sensor_offline:
        return False

    _sensor_offline = False
    # Invalidate the finished outage's pending failsafe.
    _sensor_offline_generation += 1
    log.info("Sensor is back ONLINE")
    tg_fire(
        "✅ <b>Sensor back ONLINE</b>" + sensor_summary(),
        cooldown_key="sensor_online",
    )
    return True


# Telegram callback and command handling

async def handle_manual_power(query_id: str, chat_id, message_id: int, turn_on: bool):
    action = "ON" if turn_on else "OFF"
    if _mode != "manual":
        await tg_answer(query_id, "Switch to Manual mode first.")
        await tg_edit(chat_id, message_id, control_text(), control_kb())
        return

    await tg_answer(query_id, f"Sending {action} command...")
    if await set_device_power(turn_on, "Telegram manual", force=True):
        result = f"✅ {action} command confirmed by ConnectLife."
    else:
        detail = f"\nError: {_api_last_error}" if _api_last_error else ""
        result = f"❌ {action} command failed; device state is unknown.{detail}"
    await tg_edit(chat_id, message_id, control_text(result), control_kb())


async def handle_callback(query: dict):
    data = query.get("data", "")
    query_id = query["id"]
    chat_id = query["message"]["chat"]["id"]
    message_id = query["message"]["message_id"]

    if data in ("ctrl:on", "ctrl:off"):
        # Answer exactly once: Telegram ignores a second answer to a query.
        await handle_manual_power(query_id, chat_id, message_id, data == "ctrl:on")
        return

    await tg_answer(query_id)
    if data == "noop":
        return

    if data == "menu:main":
        await tg_edit(
            chat_id,
            message_id,
            "🏠 <b>Main Menu</b>",
            main_menu_kb(),
        )
    elif data == "menu:status":
        try:
            await refresh_for_status()
        except Exception:
            pass
        await tg_edit(
            chat_id,
            message_id,
            status_text(),
            _ik(
                [
                    [
                        {"text": "🔄 Refresh", "callback_data": "menu:status"},
                        {"text": "« Back", "callback_data": "menu:main"},
                    ]
                ]
            ),
        )
    elif data == "menu:control":
        await tg_edit(chat_id, message_id, control_text(), control_kb())
    elif data == "menu:settings":
        await tg_edit(
            chat_id,
            message_id,
            settings_text(),
            settings_kb(),
        )
    elif data in ("ctrl:mode:auto", "ctrl:mode:manual"):
        set_mode(data.rsplit(":", 1)[1], "Telegram")
        await tg_edit(chat_id, message_id, control_text(), control_kb())
    elif data.startswith("set:"):
        _, key, delta_string = data.split(":")
        delta = int(delta_string)
        if key == "hum_on":
            _settings["humidity_on_above"] = max(
                _settings["humidity_off_below"] + 1,
                min(99, _settings["humidity_on_above"] + delta),
            )
        elif key == "hum_off":
            _settings["humidity_off_below"] = min(
                _settings["humidity_on_above"] - 1,
                max(1, _settings["humidity_off_below"] + delta),
            )
        save_settings(_settings)
        log.info("Settings updated: %s", _settings)
        await tg_edit(
            chat_id,
            message_id,
            settings_text(),
            settings_kb(),
        )


async def handle_command(message: dict):
    text = message.get("text", "").strip()
    if text in ("/start", "/menu"):
        await tg_send("🏠 <b>Main Menu</b>", main_menu_kb())
    elif text == "/status":
        try:
            await refresh_for_status()
        except Exception:
            pass
        await tg_send(status_text())


def telegram_update_chat_id(update: dict):
    if "callback_query" in update:
        message = update["callback_query"].get("message") or {}
    else:
        message = update.get("message") or {}
    return (message.get("chat") or {}).get("id")


async def dispatch_telegram_update(update: dict) -> None:
    if str(telegram_update_chat_id(update)) != TELEGRAM_CHAT_ID.strip():
        # Only the configured chat may read status or control the device.
        log.warning("Ignoring Telegram update from an unauthorized chat")
        return
    if "callback_query" in update:
        await handle_callback(update["callback_query"])
    elif "message" in update:
        await handle_command(update["message"])


def telegram_retry_delay(failures: int, err: BaseException) -> float:
    retry_after = getattr(err, "retry_after", None)
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        return float(retry_after)
    return min(
        TELEGRAM_POLL_BASE_BACKOFF * 2 ** (failures - 1),
        TELEGRAM_POLL_MAX_BACKOFF,
    )


async def telegram_poll():
    offset = 0
    failures = 0
    log.info("Telegram bot polling started")
    while True:
        try:
            result = await _tg_request(
                "getUpdates",
                offset=offset,
                timeout=20,
                allowed_updates=["message", "callback_query"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            # Every failure must back off: a refused connection fails
            # instantly and would otherwise spin in a tight loop.
            failures += 1
            delay = telegram_retry_delay(failures, err)
            log.log(
                logging.WARNING if failures == 1 else logging.DEBUG,
                "Telegram polling failed (failure %d); retrying in %.0fs: %s",
                failures,
                delay,
                format_exception(err),
            )
            await asyncio.sleep(delay)
            continue

        if failures:
            log.info("Telegram polling recovered after %d failures", failures)
            failures = 0
        for update in result.get("result", []):
            offset = update["update_id"] + 1
            try:
                await dispatch_telegram_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.warning(
                    "Telegram update handling failed: %s",
                    format_exception(err),
                )


# MQTT

def handle_mqtt_message(topic: str, payload: str) -> None:
    """Process one MQTT message on the event-loop thread."""
    global _first_data_seen

    if topic == f"{SENSOR_TOPIC}/json":
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("Bad sensor JSON: %s", payload)
            return
        if not isinstance(data, dict):
            log.warning("Ignoring non-object sensor JSON")
            return
        _last_sensor.update(data)
        if not sensor_reading_is_fresh(data):
            # Typically the broker's retained message from a scanner that is
            # no longer publishing. Show it, but never act on it.
            log.info(
                "Ignoring sensor reading from %ds ago for control",
                int(sensor_reading_age(data)),
            )
            return
        mark_sensor_online()
        log.debug(
            "Sensor: %s°C RH=%s%% Batt=%s%%",
            data.get("temperature_c"),
            data.get("humidity"),
            data.get("battery"),
        )
        if not _first_data_seen:
            _first_data_seen = True
            log.info("First sensor data received after startup")
            if TELEGRAM_NOTIFY_SENSOR_ONLINE:
                tg_fire(
                    "✅ <b>Sensor is ONLINE</b>" + sensor_summary(),
                    cooldown_key="sensor_online",
                )
        evaluate_auto(data)
    elif topic == f"{SENSOR_TOPIC}/status":
        if payload == "offline":
            schedule_sensor_offline_confirmation()
        else:
            recovered = mark_sensor_online()
            if not recovered and TELEGRAM_NOTIFY_SENSOR_ONLINE:
                tg_fire(
                    "✅ <b>Sensor is ONLINE</b>" + sensor_summary(),
                    cooldown_key="sensor_online",
                )
    elif topic == f"{CONTROL_TOPIC}/mode":
        set_mode(payload, "MQTT")
    elif topic == f"{CONTROL_TOPIC}/command":
        if _mode != "manual":
            log.warning("Ignoring command %r because mode is not manual", payload)
            return
        if payload in ("on", "off"):
            spawn(set_device_power(payload == "on", "MQTT command", force=True))


def _handle_mqtt_message_safely(topic: str, payload: str) -> None:
    try:
        handle_mqtt_message(topic, payload)
    except Exception:
        log.exception("MQTT message handling failed for %s", topic)


def on_message(client, userdata, message):
    """Runs on paho's network thread: hand the message to the event loop.

    All controller state is owned by the event-loop thread, so no state is
    read or written here.
    """
    loop = _loop
    if loop is None or loop.is_closed():
        return
    payload = message.payload.decode("utf-8", errors="replace")
    try:
        loop.call_soon_threadsafe(
            _handle_mqtt_message_safely,
            message.topic,
            payload,
        )
    except RuntimeError:
        # The loop closed between the check and the call during shutdown.
        pass


def setup_mqtt() -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=MQTT_CLIENT_ID,
    )

    def on_connect(connected_client, userdata, flags, reason_code, properties):
        log.info("MQTT connected to %s:%d", MQTT_HOST, MQTT_PORT)
        connected_client.subscribe(f"{SENSOR_TOPIC}/json")
        connected_client.subscribe(f"{SENSOR_TOPIC}/status")
        connected_client.subscribe(f"{CONTROL_TOPIC}/mode")
        connected_client.subscribe(f"{CONTROL_TOPIC}/command")

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT)
    client.loop_start()
    return client


async def main():
    global _loop
    _loop = asyncio.get_running_loop()

    log.info("Starting SwitchBot to ConnectLife controller")
    log.info(
        "Thresholds: ON >= %d%% OFF <= %d%%; mode=%s",
        _settings["humidity_on_above"],
        _settings["humidity_off_below"],
        _mode,
    )

    api_ok = await connectlife_login()
    client = setup_mqtt()

    tasks = [asyncio.create_task(reconcile_device_loop())]
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        tasks.append(asyncio.create_task(telegram_poll()))
        if TELEGRAM_NOTIFY_STARTUP:
            await tg_notify(
                "🤖 <b>ConnectLife controller started</b>\n"
                f"API: {'✅ OK' if api_ok else '❌ retrying'}\n"
                f"Mode: {_mode}\n"
                f"Thresholds: ON ≥{_settings['humidity_on_above']}%  "
                f"OFF ≤{_settings['humidity_off_below']}%"
            )

    stop = asyncio.Event()
    for watched_signal in (signal.SIGTERM, signal.SIGINT):
        _loop.add_signal_handler(watched_signal, stop.set)

    try:
        await stop.wait()
    finally:
        client.loop_stop()
        client.disconnect()
        pending = [*tasks, *_background_tasks]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await close_telegram_session()
        if _cl_api:
            await _cl_api.close()
        log.info("Stopped")


if __name__ == "__main__":
    asyncio.run(main())
