#!/usr/bin/env python3
"""
Example script demonstrating how to control ConnectLife devices.

This script shows how to:
1. Connect to the ConnectLife API
2. List available devices
3. View device properties
4. Control device settings
"""

import asyncio
import argparse
from connectlife.api import ConnectLifeApi


async def list_devices(username: str, password: str):
    """List all devices and their basic information."""
    api = ConnectLifeApi(username, password)
    try:
        appliances = await api.get_appliances()

        print(f"Found {len(appliances)} device(s):\n")

        for i, appliance in enumerate(appliances):
            print(f"Device {i + 1}:")
            print(f"  Name: {appliance.device_nickname}")
            print(f"  Type: {appliance.device_type_name} ({appliance.device_type})")
            print(f"  Model: {appliance.device_feature_name}")
            print(f"  Room: {appliance.room_name}")
            print(f"  Online: {'Yes' if appliance.offline_state == 0 else 'No'}")
            print()
    finally:
        await api.close()


async def show_device_properties(username: str, password: str, device_index: int):
    """Show all properties for a specific device."""
    api = ConnectLifeApi(username, password)
    try:
        appliances = await api.get_appliances()

        if device_index >= len(appliances):
            print(f"Error: Device index {device_index} not found. Available devices: 0-{len(appliances)-1}")
            return

        appliance = appliances[device_index]
        print(f"Properties for '{appliance.device_nickname}':")
        print("-" * 60)

        properties = appliance.list_properties()
        for prop in sorted(properties):
            value = appliance.get_property(prop)
            print(f"{prop}: {value}")
    finally:
        await api.close()


async def control_device(username: str, password: str, device_index: int,
                        property_name: str, value: str):
    """Control a specific device property."""
    api = ConnectLifeApi(username, password)
    try:
        appliances = await api.get_appliances()

        if device_index >= len(appliances):
            print(f"Error: Device index {device_index} not found. Available devices: 0-{len(appliances)-1}")
            return

        appliance = appliances[device_index]

        # Check if property exists
        if property_name not in appliance.list_properties():
            print(f"Error: Property '{property_name}' not found on device '{appliance.device_nickname}'")
            print("Available properties:")
            for prop in sorted(appliance.list_properties()):
                print(f"  - {prop}")
            return

        # Get current value
        current_value = appliance.get_property(property_name)
        print(f"Current value of '{property_name}': {current_value}")

        # Update the property
        await appliance.update_properties({property_name: value})
        print(f"Successfully updated '{property_name}' to '{value}'")

        # Refresh and show new value
        await appliance.refresh_status()
        new_value = appliance.get_property(property_name)
        print(f"New value: {new_value}")
    finally:
        await api.close()


async def main():
    parser = argparse.ArgumentParser(description="ConnectLife Device Control Tool")
    parser.add_argument("-u", "--username", required=True, help="ConnectLife username")
    parser.add_argument("-p", "--password", required=True, help="ConnectLife password")
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # List devices command
    subparsers.add_parser("list", help="List all devices")
    
    # Show properties command
    props_parser = subparsers.add_parser("properties", help="Show device properties")
    props_parser.add_argument("device", type=int, help="Device index (0-based)")
    
    # Control device command
    control_parser = subparsers.add_parser("control", help="Control device property")
    control_parser.add_argument("device", type=int, help="Device index (0-based)")
    control_parser.add_argument("property", help="Property name to change")
    control_parser.add_argument("value", help="New value for the property")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    try:
        if args.command == "list":
            await list_devices(args.username, args.password)
        elif args.command == "properties":
            await show_device_properties(args.username, args.password, args.device)
        elif args.command == "control":
            await control_device(args.username, args.password, args.device, 
                               args.property, args.value)
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
