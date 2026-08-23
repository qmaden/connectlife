import asyncio
import datetime as dt
import json
import logging
import unittest
from unittest.mock import AsyncMock

import aiohttp

from connectlife.api import (
    ConnectLifeApi,
    GATEWAY_DEVICE_LIST_URL,
    GATEWAY_INVALID_ACCESS_TOKEN,
    GATEWAY_UPDATE_URL,
    LifeConnectAuthError,
    LifeConnectError,
)

logging.getLogger("connectlife.api").disabled = True


class FailingRequest:
    async def __aenter__(self):
        raise asyncio.TimeoutError()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class ResponseRequest:
    def __init__(self, status, text="unavailable"):
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def text(self):
        return self._text

    async def read(self):
        return self._text.encode()


class FakeSession:
    def __init__(self, responses=None, timeout=False):
        self.closed = False
        self.responses = list(responses or [])
        self.timeout = timeout
        self.requests = 0

    def request(self, method, url, **kwargs):
        self.requests += 1
        if self.timeout:
            return FailingRequest()
        return ResponseRequest(self.responses.pop(0))


class FakeGatewaySession:
    def __init__(self, responses, statuses=None):
        self.closed = False
        self.responses = list(responses)
        self.statuses = list(statuses or [200] * len(self.responses))
        self.calls = []

    def _request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return ResponseRequest(
            self.statuses.pop(0),
            json.dumps(self.responses.pop(0)),
        )

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)


class ApiRetryTests(unittest.IsolatedAsyncioTestCase):
    def make_api(self):
        api = ConnectLifeApi(
            "user",
            "password",
            retry_delays=(0.0, 0.0),
            token_cache=None,
            timeout=aiohttp.ClientTimeout(total=0.1),
        )
        api._access_token = "token"
        api._refresh_token = "refresh"
        api._expires = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
        return api

    async def test_timeout_error_keeps_exception_type_and_attempt_count(self):
        api = self.make_api()
        session = FakeSession(timeout=True)
        api._session = session

        with self.assertRaisesRegex(
            LifeConnectError,
            r"after 3 attempts: TimeoutError: TimeoutError\(\)",
        ):
            await api._request_with_current_token(
                "GET",
                api.appliances_url,
                operation="get appliances",
            )

        self.assertEqual(3, session.requests)

    async def test_server_errors_are_retried_without_auth_churn(self):
        api = self.make_api()
        session = FakeSession(responses=[500, 502, 503])
        api._session = session

        with self.assertRaisesRegex(
            LifeConnectError,
            r"unavailable during get appliances.*status=503",
        ):
            await api._request_with_token(
                "GET",
                api.appliances_url,
                operation="get appliances",
            )

        self.assertEqual(3, session.requests)
        self.assertEqual("token", api._access_token)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def make_api(self):
        api = ConnectLifeApi("user", "password", token_cache=None)
        api._access_token = "token"
        api._refresh_token = "refresh"
        api._expires = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
        return api

    async def test_appliance_list_uses_signed_direct_gateway_get(self):
        api = self.make_api()
        session = FakeGatewaySession([
            {
                "response": {
                    "resultCode": 0,
                    "deviceList": [{"deviceId": "device-1"}],
                }
            }
        ])
        api._session = session

        result = await api.get_appliances_json()

        self.assertEqual([{"deviceId": "device-1"}], result)
        method, url, kwargs = session.calls[0]
        self.assertEqual("GET", method)
        self.assertEqual(GATEWAY_DEVICE_LIST_URL, url)
        self.assertEqual("token", kwargs["params"]["accessToken"])
        self.assertEqual(32, len(kwargs["params"]["randStr"]))
        self.assertTrue(kwargs["params"]["sign"])

    async def test_update_uses_signed_direct_gateway_post(self):
        api = self.make_api()
        session = FakeGatewaySession([
            {"response": {"resultCode": 0}}
        ])
        api._session = session

        await api.update_appliance("puid-1", {"t_power": "1"})

        method, url, kwargs = session.calls[0]
        self.assertEqual("POST", method)
        self.assertEqual(GATEWAY_UPDATE_URL, url)
        self.assertEqual("puid-1", kwargs["json"]["puid"])
        self.assertEqual({"t_power": "1"}, kwargs["json"]["properties"])
        self.assertTrue(kwargs["json"]["sign"])

    async def test_invalid_gateway_token_reauthenticates_once(self):
        api = self.make_api()
        session = FakeGatewaySession([
            {
                "response": {
                    "resultCode": 1,
                    "errorCode": GATEWAY_INVALID_ACCESS_TOKEN,
                    "errorDesc": "invalid token",
                }
            },
            {"response": {"resultCode": 0, "deviceList": []}},
        ])
        api._session = session
        api._force_fresh_access_token = AsyncMock()

        result = await api.get_appliances_json()

        self.assertEqual([], result)
        api._force_fresh_access_token.assert_awaited_once()
        self.assertEqual(2, len(session.calls))

    async def test_persistent_gateway_token_rejection_is_an_auth_error(self):
        api = self.make_api()
        rejected = {
            "response": {
                "resultCode": 1,
                "errorCode": GATEWAY_INVALID_ACCESS_TOKEN,
                "errorDesc": "invalid token",
            }
        }
        session = FakeGatewaySession([rejected, rejected])
        api._session = session
        api._force_fresh_access_token = AsyncMock()

        with self.assertRaises(LifeConnectAuthError):
            await api.get_appliances_json()

        api._force_fresh_access_token.assert_awaited_once()

    async def test_gateway_retry_uses_a_fresh_nonce(self):
        api = self.make_api()
        api._retry_delays = (0.0,)
        session = FakeGatewaySession(
            [
                {"error": "temporary"},
                {"response": {"resultCode": 0, "deviceList": []}},
            ],
            statuses=[503, 200],
        )
        api._session = session

        result = await api.get_appliances_json()

        self.assertEqual([], result)
        self.assertEqual(2, len(session.calls))
        first_nonce = session.calls[0][2]["params"]["randStr"]
        second_nonce = session.calls[1][2]["params"]["randStr"]
        self.assertNotEqual(first_nonce, second_nonce)

    async def test_incomplete_snapshot_fails_without_reindexing(self):
        api = self.make_api()
        session = FakeGatewaySession([
            {
                "response": {
                    "resultCode": 0,
                    "deviceList": [
                        {"deviceId": "incomplete"},
                        {"deviceId": "target", "statusList": {}},
                    ],
                }
            }
        ])
        api._session = session

        with self.assertRaisesRegex(
            LifeConnectError,
            "snapshot is incomplete",
        ):
            await api.get_appliances()

    async def test_failed_refresh_token_falls_back_to_full_login(self):
        api = self.make_api()
        api._expires = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        api._refresh_access_token = AsyncMock(
            side_effect=LifeConnectAuthError("expired refresh token")
        )
        api._initial_access_token = AsyncMock()

        await api._fetch_access_token()

        api._refresh_access_token.assert_awaited_once()
        api._initial_access_token.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
