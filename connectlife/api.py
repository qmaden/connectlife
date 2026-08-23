import asyncio
import base64
import datetime as dt
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Sequence, cast

import aiohttp
import logging
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from .appliance import ConnectLifeAppliance

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
USER_AGENT = "connectlife-api-connector 2.1.4"
SERVER_ERROR_RETRY_DELAYS = (1.0, 3.0)
DEFAULT_TOKEN_CACHE = Path.home() / ".config" / "connectlife" / "tokens.json"

GATEWAY_USER_AGENT = "connectlife/0.5.4"
GATEWAY_BASE_URL = "https://clife-eu-gateway.hijuconn.com"
GATEWAY_DEVICE_LIST_URL = (
    f"{GATEWAY_BASE_URL}/clife-svc/pu/get_device_status_list"
)
GATEWAY_UPDATE_URL = f"{GATEWAY_BASE_URL}/device/pu/property/set"
GATEWAY_APP_ID = "47110565134383"
GATEWAY_APP_SECRET = (
    "yOzhz6junYno-nmULM3Wr7PU_dpSZN22ZdluvVWZ4uW5ZwwG8fIGCHTbrhcnU-iv"
)
GATEWAY_LANGUAGE_ID = "12"
GATEWAY_TIMEZONE = "1.0"
GATEWAY_VERSION = "5.0"
GATEWAY_SIGN_SUFFIX = "D9519A4B756946F081B7BB5B5E8D1197"
GATEWAY_INVALID_ACCESS_TOKEN = 100026
GATEWAY_RANDSTR_CHECK_FAILED = 101005
GATEWAY_PUBLIC_KEY = cast(
    RSAPublicKey,
    serialization.load_pem_public_key(
        b"-----BEGIN PUBLIC KEY-----\n"
        b"MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAyyWrNG6q475HIHu7sMVu\n"
        b"vHof6vlgPeixmxa4EL/UsvVvHPz33NnWoQetQqit9TBNzUjMXw0KlY9PXM4iqHUU\n"
        b"U+dSyNDq1jZWIiJ2C2FccppswJtIKL3NRMFvT9PFh6NlP/4FUcQKojgKFbF7Kacc\n"
        b"JPKYHlwaO7qgoIjLxAHlSOXGpucJcOkPzT2EqsSVnW8sn8kenvNmghXDayhgxsh6\n"
        b"AyxK4kehJplEnmX/iYCfNoFXknGcLqFWYccgBz3fybvx30C/0IgU1980L8QsUAv5\n"
        b"esZmN8ugnbRgLRxKRlkQQLxQAiZMZdKTAx665YflT3YMHJvEFE8c2XFgoxHzSMc4\n"
        b"BwIDAQAB\n"
        b"-----END PUBLIC KEY-----\n"
    ),
)


class LifeConnectError(Exception):
    pass


class LifeConnectAuthError(LifeConnectError):
    pass


class ConnectLifeApi:
    api_key = "4_yhTWQmHFpZkQZDSV1uV-_A"
    client_id = "5065059336212"
    client_secret = "07swfKgvJhC3ydOUS9YV_SwVz0i4LKqlOLGNUukYHVMsJRF1b-iWeUGcNlXyYCeK"

    login_url = "https://accounts.eu1.gigya.com/accounts.login"
    jwt_url = "https://accounts.eu1.gigya.com/accounts.getJWT"

    oauth2_redirect = "https://api.connectlife.io/swagger/oauth2-redirect.html"
    oauth2_authorize = "https://oauth.hijuconn.com/oauth/authorize"
    oauth2_token = "https://oauth.hijuconn.com/oauth/token"

    appliances_url = ""  # Populated only for the local legacy test server.
    gateway_device_list_url = GATEWAY_DEVICE_LIST_URL
    gateway_update_url = GATEWAY_UPDATE_URL

    def __init__(
        self,
        username: str,
        password: str,
        test_server: str = None,
        *,
        timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT,
        retry_delays: tuple[float, ...] = SERVER_ERROR_RETRY_DELAYS,
        token_cache: Path | None = DEFAULT_TOKEN_CACHE,
    ):
        """Initialize the auth."""
        self._test_server = test_server is not None
        if test_server:
            self.login_url = f"{test_server}/accounts.login"
            self.jwt_url = f"{test_server}/accounts.getJWT"
            self.oauth2_redirect = f"{test_server}/swagger/oauth2-redirect.html"
            self.oauth2_authorize = f"{test_server}/oauth/authorize"
            self.oauth2_token = f"{test_server}/oauth/token"
            self.appliances_url = f"{test_server}/appliances"
            self.gateway_device_list_url = (
                f"{test_server}/clife-svc/pu/get_device_status_list"
            )
            self.gateway_update_url = f"{test_server}/device/pu/property/set"

        self._username = username
        self._password = password
        self._access_token: str | None = None
        self._expires: dt.datetime | None = None
        self._refresh_token: str | None = None
        self.appliances: Sequence[ConnectLifeAppliance] = []
        self._session: aiohttp.ClientSession | None = None
        self._timeout = timeout
        self._retry_delays = retry_delays
        self._token_cache = token_cache
        self._token_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create a reusable client session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        """Close the client session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def authenticate(self) -> bool:
        """Test if we can authenticate with the host."""
        session = await self._get_session()
        async with session.post(self.login_url, data={
            "loginID": self._username,
            "password": self._password,
            "APIKey": self.api_key,
        }) as response:
            if response.status == 200:
                body = await self._json(response)
                return "UID" in body and "sessionInfo" in body and "cookieValue" in body["sessionInfo"]
        return False

    async def login(self) -> None:
        await self._fetch_access_token()

    async def get_appliances(self) -> Any:
        """Make a request."""
        appliances = self._normalize_appliance_payloads(
            await self.get_appliances_json()
        )
        self.appliances = [ConnectLifeAppliance(self, a) for a in appliances if "deviceId" in a]
        return self.appliances

    async def get_appliances_json(self) -> Any:
        """Make a request and return the response as text."""
        if self._test_server:
            text = await self._request_with_token(
                "GET",
                self.appliances_url,
                operation="get appliances",
            )
            return json.loads(text)

        await self._fetch_access_token()
        gateway_response = await self._request_gateway_json(
            self.gateway_device_list_url,
            payload={},
            retry_on_reauth=True,
            retry_on_randstr=True,
            method="GET",
        )
        device_list = gateway_response.get("deviceList")
        if not isinstance(device_list, list):
            raise LifeConnectError(
                "Unexpected response from HijuConn gateway: missing 'deviceList'"
            )
        return device_list

    async def refresh_appliances(self) -> None:
        """Refresh all cached appliances from one API request."""
        appliances_data = self._normalize_appliance_payloads(
            await self.get_appliances_json()
        )
        by_puid = {a["puid"]: a for a in appliances_data if "puid" in a}
        for appliance in self.appliances:
            if appliance.puid in by_puid:
                appliance._update_status(by_puid[appliance.puid])

    async def update_appliance(self, puid: str, properties: dict[str, str]):
        data = {
            "puid": puid,
            "properties": properties
        }
        _LOGGER.debug("Updating appliance with puid %s to %s", puid, json.dumps(properties))
        if self._test_server:
            result = await self._request_with_token(
                "POST",
                self.appliances_url,
                operation="update appliance",
                json=data,
            )
            _LOGGER.debug("Update response: %s", result)
        else:
            await self._fetch_access_token()
            await self._request_gateway_json(
                self.gateway_update_url,
                payload=data,
                retry_on_reauth=True,
                retry_on_randstr=True,
            )
        _LOGGER.debug("Updated appliance with puid %s", puid)

    def _normalize_appliance_payloads(
        self,
        payloads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Reject incomplete snapshots instead of treating cached state as fresh."""
        for payload in payloads:
            device_id = payload.get("deviceId")
            if not isinstance(payload.get("statusList"), dict):
                raise LifeConnectError(
                    "ConnectLife appliance snapshot is incomplete: "
                    f"device {device_id!r} has no statusList"
                )
        return payloads

    async def _request_gateway_json(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        retry_on_reauth: bool,
        retry_on_randstr: bool = False,
        method: str = "POST",
    ) -> dict[str, Any]:
        """Send one signed request directly to the HijuConn gateway."""
        body = await self._request_gateway_http_json(
            url,
            payload=payload,
            method=method,
        )

        gateway_response = body.get("response")
        if not isinstance(gateway_response, dict):
            raise LifeConnectError(
                "Unexpected response from HijuConn gateway: missing 'response'"
            )

        result_code = gateway_response.get("resultCode")
        if result_code in (0, "0", None):
            return gateway_response

        error_code = gateway_response.get("errorCode")
        if retry_on_reauth and error_code in (
            GATEWAY_INVALID_ACCESS_TOKEN,
            str(GATEWAY_INVALID_ACCESS_TOKEN),
        ):
            _LOGGER.warning(
                "HijuConn gateway access token rejected; re-authenticating once"
            )
            await self._force_fresh_access_token()
            return await self._request_gateway_json(
                url,
                payload=payload,
                retry_on_reauth=False,
                retry_on_randstr=retry_on_randstr,
                method=method,
            )

        if retry_on_randstr and error_code in (
            GATEWAY_RANDSTR_CHECK_FAILED,
            str(GATEWAY_RANDSTR_CHECK_FAILED),
        ):
            _LOGGER.debug(
                "HijuConn gateway nonce rejected; retrying with a fresh signature"
            )
            return await self._request_gateway_json(
                url,
                payload=payload,
                retry_on_reauth=retry_on_reauth,
                retry_on_randstr=False,
                method=method,
            )

        error_description = (
            gateway_response.get("errorDesc") or "Unknown gateway error"
        )
        error_type = (
            LifeConnectAuthError
            if error_code in (
                GATEWAY_INVALID_ACCESS_TOKEN,
                str(GATEWAY_INVALID_ACCESS_TOKEN),
            )
            else LifeConnectError
        )
        raise error_type(
            "Unexpected response from HijuConn gateway: "
            f"code={error_code} description='{error_description}'"
        )

    async def _request_gateway_http_json(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        method: str,
    ) -> dict[str, Any]:
        """Retry transient gateway failures with a fresh nonce each time."""
        delays = (*self._retry_delays, None)
        for attempt, delay in enumerate(delays, start=1):
            request_data = self._gateway_request_data(payload)
            session = await self._get_session()
            request = session.get if method == "GET" else session.post
            request_kwargs: dict[str, Any]
            if method == "GET":
                request_kwargs = {
                    "params": request_data,
                    "headers": {"User-Agent": GATEWAY_USER_AGENT},
                }
            else:
                request_kwargs = {
                    "json": request_data,
                    "headers": {"User-Agent": GATEWAY_USER_AGENT},
                }

            started = time.monotonic()
            try:
                async with request(url, **request_kwargs) as response:
                    text = await response.text()
                    elapsed = time.monotonic() - started
                    if response.status == 200:
                        try:
                            body = json.loads(text)
                        except json.JSONDecodeError as err:
                            raise LifeConnectError(
                                "Non-JSON response from HijuConn gateway"
                            ) from err
                        if not isinstance(body, dict):
                            raise LifeConnectError(
                                "Unexpected response from HijuConn gateway: "
                                "expected an object"
                            )
                        return body

                    if response.status < 500 or delay is None:
                        raise LifeConnectError(
                            "Unexpected response from HijuConn gateway after "
                            f"{attempt} attempts: status={response.status}"
                        )
                    _LOGGER.warning(
                        "HijuConn gateway attempt %d returned status=%d in %.2fs; "
                        "retrying in %.1fs",
                        attempt,
                        response.status,
                        elapsed,
                        delay,
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                elapsed = time.monotonic() - started
                error = self._format_request_error(err)
                if delay is None:
                    raise LifeConnectError(
                        "ConnectLife HijuConn gateway request failed after "
                        f"{attempt} attempts: {error}"
                    ) from err
                _LOGGER.warning(
                    "HijuConn gateway attempt %d failed in %.2fs (%s); "
                    "retrying in %.1fs",
                    attempt,
                    elapsed,
                    error,
                    delay,
                )
            await asyncio.sleep(delay)

        raise LifeConnectError("ConnectLife HijuConn gateway request failed")

    def _gateway_request_data(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the signed request envelope expected by HijuConn."""
        if not self._access_token:
            raise LifeConnectAuthError("Missing 'access_token' in response")
        request_data: dict[str, Any] = {
            "accessToken": self._access_token,
            "appId": GATEWAY_APP_ID,
            "appSecret": GATEWAY_APP_SECRET,
            "languageId": GATEWAY_LANGUAGE_ID,
            "randStr": secrets.token_hex(16),
            "timeStamp": str(int(dt.datetime.now().timestamp() * 1000)),
            "timezone": GATEWAY_TIMEZONE,
            "version": GATEWAY_VERSION,
        }
        request_data.update(payload)
        request_data["sign"] = self._sign_gateway_request(request_data)
        return request_data

    @staticmethod
    def _sign_gateway_request(payload: dict[str, Any]) -> str:
        """Sign a gateway request using the ConnectLife client protocol."""
        unsigned_items: list[str] = []
        for key in sorted(key for key in payload if key != "sign"):
            value = payload[key]
            if isinstance(value, (dict, list)):
                value = json.dumps(value, separators=(",", ":"))
            unsigned_items.append(f"{key}={value}")
        digest = hashlib.sha256(
            f"{'&'.join(unsigned_items)}{GATEWAY_SIGN_SUFFIX}".encode()
        ).digest()
        encrypted = GATEWAY_PUBLIC_KEY.encrypt(digest, padding.PKCS1v15())
        return base64.b64encode(encrypted).decode()

    async def _request_with_token(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        **kwargs,
    ) -> str:
        """Run an authenticated request with transient retries and one re-login."""
        await self._fetch_access_token()
        used_fresh_token = False

        while True:
            status, text = await self._request_with_current_token(
                method,
                url,
                operation=operation,
                **kwargs,
            )
            if status == 200:
                return text

            if status in (401, 403):
                if used_fresh_token:
                    raise LifeConnectError(
                        f"Authentication rejected during {operation} after re-login "
                        f"(status={status})"
                    )
                _LOGGER.warning(
                    "Token rejected during %s (status=%d); re-authenticating once",
                    operation,
                    status,
                )
                await self._force_fresh_access_token()
                used_fresh_token = True
                continue

            if status >= 500:
                raise LifeConnectError(
                    f"ConnectLife API unavailable during {operation} after retries "
                    f"(status={status})"
                )

            raise LifeConnectError(
                f"Unexpected response during {operation} (status={status})"
            )

    async def _request_with_current_token(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        **kwargs,
    ) -> tuple[int, str]:
        """Run a request with the current token and retry transient failures."""
        delays = (*self._retry_delays, None)
        for attempt, delay in enumerate(delays, start=1):
            started = time.monotonic()
            try:
                session = await self._get_session()
                async with session.request(
                    method,
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "X-Token": self._access_token,
                    },
                    **kwargs,
                ) as response:
                    text = await response.text()
                    elapsed = time.monotonic() - started
                    if response.status != 200:
                        _LOGGER.warning(
                            "ConnectLife %s attempt %d returned status=%d in %.2fs",
                            operation,
                            attempt,
                            response.status,
                            elapsed,
                        )
                        _LOGGER.debug("ConnectLife response body: %s", text)
                    if response.status < 500 or delay is None:
                        return response.status, text
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                elapsed = time.monotonic() - started
                error = self._format_request_error(err)
                if delay is None:
                    raise LifeConnectError(
                        f"ConnectLife request failed during {operation} after "
                        f"{attempt} attempts: {error}"
                    ) from err
                _LOGGER.warning(
                    "ConnectLife %s attempt %d failed in %.2fs (%s); "
                    "retrying in %.1fs",
                    operation,
                    attempt,
                    elapsed,
                    error,
                    delay,
                )
            else:
                _LOGGER.warning(
                    "ConnectLife %s attempt %d hit a server error; retrying in %.1fs",
                    operation,
                    attempt,
                    delay,
                )
            await asyncio.sleep(delay)

        raise LifeConnectError(f"ConnectLife request failed during {operation}")

    @staticmethod
    def _format_request_error(err: BaseException) -> str:
        """Return useful text even for exceptions whose string is empty."""
        detail = str(err).strip()
        if detail:
            return f"{type(err).__name__}: {detail}"
        return f"{type(err).__name__}: {err!r}"

    async def _fetch_access_token(self):
        async with self._token_lock:
            if self._expires is None:
                if not self._load_token_cache():
                    await self._initial_access_token()
                    self._save_token_cache()
            elif self._expires < dt.datetime.now(dt.UTC):
                try:
                    await self._refresh_access_token()
                except (
                    LifeConnectAuthError,
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    ValueError,
                ) as err:
                    _LOGGER.warning(
                        "ConnectLife token refresh failed; retrying full login: %s",
                        self._format_request_error(err),
                    )
                    self._clear_token_cache()
                    await self._initial_access_token()
                self._save_token_cache()

    async def _force_fresh_access_token(self) -> None:
        async with self._token_lock:
            self._clear_token_cache()
            await self._initial_access_token()
            self._save_token_cache()

    def _load_token_cache(self) -> bool:
        """Load valid tokens belonging to the configured account."""
        if self._token_cache is None:
            return False
        try:
            if not self._token_cache.exists():
                return False
            with self._token_cache.open() as file:
                data = json.load(file)
            if data.get("username") != self._username:
                return False
            expires = dt.datetime.fromisoformat(data["expires"])
            if expires <= dt.datetime.now(dt.UTC):
                return False
            self._access_token = data["access_token"]
            self._refresh_token = data["refresh_token"]
            self._expires = expires
            return True
        except (KeyError, ValueError, OSError, json.JSONDecodeError):
            return False

    def _save_token_cache(self) -> None:
        if self._token_cache is None:
            return
        try:
            self._token_cache.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                self._token_cache,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(fd, "w") as file:
                json.dump(
                    {
                        "username": self._username,
                        "access_token": self._access_token,
                        "refresh_token": self._refresh_token,
                        "expires": self._expires.isoformat(),
                    },
                    file,
                )
            self._token_cache.chmod(0o600)
        except OSError as err:
            _LOGGER.warning("Could not save token cache: %s", err)

    def _clear_token_cache(self) -> None:
        self._access_token = None
        self._refresh_token = None
        self._expires = None
        if self._token_cache is None:
            return
        try:
            self._token_cache.unlink(missing_ok=True)
        except OSError as err:
            _LOGGER.warning("Could not clear token cache: %s", err)

    async def _initial_access_token(self):
        session = await self._get_session()

        async with session.post(self.login_url, data={
            "loginID": self._username,
            "password": self._password,
            "APIKey": self.api_key
        }) as response:
            if response.status != 200:
                _LOGGER.debug("Response status code: %d", response.status)
                _LOGGER.debug(response.headers)
                _LOGGER.debug(await response.text())
                raise LifeConnectAuthError(f"Unexpected response from login: status={response.status}")
            body = await self._json(response)
            error_code = body.get("errorCode")
            error_message = body.get("errorMessage")
            error_details = body.get("errorDetails")
            if error_code or error_message or error_details:
                raise LifeConnectAuthError(f"Failed to login. Code: {error_code} Message: '{error_message}' Details: '{error_details}'")
            uid = self._require_auth_field(body, "UID")
            session_info = self._require_auth_field(body, "sessionInfo")
            if "cookieValue" not in session_info:
                _LOGGER.debug("Missing 'sessionInfo.cookieValue' in login response")
                raise LifeConnectAuthError("Missing 'sessionInfo.cookieValue' in response")
            login_token = body["sessionInfo"]["cookieValue"]

        async with session.post(self.jwt_url, data={
            "APIKey": self.api_key,
            "login_token":  login_token
        }) as response:
            if response.status != 200:
                _LOGGER.debug("Response status code: %d", response.status)
                _LOGGER.debug(response.headers)
                _LOGGER.debug(await response.text())
                raise LifeConnectAuthError(f"Unexpected response from getJWT: status={response.status}")
            body = await self._json(response)
            if "id_token" not in body:
                raise LifeConnectAuthError("Missing 'id_token' in response")
            id_token = body["id_token"]

        async with session.post(self.oauth2_authorize, json={
            "client_id": self.client_id,
            "redirect_uri": self.oauth2_redirect,
            "idToken":  id_token,
            "response_type": "code",
            "thirdType": "CDC",
            "thirdClientId": uid,
        }) as response:
            if response.status != 200:
                _LOGGER.debug("Response status code: %d", response.status)
                _LOGGER.debug(response.headers)
                _LOGGER.debug(await response.text())
                raise LifeConnectAuthError(f"Unexpected response from authorize: status={response.status}")
            body = await response.json()
            code = self._require_auth_field(body, "code")

        async with session.post(self.oauth2_token, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.oauth2_redirect,
            "grant_type": "authorization_code",
            "code": code,
        }) as response:
            if response.status != 200:
                _LOGGER.debug("Response status code: %d", response.status)
                _LOGGER.debug(response.headers)
                _LOGGER.debug(await response.text())
                raise LifeConnectAuthError(f"Unexpected response from initial access token: status={response.status}")
            body = await self._json(response)
            self._access_token = self._require_auth_field(body, "access_token")
            expires_in = self._require_auth_field(body, "expires_in")
            # Renew 90 seconds before expiration
            self._expires = dt.datetime.now(dt.UTC) + dt.timedelta(0, expires_in - 90)
            self._refresh_token = self._require_auth_field(body, "refresh_token")

    async def _refresh_access_token(self) -> None:
        session = await self._get_session()
        async with session.post(self.oauth2_token, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.oauth2_redirect,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }) as response:
            if response.status != 200:
                _LOGGER.debug("Response status code: %d", response.status)
                _LOGGER.debug(response.headers)
                _LOGGER.debug(await response.text())
                raise LifeConnectAuthError(f"Unexpected response from refreshing access token: status={response.status}")
            body = await response.json()
            self._access_token = self._require_auth_field(body, "access_token")
            expires_in = self._require_auth_field(body, "expires_in")
            # Renew 90 seconds before expiration
            self._expires = dt.datetime.now(dt.UTC) + dt.timedelta(0, expires_in - 90)
            if "refresh_token" in body:
                self._refresh_token = body["refresh_token"]

    @staticmethod
    async def _json(response: aiohttp.ClientResponse) -> Any:
        # response may have wrong content-type, cannot use response.json()
        text = await response.text()
        _LOGGER.debug("Received response (length=%d)", len(text))
        return json.loads(text)

    @staticmethod
    def _require_auth_field(response: dict[str, Any], field: str):
        if field not in response:
            _LOGGER.debug("Missing '%s' in auth response", field)
            raise LifeConnectAuthError(f"Missing '{field}' in response")
        return response[field]
