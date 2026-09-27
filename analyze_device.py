#!/usr/bin/env python3
"""
Device property discovery script for humidity control setup.

This script helps identify the correct property names for your specific dehumidifier.
"""

import asyncio
import argparse
from connectlife.api import ConnectLifeApi


async def analyze_device(username: str, password: str, device_index: int = 0):
    """Analyze device properties to identify control properties."""
    api = ConnectLifeApi(username, password)
    try:
        appliances = await api.get_appliances()

        if device_index >= len(appliances):
            print(f"Error: Device index {device_index} not found. Available devices: 0-{len(appliances)-1}")
            return

        device = appliances[device_index]

        print(f"=== Device Analysis: {device.device_nickname} ===")
        print(f"Type: {device.device_type_name} ({device.device_type})")
        print(f"Model: {device.device_feature_name}")
        print()

        # Get all properties
        properties = device.list_properties()

        # Categorize properties
        humidity_props = []
        power_props = []
        fan_props = []
        temperature_props = []
        other_props = []

        for prop in properties:
            prop_lower = prop.lower()
            value = device.get_property(prop)

            if 'humid' in prop_lower:
                humidity_props.append((prop, value))
            elif any(word in prop_lower for word in ['power', 'on_off', 'status', 'running']):
                power_props.append((prop, value))
            elif any(word in prop_lower for word in ['fan', 'air', 'flow', 'speed']):
                fan_props.append((prop, value))
            elif any(word in prop_lower for word in ['temp', 'temperature']):
                temperature_props.append((prop, value))
            else:
                other_props.append((prop, value))

        # Display categorized properties
        print("HUMIDITY RELATED PROPERTIES:")
        if humidity_props:
            for prop, value in humidity_props:
                print(f"   {prop}: {value}")
        else:
            print("   No humidity properties found")
        print()

        print("POWER/STATUS PROPERTIES:")
        if power_props:
            for prop, value in power_props:
                print(f"   {prop}: {value}")
        else:
            print("   No power properties found")
        print()

        print("FAN/AIRFLOW PROPERTIES:")
        if fan_props:
            for prop, value in fan_props:
                print(f"   {prop}: {value}")
        else:
            print("   No fan properties found")
        print()

        print("TEMPERATURE PROPERTIES:")
        if temperature_props:
            for prop, value in temperature_props:
                print(f"   {prop}: {value}")
        else:
            print("   No temperature properties found")
        print()

        # Show suggestions
        print("SUGGESTED CONFIGURATION:")
        print("Based on the properties found, here are the recommended settings:")
        print()

        if humidity_props:
            current_humid = [p for p in humidity_props if 'current' in p[0].lower() or 'sensor' in p[0].lower()]
            target_humid = [p for p in humidity_props if 'target' in p[0].lower() or 'set' in p[0].lower()]

            if current_humid:
                print(f"Current humidity sensor: {current_humid[0][0]}")
            if target_humid:
                print(f"Target humidity setting: {target_humid[0][0]}")

        if power_props:
            power_control = power_props[0]
            print(f"Power control: {power_control[0]}")

        if fan_props:
            fan_control = fan_props[0]
            print(f"Fan control: {fan_control[0]}")

        print()
        print("NEXT STEPS:")
        print("1. Note the property names above")
        print("2. Update humidity_control.py if needed")
        print("3. Test with: python3 humidity_control.py")
        print()

        # Show a sample control command
        if power_props:
            power_prop = power_props[0][0]
            print("SAMPLE CONTROL COMMANDS:")
            print(f"Turn on:  python3 control_example.py -u username -p password control {device_index} '{power_prop}' '1'")
            print(f"Turn off: python3 control_example.py -u username -p password control {device_index} '{power_prop}' '0'")

            if humidity_props:
                humid_prop = [p for p in humidity_props if 'target' in p[0].lower() or 'set' in p[0].lower()]
                if humid_prop:
                    print(f"Set humidity: python3 control_example.py -u username -p password control {device_index} '{humid_prop[0][0]}' '40'")
    finally:
        await api.close()


async def main():
    parser = argparse.ArgumentParser(description="Analyze ConnectLife device for humidity control")
    parser.add_argument("-u", "--username", required=True, help="ConnectLife username")
    parser.add_argument("-p", "--password", required=True, help="ConnectLife password")
    parser.add_argument("-d", "--device", type=int, default=0, help="Device index (default: 0)")

    args = parser.parse_args()

    try:
        await analyze_device(args.username, args.password, args.device)
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
