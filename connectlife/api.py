import asyncio
import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any, Sequence

import aiohttp
import logging

from .appliance import ConnectLifeAppliance

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
USER_AGENT = "connectlife-api-connector 2.1.4"
SERVER_ERROR_RETRY_DELAYS = (1.0, 3.0)
DEFAULT_TOKEN_CACHE = Path.home() / ".config" / "connectlife" / "tokens.json"


class LifeConnectError(Exception):
    pass


class LifeConnectAuthError(Exception):
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

    appliances_url = "https://connectlife.bapi.ovh/appliances"

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
        if test_server:
            self.login_url = f"{test_server}/accounts.login"
            self.jwt_url = f"{test_server}/accounts.getJWT"
            self.oauth2_redirect = f"{test_server}/swagger/oauth2-redirect.html"
            self.oauth2_authorize = f"{test_server}/oauth/authorize"
            self.oauth2_token = f"{test_server}/oauth/token"
            self.appliances_url = f"{test_server}/appliances"

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
        appliances = await self.get_appliances_json()
        self.appliances = [ConnectLifeAppliance(self, a) for a in appliances if "deviceId" in a]
        return self.appliances

    async def get_appliances_json(self) -> Any:
        """Make a request and return the response as text."""
        text = await self._request_with_token(
            "GET",
            self.appliances_url,
            operation="get appliances",
        )
        return json.loads(text)

    async def refresh_appliances(self) -> None:
        """Refresh all cached appliances from one API request."""
        appliances_data = await self.get_appliances_json()
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
        result = await self._request_with_token(
            "POST",
            self.appliances_url,
            operation="update appliance",
            json=data,
        )
        _LOGGER.debug("Update response: %s", result)
        _LOGGER.debug("Updated appliance with puid %s", puid)

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
                await self._refresh_access_token()
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
