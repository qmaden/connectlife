import asyncio
import datetime as dt
import logging
import unittest

import aiohttp

from connectlife.api import ConnectLifeApi, LifeConnectError

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


if __name__ == "__main__":
    unittest.main()
