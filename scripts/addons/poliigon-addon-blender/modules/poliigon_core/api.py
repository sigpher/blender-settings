# #### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

"""General purpose, pure python interface to Poliigon web APIs and services."""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum, IntEnum
from threading import Lock
from typing import (
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Union)
from urllib.request import getproxies
import errno
import json
import os
import requests
import time
import webbrowser
from xml.etree import ElementTree


from .assets import AssetData
from .env import PoliigonEnvironment
from .multilingual import _m


TIMEOUT = 20  # Request timeout in seconds.
MAX_DL_THREADS = 6
MAX_RETRIES_PER_FILE = 3
MIN_VIEW_SCREEN_INTERVAL = 2.0  # seconds min between screen report calls.

# Enum values to reference
ERR_NOT_AUTHORIZED = _m("Not authorized")
ERR_CONNECTION = _m("Connection error")
ERR_OS_NO_SPACE = _m("No space left on device")
ERR_OS_NO_PERMISSION = _m("Disk permission denied")
ERR_OS_WRITE = _m("Failed to write file")
ERR_UNZIP_ERROR = _m("Error during asset unzip")
ERR_NO_POPULATED = _m("Data not populated in response")
ERR_NO_DL_FILES = _m("No files in download response")
ERR_OTHER = _m("Unknown error occurred")
ERR_INTERNAL = _m("Internal Server Error")
ERR_NOT_ENOUGH_CREDITS = _m("User doesn't have enough credits")
ERR_USER_CANCEL_MSG = _m("User cancelled download")
ERR_OPTED_OUT = _m("Did not send event, user is opted out")
ERR_INVALID_SCREEN_NAME = _m("Invalid screen name")
ERR_INTERVAL_VIEW = _m("Last view was too recent per min interval")
ERR_TIMEOUT = _m("Connection timed out after {} seconds".format(TIMEOUT))
ERR_NO_TOKEN = _m("Failed to get token from login")
ERR_LOGIN_NOT_INITIATED = _m("Failed to initiate login via website")
ERR_WRONG_CREDS = _m("The email/password provided doesn't match our records, "
                     "please try again.")
ERR_PROXY = _m("Cannot connect due to proxy error")
ERR_MISSING_STREAM = _m("Requests response object missing from stream")
ERR_MISSING_URLS = _m("Requests response object lacking URLs")
ERR_URL_EXPIRED = _m("Download URL expired")
ERR_FILESIZE_MISMATCH = _m("Download filesize mismatch")
# ERR_SUBSCRIPTION_PAUSED _starts_ with this, it continues something like
# asset ID: ..., which I would recommend to not take into account when
# checking for this error.
ERR_SUBSCRIPTION_PAUSED = _m("You do not have an active subscription.")


MSG_ERR_RECORD_NOT_FOUND = _m("Record not found")

STR_NO_PLAN = _m("No plan active")

# Values exactly matching API responses.
API_ALREADY_OWNED = _m("User already owns the asset")
API_NO_SUBSCRIPTION = _m("Subscription not found.")

# Reusable lists of err constants for which don't warrant reporting.
SKIP_REPORT_ERRS = [
    ERR_NOT_AUTHORIZED, ERR_CONNECTION, ERR_TIMEOUT, ERR_PROXY]

DOWNLOAD_TEMP_SUFFIX = "dl"

HEADERS_LOGIN = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}

URL_BASE = "https://www.poliigon.com"
URL_PATHS = {
    "signup": "/register",
    "account": "/account",
    "subscribe": "/pricing/subscribe",
    "forgot": "/reset",
    "credits": "/account?tab=dashboard",
    "privacy": "/privacy",
    "terms": "/terms",
    # Common DCC specific additions
    # "help": "...",
    # "survey": "...",
    # "suggestion": "...",
}


MODEL_FILE_EXT = ["fbx", "blend", "max", "c4d", "skp", "ma"]

DL_RESPONSETYPE_JSON = "json"
DL_RESPONSETYPE_ZIP = "zip"


# In DCCs use SOFTWARE_NAME_xyz to pass to PoliigonAddon
SOFTWARE_NAME_BLENDER = "blender"
SOFTWARE_DOWNLOAD_NAME_BLENDER = "Blender"
PLATFORM_NAME_BLENDER = "addon-blender"

SOFTWARE_NAME_MAX = "3dsmax"
SOFTWARE_DOWNLOAD_NAME_MAX = "3dsMax"
PLATFORM_NAME_MAX = "addon-3dsmax"

SOFTWARE_NAME_MAYA = "maya"
SOFTWARE_DOWNLOAD_NAME_MAYA = "Maya"
PLATFORM_NAME_MAYA = "addon-maya"

SOFTWARE_NAME_C4D = "cinema4d"
SOFTWARE_DOWNLOAD_NAME_C4D = "Cinema4D"
PLATFORM_NAME_C4D = "addon-cinema4d"

SOFTWARE_NAME_SKETCHUP = "sketchup"
SOFTWARE_DOWNLOAD_NAME_SKETCHUP = "SketchUp"
PLATFORM_NAME_SKETCHUP = "addon-sketchup"

SOFTWARE_NAME_UNREAL = "unreal"
SOFTWARE_DOWNLOAD_NAME_UNREAL = "Generic"
PLATFORM_NAME_UNREAL = "addon-unreal"

SOFTWARE_NAME_TEST = "test_dcc"
SOFTWARE_DOWNLOAD_NAME_TEST = "Generic"
PLATFORM_NAME_TEST = "addon-blender"  # Intentionally use a live one.


def construct_error(url: str, response: str, source: dict) -> str:
    """Create a json string with details about an error.

    Args:
        url: The url of the api endpoint called.
        response: Short message describing the error.
        source: Structure of data sent with the the api request.
    """

    error = {
        "request_url": url,
        "server_response": response,
        "source_request": source
    }

    return json.dumps(error, indent=4)


@dataclass
class ApiResponse:
    """Container object for a response from the Poliigon API."""
    body: Dict  # Contents of the reply in all cases.
    ok: bool  # Did the request complete with a successful result.
    error: str  # Meant to be a short, user-friendly message.


class ApiStatus(Enum):
    """Event indicator which parent modules can subscribe to listen for."""
    CONNECTION_OK = 1  # Could connect to API, even if transaction failed.
    NO_INTERNET = 2  # Appears to be no internet.
    PROXY_ERROR = 3  # Appears to be a proxy error.


class DownloadStatus(Enum):
    INITIALIZED = 0
    WAITING = 1
    ONGOING = 2
    CANCELLED = 3
    DONE = 4  # final state
    ERROR = 5  # final state


class DQStatus(IntEnum):
    """Used in download quality tracking, enum symbols get send lower case
    as 'status'.
    """

    SUCCESS = 0
    FAILED = 1
    CANCELED = 2


@dataclass
class FileDownload:
    asset_id: int
    url: str
    filename: str
    convention: int
    size_expected: int
    size_downloaded: int = 0
    resolution_size: str = None
    status: DownloadStatus = DownloadStatus.INITIALIZED
    directory: str = ""
    fut: Optional[Future] = None
    duration: float = -1.0  # avoid div by zero, but result stays clearly wrong
    lock: Lock = Lock()
    retries: int = MAX_RETRIES_PER_FILE
    error: Optional[str] = None
    cf_ray: Optional[str] = None

    def get_path(self, temp=False) -> str:
        directory = self.directory

        # For convention 1 it should be saved in a size folder
        if self.resolution_size is not None and self.convention == 1:
            directory = os.path.join(self.directory, self.resolution_size)
            if not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, self.get_filename(temp))

    def get_filename(self, temp=False) -> str:
        if temp:
            return self.filename + DOWNLOAD_TEMP_SUFFIX
        else:
            return self.filename

    def set_status_cancelled(self) -> None:
        # do not overwrite final states
        with self.lock:
            is_done = self.status == DownloadStatus.DONE
            has_error = self.status == DownloadStatus.ERROR
            if not is_done and not has_error:
                self.status = DownloadStatus.CANCELLED

    def set_status_ongoing(self) -> bool:
        res = True
        # do not overwrite user cancellation
        with self.lock:
            if self.status != DownloadStatus.CANCELLED:
                self.status = DownloadStatus.ONGOING
            else:
                res = False
        return res

    def set_status_error(self) -> None:
        with self.lock:
            self.status = DownloadStatus.ERROR

    def set_status_done(self) -> None:
        with self.lock:
            self.status = DownloadStatus.DONE


@dataclass
class DynamicFile:
    name: Optional[str]
    contents: Optional[str]


@dataclass
class AssetDownload:
    asset_data: AssetData
    download_list: Optional[List[FileDownload]] = None
    dynamic_files_list: Optional[List[DynamicFile]] = None
    size_asset_bytes: int = 0
    uuid: str = ""


class PoliigonConnector():
    """Poliigon connector used for integrating with the web API."""

    software_source: str = ""  # e.g. blender
    software_version: str = ""  # e.g. 3.2
    software_dl_dcc: str  # e.g. Blender, to match API download software name
    version_str: str = ""  # e.g. 1.2.3, populated after calling init.
    api_url: str
    token: str = None  # Populated on login/settings read, cleared sans auth.
    login_token: str = None  # Used during login with website
    invalidated: bool = False  # Set true if outdated token detected.
    common_meta: Dict  # Fields to add to all POST requests.
    status: ApiStatus = ApiStatus.CONNECTION_OK

    _last_screen_view: int = None  # State to avoid excessive reporting.

    # Injected function to check if opted into tracking.
    # args: This function should take in no arguments
    get_optin: callable

    # Injected function for remote reporting.
    # args (message, code_msg, level)
    _report_message: callable

    # Injected, called when the overall API status is changed.
    # args (ApiEvent)
    _status_listener: callable

    _mp_relevant: bool  # mp information in message meta data

    # Injected, called when the API login token is invalidated.
    # args (ApiEvent)
    _on_invalidated: callable = None

    _platform: str = "addon"

    _url_paths: Dict[str, str]

    # For local testing only
    _assetdata_overrides: Dict = {}

    def __init__(self,
                 env: PoliigonEnvironment,
                 software: str,  # use SOFTWARE_NAME_xyz from above
                 api_url: str = "",
                 api_url_v2: str = "",
                 get_optin: Optional[callable] = None,
                 report_message: Optional[callable] = None,
                 status_listener: Optional[callable] = None,
                 mp_relevant: bool = False):
        self.software_source = software
        self.api_url = api_url if api_url else env.api_url
        self.api_url_v2 = api_url_v2 if api_url_v2 else env.api_url_v2
        self.get_optin = get_optin
        self._report_message = report_message
        self._status_listener = status_listener
        self._mp_relevant = mp_relevant

        # Update platform to be one of the hard coded API allowable types.
        if software == SOFTWARE_NAME_BLENDER:
            self._platform = PLATFORM_NAME_BLENDER
            self.software_dl_dcc = SOFTWARE_DOWNLOAD_NAME_BLENDER
        elif software == SOFTWARE_NAME_MAX:
            self._platform = PLATFORM_NAME_MAX
            self.software_dl_dcc = SOFTWARE_DOWNLOAD_NAME_MAX
        elif software == SOFTWARE_NAME_MAYA:
            self._platform = PLATFORM_NAME_MAYA
            self.software_dl_dcc = SOFTWARE_DOWNLOAD_NAME_MAYA
        elif software == SOFTWARE_NAME_C4D:
            self._platform = PLATFORM_NAME_C4D
            self.software_dl_dcc = SOFTWARE_DOWNLOAD_NAME_C4D
        elif software.lower() == SOFTWARE_NAME_SKETCHUP:
            self._platform = PLATFORM_NAME_SKETCHUP
            self.software_dl_dcc = SOFTWARE_DOWNLOAD_NAME_SKETCHUP
        elif software == SOFTWARE_NAME_UNREAL:
            self._platform = PLATFORM_NAME_UNREAL
            self.software_dl_dcc = SOFTWARE_DOWNLOAD_NAME_UNREAL
        elif software == SOFTWARE_NAME_TEST:
            # Case used for testing, may be disallowed in the future.
            self._platform = PLATFORM_NAME_TEST
            self.software_dl_dcc = SOFTWARE_DOWNLOAD_NAME_TEST
        else:
            raise ValueError(f"Invalid software selection {software}")

        self._url_paths = URL_PATHS

        # Load API overrides if a nonproduction environment and dir specified
        if env.config is not None:
            override_dir = env.config.get(
                "DEFAULT", "asset_overrides_dir", fallback=None)
            if env.env_name != "prod" and override_dir is not None:
                self._load_asset_overrides(override_dir)

    def set_on_invalidated(self, func: Callable) -> None:
        """Set the on_invalidated callback."""
        self._on_invalidated = func

    def register_update(self, addon_v: str, software_v: str) -> None:
        """Run soon after __init__ after app has readied itself.

        Args:
            addon_v: In form "1.2.3"
            software_v: In form "3.2"
        """
        self.version_str = addon_v
        self.software_version = software_v
        self.common_meta = {
            "addon_version": self.version_str,
            "software_name": self.software_source
        }

    def report_message(self,
                       message: str,
                       code_msg: str,
                       level: str,
                       max_reports: int = 10) -> None:
        """Send a report to a downstream system.

        Forwards to a system if callback configured. Any optin or eligibility
        checks are performed downstream.

        Args:
            message: The unique identifier used for issue-grouping.
            code_msg: More details about the situation.
            level: One of error, warning, info.
            max_reports: Maximum reports sent per message string, zero for all
        """
        if self._report_message is not None:
            self._report_message(message, code_msg, level, max_reports)

    def print_debug(self, dbg, *args):
        """Print out a debug statement with no separator line."""
        if dbg and dbg > 0:
            print(*args)

    def _request_url(self,
                     url: str,
                     method: str,
                     payload: Optional[Dict] = None,
                     headers: Optional[Dict] = None,
                     do_invalidate: bool = True,
                     skip_mp: bool = False,
                     ) -> ApiResponse:
        """Request a repsonse from an api.

        Args:
            url: The URL to request from.
            method: Type of http request, e.g. POST or GET.
            payload: The body of the request.
            headers: Prepopulated headers for the request including auth.
        """
        try:
            proxies = getproxies()
            if method == "POST":
                payload = self._update_meta_payload(payload, skip_mp)
                # TODO(SOFT-380): Use injected logger when available through core.
                # print(f"Request payload to {url}: {payload}")
                res = requests.post(url,
                                    data=json.dumps(payload),
                                    headers=headers,
                                    proxies=proxies,
                                    timeout=TIMEOUT)
            elif method == "GET":
                res = requests.get(url,
                                   headers=headers,
                                   proxies=proxies,
                                   timeout=TIMEOUT)
            else:
                raise ValueError("raw_request input must be GET, POST, or PUT")
        except requests.exceptions.ConnectionError as e:
            resp = {"error": str(e), "request_url": url}
            self._trigger_status_change(ApiStatus.NO_INTERNET)
            return ApiResponse(resp, False, ERR_CONNECTION)
        except requests.exceptions.Timeout as e:
            resp = {"error": str(e), "request_url": url}
            self._trigger_status_change(ApiStatus.NO_INTERNET)
            return ApiResponse(resp, False, ERR_TIMEOUT)
        except requests.exceptions.ProxyError as e:
            resp = {"error": str(e), "request_url": url, "message": ERR_PROXY}
            self.report_message("failed_proxy_error", url, level="error")
            self._trigger_status_change(ApiStatus.PROXY_ERROR)
            return ApiResponse(resp, False, ERR_PROXY)

        # Connection to site was a success, signal online.
        self._trigger_status_change(ApiStatus.CONNECTION_OK)

        http_err = f"({res.status_code}) {res.reason}" if not res.ok else None
        error = None

        invalid_auth = "unauthorized" in res.text.lower()
        invalid_auth = invalid_auth or "unauthenticated" in res.text.lower()

        if invalid_auth or res.status_code == 401:
            resp = {}
            ok = False
            error = ERR_NOT_AUTHORIZED
            self.token = None
            if do_invalidate:
                self.invalidated = True
                if self._on_invalidated is not None:
                    self._on_invalidated()
        elif res.status_code == 403:
            resp = {}
            ok = False
            error = ERR_URL_EXPIRED
        elif res.text:
            try:
                resp = json.loads(res.text)
                ok = res.ok

                # If server error, pass forward any message from api, but
                # fallback to generic http status number/name.
                # Requests that fail will not return 200 status, and should
                # include a specific message on what was wrong.
                # There is also typically an "errors" field in the body,
                # but that is a more complex structure (not just a string).
                if not ok:
                    error = resp.get("message", http_err)
                    if error == "":
                        error = f"{http_err} - message present but empty"

            except json.decoder.JSONDecodeError:
                resp = {}
                ok = False
                error = f"Failed to parse response as json - {http_err}"
        else:
            resp = {}
            ok = False
            error = f"No contents in response - {http_err}"

        resp["request_url"] = url

        return ApiResponse(resp, ok, error)

    def _request(self,
                 path: str,
                 method: str,
                 payload: Optional[Dict] = None,
                 headers: Optional[Dict] = None,
                 do_invalidate: bool = True,
                 api_v2: bool = False,
                 skip_mp: bool = False
                 ) -> ApiResponse:
        """Request a repsonse from an api.

        Args:
            path: The api endpoint path without the url domain.
            method: Type of http request, e.g. POST or GET.
            payload: The body of the request.
            headers: Prepopulated headers for the request including auth.
        """
        if api_v2:
            url = self.api_url_v2 + path
        else:
            url = self.api_url + path
        return self._request_url(url,
                                 method=method,
                                 payload=payload,
                                 headers=headers,
                                 do_invalidate=do_invalidate,
                                 skip_mp=skip_mp)

    def _request_stream(self,
                        url: str,
                        headers: Optional[Dict] = None
                        ) -> ApiResponse:
        """Stream a request from an explicit fully defined url.

        Args:
            path: The api endpoint path without the url domain.
            headers: Prepopulated headers for the request including auth.

        Response: ApiResponse where the body is a dict including the key:
            "stream": requests get response object (the streamed connection).
            "session": Session needs to be closed, when done.
        """
        try:
            proxies = getproxies()
            session = requests.Session()
            res = session.get(url,
                              headers=headers,
                              proxies=proxies,
                              timeout=TIMEOUT,
                              stream=True)
        except requests.exceptions.ConnectionError as e:
            session.close()
            resp = {"error": str(e), "request_url": url}
            self._trigger_status_change(ApiStatus.NO_INTERNET)
            return ApiResponse(resp, False, ERR_CONNECTION)
        except requests.exceptions.Timeout as e:
            session.close()
            resp = {"error": str(e), "request_url": url}
            self._trigger_status_change(ApiStatus.NO_INTERNET)
            return ApiResponse(resp, False, ERR_TIMEOUT)
        except requests.exceptions.ProxyError as e:
            session.close()
            resp = {"error": str(e), "request_url": url, "message": ERR_PROXY}
            self.report_message("failed_proxy_error", url, level="error")
            self._trigger_status_change(ApiStatus.PROXY_ERROR)
            return ApiResponse(resp, False, ERR_PROXY)

        cf_ray = res.headers.get("CF-RAY", "")

        # Connection to site was a success, signal online.
        self._trigger_status_change(ApiStatus.CONNECTION_OK)

        error = f"({res.status_code}) {res.reason}" if not res.ok else None

        invalid_auth = res.status_code == 401
        url_expired = res.status_code == 403

        if invalid_auth:
            session.close()
            resp = {"response": None, "CF-RAY": cf_ray}
            ok = False
            error = ERR_NOT_AUTHORIZED
            self.token = None
            self.invalidated = True
        elif url_expired:
            session.close()
            resp = {"response": None, "CF-RAY": cf_ray}
            ok = False
            error = ERR_URL_EXPIRED
        else:
            resp = {"stream": res, "session": session, "CF-RAY": cf_ray}
            ok = res.ok

        return ApiResponse(resp, ok, error)

    def _request_authenticated(self,
                               path: str,
                               payload: Optional[Dict] = None,
                               skip_mp: bool = False,
                               api_v2: bool = False
                               ) -> ApiResponse:
        """Make an authenticated request to the API using the user token."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            return ApiResponse({}, False, ERR_NOT_AUTHORIZED)
        method = "POST" if payload is not None else "GET"
        res = self._request(
            path, method, payload=payload, headers=headers, skip_mp=skip_mp, api_v2=api_v2)
        if res.error and "server error" in res.error.lower():
            res.ok = False
            res.error = ERR_INTERNAL

        return res

    def add_utm_suffix(self, url: str, content: Optional[str] = None) -> str:
        """Return the UTM tag to append to any url requests for tracking."""

        # Detect if the url already has a param.
        initial_char = ""
        if url[-1] == "/":
            initial_char = "?"
        elif url[-1] == "?":
            initial_char = ""
        elif url[-1] == "&":
            initial_char = ""
        elif "?" in url:
            initial_char = "&"
        else:
            initial_char = "?"

        # Ensure the version str starts with a leading v.
        if not self.version_str or self.version_str[0] != "v":
            addon_v = "v" + self.version_str
        else:
            addon_v = self.version_str
        campaign = f"addon-{self.software_source}-{addon_v}"

        outstr = "{url}{init}utm_campaign={cmpg}&utm_source={src}&utm_medium={med}{cnt}".format(
            url=url,
            init=initial_char,
            cmpg=campaign,  # Granular addon+software+version
            src=self.software_source,  # such as "blender"
            med="addon",
            cnt="" if not content else "&{content}"
        )
        return outstr

    def log_in(self,
               email: str,
               password: str,
               time_since_enable: Optional[int] = None,
               api_v2: bool = False) -> ApiResponse:
        """Log the user in with a password/email combo.

        time_since_enable is the number of seconds since the addon was first
        enabled, only populated if this was the first login event for this
        install, used to identify an install event.
        """
        data = {
            "email": email,
            "password": password,
        }
        if time_since_enable is not None:
            data["time_since_enable"] = time_since_enable

        res = self._request(
            "/login", "POST", data, HEADERS_LOGIN, api_v2=api_v2)

        token = None
        if api_v2 is False:
            token = res.body.get("access_token")
        elif "results" in res.body:
            token = res.body["results"].get("access_token")

        if not res.ok:
            msg = res.body.get("message", "")
            err = res.body.get("errors", "")
            if "do not match" in msg:
                res.error = ERR_WRONG_CREDS
            elif msg:
                self.report_message(
                    "login_error_other", f"{res.error}: {msg}", "error")
                res.error = msg
            elif res.error in SKIP_REPORT_ERRS:
                pass
            elif err:
                self.report_message(
                    "login_error_err_no_msg", f"{res.error} - {err}", "error")
                # err can be a struc, not great for ui, prefer res.error
                res.error = res.error or ERR_OTHER
            else:
                self.report_message(
                    "login_error_no_message", str(res.error), "error")
                # Don't override res.error, which can include status code.
                # Pass forward to front end, likely connection or proxy.
        elif token is None:
            self.report_message("login_error_no_token", str(res.body), "error")
            res.ok = False
            res.error = ERR_NO_TOKEN
        else:
            # Request was success.
            self.token = token
            self.invalidated = False
        return res

    def log_in_with_website(self,
                            time_since_enable: Optional[int] = None,  # Deprecated.
                            open_browser: bool = True
                            ) -> ApiResponse:
        """Log the user via website login.

        time_since_enable is deprecated and not used in this request anymore,
        instead it is shifted to the validate login event, to better match the
        end-perceived completion of the login back in the addon.
        """
        data_req = {
            "platform": self._platform,
            "meta": {
                "addon_version": self.version_str,
                "software_version": self.software_version,
                "software_name": self.software_source
            }
        }

        self.login_token = None
        url_login = ""

        res = self._request("/initiate/login",
                            "POST",
                            data_req,
                            HEADERS_LOGIN,
                            api_v2=True)

        # TODO(POL-2961): Move to only the proposed way once POL-2961 is done.
        api_current_ok = res.body.get("message", "").lower() == "success"
        api_proposed_ok = res.body.get("status", "").lower() == "success"

        if not res.ok:
            msg = res.body.get("message", "")
            err = res.body.get("errors", "")
            # TODO(SOFT-603): What errors might occur?
            #                 There're sibling TODOs in
            #                 p4b/toolbox.py:f_login_with_website_handler()
            #                 and below check_login_with_website_success()
            if msg:
                self.report_message(
                    "login_error_other", f"{res.error}: {msg}", "error")
                res.error = msg
            elif err:
                self.report_message(
                    "login_error_err_no_msg", f"{res.error} - {err}", "error")
                # err can be a struct, not great for ui, prefer res.error
                res.error = res.error or ERR_OTHER
            else:
                self.report_message(
                    "login_error_no_message", str(res.error), "error")
                # Don't override res.error, which can include status code.
                # Pass forward to front end, likely connection or proxy.

        elif not api_proposed_ok and not api_current_ok:
            self.report_message("login_error_not_initiated",
                                str(res.body),
                                "error")
            res.ok = False
            res.error = ERR_LOGIN_NOT_INITIATED
        else:
            # Request was success.
            results = res.body.get("results", {})
            self.login_token = results.get("login_token", None)
            url_login = results.get("login_url", "")

        if url_login == "" or self.login_token is None:
            self.login_token = None
            res.ok = False
            res.error = ERR_LOGIN_NOT_INITIATED
            return res

        if open_browser:
            webbrowser.open(url_login, new=0, autoraise=True)
        return res

    def check_login_with_website_success(
            self, time_since_enable: Optional[int] = None) -> ApiResponse:
        """Checks if a login with website was successful."""

        data_validate = {
            "platform": self._platform,
            "login_token": self.login_token,
            "meta": {
                "optin": self._is_opted_in(),
                "addon_version": self.version_str,
                "software_version": self.software_version,
                "software_name": self.software_source
            }
        }

        if time_since_enable is not None:
            data_validate["time_since_enable"] = time_since_enable

        res = self._request("/validate/login",
                            "POST",
                            data_validate,
                            HEADERS_LOGIN,
                            do_invalidate=False,
                            api_v2=True)

        # TODO(POL-2961): Move to only the proposed way once POL-2961 is done.
        api_current_ok = res.body.get("message", "").lower() == "successful login"
        api_proposed_ok = res.body.get("status", "").lower() == "success"

        if not res.ok:
            if res.error != ERR_NOT_AUTHORIZED:
                # TODO(SOFT-603): Error handling, other errors than ERR_NOT_AUTHORIZED
                #                 There're sibling TODOs in
                #                 p4b/toolbox.py:f_login_with_website_handler()
                #                 and above log_in_with_website()

                print(f"Validation error {res.error}")
        elif not api_proposed_ok and not api_current_ok:
            # TODO(SOFT-603): Error handling
            pass
        else:
            results = res.body.get("results", {})
            self.token = results.get("access_token", "")
            user = results.get("user", {})
            # user.get("id", -1)
            # user.get("name", "John Doe")
            # user.get("email", "Unknown email")
            # P4B currently expects user info directly in body
            res.body["user"] = user
            self.invalidated = False

        return res

    def poll_login_with_website_success(self,
                                        timeout: int = 300,
                                        cancel_callback: Callable = lambda: False,
                                        time_since_enable: Optional[int] = None
                                        ) -> ApiResponse:
        """Waits for a login with website to finish.

        Args:
        timeout: Number of seconds to wait for a successful login
        cancel_callback: Callable returning True, if the wait is to be aborted
        """

        # Poll for finished login
        while timeout > 0:
            timeout -= 1
            time.sleep(1)

            res = self.check_login_with_website_success(time_since_enable)
            if res.ok:
                break
            else:
                if cancel_callback():
                    res = ApiResponse(body={},
                                      ok=False,
                                      error="Login cancelled")
                    break
                if res.error == ERR_NOT_AUTHORIZED:
                    continue
                # TODO(SOFT-603): Error handling
                break
        return res

    def log_out(self) -> ApiResponse:
        """Logs the user out."""
        path = "/logout"
        payload = {}
        res = self._request_authenticated(path, payload)
        return res

    def categories(self) -> ApiResponse:
        """Get the list of website requests."""
        # TODO(SOFT-762): Have a cached mechanism.
        res = self._request_authenticated("/categories")
        if res.ok:
            if "payload" in res.body:
                res.body = res.body.get("payload")
            else:
                res.ok = False
                res.error = "Categories not populated"
        return res

    def get_user_balance(self) -> ApiResponse:
        """Get the balance for the given user."""
        path = "/available/user/balance"
        res = self._request_authenticated(path)
        error_in_body = res.body.get("error", "")
        if not res.ok and error_in_body == MSG_ERR_RECORD_NOT_FOUND:
            # This happens for free users without any transactions
            res = ApiResponse(body={"subscription_balance": 0,
                                    "ondemand_balance": 0,
                                    "available_balance": 0,
                                    "error": error_in_body,
                                    "request_url": res.body.get("request_url",
                                                                "")
                                    },
                              ok=True,
                              error=None
                              )
        return res

    def get_user_info(self) -> ApiResponse:
        """Get information for the given user."""
        path = "/me"
        res = self._request_authenticated(path)
        return res

    def get_subscription_details(self) -> ApiResponse:
        """Get the subscription details for the given user."""
        path = "/subscription/details"
        res = self._request_authenticated(path, {})
        if "plan_name" in res.body:
            return res
        elif not res.ok and res.body.get("error") == API_NO_SUBSCRIPTION:
            # Api returns error if no plan is active, but we want to draw the
            # plan as just inactive in the UI and not treat as an error.
            return ApiResponse(
                {"plan_name": STR_NO_PLAN},
                True,
                None)
        return res

    def get_download_url(self,
                         download_data: dict,
                         is_retry: bool = False
                         ) -> ApiResponse:
        """Request the download URL(s) for a purchased asset.

        Args:
            download_data: Structure of data defining the download.

        Response: ApiResponse where body is a dict including server-given keys:
            uuid: str, files: List[Dict]
        """
        res = self._request_authenticated(
            "/assets/download", payload=download_data, skip_mp=is_retry)
        request_url = res.body.get("request_url", "No request_url in body.")
        if res.ok:
            if res.body.get("message") and "expired" in res.body["message"]:
                res = ApiResponse(
                    {"message": res.body["message"]},
                    False,
                    construct_error(
                        request_url,
                        "Download link expired",
                        download_data
                    )
                )
            elif "files" not in res.body:
                res = ApiResponse(
                    {"message": "Failed to fetch download url"},
                    False,
                    construct_error(
                        request_url,
                        "Download failed",
                        download_data
                    )
                )
        elif res.error in SKIP_REPORT_ERRS:
            return res
        else:
            res.error = construct_error(request_url, res.error, download_data)
        return res

    def download_asset_get_urls(self,
                                asset_id: int,
                                download_data: dict,
                                convention: int = 0,
                                is_retry: bool = False
                                ) -> ApiResponse:
        """Request a set of download URLs for a purchased asset.

        Args:
            asset_id: The integer asset id.
            download_data: Structure of data defining the download.
            convention: The convention used to build the asset.
            is_retry: Define if process is a retrial.

        Response: ApiResponse where the body is a dict including the key:
            "downloads": A list of FileDownload objects.
            "size_asset": Accumulated size of all individual files.
        """
        dbg = 0

        # Insert the field which ensures multiple downloads are present.
        # Copy DL data in case download data re-referenced (speed tests)
        dl_data = download_data.copy()

        dl_data["response_type"] = DL_RESPONSETYPE_JSON

        # Fetch the download URLs (or legacy: downloader request).
        res = self.get_download_url(dl_data, is_retry)
        if not res.ok:
            # No UUID in this case, download wasn't even able to start,
            # so no download quality tracking here
            return res

        # Check which version of the API is live. Older versions required a
        # second URL request to get the list of URLs.

        uuid = res.body.get("uuid", "")
        files_list = res.body.get("files", None)
        if files_list is None:
            return ApiResponse(ERR_NO_DL_FILES, False, res.body)

        dl_list = []
        size_asset = 0
        model_exists = False
        filename_model_fbx_source = None
        url_model_fbx_source = None
        size_expected_model_fbx_source = 0
        for url_dict in files_list:
            url = url_dict.get("url")
            filename = url_dict.get("name")
            size_expected = url_dict.get("bytes", 0)
            resolution_size = url_dict.get("resolution", None)

            if not url or not filename:
                self.print_debug(dbg, f"Missing url or filename {url}")
                raise RuntimeError(f"Missing url or filename {url}")
            elif "_SOURCE" in filename:
                if filename.lower().endswith(".fbx"):
                    filename_model_fbx_source = filename
                    url_model_fbx_source = url
                    size_expected_model_fbx_source = size_expected
                continue

            if size_expected == 0:
                self.print_debug(dbg, f"Zero size reported for {url}")

            size_asset += size_expected

            # Ignore duplicates
            found = False
            for check in dl_list:
                same_filename = check.filename.lower() == filename.lower()
                same_size = check.size_expected == size_expected
                if same_filename and same_size:
                    found = True
                    break
                elif same_filename and not same_size:
                    msg = (f"{asset_id}\n"
                           f"{filename}, {size_expected}, {url}\n"
                           f"{check.filename}, {check.size_expected}, {check.url}")
                    self.report_message(
                        "asset_duplicate_file_url", msg, level="info")
                    found = True
                    break

            if found:
                continue

            filename_ext = os.path.splitext(filename)[1].lower()
            filename_ext = filename_ext[1:]  # get rid of dot
            if filename_ext.lower() in MODEL_FILE_EXT:
                model_exists = True

            dl = FileDownload(
                asset_id=asset_id,
                url=url,
                filename=filename,
                convention=convention,
                size_expected=size_expected,
                resolution_size=resolution_size)
            dl_list.append(dl)

        # Fallback if "xyz_SOURCE.fbx" is the only model file
        if filename_model_fbx_source is not None and not model_exists:
            dl = FileDownload(asset_id=asset_id,
                              url=url_model_fbx_source,
                              filename=filename_model_fbx_source,
                              convention=convention,
                              size_expected=size_expected_model_fbx_source)
            dl_list.append(dl)
            msg = f"Model asset with just SOURCE LOD: {asset_id}"
            self.report_message(
                "model_with_only_source_lod", msg, level="info")

        dynamic_files_list = res.body.get("dynamic_files", None)

        return ApiResponse({"downloads": dl_list,
                            "size_asset": size_asset,
                            "uuid": uuid,
                            "dynamic_files": dynamic_files_list},
                           True,
                           None)

    def check_exist_and_finished(
            self, path: str, download: FileDownload) -> bool:
        if not os.path.exists(path):
            return False
        return os.path.getsize(path) == download.size_expected

    def delete_file(self, path: str) -> None:
        dbg = 0
        if not os.path.exists(path):
            return
        try:
            os.remove(path)
        except BaseException as e:
            # Deliberately silencing all exceptions, here!
            # Most of the time, we are here in reaction to an exception
            # during download. Neither can we do anything here, if remove
            # fails, nor would we gain much information from reporting
            # this exception.
            self.print_debug(dbg, f"Failed to delete partial download:\n{e}")
            pass

    def download_asset_file(self, download: FileDownload) -> ApiResponse:
        """Stream download a single file of a purchased asset.

        Args:
            download: Structure of data defining the download.

        Response: ApiResponse where the body is a dict including the key:
            "download": FileDownload object
                        (for convenience it's identical to the one passed in)
        """

        dbg = 0
        t_start = time.monotonic()

        path_temp = download.get_path(temp=True)
        file_exists_temp = self.check_exist_and_finished(path_temp, download)
        path_final = download.get_path(temp=False)
        file_exists_final = self.check_exist_and_finished(path_final, download)

        if file_exists_temp or file_exists_final:
            self.print_debug(
                dbg, "download_asset_file ALREADY EXISTS", download.filename)
            download.set_status_done()
            download.size_downloaded = download.size_expected
            return ApiResponse({"download": download}, True, None)

        res = self._request_stream(download.url)
        # Grab header so we can uniquely pair a download error to server-side.
        cf_ray = res.body.get("CF-RAY", "")
        download.cf_ray = cf_ray
        if not res.ok:
            if res.error == ERR_URL_EXPIRED:
                self.print_debug(
                    dbg, "download_asset_file URL timeout", download.filename)
            else:
                self.print_debug(
                    dbg, "download_asset_file ERR request stream", download.filename)
                err = construct_error(
                    download.url,
                    f"Received {res.error} error during download",
                    download.filename)
                res.error = err
            download.set_status_error()
            return res
        elif "stream" not in res.body:
            self.print_debug(
                dbg, "download_asset_file ERR stream MISSING", download.filename)
            err = construct_error(
                download.url,
                ERR_MISSING_STREAM,
                download.filename)
            msg = {"message": "Requests response missing from stream",
                   "CF-RAY": cf_ray}
            download.set_status_error()
            return ApiResponse(msg, False, err)

        stream = res.body["stream"]
        stream_size = int(stream.headers["Content-Length"])
        session = res.body["session"]

        if download.status != DownloadStatus.WAITING:
            self.print_debug(dbg, "download_asset_file DOWNLOAD STATUS NOT WAITING", download.filename, download.status)

        if not download.set_status_ongoing():
            self.print_debug(dbg, "download_asset_file CANCELLED BEFORE START")
            session.close()
            msg = ERR_USER_CANCEL_MSG
            return ApiResponse({"error": msg, "CF-RAY": cf_ray}, False, msg)

        asset_id = download.asset_id
        download.size_downloaded = 0

        try:
            with open(path_temp, "wb") as write_file:
                for chunk in stream.iter_content(chunk_size=1024):
                    if chunk is None:
                        continue
                    write_file.write(chunk)
                    download.size_downloaded += len(chunk)
                    if download.status == DownloadStatus.CANCELLED:
                        break
        except requests.exceptions.ConnectionError as e:
            download.set_status_error()
            self.delete_file(path_temp)
            return ApiResponse(
                {"error": e, "CF-RAY": cf_ray}, False, ERR_CONNECTION)
        except requests.exceptions.Timeout as e:
            download.set_status_error()
            self.delete_file(path_temp)
            self._trigger_status_change(ApiStatus.NO_INTERNET)
            return ApiResponse(
                {"error": str(e), "CF-RAY": cf_ray}, False, ERR_TIMEOUT)
        except OSError as e:
            download.set_status_error()
            self.delete_file(path_temp)
            if e.errno == errno.ENOSPC:
                return ApiResponse(
                    {"error": e, "CF-RAY": cf_ray}, False, ERR_OS_NO_SPACE)
            elif e.errno == errno.EACCES:
                return ApiResponse(
                    {"error": e, "CF-RAY": cf_ray}, False, ERR_OS_NO_PERMISSION)
            else:
                return ApiResponse(
                    {"error": e, "CF-RAY": cf_ray},
                    False,
                    f"Download error for {asset_id} - {ERR_OS_WRITE}\n{e}")
        except Exception as e:
            download.set_status_error()
            self.delete_file(path_temp)
            err = construct_error(
                download.url,
                f"Streaming error during download of {asset_id} ({e})",
                {"filename": download.filename})
            return ApiResponse({"error": e, "CF-RAY": cf_ray}, False, err)
        finally:
            session.close()

        if download.size_expected == download.size_downloaded == stream_size:
            # Download success
            download.set_status_done()
            t_end = time.monotonic()
            download.duration = t_end - t_start
        else:
            # Download cancelled or error
            if download.status == DownloadStatus.ONGOING:
                self.print_debug(dbg, "download_asset_file size difference despite no error!!!", download.filename)
                # TODO(Andreas): We shouldn't be here
            if download.status == DownloadStatus.CANCELLED:
                msg = ERR_USER_CANCEL_MSG
            else:
                msg = ERR_FILESIZE_MISMATCH
            download.set_status_error()
            # Delete incomplete file
            self.delete_file(path_temp)
            return ApiResponse({"error": msg, "CF-RAY": cf_ray}, False, msg)

        # Downloaded file does not get its "dl" suffix removed, yet.
        # Needs to be done, when entire asset (all files) is complete.

        return ApiResponse(
            {"download": download, "CF-RAY": cf_ray}, True, None)

    def track_download_quality(self,
                               uuid: str,
                               status: DQStatus,
                               error: str = ""
                               ) -> bool:
        """Send download quality tracking information, if user has opten in to
        sharing information.
        """

        if not self._is_opted_in():
            return False

        payload = {
            "uuid": uuid,
            "status": status.name.lower(),
            "error": error
        }
        res = self._request_authenticated(
            "/t/download_quality", payload=payload, skip_mp=False, api_v2=True)
        if not res.ok:
            self.report_message(
                "download_quality_error",
                f"{res.error}",
                "error")
        return res.ok

    def cdn_resize_img_url(self,
                           url: str,
                           size: int = 300,
                           sharpen: int = 1,
                           quality: int = 75,
                           ext: str = "auto") -> str:
        """Wraps url with the CDN url to change thumb resolution for download.

        Args:
            url: the original full url for the image.
            size: The resized with and height of the image.
            sharpen: How much image sharpening to apply.
            quality: How much compression to apply, 100 is higher / larger file.
            ext: Extension format to use, such as png or jpg.
        """

        return (
            "https://poliigon.net/cdn-cgi/image/width="
            f"{size},sharpen={sharpen},q={quality},f={ext}/{url}"
        )

    def download_preview(self,
                         url: str,
                         dst_file: str,
                         asset_name: str = ""
                         ) -> ApiResponse:
        """Stream download a preview to a file from a custom domain url."""

        # TODO: Add an optional chunk size callback for UI updates mid stream.
        # print(f"download_asset: Downloading {url} to {dst_file}")
        session = None
        try:
            resp = self._request_stream(url)
            if not resp.ok and resp.error in SKIP_REPORT_ERRS:
                return resp
            elif not resp.ok:
                self.report_message(
                    "download_preview_not_ok",
                    f"{asset_name}: {resp.error}",
                    "error")
                return ApiResponse(
                    {"error": resp.error},
                    False,
                    resp.error)

            stream = resp.body.get("stream")
            session = resp.body.get("session")
            if not stream:
                self.report_message(
                    "download_preview_resp_missing",
                    f"{asset_name}: {ERR_MISSING_STREAM} - {resp.body}",
                    "error")
                return ApiResponse(
                    {"error": ERR_MISSING_STREAM, "body": resp.body},
                    False,
                    ERR_MISSING_STREAM)
            elif resp.ok:
                directory = os.path.dirname(dst_file)
                try:
                    os.makedirs(directory, exist_ok=True)
                except OSError as e:
                    self.report_message(
                        "download_preview_no_directory",
                        ("Failed to create non-existing thumb directory: "
                         f"{directory}\n{e}"),
                        "error")

                if os.path.exists(directory):
                    with open(dst_file, "wb") as fwriter:
                        fwriter.write(stream.content)
            else:
                msg = f"{asset_name}: {resp.error}"
                self.report_message(
                    "download_preview_error", msg, "error")
                return resp
        except requests.exceptions.ConnectionError as e:
            return ApiResponse({"error": e}, False, ERR_CONNECTION)
        except OSError as e:
            self.report_message(
                "download_preview_error_write", str(e), "error")
            return ApiResponse({"error": e}, False, ERR_OS_WRITE)
        except Exception as e:
            self.report_message(
                "download_preview_error_other", str(e), "error")
            return ApiResponse({"error": e}, False, ERR_OTHER)
        finally:
            if session is not None:
                session.close()

        return ApiResponse({"file": dst_file}, True, None)

    def purchase_asset(
            self, asset_id: int, search: str, category: str) -> ApiResponse:
        """Purchase a given asset for the logged in user.

        Args:
            asset_id: The unique poliigon asset id.
            search: Current active search query.
            category: Current category in slug form: "/brushes/free.
        """
        path = f"/assets/{asset_id}/purchase"
        payload = {}  # Force to be POST request.

        if self._is_opted_in():
            # Only send if user is opted in.
            payload["last_search_term"] = search
            payload["last_category"] = category

        res = self._request_authenticated(path, payload)
        if not res.ok:
            if "message" in res.body:
                msg = res.body["message"]
                err = str(res.body.get("errors", ""))  # Detailed server error.
                if API_ALREADY_OWNED.lower() in msg.lower():
                    self.report_message("purchased_existing", msg, "info")
                    res.ok = True  # Override so that download initiates.
                    res.error = None
                elif "enough credits" in msg:
                    self.report_message("not_enough_credits", asset_id, "info")
                    res.error = ERR_NOT_ENOUGH_CREDITS
                else:
                    res.error = f"{msg} - asset_id: {asset_id}"
                    self.report_message(
                        "purchase_failed", f"{res.error} {err}", "error")
            else:
                # To API, pass original message before updating to generic one.
                self.report_message("purchase_failed",
                                    f"{res.error}  - asset_id: {asset_id}",
                                    "error")
                res.error = f"{ERR_OTHER} - asset_id: {asset_id}"
        return res

    def get_assets(self, query_data: Dict) -> ApiResponse:
        """Get the assets with an optional query parameter."""
        res = self._request_authenticated("/assets", payload=query_data)
        if not res.ok:
            if res.error not in SKIP_REPORT_ERRS:
                self.report_message("online_assets_error",
                                    f"{query_data} - {res.error}", "error")
            return res
        elif "payload" in res.body:
            res.body = res.body.get("payload")
            self._override_assets_payload(res.body)
        else:
            self.report_message(
                "online_assets_no_payload", str(res.body), "error")
            res.ok = False
            res.error = ERR_NO_POPULATED
        return res

    def get_user_assets(self, query_data: Dict) -> ApiResponse:
        """Get assets the user has already purchased."""
        res = self._request_authenticated("/my-assets", payload=query_data)
        if not res.ok:
            if res.error not in SKIP_REPORT_ERRS:
                self.report_message("get_user_assets_error",
                                    f"{query_data} - {res.error}", "error")
            return res
        elif "payload" in res.body:
            res.body = res.body.get("payload")
            # Normally API returns a dict with page infos and a list with asset
            # data inside under the "data" key.
            # But in case of "my_assets" with no purchased assets in account,
            # strangely we receive an empty list.
            if type(res.body) is list:
                res.body = {"data": {},
                            "current_page": query_data["page"],
                            "from": 0,
                            "last_page": 0,
                            "per_page": query_data["perPage"],
                            "to": 0,
                            "total": 0
                            }
            self._override_assets_payload(res.body)
        else:
            self.report_message(
                "get_user_assets_no_payload", str(res.body), "error")
            res.ok = False
            res.error = ERR_NO_POPULATED
        return res

    def pooled_preview_download(
            self, urls: Sequence, files: str) -> ApiResponse:
        """Threadpool executor for downloading assets or previews.

        Arguments:
            urls: A list of full urls to each download file, not just api stub.
            files: The parallel output list of files to create.
        """
        if len(urls) != len(files):
            raise RuntimeError("List of urls and files are not equal")
        futures = []
        with ThreadPoolExecutor(
                max_workers=MAX_DL_THREADS) as executor:
            for i in range(len(urls)):
                future = executor.submit(
                    self.download_preview,
                    urls[i],
                    files[i]
                )
                futures.append(future)

        any_failures = []
        for ftr in futures:
            res = ftr.result()
            if not res or not res.ok:
                any_failures.append(res)

        if any_failures:
            return ApiResponse(
                any_failures, False, "Error during pooled preview download")
        else:
            return ApiResponse("", True, None)

    def _signal_event(self, event_name: str, payload: Dict) -> ApiResponse:
        """Reusable entry to send an event, only if opted in."""
        if not self._is_opted_in():
            return ApiResponse(None, False, ERR_OPTED_OUT)
        return self._request_authenticated(f"/t/{event_name}",
                                           payload=payload)

    def signal_preview_asset(self, asset_id: int) -> ApiResponse:
        """Sends quick asset preview event if opted in."""
        payload = {"asset_id": asset_id}
        return self._signal_event("preview_asset", payload=payload)

    def signal_import_asset(self, asset_id: int = 0):
        """Sends import asset event if opted in."""
        payload = {"asset_id": asset_id}
        return self._signal_event("import_asset", payload=payload)

    def signal_view_screen(self, screen_name: str) -> ApiResponse:
        """Sends view screen event if opted in.

        Limits one signal event per session per notification type.

        Args:
            screen_name: Explicit agreed upon view names within addon.
        """
        now = time.time()
        if self._last_screen_view:
            if now - self._last_screen_view < MIN_VIEW_SCREEN_INTERVAL:
                return ApiResponse({}, False, ERR_INTERVAL_VIEW)
            else:
                self._last_screen_view = now
        else:
            self._last_screen_view = now

        # Any name changes here require server-side coordination.
        valid_screens = [
            "home",
            "my_assets",
            "imported",
            "my_account",
            "settings",
            "help",
            "onboarding",
            "large_preview",
            "blend_node_add",
            "blend_browser_lib",  # If the Poliigon lib is visible
            "blend_browser_import"  # If the right side bar is visible
        ]

        if screen_name not in valid_screens:
            print("Screen name is not valid:", screen_name)
            return ApiResponse(
                {"invalid_screen": screen_name},
                False,
                ERR_INVALID_SCREEN_NAME)
        payload = {"screen_name": screen_name}
        return self._signal_event("view_screen", payload=payload)

    def signal_view_notification(self, notification_id: str) -> ApiResponse:
        """Sends view notification event if opted in."""
        payload = {"notification_id": notification_id}
        return self._signal_event("view_notification", payload=payload)

    def signal_click_notification(
            self, notification_id: str, action: str) -> ApiResponse:
        """Sends click notification event if opted in."""
        payload = {"notification_id": notification_id, "action": action}
        return self._signal_event("click_notification", payload=payload)

    def signal_dismiss_notification(self, notification_id: str) -> ApiResponse:
        """Sends dismissed notification event if opted in."""
        payload = {"notification_id": notification_id}
        return self._signal_event("dismiss_notification", payload=payload)

    def _is_opted_in(self) -> bool:
        return self.get_optin and self.get_optin()

    def _update_meta_payload(self, payload: Dict, skip_mp: bool) -> Dict:
        """Take the given payload and add or update its meta fields."""
        if "meta" not in payload:
            payload["meta"] = {}

        # mp flag is independent of opted_in state,
        # overwritten if opted out.
        payload["meta"]["mp"] = self._mp_relevant

        if self._is_opted_in():
            payload["meta"]["optin"] = True
            payload["meta"]["software_version"] = self.software_version
        else:
            payload["meta"] = {}  # Clear out any existing tracking.
            payload["meta"]["mp"] = False
            payload["meta"]["optin"] = False

        # Very last, override to always assign skip, if skip is defined.
        # But if not specifically skipping, defer to self._mp_relevant
        if skip_mp is True:
            payload["meta"]["mp"] = False

        # Always populate addon version and platform.
        payload["meta"].update(self.common_meta)
        payload["platform"] = self._platform

        return payload

    def _trigger_status_change(self, status_name: ApiStatus) -> None:
        """Trigger callbacks to other modules on API status change.

        Typically used to update the UI in a central location, instead of
        needing to wrap each and every call with the same handler.
        """
        self.status = status_name
        if self._status_listener is None:
            return
        self._status_listener(status_name)

    def get_base_url(self, env_name: str = "prod") -> str:
        if env_name == "prod":
            return URL_BASE

        pos = self.api_url.rfind("/api")
        url_base = self.api_url[:pos]
        api_url_str = "apiv1" if "apiv1" in url_base else "api"
        url_base = url_base.replace(api_url_str, "dev")
        return url_base

    def add_poliigon_urls(self, urls: Dict[str, str]) -> None:
        """Adds or changes Poliigon URLs.

        Arguments:
        urls: Dictionary {key: url}
              url may either be a complete URL or
              a URL path being appended to the base URL (needs to start with /)
        """

        for key, _url in urls.items():
            self._url_paths[key] = _url

    def open_poliigon_link(self,
                           link_type: str,
                           add_utm_suffix: bool = True,
                           env_name: str = "prod"
                           ) -> None:
        """Opens a Poliigon URL"""

        if link_type not in self._url_paths:
            raise KeyError(f"Unknown link_type: {link_type}")

        url_path = self._url_paths[link_type]
        if url_path.startswith("/"):
            url_base = self.get_base_url(env_name)
            url = f"{url_base}{url_path}"
        else:
            url = url_path

        if add_utm_suffix:
            url = self.add_utm_suffix(url)

        webbrowser.open(url)

    def patch_download_url_increment_version(self, url: str) -> str:
        """Deprecated due to change in preview hosting solution."""
        return url

        # Prior implementation

        if "?" not in url:
            return url
        url_new_base, url_new_all_params = url.split("?", 1)
        url_new_params = url_new_all_params.split("&")
        for idx_param, param in enumerate(url_new_params):
            if not param.startswith("v="):
                continue
            _, version = param.split("=")
            try:
                version = int(version) + 1
                url_new_params[idx_param] = f"v={int(version)}"
            except ValueError:
                print("Version in URL not numeric")
            break
        url_new_all_params = "&".join(url_new_params)
        url_new = "?".join([url_new_base, url_new_all_params])
        return url_new

    def _load_asset_overrides(self, json_dir: str) -> None:
        """Loads all json files to override data per asset_id from api.

        Should point to a shallow directory of .json files, where each json
        contains either directly the direct data for a single asset with "id"
        as a top-level key, or be a top-level list of dictionaries where each
        dictionary contains a field for "id" ie the asset_id.
        """
        dbg = 0
        if not os.path.isdir(json_dir):
            return
        json_files = [
            os.path.join(json_dir, jfile)
            for jfile in os.listdir(json_dir)
            if jfile.endswith(".json") and os.path.isfile(os.path.join(json_dir, jfile))
        ]
        for jfile in json_files:
            try:
                with open(jfile, "r") as jopen:
                    jdata = json.load(jopen)
                self.print_debug(dbg, f"Loaded json data for {jfile}")
            except Exception as e:
                self.print_debug(dbg, f"Failed to open json file {jfile}: {e}")
                continue
            extracted_overrides = self._extract_assetdata_from_dict(jdata)
            self._assetdata_overrides.update(extracted_overrides)

    def _extract_assetdata_from_dict(
            self, json_data: Union[Dict, Sequence]) -> Dict:
        dbg = 1
        extracted = {}
        toplevel_id = json_data.get("id")
        if toplevel_id:
            # Single asset data in one json file
            extracted[toplevel_id] = json_data
        elif isinstance(json_data, list):
            # Multiple assets contained in one json list
            for itm in json_data:
                if not isinstance(itm, dict):
                    self.print_debug(dbg, "Invalid override data, not a dict")
                    self.print_debug(dbg, itm)
                    continue
                _id = itm.get("id", None)
                if not _id:
                    self.print_debug(dbg, "Invalid override data, id is invalid")
                    self.print_debug(dbg, itm)
                    continue
                extracted[_id] = itm

        if extracted:
            over_keys = extracted.keys()
            self.print_debug(dbg, f"Loaded overrides: {over_keys}")
        else:
            self.print_debug(dbg, f"No overrides extracted from json: {json_data}")

        return extracted

    def _override_assets_payload(self, src_assets_data) -> None:
        """Patches raw assets api response structure with loaded overrides."""
        dbg = 1
        if not self._assetdata_overrides:
            return
        if not src_assets_data.get("data"):
            return

        for idx, itm in enumerate(src_assets_data["data"]):
            if itm["id"] in self._assetdata_overrides.keys():
                src_assets_data["data"][idx] = self._assetdata_overrides[itm["id"]]
                self.print_debug(dbg, "Patched ", itm["id"])

    @staticmethod
    def get_dynamic_files(asset_download: AssetDownload,
                          dynamic_files_api: Optional[List[Dict]]
                          ) -> None:
        """Reads dynamic file information for server's API response."""

        if dynamic_files_api is None:
            return

        asset_download.dynamic_files_list = []
        for _dynamic_file_dict in dynamic_files_api:
            name = _dynamic_file_dict.get("name", None)
            contents = _dynamic_file_dict.get("contents", None)
            dynamic_file = DynamicFile(name=name, contents=contents)
            asset_download.dynamic_files_list.append(dynamic_file)

    def _check_xml_data(self,
                        asset_download: AssetDownload,
                        xml_s: str,
                        expose_api_error: bool = False
                        ) -> bool:
        """Checks an XML string for correct XML structure."""

        dbg = 0

        asset_data = asset_download.asset_data
        asset_id = asset_data.asset_id

        try:
            # While we are not really interested in actual contents atm,
            # we parse the XML nevertheless to make sure it is "parseable".
            xml_ok = False
            xml_root = ElementTree.XML(xml_s)
            if xml_root is not None:
                xml_ok = True
        except ElementTree.ParseError as e:
            if expose_api_error:
                asset_data.state.dl.set_error(error_msg="Dynamic file error")
            msg = (f"Could not save dynamic file for {asset_id}, "
                   f"XML parsing issue\n{e}")
            # TODO(Andreas): No logger in PoliigonConnector, yet
            # self.logger.exception(msg)
            self.print_debug(dbg, msg)
            self.report_message(
                "download_df_xml_issue", msg, "error")
        if not xml_ok:
            return False  # NOK reported above in exception

        return True

    def _check_dynamic_file_data(self,
                                 asset_download: AssetDownload,
                                 dynamic_file: DynamicFile,
                                 expose_api_error: bool = False
                                 ) -> bool:
        """Checks the dynamic file data (currently expecting XML) received
        from API.
        """

        dbg = 0

        asset_data = asset_download.asset_data
        asset_id = asset_data.asset_id

        if dynamic_file.name is None:
            if expose_api_error:
                asset_data.state.dl.set_error(error_msg="Dynamic file error")
            msg = (f"Could not save dynamic file for {asset_id}, "
                   "no name provided")
            # TODO(Andreas): No logger in PoliigonConnector, yet
            # self.logger.error(msg)
            self.print_debug(dbg, msg)
            self.report_message(
                "download_df_no_filename", msg, "error")
            return False
        contents = dynamic_file.contents
        if contents is None:
            if expose_api_error:
                asset_data.state.dl.set_error(error_msg="Dynamic file error")
            msg = (f"Could not save dynamic file for {asset_id}, "
                   "no contents provided")
            # TODO(Andreas): No logger in PoliigonConnector, yet
            # self.logger.error(msg)
            self.print_debug(dbg, msg)
            self.report_message(
                "download_df_no_contents", msg, "error")
            return False

        return self._check_xml_data(asset_download, contents, expose_api_error)

    def _store_single_dynamic_file(self,
                                   asset_download: AssetDownload,
                                   dynamic_file: DynamicFile,
                                   expose_api_error: bool = False
                                   ) -> bool:
        """Stores a dynamic file (currently only XML data) to disk."""

        dbg = 0

        asset_data = asset_download.asset_data
        asset_id = asset_data.asset_id

        result = self._check_dynamic_file_data(
            asset_download, dynamic_file, expose_api_error)
        if not result:
            # Here download fails only, if exposure of errors in dynamic_file
            # data from server is desired.
            return not expose_api_error

        # Since we need to store into the correct "size" subfolder and
        # also since MaterialX does have little sense, if there were no other
        # files downloaded, we'll use the path of the first FileDownload
        if len(asset_download.download_list) > 0:
            file_download = asset_download.download_list[0]
            path_file = file_download.get_path()
            dir_asset = os.path.dirname(path_file)
        else:
            # Without any file downloads, all we have here is asset's path:
            dir_asset = asset_download.asset_data.state.dl.get_directory()
        path_dynamic_file = os.path.join(dir_asset, dynamic_file.name)

        try:
            with open(path_dynamic_file, "w") as write_file:
                write_file.write(dynamic_file.contents)
        except OSError as e:
            if e.errno == errno.ENOSPC:
                asset_data.state.dl.set_error(error_msg=ERR_OS_NO_SPACE)
                msg = f"Asset {asset_id}: No space for dynamic file."
                # TODO(Andreas): No logger in PoliigonConnector, yet
                # self.logger.exception(msg)
                self.print_debug(dbg, msg)
                self.report_message("download_df_no_space", msg, "error")
            elif e.errno == errno.EACCES:
                asset_data.state.dl.set_error(
                    error_msg=ERR_OS_NO_PERMISSION)
                msg = f"Asset {asset_id}: No permission to write dynamic file."
                # TODO(Andreas): No logger in PoliigonConnector, yet
                # self.logger.exception(msg)
                self.print_debug(dbg, msg)
                self.report_message("download_df_permission", msg, "error")
            else:
                asset_data.state.dl.set_error(error_msg=str(e))
                msg = (f"Asset {asset_id}: Unexpected error "
                       "upon writing dynamic file.")
                # TODO(Andreas): No logger in PoliigonConnector, yet
                # self.logger.exception(msg)
                self.print_debug(dbg, msg)
                msg += "\n{e}"
                self.report_message("download_df_os_error", msg, "error")
            # Note: Even if dynamic file data issue above does not get exposed
            #       to user, any failure to write the correct MaterialX data
            #       will still lead to a failed download.
            return False
        return True

    def store_dynamic_files(self,
                            asset_download: AssetDownload,
                            expose_api_error: bool = False
                            ) -> bool:
        """Stores all dynamic files belonging to an asset download to disk."""

        if asset_download.dynamic_files_list is None:
            return True
        if len(asset_download.dynamic_files_list) == 0:
            return True

        # Note: We'll get here only after all asset files got downloaded
        #       successfully. Thus we can store any dynamic files errors in
        #       AssetData's download status (no need to be afraid of
        #       overwriting any other error) to present in UI.
        for _dynamic_file in asset_download.dynamic_files_list:
            result = self._store_single_dynamic_file(
                asset_download, _dynamic_file, expose_api_error)
            if not result:
                return False
        return True
