"""Provides access to the Keurig SMART APIs."""
# pylint: disable=protected-access, broad-except, too-many-lines
import json
import logging
import time
from typing import Dict, Optional
import uuid
import requests
from tzlocal import get_localzone


import httpx

from signalrcoreplus.hub_connection_builder import HubConnectionBuilder

from pykeurig.const import (
    API_URL,
    BREW_COFFEE,
    BREW_HOT_WATER,
    BREW_OVER_ICE,
    BREWER_STATUS_READY,
    CLIENT_ID,
    COMMAND_NAME_BREW,
    COMMAND_NAME_CANCEL_BREW,
    COMMAND_NAME_OFF,
    COMMAND_NAME_ON,
    FAVORITE_BREW_MODE,
    FAVORITE_MODEL_NAME,
    HEADER_OCP_SUBSCRIPTION_KEY,
    HEADER_USER_AGENT,
    NODE_APPLIANCE_STATE,
    NODE_BREW_STATE,
    NODE_POD_STATE,
    NODE_SW_INFO,
    POD_STATUS_EMPTY,
    BrewCategory,
    DaysOfWeek,
    Intensity,
    Size,
    Temperature,
)


_LOGGER = logging.getLogger(__name__)


class KeurigApi:
    """Provide access to the Keurig SMART API."""

    MAX_RECONNECT_DELAY: int = 240

    def __init__(self, timeout: int = 10, locale: str = "us", language: str = "en"):
        self._access_token = None
        self._token_expires_at = None
        self._refresh_token = None
        self._customer_id = None
        self._timeout = timeout
        self.locale = locale
        self.language = language
        self._signalr_access_token = None
        self._signalr_url = None
        self._devices = None
        self._signalr_connection = None
        self._reconnect_count = 0
        self._disconnecting = False

    async def login(self, email: str, password: str):
        """Logs you into the Keurig API"""
        try:
            data = {
                "grant_type": "password",
                "client_id": CLIENT_ID,
                "username": email,
                "password": password,
            }
            client = httpx.AsyncClient()
            client.headers = self._get_headers({"Accept-Encoding": "identity"})

            endpoint = f"{API_URL}api/claa/v1/oauth/token"
            res = await client.post(endpoint, json=data, timeout=self._timeout)
            res.raise_for_status()

            json_result = res.json()

            self._access_token = json_result["access_token"]
            self._token_expires_at = time.time() + json_result["expires_in"] - 120
            self._refresh_token = json_result["refresh_token"]
        except Exception as ex:
            return False
        finally:
            await client.aclose()
        return True

    async def async_get_customer(self):
        """Retrieves the customer information associated with the username logged into the API"""
        res = await self._async_get("api/usdm/v1/user/profile")
        json_result = res.json()

        self._customer_id = json_result["customerID"]
        return json_result

    async def async_get_devices(self):
        """Gets a list of the Keurig devices associated with the logged in account"""
        # If we don't already have the customer details, get it
        if self._customer_id is None:
            await self.async_get_customer()
        res = await self._async_get(
            "api/alcm/v1/devices?customerId=" + self._customer_id
        )
        json_result = res.json()

        self._devices = []
        for device in json_result["devices"]:
            self._devices.append(
                KeurigDevice(
                    self,
                    device["id"],
                    device["serialNumber"],
                    device["model"],
                    device["registration"]["name"],
                )
            )

        return self._devices

    async def async_add_favorite(
        self, name: str, size: Size, temperature: Temperature, intensity: Intensity
    ):
        """Add a favorite"""
        await self._async_post(
            "api/usdm/v1/presets",
            data={
                "name": name,
                "size": int(size),
                "temperature": int(temperature),
                "flowRate": int(intensity),
                "brewMode": FAVORITE_BREW_MODE,
                "deviceModel": FAVORITE_MODEL_NAME,
            },
        )

    async def async_update_favorite(
        self,
        favorite_id: str,
        name: str,
        size: Size,
        temperature: Temperature,
        intensity: Intensity,
    ):
        """Update a favorite"""
        await self._async_put(
            "api/usdm/v1/presets/" + favorite_id,
            data={
                "name": name,
                "size": int(size),
                "temperature": int(temperature),
                "flowRate": int(intensity),
                "brewMode": FAVORITE_BREW_MODE,
                "deviceModel": FAVORITE_MODEL_NAME,
            },
        )

    async def async_get_favorites(self):
        """Retrieves the list of favorites from the API"""
        res = await self._async_get("api/usdm/v1/presets")
        json_result = res.json()

        return json_result

    async def async_delete_favorite(self, favorite_id: str):
        """Delete a favorite"""
        await self._async_delete("api/usdm/v1/presets/" + favorite_id)

    async def async_connect(self):
        """Establish a connection to the SignalR server to receive real-time push notifications."""
        self._disconnecting = False

        try:
            # We need to do this to get the URL
            await self._async_get_signalr_access_token()
        except Exception:
            return False

        hub_connection = (
            HubConnectionBuilder()
            .with_url(
                self._signalr_url,
                options={"access_token_factory": self._get_signalr_access_token},
            )
            .build()
        )
        hub_connection.on("appliance-notifications", self._receive_signalr)
        hub_connection.on_error(self._reconnect_websocket)
        hub_connection.on_close(self._reconnect_websocket)
        result = hub_connection.start()
        self._signalr_connection = hub_connection
        if result:
            self._reconnect_count = 0
        return result

    def disconnect(self):
        """Disconnect from the SignalR server"""
        self._disconnecting = True
        if self._signalr_connection is not None:
            self._signalr_connection.stop()
            self._signalr_connection = None

    def connect(self):
        """Establish a connection to the SignalR server to receive real-time push notifications."""
        self._disconnecting = False

        # We need to do this to get the URL
        try:
            self._get_signalr_access_token()
        except Exception:
            return False

        hub_connection = (
            HubConnectionBuilder()
            .with_url(
                self._signalr_url,
                options={"access_token_factory": self._get_signalr_access_token},
            )
            .build()
        )
        hub_connection.on("appliance-notifications", self._receive_signalr)
        hub_connection.on_error(self._reconnect_websocket)
        hub_connection.on_close(self._reconnect_websocket)
        result = hub_connection.start()
        self._signalr_connection = hub_connection
        if result:
            self._reconnect_count = 0
        return result

    def _reconnect_websocket(self):
        """Reconnect to the websocket if an error occurs."""
        connect_continue = True
        while connect_continue:
            if not self._disconnecting:
                if self._signalr_connection is not None:
                    try:
                        self._signalr_connection.on_error(None)
                        self._signalr_connection.on_close(None)
                        self._signalr_connection.stop()
                    except Exception:
                        pass
                print("RECONNECTING")
                self._reconnect_count += 1
                reconnect_delay = 2**self._reconnect_count
                if reconnect_delay > self.MAX_RECONNECT_DELAY:
                    reconnect_delay = self.MAX_RECONNECT_DELAY
                time.sleep(reconnect_delay)
                connect_continue = not self.connect()

    async def async_get_brand_image(self, brand_id):
        """Get the logo image for a brand"""
        res = await self._async_get(
            f"api/pldm/v1/resources/brandlogos_us/-10/{brand_id}.png"
        )
        return res.content

    async def async_get_variety_image(self, variety_id):
        """Get the logo image for a variety"""
        res = await self._async_get(
            f"api/pldm/v1/resources/herolids_us/-10/{variety_id}.png"
        )
        return res.content

    async def _async_get_signalr_access_token(self):
        """Gets the SignalR URL and access token asynchronously"""
        res = await self._async_get("api/clnt/v1/signalr/negotiate")
        self.__parse_signalr_access_token_response(res.json())

        return self._signalr_access_token

    def _get_signalr_access_token(self):
        """Gets the SignalR URL and access token synchronously"""
        try:
            res = self._get("api/clnt/v1/signalr/negotiate")
            self.__parse_signalr_access_token_response(res.json())

            return self._signalr_access_token
        except Exception as ex:
            raise requests.exceptions.ConnectionError() from ex

    def __parse_signalr_access_token_response(self, json_result):
        """Parse the JSON response to get the SignalR connection information"""
        if "accessToken" in json_result.keys():
            self._signalr_access_token = json_result["accessToken"]
        else:
            self._signalr_access_token = json_result["AccessToken"]
        if "url" in json_result.keys():
            self._signalr_url = json_result["url"]
        else:
            self._signalr_url = json_result["Url"]
        self._signalr_url = self._signalr_url.replace("https://", "wss://")

    def _receive_signalr(self, args):
        """Handle processing a SignalR message"""
        if args is not None and len(args) > 0:
            msg = args[0]
            device_id = msg["deviceId"]
            body = msg["body"]

            print(msg)

            # It will be immediately followed by a BrewStateChange so no need to trigger two updates
            if msg["eventType"] == "ApplianceStateChange" and body["current"] == "BREW":
                return

            # Find the matching device and update its data
            device = next(
                (device for device in self._devices if device.id == device_id)
            )
            if device is not None:
                device._update_properties()

    def _get_headers(self, extra_headers=None):
        """Gets the default set of headers to pass to requests."""
        headers = {
            #"User-Agent": HEADER_USER_AGENT,
            "Ocp-Apim-Subscription-Key": HEADER_OCP_SUBSCRIPTION_KEY,
            "Content-Type": "application/json",
            #"reqId": str(uuid.uuid4()),
        }
        if self._access_token is not None:
            headers["Authorization"] = "Bearer " + self._access_token

        if extra_headers is not None:
            headers.update(extra_headers)

        return headers

    def _post(
        self,
        request: str,
        content: Optional[bytes] = None,
        data: Optional[Dict] = None,
        headers: Optional[Dict] = None,
    ) -> httpx.Response:

        """Call POST endpoint of Keurig API synchronously."""

        endpoint = f"{API_URL}{request}"

        if self._token_expires_at is not None and self._token_expires_at <= time.time():
            self._get_refresh_token()

        client = httpx.Client()

        try:
            client.headers = self._get_headers(headers)

            res = client.post(
                endpoint, content=content, json=data, timeout=self._timeout
            )
            if res.status_code == 401:
                if not self._get_refresh_token():
                    raise UnauthorizedException()
                client.headers = self._get_headers(headers)

                res = client.post(
                    endpoint, content=content, json=data, timeout=self._timeout
                )
                if res.status_code == 401:
                    # Means the refresh failed, throw an unauthorized exception
                    raise UnauthorizedException()
            res.raise_for_status()
        finally:
            client.close()

        return res

    async def _async_post(
        self,
        request: str,
        content: Optional[bytes] = None,
        data: Optional[Dict] = None,
        headers: Optional[Dict] = None,
    ) -> httpx.Response:
        """Call POST endpoint of Keurig API asynchronously."""

        endpoint = f"{API_URL}{request}"

        if self._token_expires_at is not None and self._token_expires_at <= time.time():
            await self._async_refresh_token()

        client = httpx.AsyncClient()

        try:
            client.headers = self._get_headers(headers)

            res = await client.post(
                endpoint, content=content, json=data, timeout=self._timeout
            )
            if res.status_code == 401:
                if not await self._async_refresh_token():
                    raise UnauthorizedException()
                client.headers = self._get_headers(headers)

                res = await client.post(
                    endpoint, content=content, json=data, timeout=self._timeout
                )
                if res.status_code == 401:
                    # Means the refresh failed, throw an unauthorized exception
                    raise UnauthorizedException()
            res.raise_for_status()
        finally:
            await client.aclose()

    async def _async_delete(
        self, request: str, headers: Optional[Dict] = None
    ) -> httpx.Response:
        """Call DELETE endpoint of Keurig API asynchronously."""

        endpoint = f"{API_URL}{request}"

        if self._token_expires_at is not None and self._token_expires_at <= time.time():
            await self._async_refresh_token()

        client = httpx.AsyncClient()

        try:
            client.headers = self._get_headers(headers)

            res = await client.delete(endpoint, timeout=self._timeout)
            if res.status_code == 401:
                if not await self._async_refresh_token():
                    raise UnauthorizedException()
                client.headers = self._get_headers(headers)

                res = await client.delete(endpoint, timeout=self._timeout)
                if res.status_code == 401:
                    # Means the refresh failed, throw an unauthorized exception
                    raise UnauthorizedException()
            res.raise_for_status()
        finally:
            await client.aclose()

        return res

    async def _async_put(
        self,
        request: str,
        content: Optional[bytes] = None,
        data: Optional[Dict] = None,
        headers: Optional[Dict] = None,
    ) -> httpx.Response:
        """Call PUT endpoint of Keurig API asynchronously."""

        endpoint = f"{API_URL}{request}"

        if self._token_expires_at is not None and self._token_expires_at <= time.time():
            await self._async_refresh_token()

        client = httpx.AsyncClient()

        try:
            client.headers = self._get_headers(headers)

            res = await client.put(
                endpoint, content=content, json=data, timeout=self._timeout
            )
            if res.status_code == 401:
                if not await self._async_refresh_token():
                    raise UnauthorizedException()
                client.headers = self._get_headers(headers)

                res = await client.put(
                    endpoint, content=content, json=data, timeout=self._timeout
                )
                if res.status_code == 401:
                    # Means the refresh failed, throw an unauthorized exception
                    raise UnauthorizedException()
            res.raise_for_status()
        finally:
            await client.aclose()

        return res

    def _get(self, request: str) -> httpx.Response:
        """Call GET endpoint of Keurig API synchronously."""

        endpoint = f"{API_URL}{request}"

        if self._token_expires_at is not None and self._token_expires_at <= time.time():
            self._get_refresh_token()

        client = httpx.Client()
        client.headers = self._get_headers()
        try:
            res = client.get(endpoint, timeout=self._timeout)
            if res.status_code == 401:
                if not self._get_refresh_token():
                    raise UnauthorizedException()
                client.headers = self._get_headers()
                res = client.get(endpoint, timeout=self._timeout)
                if res.status_code == 401:
                    # Means the refresh failed, throw an unauthorized exception
                    raise UnauthorizedException()
            res.raise_for_status()
        finally:
            client.close()

        return res

    async def _async_get(self, request: str) -> httpx.Response:
        """Call GET endpoint of Keurig API asynchronously."""

        endpoint = f"{API_URL}{request}"

        if self._token_expires_at is not None and self._token_expires_at <= time.time():
            await self._async_refresh_token()

        client = httpx.AsyncClient()
        client.headers = self._get_headers()
        try:
            res = await client.get(endpoint, timeout=self._timeout)
            if res.status_code in (400,401):
                if not await self._async_refresh_token():
                    raise UnauthorizedException()
                client.headers = self._get_headers()
                res = await client.get(endpoint, timeout=self._timeout)
                if res.status_code == 401:
                    # Means the refresh failed, throw an unauthorized exception
                    raise UnauthorizedException()
            res.raise_for_status()
        finally:
            await client.aclose()

        return res

    async def _async_refresh_token(self):
        """Retrieve a new access token asynchronously using a refresh_token"""

        data = {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": self._refresh_token,
        }

        client = httpx.AsyncClient()
        try:
            client.headers = self._get_headers({"Accept-Encoding": "identity"})

            endpoint = f"{API_URL}api/claa/v1/oauth/token"
            res = await client.post(endpoint, json=data, timeout=self._timeout)
            if res.status_code in (400, 401, 403):
                return False
            res.raise_for_status()

            json_result = res.json()
            self._access_token = json_result["access_token"]
            self._token_expires_at = time.time() + json_result["expires_in"] - 120
            self._refresh_token = json_result["refresh_token"]
        except Exception as err:
            _LOGGER.error("Error refreshing access token: %s", err)
            raise err
        finally:
            await client.aclose()

        return True

    def _get_refresh_token(self):
        """Retrieve a new access token synchronously using a refresh_token"""

        data = {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": self._refresh_token,
        }

        client = httpx.Client()
        try:
            client.headers = self._get_headers({"Accept-Encoding": "identity"})

            endpoint = f"{API_URL}api/claa/v1/oauth/token"
            res = client.post(endpoint, json=data, timeout=self._timeout)
            if res.status_code in (400, 401, 403):
                return False
            res.raise_for_status()

            json_result = res.json()
            self._access_token = json_result["access_token"]
            self._token_expires_at = time.time() + json_result["expires_in"] - 120
            self._refresh_token = json_result["refresh_token"]
        except Exception as err:
            _LOGGER.error("Error refreshing access token: %s", err)
            raise err
        finally:
            client.close()

        return True


class KeurigDevice:
    """Represents an individual Keurig brewer."""

    def __init__(
        self, api: KeurigApi, device_id: str, serial: str, model: str, name: str
    ):
        self._callbacks = []
        self._api = api
        self._name = name
        self._id = device_id
        self._serial = serial
        self._model = model
        self._sw_version = None
        self._appliance_status = None
        self._brewer_status = None
        self._pod_status = None
        self._pod_brand = None
        self._pod_variety = None
        self._pod_brand_id = None
        self._pod_variety_id = None
        self._pod_roast_type = None
        self._pod_is_tea = None
        self._pod_is_iced = None
        self._pod_is_flavored = None
        self._pod_is_powdered = None
        self._brewer_errors = []

    @property
    def id(self):  # pylint: disable=invalid-name
        """Get the device id"""
        return self._id

    @property
    def name(self):
        """Get the device name"""
        return self._name

    @property
    def serial(self):
        """Get the device serial number"""
        return self._serial

    @property
    def model(self):
        """Get the device model"""
        return self._model

    @property
    def sw_version(self):
        """Get the device firmware version"""
        return self._sw_version

    @property
    def appliance_status(self):
        """Get the device appliance status"""
        return self._appliance_status

    @property
    def brewer_status(self):
        """Get the device brewer status"""
        return self._brewer_status

    @property
    def errors(self):
        """Get the device brewer error if in an error state"""
        return self._brewer_errors

    @property
    def pod_status(self):
        """Get the device pod status"""
        return self._pod_status

    @property
    def pod_brand(self):
        """If a pod was recognized, returns the brand"""
        return self._pod_brand

    @property
    def pod_brand_id(self):
        """If a pod was recognized, returns the brand id"""
        return self._pod_brand_id

    @property
    def pod_variety(self):
        """If a pod was recognized, returns the variety"""
        return self._pod_variety

    @property
    def pod_variety_id(self):
        """If a pod was recognized, returns the variety id"""
        return self._pod_variety_id

    @property
    def pod_roast_type(self):
        """If a pod was recognized, returns the roast type"""
        return self._pod_roast_type

    @property
    def pod_is_tea(self):
        """If a pod was recognized, return whether or not it is tea"""
        return self._pod_is_tea

    @property
    def pod_is_iced(self):
        """If a pod was recognized, return whether or not it is iced"""
        return self._pod_is_iced

    @property
    def pod_is_flavored(self):
        """If a pod was recognized, return whether or not it is flavored"""
        return self._pod_is_flavored

    @property
    def pod_is_powdered(self):
        """If a pod was recognized, return whether or not it is powdered"""
        return self._pod_is_powdered

    async def power_on(self):
        """Turn the device on"""
        await self._api._async_post(
            "api/acsm/v1/devices/" + self._id + "/commands",
            data={"command_name": COMMAND_NAME_ON},
        )
        return True

    async def power_off(self):
        """Turn the device off"""
        await self._api._async_post(
            "api/acsm/v1/devices/" + self._id + "/commands",
            data={"command_name": COMMAND_NAME_OFF},
        )
        return True

    async def hot_water(self, size: Size, temp: Temperature):
        """Brew hot water at the specified size and temperature"""
        await self._async_update_properties()
        # Must be ready, and empty
        if (
            self._brewer_status != BREWER_STATUS_READY
            or self._pod_status != POD_STATUS_EMPTY
        ):
            return False

        await self._api._async_post(
            "api/acsm/v1/devices/" + self._id + "/commands",
            data={
                "command_name": COMMAND_NAME_BREW,
                "params": {
                    "size": size,
                    "brew_type": BREW_HOT_WATER,
                    "flow_rate": Intensity.Balanced,
                    "temp": temp,
                    "enhanced": True,
                    "category": BrewCategory.Water,
                },
            },
        )
        return True

    async def brew_hot(self, size: Size, temp: Temperature, intensity: Intensity):
        """Brew a hot drink at the specified size, temperature, and intensity"""

        await self._async_update_properties()
        # Must be ready, and not empty
        if (
            self._brewer_status != BREWER_STATUS_READY
            or self._pod_status == POD_STATUS_EMPTY
        ):
            return False

        await self._api._async_post(
            "api/acsm/v1/devices/" + self._id + "/commands",
            data={
                "command_name": COMMAND_NAME_BREW,
                "params": {
                    "size": size,
                    "brew_type": BREW_COFFEE,
                    "flow_rate": intensity,
                    "temp": temp,
                    "enhanced": True,
                    "category": BrewCategory.Custom,
                },
            },
        )
        return True

    async def brew_iced(self):
        """Brew an iced drink"""

        await self._async_update_properties()
        # Must be ready, and not empty
        if (
            self._brewer_status != BREWER_STATUS_READY
            or self._pod_status == POD_STATUS_EMPTY
        ):
            return False

        await self._api._async_post(
            "api/acsm/v1/devices/" + self._id + "/commands",
            data={
                "command_name": COMMAND_NAME_BREW,
                "params": {
                    "size": 6,
                    "brew_type": BREW_OVER_ICE,
                    "flow_rate": Intensity.Intense,
                    "temp": 201,
                    "enhanced": True,
                    "category": BrewCategory.Iced,
                },
            },
        )
        return True

    async def brew_recommendation(self, size: Size):
        """Brew a drink at the recommended settings for the k-cup at the specified size"""
        json_result = await self._async_update_properties()
        # Must be ready, and not empty
        if (
            self._brewer_status != BREWER_STATUS_READY
            or self._pod_status == POD_STATUS_EMPTY
        ):
            return False
        # get recommended brew settings based on size
        pod_state = next(
            (item for item in json_result if item["name"] == NODE_POD_STATE)
        )
        recipes = pod_state["value"]["pod_details"]["recipes"]
        recipe = next((recipe for recipe in recipes if recipe["size"] == size))
        temp = recipe["temp"]
        flow_rate = recipe["flow_rate"]

        await self._api._async_post(
            "api/acsm/v1/devices/" + self._id + "/commands",
            data={
                "command_name": COMMAND_NAME_BREW,
                "params": {
                    "size": size,
                    "brew_type": BREW_COFFEE,
                    "flow_rate": flow_rate,
                    "temp": temp,
                    "enhanced": True,
                    "category": BrewCategory.Recommended,
                },
            },
        )

    async def brew_favorite(self, favorite_id: str):
        """Brew the specified favorite setting"""
        await self._async_update_properties()
        # Must be ready, and not empty
        if (
            self._brewer_status != BREWER_STATUS_READY
            or self._pod_status == POD_STATUS_EMPTY
        ):
            return False

        # get favorite
        favorites = await self._api.async_get_favorites()

        favorite = next((fav for fav in favorites if fav["id"] == favorite_id))

        if favorite is not None:
            size = favorite["size"]
            flow_rate = favorite["flowRate"]
            temp = favorite["temperature"]

            # do brew
            await self._api._async_post(
                "api/acsm/v1/devices/" + self._id + "/commands",
                data={
                    "command_name": COMMAND_NAME_BREW,
                    "params": {
                        "size": size,
                        "brew_type": BREW_COFFEE,
                        "flow_rate": flow_rate,
                        "temp": temp,
                        "enhanced": True,
                        "category": BrewCategory.Favorite,
                    },
                },
            )

    async def cancel_brew(self):
        """Cancel the current brewing."""
        try:
            await self._api._async_post(
                "api/acsm/v1/devices/" + self._id + "/commands",
                data={"command_name": COMMAND_NAME_CANCEL_BREW},
            )
        except Exception:
            return False
        return True

    async def get_schedules(self):
        """Get the list of schedules."""
        try:
            res = await self._api._async_get("api/usdm/v1/schedules")
            json_result = res.json()
            matching_schedules = list(
                (
                    schedule
                    for schedule in json_result
                    if schedule["brewer_id"] == self._id
                )
            )
            return matching_schedules
        except Exception:
            return None

    async def add_schedule(
        self,
        name: str,
        enabled: bool,
        repeat: bool,
        time_val: time.struct_time,
        days: DaysOfWeek,
        brew_type: BrewCategory,
        favorite_id=None,
        size: Size = None,
        temperature: Temperature = None,
        intensity: Intensity = None,
    ):
        """Create a new schedule"""
        offset = int(
            (time.timezone if (time.localtime().tm_isdst == 0) else time.altzone) / 60
        )
        if brew_type == BrewCategory.Favorite:
            payload_parameters = {"id": favorite_id}
        else:
            payload_parameters = {
                "recipe_format_version": "1.0",
                "size": int(size) if brew_type != BrewCategory.Iced else 6,
            }
            if brew_type == BrewCategory.Water or brew_type == BrewCategory.Custom:
                payload_parameters["flowRate"] = int(intensity)
                payload_parameters["temperature"] = int(temperature)

        payload = {
            "category": self._category_to_schedule_str(brew_type),
            "parameters": json.dumps(payload_parameters),
        }

        schedule_obj = {
            "id": None,
            "version": "1.0",
            "enabled": enabled,
            "name": name,
            "brewer_id": self._id,
            "schedule_type": "Brew",
            "repeatable": repeat,
            "scheduled_time": {
                "hours": time_val.tm_hour,
                "minutes": time_val.tm_min,
                "offset": -offset,
                "dst": time.localtime().tm_isdst,
                "timezone": str(get_localzone()),
            },
            "scheduled_days": self._days_flags_to_array(days),
            "payload": json.dumps(payload),
        }

        await self._api._async_post("api/usdm/v1/schedules", data=schedule_obj)

    async def update_schedule(
        self,
        schedule_id: str,
        name: str,
        enabled: bool,
        repeat: bool,
        time_val: time.struct_time,
        days: DaysOfWeek,
        brew_type: BrewCategory,
        favorite_id=None,
        size: Size = None,
        temperature: Temperature = None,
        intensity: Intensity = None,
    ):
        """Update an existing schedule"""
        offset = int(
            (time.timezone if (time.localtime().tm_isdst == 0) else time.altzone) / 60
        )
        if brew_type == BrewCategory.Favorite:
            payload_parameters = {"id": favorite_id}
        else:
            payload_parameters = {
                "recipe_format_version": "1.0",
                "size": int(size) if brew_type != BrewCategory.Iced else 6,
            }
            if brew_type == BrewCategory.Water or brew_type == BrewCategory.Custom:
                payload_parameters["flowRate"] = int(intensity)
                payload_parameters["temperature"] = int(temperature)

        payload = {
            "category": self._category_to_schedule_str(brew_type),
            "parameters": json.dumps(payload_parameters),
        }

        schedule_obj = {
            "id": None,
            "version": "1.0",
            "enabled": enabled,
            "name": name,
            "brewer_id": self._id,
            "schedule_type": "Brew",
            "repeatable": repeat,
            "scheduled_time": {
                "hours": time_val.tm_hour,
                "minutes": time_val.tm_min,
                "offset": -offset,
                "dst": time.localtime().tm_isdst,
                "timezone": str(get_localzone()),
            },
            "scheduled_days": self._days_flags_to_array(days),
            "payload": json.dumps(payload),
        }

        await self._api._async_put(
            "api/usdm/v1/schedules/" + schedule_id, data=schedule_obj
        )

    def _days_flags_to_array(self, days: DaysOfWeek):
        days_array = []
        if days & DaysOfWeek.Sunday:
            days_array.append("Sunday")
        if days & DaysOfWeek.Monday:
            days_array.append("Monday")
        if days & DaysOfWeek.Tuesday:
            days_array.append("Tuesday")
        if days & DaysOfWeek.Wednesday:
            days_array.append("Wednesday")
        if days & DaysOfWeek.Thursday:
            days_array.append("Thursday")
        if days & DaysOfWeek.Friday:
            days_array.append("Friday")
        if days & DaysOfWeek.Saturday:
            days_array.append("Saturday")
        return days_array

    def _category_to_schedule_str(self, category: BrewCategory):
        if category == BrewCategory.Favorite:
            return "Favorite"
        if category == BrewCategory.Recommended:
            return "Recommended"
        if category == BrewCategory.Custom:
            return "Custom"
        if category == BrewCategory.Iced:
            return "Iced"
        if category == BrewCategory.Water:
            return "Water"

    async def delete_schedule(self, schedule_id: str):
        """Delete the specified schedule"""
        await self._api._async_delete("api/usdm/v1/schedules/" + schedule_id)
        return True

    def register_callback(self, callback=lambda *args, **kwargs: None):
        """Adds a callback to be triggered when an event is received."""

        def unregister_callback() -> None:
            self._callbacks.remove(callback)

        self._callbacks.append(callback)
        return unregister_callback

    def unregister_callback(self, callback=lambda *args, **kwargs: None):
        """Removes a callback that gets triggered when an event is received."""
        self._callbacks.remove(callback)

    async def async_update(self):
        """Update the device properties"""
        await self._async_update_properties()

    async def _async_update_properties(self):
        """Asynchronously update the device properties"""
        try:
            res = await self._api._async_get(
                "api/acsm/v1/devices/" + self._id + "/properties"
            )
            json_result = res.json()

            appliance_state = next(
                (item for item in json_result if item["name"] == NODE_APPLIANCE_STATE)
            )
            brew_state = next(
                (item for item in json_result if item["name"] == NODE_BREW_STATE)
            )
            pod_state = next(
                (item for item in json_result if item["name"] == NODE_POD_STATE)
            )
            sw_info = next(
                (item for item in json_result if item["name"] == NODE_SW_INFO)
            )

            self._appliance_status = appliance_state["value"]["current"]
            self._brewer_status = brew_state["value"]["current"]
            self._sw_version = sw_info["value"]["appliance"]

            self.__populate_pod_information(pod_state["value"])
            self.__populate_brewer_errors(brew_state["value"])

            for callback in self._callbacks:
                try:
                    callback(self)
                except Exception as err:
                    _LOGGER.error("Callback error: %s", err)
            return json_result
        except UnauthorizedException:
            raise
        except Exception as err:
            _LOGGER.error(err)

    def _update_properties(self):
        """Synchronously update the device properties"""
        try:
            res = self._api._get("api/acsm/v1/devices/" + self._id + "/properties")
            json_result = res.json()

            appliance_state = next(
                (item for item in json_result if item["name"] == NODE_APPLIANCE_STATE)
            )
            brew_state = next(
                (item for item in json_result if item["name"] == NODE_BREW_STATE)
            )
            pod_state = next(
                (item for item in json_result if item["name"] == NODE_POD_STATE)
            )
            sw_info = next(
                (item for item in json_result if item["name"] == NODE_SW_INFO)
            )

            self._appliance_status = appliance_state["value"]["current"]
            self._brewer_status = brew_state["value"]["current"]
            self._pod_status = pod_state["value"]["pm_content"]
            self._sw_version = sw_info["value"]["appliance"]

            self.__populate_pod_information(pod_state["value"])
            self.__populate_brewer_errors(brew_state["value"])

            for callback in self._callbacks:
                try:
                    callback(self)
                except Exception as err:
                    _LOGGER.error("Callback error: %s", err)
            return json_result
        except UnauthorizedException:
            raise
        except Exception as err:
            _LOGGER.error(err)

    def __populate_brewer_errors(self, state: dict):
        """Parse any brewer errors"""
        brewer_error_str = None

        if state["lock_cause"] is not None:
            brewer_error_str = state["lock_cause"]
        elif state["error"] is not None:
            brewer_error_str = state["error"]
        else:
            brewer_error_str = None

        if brewer_error_str is not None:
            # If there are multiple errors they are sent as a comma
            # separated list which we parse into an array
            brewer_errors = brewer_error_str.split(",")
            brewer_errors = [item.strip() for item in brewer_errors]
            self._brewer_errors = brewer_errors
        else:
            self._brewer_errors = []

    def __populate_pod_information(self, state: dict):
        """Pull information about the loaded pod"""
        brand_key = "brand_name_" + self._api.locale
        variety_key = "variety_name_" + self._api.language + "_" + self._api.locale
        self._pod_status = state["pm_content"]
        if "pod_details" in state and state["pod_details"] is not None:
            if (
                "brand" in state["pod_details"]
                and state["pod_details"]["brand"] is not None
            ):
                self._pod_brand = state["pod_details"]["brand"][brand_key]
                self._pod_brand_id = state["pod_details"]["brand"]["brand_id"]
            else:
                self._pod_brand = None
                self._pod_brand_id = None
            if (
                "variety" in state["pod_details"]
                and state["pod_details"]["variety"] is not None
            ):
                self._pod_variety = state["pod_details"]["variety"][variety_key]
                self._pod_variety_id = state["pod_details"]["variety"]["variety_id"]
                self._pod_roast_type = state["pod_details"]["variety"]["roast"]
                self._pod_is_tea = state["pod_details"]["variety"]["is_tea"]
                self._pod_is_iced = state["pod_details"]["variety"]["is_iced"]
                self._pod_is_flavored = state["pod_details"]["variety"]["is_flavored"]
                self._pod_is_powdered = state["pod_details"]["variety"]["is_powdered"]
            else:
                self._pod_variety = None
                self._pod_variety_id = None
                self._pod_is_tea = (
                    self._pod_is_iced
                ) = self._pod_is_flavored = self._pod_is_powdered = None
        else:
            self._pod_brand = (
                self._pod_variety
            ) = self._pod_roast_type = self._pod_brand_id = self._pod_variety_id = None
            self._pod_is_tea = (
                self._pod_is_iced
            ) = self._pod_is_flavored = self._pod_is_powdered = None


class UnauthorizedException(Exception):
    """Exception thrown when the user is unauthorized to call the api."""
