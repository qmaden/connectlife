#!/usr/bin/env python3
"""
Automated humidity control script for ConnectLife dehumidifier.

This script monitors humidity levels and automatically controls a dehumidifier:
- Turns ON when humidity > HUMIDITY_THRESHOLD_HIGH (default 60%)
- Turns OFF when humidity < HUMIDITY_THRESHOLD_LOW (default 55%)
- Sets target humidity to TARGET_HUMIDITY (default 40%) when turning on

Designed to run as a cron job for automated home humidity management.
"""

import asyncio
import logging
import sys
import os
from datetime import datetime
from connectlife.api import ConnectLifeApi

# Configuration
DEVICE_INDEX = 0  # Device 0 (first device)
HUMIDITY_THRESHOLD_HIGH = 60  # Turn on when humidity above this
HUMIDITY_THRESHOLD_LOW = 55   # Turn off when humidity below this
TARGET_HUMIDITY = 40          # Target humidity when running

# Credentials (set as environment variables for security)
USERNAME = os.getenv('CONNECTLIFE_USERNAME')
PASSWORD = os.getenv('CONNECTLIFE_PASSWORD')
DEVICE_TYPE_CODE = os.getenv('DEVICE_TYPE_CODE', '007')  # Default to your dehumidifier type

# Log to project directory instead of /tmp for persistence across reboots
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(LOG_DIR, 'humidity_control.log')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class HumidityController:
    def __init__(self, username: str, password: str):
        self.api = ConnectLifeApi(username, password)
        self.appliances = None

    async def initialize(self):
        """Initialize connection and get appliances."""
        try:
            logger.info("Attempting to connect to ConnectLife API...")
            all_appliances = await self.api.get_appliances()
            if not all_appliances:
                raise Exception("No appliances found")

            # Filter for the specific device type code (e.g. "007" for dehumidifier)
            dehumidifier_appliances = [app for app in all_appliances if app.device_type_code == DEVICE_TYPE_CODE]
            if not dehumidifier_appliances:
                logger.warning("No devices found with type code '%s'. Available devices:", DEVICE_TYPE_CODE)
                for i, app in enumerate(all_appliances):
                    logger.warning("  Device %d: %s (type_code: %s, type: %s)", i, app.device_nickname, app.device_type_code, app.device_type)
                # Fall back to using all appliances if no type match
                logger.info("Using device 0 from all available devices")
                self.appliances = all_appliances
            else:
                self.appliances = dehumidifier_appliances
                logger.info("Found %d devices with type code '%s'", len(dehumidifier_appliances), DEVICE_TYPE_CODE)

            if DEVICE_INDEX >= len(self.appliances):
                raise Exception(f"Device index {DEVICE_INDEX} not found. Available: 0-{len(self.appliances)-1}")

            device = self.appliances[DEVICE_INDEX]
            logger.info("Connected to device: %s (type: %s)", device.device_nickname, device.device_type)

        except Exception as e:
            logger.error("Failed to initialize: %s", e)
            raise

    def get_device(self):
        """Get the target device."""
        return self.appliances[DEVICE_INDEX]

    async def get_current_humidity(self):
        """Get current humidity reading from the device."""
        device = self.get_device()

        # Try specific properties for your dehumidifier first
        humidity_properties = [
            'f_humidity',      # Your device has this - current humidity reading
            'Current_humidity',
            'Humidity_sensor',
            'Room_humidity',
            'Ambient_humidity',
            'Humidity_level',
            'Current_relative_humidity'
        ]

        for prop in humidity_properties:
            humidity = device.get_property(prop)
            if humidity is not None:
                try:
                    humidity_val = float(humidity)
                    logger.info("Current humidity from %s: %s%%", prop, humidity_val)
                    return humidity_val
                except (ValueError, TypeError):
                    continue

        # If no humidity sensor found, list available properties
        logger.warning("No humidity sensor found. Available properties:")
        for prop in sorted(device.list_properties()):
            logger.info("  %s: %s", prop, device.get_property(prop))

        return None

    async def is_device_running(self):
        """Check if the dehumidifier is currently running."""
        device = self.get_device()

        # Try specific properties for your dehumidifier first
        power_properties = [
            't_power',         # Your device has this - power status
            'Power',
            'Power_status',
            'Status',
            'Running_status',
            'On_off',
            'Device_status'
        ]

        for prop in power_properties:
            status = device.get_property(prop)
            if status is not None:
                is_on = status in [1, "1", "on", "ON", "running", "RUNNING", True]
                logger.info("Device power status from %s: %s (running: %s)", prop, status, is_on)
                return is_on

        logger.warning("No power status property found")
        return False

    async def turn_on_device(self):
        """Turn on the dehumidifier with target humidity and auto fan."""
        device = self.get_device()

        try:
            properties = {}

            # Power on - your device uses t_power
            power_props = ['t_power', 'Power', 'Power_status', 'On_off']
            for prop in power_props:
                if prop in device.list_properties():
                    properties[prop] = "1"
                    break

            # Set target humidity - your device uses t_humidity
            humidity_props = [
                't_humidity',
                'Target_humidity',
                'Set_humidity',
                'Humidity_setting',
                'Desired_humidity',
                'Humidity_target'
            ]
            for prop in humidity_props:
                if prop in device.list_properties():
                    properties[prop] = str(TARGET_HUMIDITY)
                    break

            # Set fan speed - your device uses t_fan_speed
            fan_props = [
                't_fan_speed',
                'Fan_mode',
                'Fan_setting',
                'Fan_speed',
                'Air_flow_setting'
            ]
            for prop in fan_props:
                if prop in device.list_properties():
                    properties[prop] = "0"  # Auto mode
                    break

            # Set work mode - your device uses t_work_mode
            work_mode_props = ['t_work_mode', 'Work_mode', 'Mode']
            for prop in work_mode_props:
                if prop in device.list_properties():
                    properties[prop] = "1"
                    break

            if properties:
                await device.update_properties(properties)
                logger.info("Turned ON device with settings: %s", properties)
            else:
                logger.warning("Could not find power control properties")

        except Exception as e:
            logger.error("Failed to turn on device: %s", e)
            raise

    async def turn_off_device(self):
        """Turn off the dehumidifier."""
        device = self.get_device()

        try:
            power_props = ['t_power', 'Power', 'Power_status', 'On_off']
            for prop in power_props:
                if prop in device.list_properties():
                    await device.update_properties({prop: "0"})
                    logger.info("Turned OFF device using %s", prop)
                    return

            logger.warning("Could not find power control property to turn off device")

        except Exception as e:
            logger.error("Failed to turn off device: %s", e)
            raise

    async def control_humidity(self):
        """Main control logic."""
        try:
            await self.initialize()

            device = self.get_device()

            # Single API call to refresh status, then read both humidity and power
            await device.refresh_status()

            # Get current humidity
            current_humidity = await self.get_current_humidity()
            if current_humidity is None:
                logger.error("Could not read humidity sensor")
                return

            # Get current device status (no extra API call needed, status already refreshed)
            is_running = await self.is_device_running()

            logger.info("Current humidity: %s%%, Device running: %s", current_humidity, is_running)
            logger.info("Thresholds: HIGH=%s%%, LOW=%s%%", HUMIDITY_THRESHOLD_HIGH, HUMIDITY_THRESHOLD_LOW)

            # Control logic
            if current_humidity > HUMIDITY_THRESHOLD_HIGH and not is_running:
                logger.info("Humidity %s%% > %s%% - Turning ON", current_humidity, HUMIDITY_THRESHOLD_HIGH)
                await self.turn_on_device()

            elif current_humidity < HUMIDITY_THRESHOLD_LOW and is_running:
                logger.info("Humidity %s%% < %s%% - Turning OFF", current_humidity, HUMIDITY_THRESHOLD_LOW)
                await self.turn_off_device()

            else:
                logger.info("No action needed - humidity within acceptable range")

        except Exception as e:
            logger.error("Control failed: %s", e)
            sys.exit(1)
        finally:
            await self.api.close()


async def main():
    """Main entry point."""
    if not USERNAME or not PASSWORD:
        logger.error("Please set CONNECTLIFE_USERNAME and CONNECTLIFE_PASSWORD environment variables")
        sys.exit(1)

    logger.info("=== Humidity Control Check Started ===")
    logger.info("Using device type code: %s", DEVICE_TYPE_CODE)

    try:
        controller = HumidityController(USERNAME, PASSWORD)
        await controller.control_humidity()
        logger.info("=== Humidity Control Check Completed ===")
    except Exception as e:
        logger.error("Main execution failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
