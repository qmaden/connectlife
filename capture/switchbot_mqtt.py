#!/usr/bin/env python3
"""Publish SwitchBot outdoor sensor BLE readings to MQTT."""

import asyncio
import json
import logging
import logging.handlers
import math
import os
import signal
import time

import paho.mqtt.client as mqtt
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData


TARGET_MAC = os.environ.get("SWITCHBOT_TARGET_MAC", "D0:C8:40:46:63:7F")
SWITCHBOT_SERVICE_UUID = "0000fd3d-0000-1000-8000-00805f9b34fb"

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC_PREFIX = "switchbot/outdoor"
MQTT_CLIENT_ID = "switchbot-ble-scanner"

NO_DATA_TIMEOUT = 60
BLE_RESTART_TIMEOUT = 180
STARTUP_GRACE = 60
PUBLISH_INTERVAL = 10

LOG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "switchbot_scanner.log",
)
log = logging.getLogger("switchbot-mqtt")
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

_last_battery = None
_last_data_time = None
_last_publish_time = 0.0
_start_time = time.monotonic()
_is_online = False
_mqtt_client = None


def calc_dew_point(temp_c: float, humidity: float) -> float:
    a = 17.27
    b = 237.7
    alpha = (a * temp_c) / (b + temp_c) + math.log(humidity / 100.0)
    return round((b * alpha) / (a - alpha), 1)


def calc_absolute_humidity(temp_c: float, humidity: float) -> float:
    return round(
        (
            6.112
            * math.exp((17.67 * temp_c) / (temp_c + 243.5))
            * humidity
            * 2.1674
        )
        / (273.15 + temp_c),
        2,
    )


def calc_vpd(temp_c: float, humidity: float) -> float:
    saturation_pressure = 0.6108 * math.exp(
        (17.27 * temp_c) / (temp_c + 237.3)
    )
    actual_pressure = saturation_pressure * (humidity / 100.0)
    return round(saturation_pressure - actual_pressure, 2)


def mqtt_publish(topic: str, payload, retain: bool = True):
    if _mqtt_client and _mqtt_client.is_connected():
        _mqtt_client.publish(
            f"{MQTT_TOPIC_PREFIX}/{topic}",
            str(payload),
            retain=retain,
        )


def callback(device: BLEDevice, advertisement_data: AdvertisementData):
    global _last_battery, _last_data_time, _last_publish_time, _is_online

    if device.address.upper() != TARGET_MAC.upper():
        return

    _last_data_time = time.monotonic()
    manufacturer = advertisement_data.manufacturer_data.get(0x0969)
    if not manufacturer or len(manufacturer) < 11:
        return
    data = manufacturer[6:]

    temperature_sign = 1 if data[3] & 0x80 else -1
    temperature_integer = data[3] & 0x7F
    temperature_decimal = data[2] & 0x0F
    temperature = temperature_sign * (
        temperature_integer + temperature_decimal / 10.0
    )
    humidity = data[4] & 0x7F

    service_data = advertisement_data.service_data.get(
        SWITCHBOT_SERVICE_UUID
    )
    if service_data and len(service_data) >= 3:
        _last_battery = service_data[2] & 0x7F
    elif _last_battery is None:
        _last_battery = data[0] & 0x7F

    now = time.monotonic()
    if now - _last_publish_time < PUBLISH_INTERVAL:
        return
    _last_publish_time = now

    temperature_f = round(temperature * 9 / 5 + 32, 1)
    dew_point = calc_dew_point(temperature, humidity)
    absolute_humidity = calc_absolute_humidity(temperature, humidity)
    vpd = calc_vpd(temperature, humidity)
    rssi = advertisement_data.rssi

    mqtt_publish("temperature_c", temperature)
    mqtt_publish("temperature_f", temperature_f)
    mqtt_publish("humidity", humidity)
    mqtt_publish("dew_point", dew_point)
    mqtt_publish("absolute_humidity", absolute_humidity)
    mqtt_publish("vpd", vpd)
    mqtt_publish("battery", _last_battery)
    mqtt_publish("rssi", rssi, retain=False)

    payload = {
        "temperature_c": temperature,
        "temperature_f": temperature_f,
        "humidity": humidity,
        "dew_point": dew_point,
        "absolute_humidity": absolute_humidity,
        "vpd": vpd,
        "battery": _last_battery,
        "rssi": rssi,
        "timestamp": int(time.time()),
    }
    mqtt_publish("json", json.dumps(payload), retain=True)

    if not _is_online:
        mqtt_publish("status", "online")
        _is_online = True
        log.info("Sensor is ONLINE")

    log.debug(
        "Temp=%.1f°C/%s°F RH=%d%% Dew=%s°C AH=%s g/m³ "
        "VPD=%s kPa Batt=%s%% RSSI=%sdBm",
        temperature,
        temperature_f,
        humidity,
        dew_point,
        absolute_humidity,
        vpd,
        _last_battery,
        rssi,
    )


async def alarm_watchdog():
    global _is_online
    await asyncio.sleep(STARTUP_GRACE)
    log.info("Watchdog active")

    while True:
        await asyncio.sleep(5)
        now = time.monotonic()
        reference = _last_data_time if _last_data_time else _start_time
        elapsed = now - reference

        if elapsed >= BLE_RESTART_TIMEOUT:
            log.warning(
                "No BLE data for %ds; exiting so systemd can restart",
                int(elapsed),
            )
            mqtt_publish("status", "offline")
            _mqtt_client.loop_stop()
            _mqtt_client.disconnect()
            raise SystemExit(1)

        if elapsed >= NO_DATA_TIMEOUT and _is_online:
            _is_online = False
            mqtt_publish("status", "offline")
            log.warning("No data for %ds; sensor is OFFLINE", int(elapsed))
        elif elapsed < NO_DATA_TIMEOUT and not _is_online and _last_data_time:
            _is_online = True
            mqtt_publish("status", "online")
            log.info("Sensor is back ONLINE")


def setup_mqtt() -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=MQTT_CLIENT_ID,
    )
    client.will_set(
        f"{MQTT_TOPIC_PREFIX}/status",
        "offline",
        retain=True,
    )
    client.on_connect = (
        lambda connected_client, userdata, flags, reason_code, properties: (
            log.info("MQTT connected to %s:%d", MQTT_HOST, MQTT_PORT)
        )
    )
    client.on_disconnect = (
        lambda connected_client, userdata, flags, reason_code, properties: (
            log.warning("MQTT disconnected")
        )
    )
    client.connect(MQTT_HOST, MQTT_PORT)
    client.loop_start()
    return client


async def main():
    global _mqtt_client
    _mqtt_client = setup_mqtt()

    log.info("Scanning for SwitchBot sensor %s", TARGET_MAC)
    log.info(
        "Publishing to MQTT %s:%d topic=%s/*",
        MQTT_HOST,
        MQTT_PORT,
        MQTT_TOPIC_PREFIX,
    )

    watchdog = asyncio.create_task(alarm_watchdog())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for watched_signal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(watched_signal, stop.set)

    try:
        async with BleakScanner(callback):
            await stop.wait()
    except Exception as err:
        log.warning("BLE scanner error: %s: %s", type(err).__name__, err)
    finally:
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
        try:
            mqtt_publish("status", "offline")
            _mqtt_client.loop_stop()
            _mqtt_client.disconnect()
        except Exception:
            pass
        log.info("Stopped")


if __name__ == "__main__":
    asyncio.run(main())
