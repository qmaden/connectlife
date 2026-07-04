import unittest

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from connectlife.api import ConnectLifeApi


class ApiIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.appliance_data = {
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
            "statusList": {
                "t_power": "0",
                "t_work_mode": "1",
                "t_fan_speed": "2",
                "t_humidity": "30",
            },
        }

        app = web.Application()
        app.router.add_post("/accounts.login", self.login)
        app.router.add_post("/accounts.getJWT", self.jwt)
        app.router.add_post("/oauth/authorize", self.authorize)
        app.router.add_post("/oauth/token", self.token)
        app.router.add_get("/appliances", self.get_appliances)
        app.router.add_post("/appliances", self.update_appliance)
        self.server = TestServer(app)
        try:
            await self.server.start_server()
        except PermissionError:
            self.skipTest("Socket creation is prohibited by this sandbox")

        self.api = ConnectLifeApi(
            "user",
            "password",
            test_server=str(self.server.make_url("")).rstrip("/"),
            timeout=aiohttp.ClientTimeout(total=1),
            retry_delays=(0.0, 0.0),
            token_cache=None,
        )

    async def asyncTearDown(self):
        await self.api.close()
        await self.server.close()

    async def login(self, request):
        return web.json_response({
            "UID": "uid",
            "sessionInfo": {"cookieValue": "login-token"},
        })

    async def jwt(self, request):
        return web.json_response({"id_token": "id-token"})

    async def authorize(self, request):
        return web.json_response({"code": "authorization-code"})

    async def token(self, request):
        return web.json_response({
            "access_token": "access-token",
            "expires_in": 3600,
            "refresh_token": "refresh-token",
        })

    async def get_appliances(self, request):
        return web.json_response([self.appliance_data])

    async def update_appliance(self, request):
        data = await request.json()
        self.appliance_data["statusList"].update(data["properties"])
        return web.json_response({"resultCode": 0})

    async def test_login_read_update_and_refresh_round_trip(self):
        appliances = await self.api.get_appliances()
        self.assertEqual(1, len(appliances))
        appliance = appliances[0]
        self.assertEqual(0, appliance.status_list["t_power"])

        await appliance.update_properties({"t_power": 1})
        await appliance.refresh_status()

        self.assertEqual(1, appliance.status_list["t_power"])


if __name__ == "__main__":
    unittest.main()
