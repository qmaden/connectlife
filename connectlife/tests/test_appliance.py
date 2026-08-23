import datetime as dt
import unittest

from connectlife.appliance import convert
from connectlife.appliance import ConnectLifeAppliance


class TestAppliance(unittest.TestCase):

    def test_convert_int(self):
        self.assertEqual(1, convert("1"))
        self.assertEqual(0, convert("0"))
        self.assertEqual(-1, convert("-1"))

    def test_convert_float(self):
        self.assertEqual(0.67, convert(0.67))

    def test_convert_datetime(self):
        self.assertEqual(
            dt.datetime(2024, 9, 12, 21, 25, 33, tzinfo=dt.UTC),
            convert("2024/09/12T21:25:33")
        )
        self.assertEqual(
            dt.datetime(2, 11, 30, 00, 00, 00, tzinfo=dt.UTC),
            convert("0002/11/30T00:00:00")
        )
        self.assertEqual(
            dt.datetime(dt.MAXYEAR, 12, 31, 23, 59, 59, tzinfo=dt.UTC),
            convert("16679/02/18T23:47:45")
        )

    def test_convert_str(self):
        self.assertEqual("string", convert("string"))

    def test_update_status_refreshes_mutable_fields(self):
        data = {
            "wifiId": "wifi",
            "deviceId": "device",
            "puid": "puid",
            "deviceNickName": "Dehumidifier",
            "deviceFeatureCode": "400",
            "deviceFeatureName": "feature",
            "deviceTypeCode": "007",
            "deviceTypeName": "dehumidifier",
            "role": 1,
            "roomId": 1,
            "roomName": "room",
            "offlineState": 0,
            "seq": 1,
            "bindTime": 0,
            "useTime": 0,
            "createTime": 0,
            "statusList": {"t_power": "0"},
        }
        appliance = ConnectLifeAppliance(None, data)

        appliance._update_status({
            "statusList": {"t_power": "1"},
            "offlineState": 1,
            "seq": 2,
        })

        self.assertEqual(1, appliance.status_list["t_power"])
        self.assertEqual(1, appliance.offline_state)
        self.assertEqual(2, appliance.seq)


class MissingStatusApi:
    async def get_appliances_json(self):
        return [{"puid": "target"}]


class ApplianceRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_status_is_not_treated_as_a_fresh_observation(self):
        appliance = object.__new__(ConnectLifeAppliance)
        appliance._api = MissingStatusApi()
        appliance._puid = "target"

        with self.assertRaisesRegex(RuntimeError, "has no statusList"):
            await appliance.fetch_status()
