import datetime as dt
import re
from enum import StrEnum
from typing import Dict


class DeviceType(StrEnum):
    """Known device types."""
    AIRCONDITIONER = "airconditioner"
    DEHUMIDIFIER = "dehumidifier"
    DISHWASHER = "dishwasher"
    HEAT_PUMP = "heat_pump"
    HOB = "hob"
    HOOD = "hood"
    OVEN = "oven"
    REFRIGERATOR = "refrigerator"
    TUMBLE_DRYER = "tumble_dryer"
    WASHING_MACHINE = "washing_machine"
    UNKNOWN = "unknown"


DEVICE_TYPES = {
    "003": DeviceType.WASHING_MACHINE,
    "004": DeviceType.TUMBLE_DRYER,
    "006": DeviceType.DEHUMIDIFIER,
    "007": DeviceType.DEHUMIDIFIER,
    "009": DeviceType.AIRCONDITIONER,
    "010": DeviceType.HOOD,
    "013": DeviceType.OVEN,
    "015": DeviceType.DISHWASHER,
    "016": DeviceType.HEAT_PUMP,
    "020": DeviceType.HOOD,
    "021": DeviceType.HOOD,
    "023": DeviceType.OVEN,
    "025": DeviceType.WASHING_MACHINE,
    "026": DeviceType.REFRIGERATOR,
    "027": DeviceType.WASHING_MACHINE,
}


RE_DATETIME = re.compile(r"^(\d{4,5})/(\d{1,2})/(\d{1,2})T(\d{1,2}):(\d{1,2}):(\d{1,2})$")
MAX_DATETIME = dt.datetime(dt.MAXYEAR, 12, 31, 23, 59, 59, tzinfo=dt.UTC)


class ConnectLifeAppliance:
    """Class representing a single appliance."""

    def __init__(self, api, data):
        self._api = api
        self._wifi_id = data["wifiId"]
        self._device_id = data["deviceId"]
        self._puid = data["puid"]
        self._device_nickname = data["deviceNickName"]
        self._device_feature_code = data["deviceFeatureCode"]
        self._device_feature_name = data["deviceFeatureName"]
        self._device_type_code = data["deviceTypeCode"]
        self._device_type_name = data["deviceTypeName"]
        self._role = data["role"]
        self._room_id = data["roomId"]
        self._room_name = data["roomName"]
        self._offline_state = data["offlineState"]
        self._seq = data["seq"]
        self._bind_time = dt.datetime.fromtimestamp(data["bindTime"]/1000, tz=dt.UTC) if data["bindTime"] else None
        self._use_time = dt.datetime.fromtimestamp(data["useTime"]/1000, tz=dt.UTC) if data["useTime"] else None
        self._create_time = dt.datetime.fromtimestamp(data["createTime"]/1000, tz=dt.UTC) if data["createTime"] else None
        self._status_list = {k: convert(v) for k, v in data["statusList"].items()}
        self._device_type = DEVICE_TYPES[self._device_type_code] \
            if self._device_type_code in DEVICE_TYPES \
            else DeviceType.UNKNOWN

    @property
    def wifi_id(self) -> str:
        return self._wifi_id

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def puid(self) -> str:
        return self._puid

    @property
    def device_nickname(self) -> str:
        return self._device_nickname

    @property
    def device_feature_code(self) -> str:
        return self._device_feature_code

    @property
    def device_feature_name(self) -> str:
        return self._device_feature_name

    @property
    def device_type_code(self) -> str:
        return self._device_type_code

    @property
    def device_type_name(self) -> str:
        return self._device_type_name

    @property
    def bind_time(self) -> dt.datetime | None:
        return self._bind_time

    @property
    def role(self) -> int:
        return self._role

    @property
    def room_id(self) -> int:
        return self._room_id

    @property
    def room_name(self) -> str:
        return self._room_name

    @property
    def status_list(self) -> Dict[str, str | int | float | dt.datetime]:
        return self._status_list

    @property
    def use_time(self) -> dt.datetime | None:
        return self._use_time

    @property
    def offline_state(self) -> int:
        return self._offline_state

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def create_time(self) -> dt.datetime | None:
        return self._create_time

    @property
    def device_type(self) -> DeviceType:
        return self._device_type

    async def update_properties(self, properties: dict[str, str | int]) -> None:
        """Update device properties/settings.
        
        Args:
            properties: Dictionary of property names and their new values.
                       Property names should match those in the device's status_list.
        
        Example:
            # Turn on power save mode
            await appliance.update_properties({"Power_Save": "1"})
            
            # Change program and temperature
            await appliance.update_properties({
                "Selected_program_id_status": "5",
                "Selected_program_set_temperature_status": "40"
            })
        """
        # Convert all values to strings as the API expects string values
        str_properties = {k: str(v) for k, v in properties.items()}
        await self._api.update_appliance(self._puid, str_properties)

    async def refresh_status(self) -> None:
        """Refresh the appliance status from the API."""
        appliance_data = await self.fetch_status()
        if appliance_data is not None:
            self._update_status(appliance_data)

    async def fetch_status(self) -> dict | None:
        """Fetch this appliance without mutating the cached status."""
        appliances = await self._api.get_appliances_json()
        for appliance_data in appliances:
            if appliance_data.get("puid") == self._puid:
                return appliance_data
        return None

    def _update_status(self, appliance_data: dict) -> None:
        """Update mutable status fields from an appliance-list response."""
        self._status_list = {
            key: convert(value)
            for key, value in appliance_data["statusList"].items()
        }
        self._offline_state = appliance_data.get("offlineState", self._offline_state)
        self._seq = appliance_data.get("seq", self._seq)

    def get_property(self, property_name: str) -> str | int | float | dt.datetime | None:
        """Get the current value of a specific property.
        
        Args:
            property_name: The name of the property to retrieve
            
        Returns:
            The current value of the property, or None if not found
        """
        return self._status_list.get(property_name)

    def list_properties(self) -> list[str]:
        """Get a list of all available property names for this device."""
        return list(self._status_list.keys())


def convert(value: str | float) -> float | int | str | dt.datetime:
    if isinstance(value, float):
        return value
    try:
        return int(value)
    except ValueError:
        pass
    try:
        # Unknown if timezone depends on property or appliance. Some properties include UTC in the name.
        # Extreme values observed:
        # "0002/11/30T00:00:00" (probably represents no value)
        # "16679/02/18T23:47:45" (probably represents no value)
        if match := RE_DATETIME.match(value):
            (year, month, day, hour, minute, seconds) = map(int, match.groups())
            if year > dt.MAXYEAR:
                return MAX_DATETIME
            return dt.datetime(year, month, day, hour, minute, seconds, tzinfo=dt.UTC)
    except ValueError:
        pass
    return value
