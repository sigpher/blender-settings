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


from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache, partial, wraps
from math import radians
from typing import Callable, Dict, List, Optional, Tuple
import atexit
import datetime
import faulthandler
import json
import mathutils
import os
import queue
import threading
import time
import traceback

try:
    import ConfigParser
except Exception:
    import configparser as ConfigParser

from bpy.app.handlers import persistent
import bpy.utils.previews

from . import reporting
from .modules.poliigon_core.addon import SUPPORTED_TEX_FORMATS
from .modules.poliigon_core import api
from .modules.poliigon_core.assets import AssetData, AssetType, SIZES
from .modules.poliigon_core.asset_index import AssetIndex
from .modules.poliigon_core import env
from .modules.poliigon_core import logger
from .modules.poliigon_core import thread_manager as tm
from .modules.poliigon_core import updater
from .modules.poliigon_core import notifications
from .material_importer import MaterialImporter
from .notifications import (build_no_internet_notification,
                            build_proxy_notification,
                            build_restart_notification,
                            build_survey_notification,
                            build_update_notification,
                            build_writing_settings_failed_notification,
                            rebuild_core_notification)
from .utils import (f_Ex, f_FName, f_FExt, f_FNameExt, f_MDir)


MAX_PURCHASE_THREADS = 5
MAX_PARALLEL_ASSET_DOWNLOADS = 2
MAX_PARALLEL_DOWNLOADS_PER_ASSET = 8
MAX_DOWNLOAD_RETRIES = 3
DOWNLOAD_POLL_INTERVAL = 0.25
SIZE_DEFAULT_POOL = 10
MAX_THUMBH_THREADS = 20
MAX_MY_ASSETS_REQUEST_SIZE = 1000

PREFETCH_PER_SECOND_MAX = 20

ERR_LOGIN_TIMEOUT = "Login with website timed out, please try again"


URLS_BLENDER = {
    "survey": "https://www.surveymonkey.com/r/p4b-addon-ui-01",
    "p4b": "https://poliigon.com/blender"
}

TAGS_WORKFLOW = ["SPECULAR", "METALNESS"]

SUPPORTED_CONVENTION = 1


# ::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::


def panel_update(context=None):
    """Force a redraw of the 3D and preferences panel from operator calls."""
    if not context:
        context = bpy.context
    cTB.f_CheckAssets()
    try:
        for wm in bpy.data.window_managers:
            for window in wm.windows:
                for area in window.screen.areas:
                    if area.type not in ("VIEW_3D", "PREFERENCES"):
                        continue
                    for region in area.regions:
                        region.tag_redraw()
    except AttributeError:
        pass  # Startup condition, nothing to redraw anyways.


def last_update_callback(value):
    """Called by the updated module to allow saving in local system."""
    if cTB.updater is None:
        return

    cTB.init_reporting()
    cTB.vSettings["last_update"] = cTB.updater.last_check
    cTB.f_SaveSettings()


# ::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::

def cleanup_future(asset_name: str, fut: Future) -> None:
    """Used as done callback for thumb download futures.
    It removes the future from the list of currently active futures.
    """

    with cTB.lock_thumb_download_futures:
        cTB.thumb_download_futures.remove((fut, asset_name))


# ::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::

def print_exc(fut: Future) -> None:
    """Used in download thread pool, added as done callback to threads."""

    try:
        exc = fut.exception()
    except CancelledError:
        exc = None
    if exc is None:
        return
    print(("=== TPE[P4B DL]: Thread Exception "
           f"({exc.__class__.__name__}): {exc}"))
    traceback.print_tb(exc.__traceback__)


# ::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::

class LoginStates(Enum):
    IDLE = 0
    WAIT_FOR_INIT = 1
    WAIT_FOR_LOGIN = 2


@dataclass
class DisplayError:
    """Container object for errors that the addon encountered."""
    button_label: str  # Short label for button drawing
    description: str  # Longer description of the issue and what to do.
    asset_id: int  # Optional value, if specific to a single asset.
    asset_name: str  # Optional value, if specific to a single asset.
    goto_account: bool = False  # Set action to move to account on click


def notification_signal_view(notice):
    if notice.viewed or not cTB._api._is_opted_in():
        return

    notice.viewed = True

    thread = threading.Thread(
        target=cTB._api.signal_view_notification,
        args=(notice.notification_id,),
    )
    thread.daemon = 1
    thread.start()
    cTB.vThreads.append(thread)


def get_prefs():
    """User preferences call wrapper, separate to support test mocking."""

    prefs = bpy.context.preferences.addons.get(__package__, None)
    # Fallback, if command line and using the standard install name.
    if prefs is None:
        addons = bpy.context.preferences.addons
        prefs = addons.get("poliigon-addon-blender", None)
    if prefs is not None and hasattr(prefs, "preferences"):
        return prefs.preferences
    else:
        return None


class c_Toolbox:

    # Container for any notifications to show user in the panel UI.
    notifications = []
    # Containers for errors to persist in UI for drawing, e.g. after dload err.
    ui_errors = []

    updater = None  # Callable set up on register.

    # Used to indicate if register function has finished for the first time
    # or not, to differentiate initial register to future ones such as on
    # toggle or update
    initial_register_complete = False
    # Container for the last time we performed a check for updated addon files,
    # only triggered from UI code so it doesn't run when addon is not open.
    last_update_addon_files_check = 0

    # Icon containers.
    vIcons = None
    vPreviews = None

    # Container for threads.
    # Initialized here so it can be referenced before register completes.
    vThreads = []

    # Static strings referenced elsewhere:
    ERR_CREDS_FORMAT = "Invalid email format/password length."

    # Establish locks for general usage:
    lock_previews = threading.Lock()  # locks access to vPreviews AND vPreviewsDownloading
    lock_asset_index = threading.Lock()
    lock_assets = threading.Lock()
    lock_client_start = threading.Lock()
    lock_download = threading.Lock()  # Protects vDownloadQueue and vDownloadCancelled
    lock_settings_file = threading.Lock()
    lock_thumb_download_futures = threading.Lock()

    # Reporting sample rates, None until value read from remote json
    reporting_error_rate = None
    reporting_transaction_rate = None

    def __init__(self, api_service=None):
        self.register_success = False

        self.env = env.PoliigonEnvironment(
            addon_name="poliigon-addon-blender",
            base=os.path.dirname(__file__)
        )
        if api_service is None:
            self._api = api.PoliigonConnector(
                software="blender",
                env=self.env,
                get_optin=reporting.get_optin,
                report_message=reporting.capture_message,
                status_listener=self.update_api_status_banners)
        else:
            self._api = api_service

        self._api.add_poliigon_urls(URLS_BLENDER)

        self.subscription_info_received = False
        self.credits_info_received = False

        self.vTimer = time.monotonic()

        self._tm = tm.ThreadManager(SIZE_DEFAULT_POOL)

        self.addon_convention = SUPPORTED_CONVENTION
        # ATM AssetIndex is used by MaterialImporter, only!
        # This is also the reason to have its reference in a slightly
        # differently named member variable. To avoid it colliding, when
        # inheriting from PoliigonAddon.
        # TODO(Andreas): This will change, once c_Toolbox gets derived
        #                from PoliigonAddon.
        self._create_asset_index_for_mat_import()
        self.mat_import = MaterialImporter(self)  # implicitly sets Cycles

    def _create_asset_index_for_mat_import(self) -> None:
        """Instances AssetIndex for material importer and makes sure,
        AssetIndex's logger does not get in the way of updating the plugin.
        """

        # We are early in the process, toolbox.register() has not run, yet.
        # So we unfortunately need to make sure, the directory also gets
        # created here.
        base_dir = os.path.join(
            os.path.expanduser("~").replace("\\", "/"),
            "Poliigon")
        dir_settings = os.path.join(base_dir, "Blender")
        f_MDir(dir_settings)
        path_log = os.path.join(dir_settings, "log.txt")

        # Set up a custom logger for AssetIndex
        self.log_manager = logger.AddonLogger(env=self.env, file_path=path_log)
        logger.addon_logger = self.log_manager
        self.logger = self.log_manager.initialize_logger(
            "PoliigonAddon.AssetIndex", log_lvl=logger.ERROR)

        self._asset_index_mat = AssetIndex(
            addon_convention=SUPPORTED_CONVENTION,
            log=self.logger)

        # Close the file handler (to get rid of locked file, even if it is now
        # located in <user home>/Poliigon)
        self.log_manager.file_handler.close()

    # Decorator copied from comment in thread_manager.py
    def run_threaded(key_pool: tm.PoolKeys,
                     max_threads: Optional[int] = None,
                     foreground: bool = False) -> callable:
        """Schedule a function to run in a thread of a chosen pool"""
        def wrapped_func(func: callable) -> callable:
            @wraps(func)
            def wrapped_func_call(self, *args, **kwargs):
                args = (self, ) + args
                return self._tm.queue_thread(func, key_pool,
                                             max_threads, foreground,
                                             *args, **kwargs)
            return wrapped_func_call
        return wrapped_func

    def register(self, version: str):
        """Deferred registration, to ensure properties exist."""

        dbg = 0

        if self.env.env_name and "dev" in self.env.env_name.lower():
            faulthandler.enable(all_threads=False)

        self.quitting = False

        self.version = version
        software_version = ".".join([str(x) for x in bpy.app.version])
        self._api.register_update(self.version, software_version)

        # Have defaults or forced sampling, regardless of any online info
        self.reporting_error_rate = 1.0 if self.env.forced_sampling else 0.2
        self.reporting_transaction_rate = 1.0 if self.env.forced_sampling else 0.2

        # TODO(Patrick): Remove once inheriting from poliigon_core.addon
        self.notification_system = notifications.NotificationSystem(addon=None)
        self.updater = updater.SoftwareUpdater(
            addon_name="poliigon-addon-blender",
            addon_version=updater.v2t(version),
            software_version=bpy.app.version,
            notification_system=self.notification_system,
            # local_json=""  # For local testing, absolute file
        )

        self.updater.last_check_callback = last_update_callback
        self.init_reporting(has_updater=False)  # init with defaults

        self.gScriptDir = os.path.join(os.path.dirname(__file__), "files")
        # Output used to recognize a fresh install (or update).
        any_updated = self.update_files(self.gScriptDir)

        # TODO(SOFT-58): Defer folder creation and prompt for user path.
        base_dir = os.path.join(
            os.path.expanduser("~").replace("\\", "/"),
            "Poliigon")

        self.gSettingsDir = os.path.join(base_dir, "Blender")
        f_MDir(self.gSettingsDir)

        self.gOnlinePreviews = os.path.join(base_dir, "OnlinePreviews")
        f_MDir(self.gOnlinePreviews)

        self.gSettingsFile = os.path.join(
            self.gSettingsDir, "Poliigon_Blender_Settings.ini")

        # self.vAsset = None

        print(":" * 100)
        print("\n", "Starting the Poliigon Addon for Blender...", "\n")
        print(self.gSettingsFile)
        print("Toggle verbose logging in addon prefrences")

        self.vRunning = 1
        self.vRedraw = 0
        self.vWidth = 1  # Pixel width, init to non-zero to avoid div by zero.

        self.vRequests = 0

        self.vCheckScale = 0

        self.vGettingData = 0

        # Flag which triggers getting local assets again when settings change
        self.vRerunGetLocalAssets = False

        self.vTimer = time.monotonic()

        self.vSettings = {}
        self.skip_legacy_settings = ["name", "email"]

        # ......................................................................................

        # Separating UI icons from asset previews.
        if self.vIcons is None:
            self.vIcons = bpy.utils.previews.new()
        else:
            self.vIcons.clear()
        self.vIcons.load("ICON_poliigon",
                         os.path.join(self.gScriptDir, "poliigon_logo.png"),
                         "IMAGE")
        self.vIcons.load("ICON_asset_balance",
                         os.path.join(self.gScriptDir, "asset_balance.png"),
                         "IMAGE")
        self.vIcons.load("ICON_myassets",
                         os.path.join(self.gScriptDir, "my_assets.png"),
                         "IMAGE")
        self.vIcons.load("ICON_new",
                         os.path.join(self.gScriptDir, "poliigon_new.png"),
                         "IMAGE")
        self.vIcons.load("GET_preview",
                         os.path.join(self.gScriptDir, "get_preview.png"),
                         "IMAGE")
        self.vIcons.load("NO_preview",
                         os.path.join(self.gScriptDir, "icon_nopreview.png"),
                         "IMAGE")
        self.vIcons.load("NOTIFY",
                         os.path.join(self.gScriptDir, "poliigon_notify.png"),
                         "IMAGE")
        self.vIcons.load("NEW_RELEASE",
                         os.path.join(self.gScriptDir, "poliigon_new.png"),
                         "IMAGE")
        self.vIcons.load("ICON_working",
                         os.path.join(self.gScriptDir, "icon_working.gif"),
                         "MOVIE")
        self.vIcons.load("ICON_dots",
                         os.path.join(self.gScriptDir, "icon_dots.png"),
                         "IMAGE")
        self.vIcons.load("ICON_acquired_check",
                         os.path.join(self.gScriptDir, "acquired_checkmark.png"),
                         "IMAGE")
        self.vIcons.load("ICON_subscription_paused",
                         os.path.join(self.gScriptDir, "subscription_paused.png"),
                         "IMAGE")

        with self.lock_previews:
            if self.vPreviews is None:
                self.vPreviews = bpy.utils.previews.new()
            else:
                self.vPreviews.clear()

        # ......................................................................................

        self.vUser = {}
        self.vUser["name"] = ""
        self.vUser["id"] = ""
        self.vUser["credits"] = 0
        self.vUser["credits_od"] = 0
        self.vUser["plan_name"] = ""  # UI friendly name
        self.vUser["plan_credit"] = 0
        self.vUser["plan_next_renew"] = ""  # Datetime overall plan renew.
        self.vUser["plan_next_credits"] = ""  # Datetime when +plan_credit added
        self.vUser["plan_paused"] = False
        self.vUser["plan_paused_at"] = ""
        self.vUser["plan_paused_until"] = ""
        self.vUser["is_free_user"] = None  # None until proven one or otherwise
        self.vIsFreeStatusSet = False  # Not saved to ini, flipped once per session.
        self.vLoginError = ""
        self.login_cancelled = False
        self.login_state = LoginStates.IDLE
        self.login_res = None
        self.login_thread = None
        self.login_time_start = 0
        self.login_via_browser = True

        self.vSettings = {}
        self.vSettings["res"] = "4K"
        self.vSettings["maps"] = []

        self.vSearch = {}
        self.vSearch["poliigon"] = ""
        self.vSearch["my_assets"] = ""
        self.vSearch["imported"] = ""
        self.vLastSearch = {}
        self.vLastSearch["poliigon"] = ""
        self.vLastSearch["my_assets"] = ""
        self.vLastSearch["imported"] = ""

        self.vPage = {}
        self.vPage["poliigon"] = 0
        self.vPage["my_assets"] = 0
        self.vPage["imported"] = 0

        self.vPages = {}
        self.vPages["poliigon"] = 0
        self.vPages["my_assets"] = 0
        self.vPages["imported"] = 0

        self.vGoTop = 0

        self.vEditPreset = None

        self.vSetup = {}
        self.vSetup["size"] = None
        self.vSetup["disp"] = 1

        self.vPrevScale = 1.0
        self.vMatSlot = 0

        self.vTexExts = [".jpg", ".png", ".tif", ".tiff", ".exr"]
        self.vModExts = [".fbx", ".blend"]

        self.vMaps = [
            "ALPHA",
            "ALPHAMASKED",
            "AO",
            "BUMP",
            "BUMP16",
            "COL",
            "DIFF",
            "DISP",
            "DISP16",
            "EMISSIVE",
            "EMISSION",
            "FUZZ",
            "GLOSS",
            "HDR",
            "IDMAP",
            "JPG",
            "MASK",
            "METALNESS",
            "NRM",
            "NRM16",
            "REFL",
            "ROUGHNESS",
            "SSS",
            "TRANSLUCENCY",
            "TRANSMISSION",
            "OVERLAY",
        ]
        self.maps_convention1 = [
            "AmbientOcclusion",
            "BaseColor",
            "BaseColorOpacity",
            "Displacement",
            "Emission",
            "MaskMap",
            "Metallic",
            "Normal",
            "Opacity",
            "Roughness",
            "ScatteringColor",
            "SheenColor",
            "Translucency",
            "Transmission"
        ]
        self.HDRI_RESOLUTIONS = ["1K", "2K", "3K", "4K", "6K", "8K", "16K"]
        self.vLODs = [f"LOD{i}" for i in range(5)]
        self.vVars = [f"VAR{i}" for i in range(1, 10)]

        self.vModSecondaries = ["Footrest", "Vase"]

        # .....................................................................

        self.f_GetSettings()
        self.prefs = self.get_prefs()
        self.ui_errors = []

        self.get_initial_update_info()

        self.print_debug(
            dbg, f"Sentry error rate: {self.reporting_error_rate}")
        self.print_debug(
            dbg, f"Sentry transaction rate: {self.reporting_transaction_rate}")

        self.vActiveCat = self.vSettings["category"][self.vSettings["area"]]
        self.vAssetType = self.vActiveCat[0]

        if self.vSettings["last_update"]:
            self.updater.last_check = self.vSettings["last_update"]

        if any_updated and not self._api.token:
            # This means this was a new install without a local login token.
            # This setup won't pick up installs in new blender instances
            # where no login event had to happen, but will pick up the first
            # install on the same machine.
            now = datetime.datetime.now()
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            self.vSettings["first_enabled_time"] = now_str
            self.f_SaveSettings()

        # Initial value to use for linking set by prefrences.
        # This way, it initially will match the preferences setting on startup,
        # but then changing this value will also persist with a single sesison
        # without changing the saved value.
        self.link_blend_session = self.vSettings["download_link_blend"]

        # .....................................................................

        self.vCategories = {}
        self.vCategories["poliigon"] = {}
        self.vCategories["my_assets"] = {}
        self.vCategories["imported"] = {}
        self.vCategories["new"] = {}

        self.vAssetTypes = ["Textures", "Models", "HDRIs", "Brushes"]

        self.vAssets = {}
        self.vAssets["poliigon"] = {}
        self.vAssets["my_assets"] = {}
        self.vAssets["imported"] = {}  # TODO(Andreas): Remove with migration to AssetIndex
        self.vAssets["local"] = {}

        # Populated in f_GetSceneAssets,
        # contains references to Blender entities.
        # { type : {asset_name : [objs, mats,...] } }
        self.imported_assets = {}

        # Ensure the base keys always exist:
        for key in self.vAssetTypes:
            self.vAssets["poliigon"][key] = {}
            self.vAssets["my_assets"][key] = {}
            self.vAssets["imported"][key] = {}
            self.vAssets["local"][key] = {}

        self.vAssetsIndex = {}
        self.vAssetsIndex["poliigon"] = {}
        self.vAssetsIndex["my_assets"] = {}
        self.vAssetsIndex["imported"] = {}

        self.vPurchased = []

        # Dictionary storing last download settings per asset.
        # Used in UI drawing to modify Apply/Import button.
        self.last_texture_size = {}  # {asset_name : tex size}

        # ..................................................

        self.vInterrupt = time.monotonic()

        self.vInvalid = 0

        self.vWorking = {}
        self.vWorking["login"] = 0
        self.vWorking["login_with_website"] = 0
        self.vWorking["startup"] = False

        self.vThreads = []

        self.vDownloadQueue = {}
        self.vPurchaseQueue = {}
        self.vDownloadCancelled = set()
        self.vPreviewsQueue = []
        self.vQuickPreviewQueue = {}

        self.vDownloadFailed = {}

        self.purchase_queue = queue.Queue()
        self.purchase_threads = []

        self.vPreviewsDownloading = []

        self.vGettingData = 1
        self.vWasWorking = False  # Identify if at last check, was still running.
        self.vGettingLocalAssets = 0
        self.vGotLocalAssets = 0

        self.vGettingPages = {}
        self.vGettingPages["poliigon"] = []
        self.vGettingPages["my_assets"] = []
        self.vGettingPages["imported"] = []

        self.f_GetCredits()
        self.f_GetUserInfo()
        self.f_GetSubscriptionDetails()

        self.queue_thumb_prefetch = queue.Queue()
        self.thread_prefetch_running = False
        self.thd_prefetch_thumbs = threading.Thread(target=self.thread_prefetch_thumbs)
        self.thd_prefetch_thumbs.daemon = 1
        self.thd_prefetch_thumbs.start()
        self.thumb_download_futures = []

        self.unsupported_assets_exist = False

        self.f_GetAssets(
            "my_assets", vMax=MAX_MY_ASSETS_REQUEST_SIZE, vBackground=0, force=True)
        self.f_GetAssets()
        self.f_GetCategories()
        self.f_GetLocalAssets()

        # Note: When being called, the function will set this to None,
        #       in order to avoid burning additional CPU cycles on this
        self.f_add_survey_notifcation_once = self._add_survey_notifcation

        self.vSortedAssets = []

        # ..................................................

        self.vActiveObjects = []
        self.vActiveAsset = None
        self.vActiveMat = None
        self.vActiveMatProps = {}
        self.vActiveTextures = {}
        self.vActiveFaces = {}
        self.vActiveMode = None

        self.vActiveMixProps = {}
        self.vActiveMix = None
        self.vActiveMixMat = None
        self.vMixTexture = ""

        self.vPropDefaults = {}
        self.vPropDefaults["Scale"] = 1.0
        self.vPropDefaults["Aspect Ratio"] = 1.0
        self.vPropDefaults["Normal Strength"] = 1.0
        self.vPropDefaults["Mix Texture Value"] = 0.0
        self.vPropDefaults["Mix Noise Value"] = 1.0
        self.vPropDefaults["Noise Scale"] = 5.0
        self.vPropDefaults["Noise Detail"] = 2.0
        self.vPropDefaults["Noise Roughness"] = 5.0
        self.vPropDefaults["Mix Softness"] = 0.5
        self.vPropDefaults["Mix Bias"] = 5.0

        self.vAllMats = None

        # Asset Browser synchronization
        self.proc_blender_client = None
        self.listener_running = False
        self.thd_listener = None
        self.sender_running = False
        self.thd_sender = None
        self.queue_send = queue.Queue()
        self.queue_ack = queue.Queue()
        self.event_hello = None
        self.num_asset_browser_jobs = 0
        self.num_jobs_ok = 0
        self.num_jobs_error = 0
        self.asset_browser_jobs_cancelled = False
        self.asset_browser_quitting = False

        self.vInitialScreenViewed = False
        self.initial_register_complete = True

        self.register_success = True

    def init_reporting(self, has_updater: bool = True) -> None:
        """(Re)Initialize sentry with current known sample rates.

        Called once immediately on startup, and then again once we have the
        latest remote reporting information.
        """

        if has_updater:
            # Get Sentry sampling rates from update info and store
            # in settings, if needed
            curr_ver = self.updater.current_version
            env_force_sample = self.env.forced_sampling
            if curr_ver.error_sample_rate is not None and not env_force_sample:
                self.reporting_error_rate = curr_ver.error_sample_rate
                self.vSettings["reporting_error_rate"] = self.reporting_error_rate
            if curr_ver.traces_sample_rate is not None and not env_force_sample:
                self.reporting_transaction_rate = curr_ver.traces_sample_rate
                self.vSettings["reporting_transaction_rate"] = self.reporting_transaction_rate

            self.f_SaveSettings()

        reporting.register(
            software_name=self._api.software_source,
            software_version=self._api.software_version,
            tool_version=self._api.version_str,
            env=self.env,
            error_rate=self.reporting_error_rate,
            transaction_rate=self.reporting_transaction_rate)

    def get_initial_update_info(self) -> None:
        """If we have no Sentry sample rates, do an inital synchrnous update
        check, otherwise just init the sample rates.
        """

        is_new_version = self.vSettings["version"] != self.version
        error_rate = self.vSettings["reporting_error_rate"]
        transaction_rate = self.vSettings["reporting_transaction_rate"]
        if error_rate != -1 and transaction_rate != -1 and not is_new_version:
            self.reporting_error_rate = error_rate
            self.reporting_transaction_rate = transaction_rate
            self.init_reporting()
            return

        self.vSettings["version"] = self.version

        # Synchronously get update info to get reporting rates
        self.updater.check_for_update(
            # Note: This "update available" callback is not needed here.
            #       The last_update_callback does all we want.
            callback=None,
            create_notifications=False)

    # ...............................................................................................

    def f_GetSettings(self):
        dbg = 0
        self.print_separator(dbg, "f_GetSettings")

        self.vSettings = {}
        self.vSettings["add_dirs"] = []
        self.vSettings["area"] = "poliigon"
        self.vSettings["auto_download"] = 1
        self.vSettings["category"] = {}
        self.vSettings["category"]["imported"] = ["All Assets"]
        self.vSettings["category"]["my_assets"] = ["All Assets"]
        self.vSettings["category"]["poliigon"] = ["All Assets"]
        self.vSettings["conform"] = 0
        self.vSettings["default_lod"] = "LOD1"
        self.vSettings["del_zip"] = 1
        self.vSettings["disabled_dirs"] = []
        self.vSettings["download_lods"] = 1
        self.vSettings["download_prefer_blend"] = 1
        self.vSettings["download_link_blend"] = 0
        self.vSettings["hdri_use_jpg_bg"] = False
        self.vSettings["hide_labels"] = 1
        self.vSettings["hide_scene"] = 0
        self.vSettings["hide_suggest"] = 0
        self.vSettings["library"] = ""
        self.vSettings["location"] = "Properties"
        self.vSettings["mapping_type"] = "UV + UberMapping"
        self.vSettings["mat_props"] = []
        self.vSettings["mix_props"] = []
        self.vSettings["new_release"] = ""
        self.vSettings["last_update"] = ""
        self.vSettings["new_top"] = 1
        self.vSettings["notify"] = 5
        self.vSettings["page"] = 10
        self.vSettings["preview_size"] = 7  # 7 currently constant/hard coded
        self.vSettings["previews"] = 1
        # Note on reporting rates:
        # These will only be changed upon successful update-requests
        # and only ever store custom rates from updater. Any default
        # values and/or forced sampling values will and must not be stored
        # here!
        self.vSettings["reporting_error_rate"] = -1  # no valid value
        self.vSettings["reporting_transaction_rate"] = -1  # no valid value
        self.vSettings["set_library"] = ""
        self.vSettings["show_active"] = 1
        self.vSettings["show_add_dir"] = 1
        self.vSettings["show_asset_info"] = 1
        self.vSettings["show_credits"] = 1
        self.vSettings["show_default_prefs"] = 1
        self.vSettings["show_display_prefs"] = 1
        self.vSettings["show_import_prefs"] = 1
        self.vSettings["show_asset_browser_prefs"] = True
        self.vSettings["show_mat_ops"] = 0
        self.vSettings["show_mat_props"] = 0
        self.vSettings["show_mat_texs"] = 0
        self.vSettings["show_mix_props"] = 1
        self.vSettings["show_pass"] = 0
        self.vSettings["show_plan"] = 1
        self.vSettings["show_feedback"] = 0
        self.vSettings["show_settings"] = 0
        self.vSettings["show_user"] = 0
        self.vSettings["sorting"] = "Latest"
        self.vSettings["thumbsize"] = "Medium"
        self.vSettings["unzip"] = 1
        self.vSettings["update_sel"] = 1
        self.vSettings["use_16"] = 1
        self.vSettings["use_ao"] = 1
        self.vSettings["use_bump"] = 1
        self.vSettings["use_disp"] = 1
        self.vSettings["use_subdiv"] = 1
        self.vSettings["version"] = self.version
        self.vSettings["win_scale"] = 1
        self.vSettings["first_enabled_time"] = ""

        self.vSettings["res"] = "2K"
        self.vSettings["lod"] = "NONE"
        self.vSettings["mres"] = "2K"
        self.vSettings["hdri"] = "1K"
        self.vSettings["hdrib"] = "8K"
        self.vSettings["hdrif"] = "EXR"  # TODO(Andreas): constant and used in commented code, only
        self.vSettings["brush"] = "2K"
        self.vSettings["maps"] = self.vMaps

        # ...............................................................................................

        self.check_dpi()

        # ...............................................................................................

        self.vPresets = {}
        self.vMixPresets = {}

        self.vReleases = {}

        # ...............................................................................................

        if f_Ex(self.gSettingsFile):  # check done outside of lock should still be ok
            vConfig = self.read_config()

            if vConfig.has_section("user"):
                for vK in vConfig.options("user"):
                    if vK in self.skip_legacy_settings:
                        continue
                    if vK in ["credits", "credits_od", "plan_credit"]:
                        try:
                            self.vUser[vK] = int(vConfig.get("user", vK))
                        except ValueError:
                            self.vUser[vK] = 0
                    elif vK == "is_free_user":
                        # Don't default to 0 value, default to not set for
                        # free user, as 0 is treated as an active user and thus
                        # would not be shown the free query.
                        try:
                            self.vUser[vK] = int(vConfig.get("user", vK))
                        except ValueError:
                            self.vUser[vK] = None
                    elif vK == "token":
                        token = vConfig.get("user", "token")
                        if token and token != "None":
                            self._api.token = vConfig.get("user", "token")
                    else:
                        self.vUser[vK] = vConfig.get("user", vK)

                if self.vUser["id"]:
                    reporting.assign_user(self.vUser["id"])

            else:
                with self.lock_settings_file:
                    os.remove(self.gSettingsFile)
                vConfig = ConfigParser.ConfigParser()

            if vConfig.has_section("settings"):
                for vS in vConfig.options("settings"):
                    if vS.startswith("category"):
                        try:
                            vArea = vS.replace("category_", "")
                            self.vSettings["category"][vArea] = vConfig.get(
                                "settings", vS
                            ).split("/")
                            if "" in self.vSettings[vS]:
                                self.vSettings["category"][vArea].remove("")
                        except Exception:
                            pass
                    else:
                        self.vSettings[vS] = vConfig.get("settings", vS)

                        if vS in [
                            "add_dirs",
                            "disabled_dirs",
                            "mat_props",
                            "mix_props",
                        ]:
                            self.vSettings[vS] = self.vSettings[vS].split(";")
                            if "" in self.vSettings[vS]:
                                self.vSettings[vS].remove("")
                        elif self.vSettings[vS] == "True":
                            self.vSettings[vS] = 1
                        elif self.vSettings[vS] == "False":
                            self.vSettings[vS] = 0
                        else:
                            try:
                                self.vSettings[vS] = int(self.vSettings[vS])
                            except Exception:
                                try:
                                    self.vSettings[vS] = float(self.vSettings[vS])
                                except Exception:
                                    pass
                        # Fallback, if lod was set to SOURCE
                        if vS == "lod" and self.vSettings[vS] == "SOURCE":
                            # TODO(Andreas): Fallback to LOD0 correct?
                            self.vSettings[vS] = "LOD0"

            if vConfig.has_section("presets"):
                for vP in vConfig.options("presets"):
                    try:
                        self.vPresets[vP] = [
                            float(vV) for vV in vConfig.get("presets", vP).split(";")
                        ]
                    except Exception:
                        pass

            if vConfig.has_section("mixpresets"):
                for vP in vConfig.options("mixpresets"):
                    try:
                        self.vMixPresets[vP] = [
                            float(vV) for vV in vConfig.get("mixpresets", vP).split(";")
                        ]
                    except Exception:
                        pass

            if vConfig.has_section("download"):
                for vO in vConfig.options("download"):
                    if vO == "res":
                        self.vSettings["res"] = vConfig.get("download", vO)
                    elif vO == "maps":
                        self.vSettings["maps"] = vConfig.get("download", vO).split(";")

        # ...............................................................................................

        # self.vSettings["library"] = ""
        if self.vSettings["library"] == "":
            self.vSettings["set_library"] = self.gSettingsDir.replace("Blender", "Library")

        self.vSettings["show_user"] = 0
        self.vSettings["mat_props_edit"] = 0

        self.vSettings["area"] = "poliigon"
        self.vSettings["category"]["poliigon"] = ["All Assets"]
        self.vSettings["category"]["imported"] = ["All Assets"]
        self.vSettings["category"]["my_assets"] = ["All Assets"]

        self._set_free_user()

        self.f_SaveSettings()

    def read_config(self):
        """Safely reads the config or returns an empty one if corrupted."""
        config = ConfigParser.ConfigParser()
        config.optionxform = str

        with self.lock_settings_file:
            try:
                config.read(self.gSettingsFile)
            except ConfigParser.Error as e:
                # Corrupted file, return empty config.
                print(e)
                print("Config parsing error, using fresh empty config instead.")
                config = ConfigParser.ConfigParser()
                config.optionxform = str

        return config

    def f_SaveSettings(self):
        dbg = 0
        self.print_separator(dbg, "f_SaveSettings")
        vConfig = self.read_config()

        # ................................................

        if not vConfig.has_section("user"):
            vConfig.add_section("user")

        for vK in self.vUser.keys():
            if vK in self.skip_legacy_settings:
                vConfig.remove_option("user", vK)
                continue
            vConfig.set("user", vK, str(self.vUser[vK]))

        # Save token as if cTB field, on load will be parsed to _api.token
        vConfig.set("user", "token", str(self._api.token))

        # ................................................

        if not vConfig.has_section("settings"):
            vConfig.add_section("settings")

        for vS in self.vSettings.keys():
            if vS == "category":
                for vA in self.vSettings[vS].keys():
                    vConfig.set(
                        "settings", vS + "_" + vA, "/".join(self.vSettings[vS][vA])
                    )

            elif vS in ["add_dirs", "disabled_dirs", "mat_props", "mix_props"]:
                vConfig.set("settings", vS, ";".join(self.vSettings[vS]))

            else:
                vConfig.set("settings", vS, str(self.vSettings[vS]))

        # ................................................

        if not vConfig.has_section("presets"):
            vConfig.add_section("presets")

        for vP in self.vPresets.keys():
            vConfig.set("presets", vP, ";".join([str(vV) for vV in self.vPresets[vP]]))

        # ................................................

        if not vConfig.has_section("mixpresets"):
            vConfig.add_section("mixpresets")

        for vP in self.vMixPresets.keys():
            vConfig.set(
                "mixpresets", vP, ";".join([str(vV) for vV in self.vMixPresets[vP]])
            )

        # ................................................

        if vConfig.has_section("download"):
            vConfig.remove_section("download")
        vConfig.add_section("download")

        for vK in self.vSettings:
            if vK == "res":
                vConfig.set("download", vK, self.vSettings[vK])
            elif vK == "maps":
                vConfig.set("download", vK, ";".join(self.vSettings[vK]))

        # ................................................

        f_MDir(self.gSettingsDir)

        with self.lock_settings_file:
            try:
                with open(self.gSettingsFile, "w+") as vFile:
                    vConfig.write(vFile)
            except OSError as e:
                if e.errno != 28:
                    # Below notice assumes the OSError is for disk space only,
                    # so let's report if this ever isn't the case
                    reporting.capture_exception(e)
                notice = build_writing_settings_failed_notification(e.strerror)
                self.register_notification(notice)

    # .........................................................................

    def set_free_search(self):
        """Assigns or clears the search field on new login or startup.

        If the user is a free user, it should be added to the search text only
        once per logged in session.
        """
        # Return early if the user is not logged in anyways
        if not self.vUser["id"] or self.vUser["id"] == "None":
            return

        # Undecided, yet?
        if self.vUser["is_free_user"] is None:
            return

        # Return early if the search value had already been assigned once for
        # this logged in session.
        if self.vIsFreeStatusSet and self.vUser["is_free_user"] is not None:
            return

        # If the user is a free user, load the free setting.
        if self.vUser["is_free_user"] == 1:
            self.vLastSearch["poliigon"] = ""
            self.vSearch["poliigon"] = "free"
        elif self.vSearch["poliigon"] == "free":
            self.vLastSearch["poliigon"] = "free"  # Set different to trigger re-query
            self.vSearch["poliigon"] = ""

        vProps = bpy.context.window_manager.poliigon_props
        vProps.search_poliigon = self.vSearch["poliigon"]
        self.vIsFreeStatusSet = True

    def refresh_ui(self):
        """Wrapper to decouple blender UI drawing from callers of self."""

        if self.quitting:
            return
        panel_update(bpy.context)

    def check_dpi(self):
        """Checks the DPI of the screen to adjust the scale accordingly.

        Used to ensure previews remain square and avoid text truncation.
        """
        prefs = bpy.context.preferences
        self.vSettings["win_scale"] = prefs.system.ui_scale

    def get_ui_scale(self):
        """Utility for fetching the ui scale, used in draw code."""
        self.check_dpi()
        return self.vSettings["win_scale"]

    def check_if_working(self):
        """See if the toolbox is currently running an operation."""
        # Not including `self.vGettingData` as that is just a flag for
        # displaying placeholders in the UI.
        res = 1 in list(self.vWorking.values())
        if res:
            self.vWasWorking = res
        return res

    # .........................................................................

    def is_logged_in(self):
        """Returns whether or not the user is currently logged in."""
        return self._api.token is not None and not self._api.invalidated

    def user_invalidated(self):
        """Returns whether or not the user token was invalidated."""

        if self._api.invalidated:
            self.prefs.any_owned_brushes = "undecided"
        return self._api.invalidated

    def clear_user_invalidated(self):
        """Clears any invalidation flag for a user."""
        self._api.invalidated = False

    def check_backplate(self, asset_name):
        """Return bool on whether this asset is a backplate."""
        lwr = asset_name.lower()
        return any(
            lwr.startswith(vS) for vS in ["backdrop", "backplate"])

    # .........................................................................

    def initial_view_screen(self):
        """Reports view from a draw panel, to avoid triggering until use."""
        if self.vInitialScreenViewed is True:
            return
        self.vInitialScreenViewed = True
        self.track_screen_from_area()

    def track_screen_from_area(self):
        """Signals the active screen in background if opted in"""
        area = self.vSettings["area"]
        if area == "poliigon":
            self.track_screen("home")
        elif area == "my_assets":
            self.track_screen("my_assets")
        elif area == "imported":
            self.track_screen("imported")
        elif area == "account":
            self.track_screen("my_account")

    def track_screen(self, area):
        """Signals input screen area in a background thread if opted in."""
        if not self._api._is_opted_in():
            return
        vThread = threading.Thread(
            target=self._api.signal_view_screen,
            args=(area,),
        )
        vThread.daemon = 1
        vThread.start()
        self.vThreads.append(vThread)

    def register_notification(self, notice):
        """Stores and displays a new notification banner and signals event."""
        self.print_debug(0, "Creating notice: ", notice.notification_id)
        # Clear any notifications with the same id.
        # TODO(Andreas): Loop being modified during itration!
        for existing_notice in self.notifications:
            if existing_notice.notification_id == notice.notification_id:
                self.notifications.remove(existing_notice)
        self.notifications.append(notice)

    def click_notification(self, notification_id, action):
        """Signals event for click notification."""
        if not self._api._is_opted_in():
            return
        vThread = threading.Thread(
            target=self._api.signal_click_notification,
            args=(notification_id, action,),
        )
        vThread.daemon = 1
        vThread.start()
        self.vThreads.append(vThread)

    def dismiss_notification(self, notification_index):
        """Signals dismissed notification in background if user opted in."""
        ntype = self.notifications[notification_index].notification_id
        del self.notifications[notification_index]

        if not self._api._is_opted_in():
            return
        vThread = threading.Thread(
            target=self._api.signal_dismiss_notification,
            args=(ntype,),
        )
        vThread.daemon = 1
        vThread.start()
        self.vThreads.append(vThread)

    def finish_notification(self, notification_id):
        """To be called last in notification operators.
        Used to execute generic finishing steps, like e.g. auto dismissal.
        """

        if notification_id == "" or notification_id is None:
            return

        for idx_notice, notification in enumerate(self.notifications):
            if notification.notification_id != notification_id:
                continue
            if notification.auto_dismiss:
                self.dismiss_notification(idx_notice)

    def notification_signal_view(self, notice):
        if notice.viewed or not self._api._is_opted_in():
            return

        notice.viewed = True

        thread = threading.Thread(
            target=self._api.signal_view_notification,
            args=(notice.notification_id,),
        )
        thread.daemon = 1
        thread.start()
        self.vThreads.append(thread)

    def signal_import_asset(self, asset_id):
        """Signals an asset import in the background if user opted in."""
        if not self._api._is_opted_in() or asset_id == 0:
            return
        vThread = threading.Thread(
            target=self._api.signal_import_asset,
            args=(asset_id,),
        )
        vThread.daemon = 1
        vThread.start()
        self.vThreads.append(vThread)

    def signal_preview_asset(self, asset_id):
        """Signals an asset preview in the background if user opted in."""
        if not self._api._is_opted_in():
            return
        vThread = threading.Thread(
            target=self._api.signal_preview_asset,
            args=(asset_id,),
        )
        vThread.daemon = 1
        vThread.start()
        self.vThreads.append(vThread)

    # .........................................................................
    def loginout_prepare(self) -> None:
        self.clear_user_invalidated()
        self.login_cancelled = False

    def login_determine_elapsed(self) -> None:
        """Calculates the time between addon enable and login.

        This is included in the initiate login or direct email/pwd login only
        if this is the first time install+login. This value gets included in
        the initiate/login request which will treat as an addon install event.
        """

        self.login_elapsed_s = None
        if not self.vSettings["first_enabled_time"]:
            return

        now = datetime.datetime.now()
        install_tstr = self.vSettings["first_enabled_time"]
        install_t = datetime.datetime.strptime(
            install_tstr, "%Y-%m-%d %H:%M:%S")
        elapsed = now - install_t
        self.login_elapsed_s = int(elapsed.total_seconds())
        if self.login_elapsed_s <= 0:
            self.print_debug(0, "Throwing out negative elapsed time")
            self.login_elapsed_s = None

    def f_Login_with_website_init(self) -> api.ApiResponse:
        self.loginout_prepare()

        dbg = 0
        self.print_separator(dbg, "f_Login_with_website_init")

        res = self._api.log_in_with_website()
        self.login_res = res
        self.login_thread = None
        return res

    def _start_login_thread(self, func: Callable):
        self.login_thread = threading.Thread(target=func)
        self.login_thread.daemon = 1
        self.login_thread.start()
        self.vThreads.append(self.login_thread)

    def f_Login_with_website_check(self):
        self.login_determine_elapsed()
        self.login_res = self._api.check_login_with_website_success(
            self.login_elapsed_s)
        self.login_thread = None

    def login_finish(self, res: api.ApiResponse):
        dbg = 0

        if res is None or not res.ok:
            self.print_debug(dbg, "f_Login", "ERROR", res.error)
            if res is not None and not self.login_cancelled:
                self.vLoginError = res.error
            self.login_cancelled = False
            self.refresh_ui()
            return

        vData = res.body

        self.vUser["name"] = vData["user"]["name"]
        self.vUser["id"] = vData["user"]["id"]

        # Ensure logging is associated with this user.
        reporting.assign_user(self.vUser["id"])

        self.vUser["credits"] = 0
        self.vUser["credits_od"] = 0
        self.vUser["plan_name"] = None
        self.vUser["plan_credit"] = None
        self.vUser["plan_next_renew"] = None
        self.vUser["plan_next_credits"] = None
        self.vUser["is_free_user"] = None

        self.f_GetCredits()
        self.f_GetCategories()

        # Non threaded to avoid double request with GetAssets,
        # as this may trigger a change in the default search query
        # to be 'free'
        self.f_APIGetSubscriptionDetails()

        # Fetch updated assets automatically.
        self.f_GetAssets(
            "my_assets", vMax=MAX_MY_ASSETS_REQUEST_SIZE, vBackground=0, force=True)
        self.f_GetAssets()

        self.vLoginError = ""

        # Clear out password after login attempt
        bpy.context.window_manager.poliigon_props.vPassHide = ""
        bpy.context.window_manager.poliigon_props.vPassShow = ""

        # Reset navigation on login
        self.vSettings["area"] = "poliigon"
        self.track_screen_from_area()

        self.vSettings["category"]["imported"] = ["All Assets"]
        self.vSettings["category"]["my_assets"] = ["All Assets"]
        self.vSettings["category"]["poliigon"] = ["All Assets"]
        self.vSettings["show_settings"] = 0
        self.vSettings["show_user"] = 0

        self.print_debug(dbg, "f_Login", "Login success")

        # Clear time since install since successful.
        if self.login_elapsed_s is not None:
            self.vSettings["first_enabled_time"] = ""
            self.f_SaveSettings()

        self.refresh_ui()

    def logout(self):
        dbg = 0

        req = self._api.log_out()
        reporting.assign_user(None)  # Clear user id from reporting.
        if req.ok:
            self.print_debug(dbg, "f_Login", "Logout success")
        else:
            self.print_debug(dbg, "f_Login", "ERROR", req.error)
            reporting.capture_message("logout_error", req.error, "error")

            self.vIsFreeStatusSet = False  # Reset as linked to user.

        self._api.token = None

        # Clear out all user fields on logout.
        self.vUser["credits"] = 0
        self.vUser["credits_od"] = 0
        self.vUser["plan_name"] = None
        self.vUser["plan_next_renew"] = None
        self.vUser["plan_next_credits"] = None
        self.vUser["plan_credit"] = None
        self.vUser["is_free_user"] = None
        self.vUser["token"] = None
        self.vUser["name"] = None
        self.vUser["id"] = None

        self.vIsFreeStatusSet = False  # Reset as linked to user.
        self.credits_info_received = False
        self.subscription_info_received = False

        bpy.context.window_manager.poliigon_props.vEmail = ""
        bpy.context.window_manager.poliigon_props.vPassHide = ""
        bpy.context.window_manager.poliigon_props.vPassShow = ""

        self.prefs.any_owned_brushes = "undecided"

        self.refresh_ui()

    def login_finalization(self):
        self.f_SaveSettings()

        self.vWorking["login"] = 0

        self.vRedraw = 1
        self.refresh_ui()

    # @timer
    def f_Login(self, vMode):
        self.loginout_prepare()

        dbg = 0
        self.print_separator(dbg, "f_Login")
        if vMode == "login":
            self.login_determine_elapsed()

            vReq = self._api.log_in(
                bpy.context.window_manager.poliigon_props.vEmail,
                bpy.context.window_manager.poliigon_props.vPassHide,
                time_since_enable=self.login_elapsed_s)

            self.login_finish(vReq)

        elif vMode == "logout":
            self.logout()

        elif vMode == "login_with_website":
            self.print_debug(dbg, "Wrong code branch")

        self.login_finalization()

    # .........................................................................

    def f_GetCategories(self):
        dbg = 0
        self.print_separator(dbg, "f_GetCategories")

        vThread = threading.Thread(target=self.f_APIGetCategories)
        vThread.daemon = 1
        vThread.start()
        self.vThreads.append(vThread)

    def f_GetCategoryChildren(self, vType, vCat):
        vChldrn = vCat["children"]
        for vC in vChldrn:
            vPath = []
            for vS in vC["path"].split("/"):
                vS = " ".join([vS1.capitalize() for vS1 in vS.split("-")])
                vPath.append(vS)

            vPath = ("/".join(vPath)).replace("/" + vType + "/", "/")
            vPath = vPath.replace("/Hdrs/", "/")

            if "Generators" in vPath:
                continue

            # self.print_debug(dbg, "f_GetCategoryChildren", vPath)

            self.vCategories["poliigon"][vType][vPath] = []

            if len(vC["children"]):
                self.f_GetCategoryChildren(vType, vC)

    @reporting.handle_function(silent=True)
    def f_APIGetCategories(self):
        """Fetch and save categories to file."""
        dbg = 0
        self.print_separator(dbg, "f_APIGetCategories")
        vReq = self._api.categories()
        if vReq.ok:
            if not len(vReq.body):
                self.print_debug(
                    dbg, "f_APIGetCategories", "ERROR",
                    vReq.error, ", ", vReq.body)

            for vC in vReq.body:
                vType = vC["name"]
                self.print_debug(dbg, "f_APIGetCategories", vType)
                if vType not in self.vCategories["poliigon"].keys():
                    self.vCategories["poliigon"][vType] = {}
                self.f_GetCategoryChildren(vType, vC)

            vDataFile = os.path.join(self.gSettingsDir, "TB_Categories.json")
            with open(vDataFile, "w") as vWrite:
                json.dump(self.vCategories, vWrite)
        self.refresh_ui()

    # .........................................................................

    # @timer
    def f_GetAssets(self, vArea=None, vPage=None, vMax=None,
                    vBackground=0, vUseThread=True, force=False):
        dbg = 0
        self.print_separator(dbg, "f_GetAssets")
        self.print_debug(dbg, "f_GetAssets", vArea, vPage, vMax, vBackground)

        if vArea is None:
            vArea = self.vSettings["area"]

        if vPage is None:
            vPage = self.vPage[vArea]

        if vMax is None:
            vMax = self.vSettings["page"]

        vPageAssets, vPageCount = self.f_GetPageAssets(vPage)
        if len(vPageAssets) and not force:
            return

        # .........................................................................

        if vPage in self.vGettingPages[vArea] and not force:
            return

        self.vGettingPages[vArea].append(vPage)

        vSearch = self.vSearch[vArea]

        if vSearch != self.vLastSearch[vArea]:
            self.flush_thumb_prefetch_queue()

        vKey = "/".join([vArea] + self.vSettings['category'][vArea])
        if vSearch != "":
            vKey = "@".join(
                [vArea] + self.vSettings['category'][vArea] + [vSearch]
            )
        self.print_debug(dbg, "f_GetAssets", vKey)
        now = time.monotonic()

        if vUseThread:
            args = (vArea, vPage, vMax, vSearch, vKey, vBackground, now, force)
            vThread = threading.Thread(
                target=self.f_APIGetAssets,
                args=args
            )
            vThread.daemon = 1
            vThread.start()
            self.vThreads.append(vThread)
        else:
            self.f_APIGetAssets(
                vArea, vPage, vMax, vSearch, vKey, vBackground, now, force)

    @reporting.handle_function(silent=True)
    def f_APIGetAssets(
            self, vArea, vPage, vMax, vSearch, vKey, vBackground, vTime, force):
        dbg = 0
        self.print_separator(dbg, "f_APIGetAssets")
        self.print_debug(
            dbg, "f_APIGetAssets",
            vArea, vPage + 1, vMax, vKey, vBackground, vTime, force)

        # ...............................................................

        if not self.vRunning:
            # Not depending on force, as this is an exit condition
            return

        if not vBackground:
            self.vGettingData = 1

        # ...............................................................

        if vMax != MAX_MY_ASSETS_REQUEST_SIZE or vArea != "my_assets":
            vGetPage = int((vPage * self.vSettings["page"]) / vMax)
        else:
            vGetPage = vPage

        vData = {
            "query": vSearch,
            "page": vGetPage + 1,
            "perPage": vMax,
            "algoliaParams": {"facetFilters": [], "numericFilters": ["Credit>=0"]},
        }

        vCat = self.vSettings["category"][vArea][0]

        if len(self.vSettings["category"][vArea]) > 1:
            if self.vSettings["category"][vArea][1] == "Free":
                vData["algoliaParams"]["numericFilters"] = ["Credit=0"]

            if vCat == "All Assets":
                vData["algoliaParams"]["facetFilters"] = [[]]
                for vType in self.vAssetTypes:
                    if (
                        "/" + self.vSettings["category"][vArea][1]
                        in self.vCategories[vArea][vType].keys()
                    ):
                        vCat = [vType] + self.vSettings["category"][vArea][1:]
                        vLvl = len(vCat) - 1
                        vCat = " > ".join(vCat).replace("HDRIs", "HDRs")
                        vData["algoliaParams"]["facetFilters"][0].append(
                            "RefineCategories.lvl" + str(vLvl) + ":" + vCat
                        )

            else:
                vLvl = len(self.vSettings["category"][vArea]) - 1
                vCat = " > ".join(self.vSettings["category"][vArea])
                vCat = vCat.replace("HDRIs", "HDRs")
                vData["algoliaParams"]["facetFilters"] = [
                    "RefineCategories.lvl" + str(vLvl) + ":" + vCat
                ]

        elif vCat != "All Assets":
            vCat = vCat.replace("HDRIs", "HDRs")
            vData["algoliaParams"]["facetFilters"] = ["RefineCategories.lvl0:" + vCat]

        self.print_debug(dbg, "f_APIGetAssets", json.dumps(vData))

        # ...............................................................

        if (not force and self.vInterrupt > vTime) or not self.vRunning:
            return

        check_owned = vArea == "my_assets"
        job_is_done = True  # always True for tab "poliigon"
        if check_owned:
            vReq = self._api.get_user_assets(query_data=vData)
            if vReq.ok:
                idx_last_page = vReq.body.get("last_page", -1)
                job_is_done = (vPage + 1) >= idx_last_page
        else:
            vReq = self._api.get_assets(query_data=vData)

        if vPage in self.vGettingPages[vArea]:
            self.vGettingPages[vArea].remove(vPage)

        # ...............................................................

        if vReq.ok:
            try:
                vData = vReq.body.get("data")
            except Exception:
                return

            total = vReq.body.get("total")
            self.print_debug(
                dbg,
                "f_APIGetAssets",
                f"{vArea}: {len(vData)} assets ({total} total)"
            )

            vPages = vReq.body.get("total", 1) / self.vSettings.get("page", 1)
            vPages = int(vPages + 0.999)

            if not vBackground and vPage == self.vPage[vArea]:
                self.vPages[vArea] = vPages

            with self.lock_asset_index:
                if vKey not in self.vAssetsIndex[vArea].keys():
                    self.vAssetsIndex[vArea][vKey] = {}
                    self.vAssetsIndex[vArea][vKey]["pages"] = vPages

            self.print_debug(
                dbg, "f_APIGetAssets", len(vData), vPages, "pages")

            vIdx = vGetPage * vMax

            brush_among_assets = False
            for vA in vData:
                if vA["type"] == "Brushes":
                    brush_among_assets = True
                did_load = self.load_asset(vA, vArea, vKey, vIdx)

                if did_load:
                    self.vRedraw = 1
                    self.refresh_ui()

                    vIdx += 1
            try:
                if self.prefs.any_owned_brushes == "undecided" and check_owned:
                    self.prefs.any_owned_brushes = "owned_brushes" if brush_among_assets else "no_brushes"
            except AttributeError:
                # TODO(SOFT-988): Prefer better fix.
                # Sometimes we are getting:
                # AttributeError: 'PoliigonPreferences' object has no attribute 'any_owned_brushes'
                # which ends test runs early.
                pass

            # vMax check to avoid additional page requests, when prefetching
            # (which happens with UI page size)
            if vArea == "my_assets" and vMax == MAX_MY_ASSETS_REQUEST_SIZE:
                if not job_is_done:
                    idx_next_page = vPage + 1
                    self.f_GetAssets(vArea=vArea, vPage=idx_next_page, vMax=vMax,
                                     vBackground=vBackground, vUseThread=True, force=force)

            if (self.vInterrupt > vTime and not force) or not self.vRunning:
                return

            if not vBackground and vPage == self.vPage[vArea]:
                self.vGettingData = 0

                self.vRedraw = 1
                self.refresh_ui()

        else:
            self.print_debug(dbg, "f_APIGetAssets", "ERROR", vReq.error)

    def flush_thumb_prefetch_queue(self):
        # Flush prefetch queue, i.e. prefetch requests not yet in thread pool
        while not self.queue_thumb_prefetch.empty():
            try:
                self.queue_thumb_prefetch.get_nowait()
            except Exception:
                pass  # not interested in exceptions in here

        # Try to cancel download threads in threadpool
        with self.lock_thumb_download_futures:
            # As the done callback of the futures removes from this list
            # (using the same lock) we need a copy
            futures_to_cancel = self.thumb_download_futures.copy()
        # Now cancel the futures without lock acquired
        for fut, asset_name in futures_to_cancel:
            if not fut.cancel():
                # Thread either executing or done already
                continue
            with self.lock_previews:
                if asset_name in self.vPreviewsDownloading:
                    self.vPreviewsDownloading.remove(asset_name)

    def enqueue_thumb_prefetch(self, asset_name: str):
        path_thumb = self.f_GetThumbnailPath(asset_name, 0)
        if os.path.exists(path_thumb):
            return

        self.queue_thumb_prefetch.put(asset_name)

    def thread_prefetch_thumbs(self):
        self.thread_prefetch_running = True

        while self.thread_prefetch_running:
            try:
                asset_name = self.queue_thumb_prefetch.get(timeout=1.0)
            except queue.Empty:
                continue

            if not self.thread_prefetch_running:
                break

            time.sleep(1 / PREFETCH_PER_SECOND_MAX)

            path_thumb = self.f_GetThumbnailPath(asset_name, 0)
            if os.path.exists(path_thumb):
                continue

            # load_image=False: Avoid messing with Blender's image preview from this thread
            self.f_GetPreview(asset_name, load_image=False)

    def load_asset(self, vA, vArea, vKey, vIdx):
        """Loads a single asset into the structure.

        Args:
            vA: Asset data from the API.
            vArea: Interface load context.
            vKey: Key for this asset.
            vIdx: Index within current struncure loading into.

        Return: bool on whether did load asset, false if skipped.
        """
        vType = vA["type"].replace("HDRS", "HDRIs")

        if vType == "Substances":
            return False

        with self.lock_assets:
            if vType not in self.vAssets[vArea].keys():
                self.vAssets[vArea][vType] = {}

        vName = vA["asset_name"]
        asset_id = vA["id"]

        if vArea == "my_assets" and vName not in self.vPurchased:
            self.vPurchased.append(vName)

        # TODO(SOFT-539): Turn this into a dataclass structure to avoid keying.
        asset_data = {}
        asset_data["name"] = vName
        asset_data["name_beauty"] = vA["name"]
        asset_data["id"] = asset_id
        asset_data["slug"] = vA["slug"]
        asset_data["type"] = vType
        asset_data["files"] = []
        asset_data["maps"] = []
        asset_data["lods"] = []
        asset_data["sizes"] = []
        asset_data["workflows"] = []
        asset_data["vars"] = []
        asset_data["date"] = vA["published_at"]
        asset_data["credits"] = vA["credit"]
        asset_data["categories"] = vA["categories"]
        asset_data["preview"] = ""
        asset_data["thumbnails"] = []
        asset_data["quick_preview"] = vA["toolbox_previews"]
        asset_data["in_asset_browser"] = False
        asset_data["url"] = vA.get("url", None)

        try:
            api_convention = int(vA.get("convention", 0))
        except ValueError:
            api_convention = 0
        asset_data["api_convention"] = api_convention
        asset_data["local_convention"] = None  # to be determined in build_local_asset_data
        if api_convention > SUPPORTED_CONVENTION:
            self.unsupported_assets_exist = True  # could be used for notification

        if "lods" in vA.keys():
            asset_data["lods"] = [lod for lod in vA["lods"] if lod != "SOURCE"]

        if len(vA["cloudflare_previews"]):
            # Primary thumbnail previews
            cf_details = vA["cloudflare_previews"][0]
            # Size used later via string formatting in downloader.
            asset_data["preview"] = cf_details["base_url"] + "/{size}px"
            # Additional previews
            valid = [cf_details["base_url"] + "/{size}px"
                     for x in vA["cloudflare_previews"]]
            asset_data["thumbnails"] = valid

        # Asset-type based loading.
        if vType in ["Textures", "HDRIs", "Brushes"]:
            # Identify workflow types and sizes available.
            if api_convention == 0:
                all_sizes = []
                if "render_schema" in vA.keys():
                    for schema in vA["render_schema"]:
                        # Set workflow type
                        workflow = schema.get('name', 'REGULAR')
                        if workflow not in asset_data["workflows"]:
                            asset_data["workflows"].append(workflow)

                        # A single 'type' is a dict of a single map, such as:
                        # {
                        #    "type_code": "COL",  # COL here even if 'SPECULAR_COL'
                        #    "type_name": "Diffuse",
                        #    "type_preview": "diffuse.jpg",
                        #    "type_options": ["1K", "2K", "3K", "4K"]
                        # }
                        if "types" in schema.keys():
                            for vM in schema["types"]:
                                all_sizes.extend(vM["type_options"])
                all_sizes = list(set(all_sizes))
                asset_data["sizes"] = all_sizes

                # Workflow partitioned map names, e.g. "SPECULAR_COL"
                asset_data["maps"] = vA.get("type_options")
            elif api_convention == 1:
                asset_data["workflows"] = ["METALNESS"]
                asset_data["maps"] = vA.get("maps", [])
                asset_data["sizes"] = vA.get("resolutions", [])

        elif vType == "Models":

            asset_data["workflows"] = ["METALNESS"]

            all_sizes = vA["render_schema"].get("options", [])

            # Some models will not be having any "additional sizes", and thus
            # "type_options" won't list more sizes. Hence we also include
            # the default resolution as part of the size listing.
            if "render_custom_schema" in vA.keys():
                incl_size = vA["render_custom_schema"].get("included_resolution")
                if incl_size is not None and incl_size in SIZES:
                    all_sizes.extend(incl_size)
                    all_sizes = list(set(all_sizes))
            else:
                # We probably should report if this branch happens,
                # as included resolution should always be present.
                reporting.capture_message(
                    "no_included_resolution", f"{vName} - {asset_id}", "info")

            asset_data["sizes"] = all_sizes

        # Cleanup processing.
        sorted_sizes = [vS for vS in SIZES if vS in asset_data["sizes"]]
        if not sorted_sizes:
            # Keep the same sizes as they will exist online, but un-sorted.
            self.print_debug(0, "Invalid sizes found", asset_data["sizes"])
            # Disabling this as volume can be large, given number of times
            # already seen during UAT.
            # reporting.capture_message(
            #     "asset_size_empty",
            #     asset_data["sizes"],
            #     "error")
        else:
            asset_data["sizes"] = sorted_sizes

        with self.lock_assets:
            self.vAssets[vArea][vType][vName] = asset_data

        with self.lock_asset_index:
            # NOTE: This "if" is a bandaid for an underlying threading issue.
            #       here the callchain is:
            #       - f_APIGetAssets() prepares self.vAssetsIndex
            #         - load_asset(), here
            #       But there is:
            #       - _refresh_data_thread(), also executed in a thread and
            #          wiping the content of self.vAssetsIndex
            #       If these threads meet at the right point, we experience
            #       a key error here.
            #       Not sure, how we would want to solve this without locks
            #       protecting entire threads.
            #       In unit tests there's the additional problem of different
            #       toolbox instances being "registered" over annd over again,
            #       which can also lead tt an issue here.
            # Beware: Trying to fix it, by creating the key with an empty dict,
            #         causes issues in other places.
            if vKey in self.vAssetsIndex[vArea].keys():
                self.vAssetsIndex[vArea][vKey][vIdx] = [vType, vName]

        if vArea == cTB.vSettings["area"]:
            self.enqueue_thumb_prefetch(vName)

        return True  # Indicates structure was loaded.

    # @timer
    def f_GetPageAssets(self, vPage):
        dbg = 0
        self.print_separator(dbg, "f_GetPageAssets")

        vArea = self.vSettings["area"]

        vSearch = self.vSearch[vArea]

        vMax = self.vSettings["page"]

        vPageAssets = []
        vPageCount = 0
        with self.lock_asset_index:
            types_and_assets_per_page = []
            if vArea not in self.vAssetsIndex.keys():
                return [vPageAssets, vPageCount]

            vKey = "/".join([vArea] + self.vSettings['category'][vArea])
            if vSearch != "":
                vKey = "@".join([vArea] + self.vSettings['category'][vArea] + [vSearch])

            self.print_debug(dbg, "f_GetPageAssets", vKey)

            if vKey not in self.vAssetsIndex[vArea].keys():
                return [vPageAssets, vPageCount]

            for i in range(vPage * vMax, (vPage * vMax) + vMax):
                if i in self.vAssetsIndex[vArea][vKey].keys():
                    vType, vAsset = self.vAssetsIndex[vArea][vKey][i]

                    types_and_assets_per_page.append((vType, vAsset))

            vPageCount = self.vAssetsIndex[vArea][vKey]['pages']

        occured_errors = []
        with self.lock_assets:
            for vType, vAsset in types_and_assets_per_page:
                try:
                    vPageAssets.append(self.vAssets[vArea][vType][vAsset])
                except KeyError as err:
                    occured_errors.append((vType, vAsset, err))

        for (vType, vAsset, err) in occured_errors:
            msg = f"Failed to vPageAssets.append, asset not found: {vType} {vAsset}"
            print(msg)
            print(err)
            reporting.capture_message("page-assets-error", msg, "info")

        return [vPageAssets, vPageCount]

    # @timer
    def f_GetAssetsSorted(self, vPage):
        dbg = 0
        self.print_separator(dbg, "f_GetAssetsSorted")

        vArea = self.vSettings["area"]
        vSearch = self.vSearch[vArea]

        if vArea in ["poliigon", "my_assets"]:
            vPageAssets, vPageCount = self.f_GetPageAssets(vPage)
            if len(vPageAssets):
                self.vPages[vArea] = vPageCount
                return vPageAssets

            if self.vGettingData:
                self.print_debug(dbg, "f_GetAssetsSorted", "f_DummyAssets")
                return self.f_DummyAssets()

            else:
                self.print_debug(dbg, "f_GetAssetsSorted", "[]")
                return []

        else:
            vAssetType = self.vSettings["category"]["imported"][0]

            vSortedAssets = []
            for vType in self.imported_assets.keys():
                if vAssetType in ["All Assets", vType]:
                    for vA in self.imported_assets[vType].keys():
                        if (
                            len(vSearch) >= 3
                            and vSearch.lower() not in vA.lower()
                        ):
                            continue

                        with self.lock_assets:
                            if vType in self.vAssets["local"].keys():
                                if vA in self.vAssets["local"][vType].keys():
                                    vSortedAssets.append(self.vAssets["local"][vType][vA])

            self.vPages[vArea] = int(
                (len(vSortedAssets) / self.vSettings["page"]) + 0.99999
            )

            return vSortedAssets

    def get_poliigon_asset(self, vType, vAsset):
        """Get the data for a single explicit asset of a given type."""
        with self.lock_assets:
            if vType not in self.vAssets["poliigon"]:
                self.print_debug(0, f"Was missing {vType}, populated now")
                self.vAssets["poliigon"][vType] = {}

            asset_missing = False
            if vAsset not in self.vAssets["poliigon"][vType]:
                asset_missing = True

        if asset_missing:
            # Handle a given datapoint being missing at moment of request
            # and fetch it.
            # raise Exception("Asset is not avaialble")

            # This is the exception, not the norm, and should be trated as a
            # warning. This would mostly occur when there is a cache miss if
            # an operator is called for an arbitrary asset from an automated
            # script and not from within the normal use of the plugin.
            self.print_debug(
                0,
                "get_poliigon_asset",
                f"Had to fetch asset info for {vAsset}")
            vArea = "poliigon"
            vSearch = vAsset
            vKey = "@".join([vArea] + self.vSettings['category'][vArea] + [vSearch])

            vPage = 0
            vMax = 100
            force = False  # More to do with multi=page requests.
            self.f_APIGetAssets(
                vArea, vPage, vMax, vSearch, vKey, 0, time.monotonic(), force)

            with self.lock_assets:
                if not self.vAssets["poliigon"][vType].get(vAsset):
                    raise RuntimeError("Failed to fetch asset information")
                else:
                    # Report this cache miss, as generally shouln't happen.
                    reporting.capture_message(
                        "get_asset_miss", vAsset, "error")

        with self.lock_assets:
            asset_data = self.vAssets["poliigon"][vType].get(vAsset)

        return asset_data

    def get_data_for_asset_id(self, asset_id):
        """Get the data structure for an asset by asset_id alone."""
        area_order = ["poliigon", "my_assets", "local"]
        for area in area_order:
            with self.lock_assets:
                subcats = list(self.vAssets[area])
                for cat in subcats:  # e.g. HDRIs
                    for asset_data in self.vAssets[area][cat].values():
                        if asset_data.get("id") == asset_id:
                            return asset_data

        # Failed to fetch asset, return empty structure.
        return {}

    def get_data_for_asset_name(self,
                                asset_name: str,
                                *,
                                area_order: List[str] = ["poliigon",
                                                         "my_assets",
                                                         "local"]
                                ) -> Dict:
        """Get the data structure for an asset by asset_name alone."""

        for area in area_order:
            with self.lock_assets:
                subcats = list(self.vAssets[area])
                for cat in subcats:
                    for asset in self.vAssets[area][cat]:
                        if asset == asset_name:
                            return self.vAssets[area][cat][asset]

        # Failed to fetch asset, return empty structure.
        return {}

    def f_DummyAssets(self):
        dbg = 0
        self.print_separator(dbg, "f_DummyAssets")

        vDummyAssets = []

        vDummy = {}
        vDummy["name"] = "dummy"
        vDummy["name_beauty"] = "dummy"
        vDummy["slug"] = ""
        vDummy["type"] = ""
        vDummy["files"] = []
        vDummy["maps"] = []
        vDummy["lods"] = []
        vDummy["sizes"] = []
        vDummy["vars"] = []
        vDummy["date"] = ""
        vDummy["credits"] = 0
        vDummy["categories"] = []
        vDummy["preview"] = ""
        vDummy["thumbnails"] = []

        for i in range(self.vSettings["page"]):
            vDummyAssets.append(vDummy)

        return vDummyAssets

    # TODO(Andreas): Function not in use
    def f_UpdateData(self):
        dbg = 0
        self.print_separator(dbg, "f_UpdateData")

        vDFile = self.gSettingsDir + "/Poliigon_Data.ini"

        vConfig = ConfigParser.ConfigParser()
        vConfig.optionxform = str
        if f_Ex(vDFile):
            vConfig.read(vDFile)

        vArea = "my_assets"

        with self.lock_assets:
            if vArea in self.vAssets.keys():
                for vType in self.vAssets[vArea].keys():
                    for vAsset in self.vAssets[vArea][vType].keys():
                        if not vConfig.has_section(vAsset):
                            vConfig.add_section(vAsset)

                        vConfig.set(vAsset, "id", self.vAssets[vArea][vType][vAsset]["id"])
                        vConfig.set(
                            vAsset, "type", self.vAssets[vArea][vType][vAsset]["type"]
                        )
                        vConfig.set(
                            vAsset, "date", self.vAssets[vArea][vType][vAsset]["date"]
                        )
                        vConfig.set(
                            vAsset,
                            "categories",
                            ";".join(self.vAssets[vArea][vType][vAsset]["date"]),
                        )

        with open(vDFile, "w+") as vFile:
            vConfig.write(vFile)

    # .........................................................................

    def _set_free_user(self,
                       force_unknown: bool = False,
                       force_paying_user: bool = False):
        no_credits = self.vUser["credits"] == 0
        no_credits_od = self.vUser["credits_od"] == 0
        missing_info = not self.credits_info_received
        missing_info |= not self.subscription_info_received

        if force_unknown:
            self.vUser["is_free_user"] = None
        elif force_paying_user:
            self.vUser["is_free_user"] = 0
        elif missing_info:
            self.vUser["is_free_user"] = None
        elif no_credits and no_credits_od:
            self.vUser["is_free_user"] = 1
        else:
            self.vUser["is_free_user"] = 0
        self.set_free_search()

    def f_GetCredits(self):
        dbg = 0
        self.print_separator(dbg, "f_GetCredits")

        vThread = threading.Thread(target=self.f_APIGetCredits)
        vThread.daemon = 1
        vThread.start()
        self.vThreads.append(vThread)

    @reporting.handle_function(silent=True)
    def f_APIGetCredits(self):
        dbg = 0
        self.print_separator(dbg, "f_APIGetCredits")

        vReq = self._api.get_user_balance()

        if vReq.ok:
            self.credits_info_received = True
            self.vUser["credits"] = vReq.body.get("subscription_balance")
            self.vUser["credits_od"] = vReq.body.get("ondemand_balance")
            # Here again, we can not finally decide if it's a free user.
            # User may have no credits at all, but still be subscribed,
            # which we may not know about, yet.
        else:
            self.credits_info_received = False
            self.print_debug(dbg, "f_APIGetCredits", "ERROR", vReq.error)
        self._set_free_user()

    # .........................................................................

    def f_GetUserInfo(self):
        dbg = 0
        self.print_separator(dbg, "f_GetUserInfo")

        vThread = threading.Thread(target=self.f_APIGetUserInfo)
        vThread.daemon = 1
        vThread.start()
        self.vThreads.append(vThread)

    @reporting.handle_function(silent=True)
    def f_APIGetUserInfo(self):
        dbg = 0
        self.print_separator(dbg, "f_APIGetUserInfo")

        vReq = self._api.get_user_info()

        if vReq.ok:
            self.vUser["name"] = vReq.body.get("user")["name"]
            self.vUser["id"] = vReq.body.get("user")["id"]
        else:
            self.print_debug(dbg, "f_APIGetUserInfo", "ERROR", vReq.error)

    # .........................................................................

    def f_GetSubscriptionDetails(self):
        dbg = 0
        self.print_separator(dbg, "f_GetSubscriptionDetails")

        vThread = threading.Thread(target=self.f_APIGetSubscriptionDetails)
        vThread.daemon = 1
        vThread.start()
        self.vThreads.append(vThread)

    @reporting.handle_function(silent=True)
    def f_APIGetSubscriptionDetails(self):
        """Fetches the current user's subscription status."""
        dbg = 0
        self.print_separator(dbg, "f_APIGetSubscriptionDetails")

        vReq = self._api.get_subscription_details()

        if vReq.ok:
            self.subscription_info_received = True
            force_paying_user = False
            plan = vReq.body
            if plan.get("plan_name") and plan["plan_name"] != api.STR_NO_PLAN:
                self.vUser["plan_name"] = plan["plan_name"]
                self.vUser["plan_credit"] = plan.get("plan_credit", None)

                # Extract "2022-08-19" from "2022-08-19 23:58:37"
                renew = plan.get("next_subscription_renewal_date", "")
                if renew is None:
                    renew = ""
                renew = renew.split(" ")[0]
                self.vUser["plan_next_renew"] = renew

                next_credits = plan.get("next_credit_renewal_date", "")
                if next_credits is not None:
                    next_credits = next_credits.split(" ")[0]
                self.vUser["plan_next_credits"] = next_credits
                # Here we are sure: sub == paying user
                # (regardless of any credits)
                force_paying_user = True
            else:
                self.vUser["plan_name"] = None
                self.vUser["plan_credit"] = None
                self.vUser["plan_next_renew"] = None
                self.vUser["plan_next_credits"] = None
                # Here we can not decide if it is a free user.
                # User may have on demand credits,
                # which we may not know about, yet.

            if "paused_info" in plan:
                paused_info = plan.get("paused_info", {})
                if paused_info is not None:
                    self.vUser["plan_paused"] = True
                else:
                    self.vUser["plan_paused"] = False
                    paused_info = {}
                self.vUser["plan_paused_at"] = paused_info.get("pause_date",
                                                               "")
                self.vUser["plan_paused_until"] = paused_info.get("resume_date",
                                                                  "")
            else:
                self.vUser["plan_paused"] = False
                self.vUser["plan_paused_at"] = ""
                self.vUser["plan_paused_until"] = ""

            self._set_free_user(force_paying_user=force_paying_user)
            self.f_SaveSettings()
        else:
            self.subscription_info_received = False
            self.vUser["plan_name"] = None
            self.vUser["plan_credit"] = None
            self.vUser["plan_next_renew"] = None
            self.vUser["plan_next_credits"] = None
            self._set_free_user(force_unknown=True)
            self.print_debug(
                dbg, "f_APIGetSubscriptionDetails", "ERROR", vReq.error)

    # .........................................................................

    def f_QueuePreview(self, vAsset, thumbnail_index=0):
        dbg = 0
        self.print_separator(dbg, "f_QueuePreview")

        fut = self.f_DownloadPreview(vAsset, thumbnail_index)
        cleanup_partial = partial(cleanup_future, vAsset)
        fut.add_done_callback(cleanup_partial)
        with self.lock_thumb_download_futures:
            self.thumb_download_futures.append((fut, vAsset))

    @run_threaded(tm.PoolKeys.PREVIEW_DL, MAX_THUMBH_THREADS)
    @reporting.handle_function(silent=True)
    def f_DownloadPreview(self, vAsset, thumbnail_index):
        """Download a single thumbnail preview for a single asset."""
        dbg = 0
        self.print_separator(dbg, "f_DownloadPreview")

        if self.vSettings["area"] not in ["poliigon", "my_assets"]:
            return

        already_local = 0
        target_file = self.f_GetThumbnailPath(vAsset, thumbnail_index)
        target_base, target_ext = os.path.splitext(target_file)

        # Check if a partial or complete download already exists.
        for vExt in [".jpg", ".png", "X.jpg", "X.png"]:
            f_MDir(self.gOnlinePreviews)

            vQPrev = os.path.join(self.gOnlinePreviews, target_base + vExt)
            if f_Ex(vQPrev):
                self.print_debug(dbg, "f_DownloadPreview", vQPrev)
                if "X" in vExt:
                    try:
                        os.rename(vQPrev, vQPrev.replace("X.jpg", ".jpg"))
                    except Exception:
                        os.remove(vQPrev)

                already_local = 1
                break

        if already_local:
            return

        # .....................................................................

        # Download to a temp filename.
        vPrev = os.path.join(self.gOnlinePreviews,
                             target_base + "X" + target_ext)

        vURL = None
        with self.lock_assets:
            for vType in self.vAssets[self.vSettings["area"]]:
                # One of Models, HDRIs, Textures.
                if vAsset in self.vAssets[self.vSettings["area"]][vType]:
                    # This specific combo, width=300, sharpen=1, and q=75 will
                    # ensure we make use of the same caching as the website.
                    if thumbnail_index == 0:
                        base_url = self.vAssets[self.vSettings["area"]][vType][
                            vAsset]["preview"]
                        vURL = base_url.format(size=300)
                    else:
                        base_url = self.vAssets[self.vSettings["area"]][vType][
                            vAsset]["thumbnails"][thumbnail_index - 1]
                        vURL = base_url.format(size=900)
                    break

        if vURL:
            self.print_debug(dbg, "f_DownloadPreview", vPrev, vURL)

            resp = self._api.download_preview(vURL, vPrev, vAsset)
            if resp.ok:
                if f_Ex(vPrev):
                    if vPrev.endswith("X.png"):
                        try:
                            os.rename(vPrev, vPrev.replace("X.png", ".png"))
                        except Exception:
                            pass
                    else:
                        try:
                            os.rename(vPrev, vPrev.replace("X.jpg", ".jpg"))
                        except Exception:
                            pass

            else:
                print(f"Encountered preview download error: {len(resp.error)}")
        else:
            reporting.capture_message(
                "download_preview_error",
                f"Failed to find preview url for {vAsset}",
                "error")

        with self.lock_previews:
            # Always remove from download queue (can have thread conflicts, so try)
            try:
                if vAsset in self.vPreviewsDownloading:
                    self.vPreviewsDownloading.remove(vAsset)
            except ValueError:  # Already removed.
                pass

    # .........................................................................

    def check_if_purchase_queued(self, asset_id):
        """Checks if an asset is queued for purchase"""
        queued = asset_id in list(self.vPurchaseQueue.keys())
        return queued

    def queue_purchase(self, asset_id, asset_data, start_thread=True):
        """Adds an asset to the purchase_queue and starts threads"""
        self.vPurchaseQueue[asset_id] = asset_data
        self.purchase_queue.put(asset_id)
        self.print_debug(0, f"Queued asset {asset_id}")

        self.purchase_threads = [
            thread for thread in self.purchase_threads if thread.is_alive()]

        if start_thread and len(self.purchase_threads) < MAX_PURCHASE_THREADS:
            thread = threading.Thread(target=self.purchase_assets_thread)
            thread.daemon = 1
            thread.start()
            self.purchase_threads.append(thread)

    @reporting.handle_function(silent=True)
    def purchase_assets_thread(self):
        """Thread to purchase queue of assets"""
        while self.purchase_queue.qsize() > 0:
            try:
                asset_id = int(self.purchase_queue.get_nowait())
            except queue.Empty:
                time.sleep(0.1)
                continue

            if not self.vRunning:
                print("Cancelling in progress purchases")
                return

            asset_data = self.vPurchaseQueue[asset_id]

            asset = asset_data['name']

            # Metadata required to pass forward
            wm_props = bpy.context.window_manager.poliigon_props
            search = wm_props.search_poliigon.lower()

            # Get the slug format of the active category, e.g.
            # from ["All Models"] to "/"
            # from ["Models", "Bathroom"] to "/models/bathroom"
            # and undo transforms of f_GetCategoryChildren.
            # TODO(related to SOFT-762 and SOFT-598):
            #      Refactor f_GetCategoryChildren as part of Core migration.
            category = "/" + "/".join(
                [cat.lower().replace(" ", "-") for cat in self.vActiveCat]
            )
            if category.startswith("/hdris/"):
                category = category.replace("/hdris/", "/hdrs/")
            elif category == "/all-assets":
                category = "/"
            self.print_debug(0, "Active cat: ", self.vActiveCat, category)

            req = self._api.purchase_asset(asset_id, search, category)
            del self.vPurchaseQueue[asset_id]  # Remove regardless, for ui draw

            if req.ok:
                # Append purchased if success, or if the asset is free.
                self.vPurchased.append(asset)
                with self.lock_assets:
                    self.vAssets["my_assets"][asset_data["type"]][asset] = asset_data

                # Process auto download if setting enabled.
                if self.vSettings["auto_download"]:
                    download_dict = {
                        "data": asset_data,
                        "size": None,
                        "download_size": None
                    }

                    with self.lock_download:
                        self.vDownloadQueue[asset_id] = download_dict

                    fut = self.download_asset_thread(asset_id)
                    download_dict["future"] = fut
            else:
                self.print_debug(
                    0, f"Failed to purchase asset {asset_id} {asset}",
                    str(req.error), str(req.body))

                # Check the reason for failure.
                if "enough credits" in req.error:
                    if self.vUser["is_free_user"] == 1:
                        button_label = "Low balance"
                        description = (
                            "Your Asset balance is empty. Start a Poliigon\n"
                            "subscription and start downloading assets")
                    else:
                        button_label = "Low balance"
                        description = (
                            "Your asset balance is empty. Upgrade\n"
                            "your plan to purchase more assets")

                    ui_err = DisplayError(
                        asset_id=asset_id,
                        asset_name=asset,
                        button_label=button_label,
                        description=description,
                        goto_account=True  # Go to account instead of dl retry
                    )
                else:
                    ui_err = DisplayError(
                        asset_id=asset_id,
                        asset_name=asset,
                        button_label="Failed, retry",
                        description=f"Error during purchase, please try again\n{req.error}"
                    )
                self.ui_errors.append(ui_err)

            # Clear cached data in index to prompt refresh after purchase
            with self.lock_asset_index:
                self.vAssetsIndex["my_assets"] = {}

            # Runs in this same thread, and if there are many purchase
            # events then there may be multiple executions of this. It is
            # important that the last purchase always does update the
            # credits balance, so this tradeoff is ok to have overlapping
            # requests potentially.
            self.f_APIGetCredits()
            self.vRedraw = 1
            self.refresh_ui()

    # .........................................................................

    def refresh_data(self, icons_only=False):
        """Reload data structures of the addon to update UI and stale data.

        This function could be called in main or background thread.
        """
        self.print_debug(0, "refresh_data")
        thread = threading.Thread(
            target=self._refresh_data_thread,
            args=(icons_only,))
        thread.daemon = 1
        thread.start()
        self.vThreads.append(thread)

    @reporting.handle_function(silent=True)
    def _refresh_data_thread(self, icons_only):
        """Background thread for the data resets."""

        self.ui_errors.clear()

        # Clear out state variables.
        with self.lock_previews:
            self.vPreviews.clear()

        if icons_only is False:
            self.notifications = []
            self.vPurchased = []

            with self.lock_asset_index:
                self.vAssetsIndex["poliigon"] = {}
                self.vAssetsIndex["my_assets"] = {}

        # Non-background thread requests
        self.vGettingData = 1
        self.f_GetAssets(
            "my_assets", vMax=MAX_MY_ASSETS_REQUEST_SIZE, vBackground=1, force=True)  # Populates vPurchased.
        self.f_GetAssets(vBackground=1)
        if icons_only is False:
            self.f_APIGetCategories()
            self.f_GetLocalAssetsThread()
        self.vGettingData = 0

        if icons_only is False:
            self.f_APIGetCredits()
            self.f_APIGetUserInfo()
            self.f_GetSubscriptionDetails()

        self.last_texture_size = {}

        self.vRedraw = 1
        self.refresh_ui()

    def check_if_download_queued(self, asset_id):
        """Checks if an asset is queued for download"""

        with self.lock_download:
            cancelled = asset_id in self.vDownloadCancelled
            queued = asset_id in self.vDownloadQueue
        return queued and not cancelled

    def get_maps_by_workflow(self, maps, workflow):
        """Download only relevant maps.

        Where `workflow` should be one of: REGULAR, SPECULAR, METALNESS.
        """

        # Some maps in API belong only to a single workflow, even though
        # they are the same for both.
        force_dl = ["IDMAP"]

        # Each map should be in the form of "WORKFLOW_MAPNAME".
        target_maps = [
            m.split("_", maxsplit=1)[-1] for m in maps
            if m.startswith(workflow) or m.split("_", 1)[-1] in force_dl]
        return list(set(target_maps))

    def check_need_hdri_sizes(self,
                              asset_data: Dict,
                              size_exr: str,
                              size_jpg: str) -> Tuple[bool, bool]:
        """Determines if the download request should include exr, jpg or both.

        NOTE: In preferences it is not possible to configure the same size
              for light and background. And quick menu allows to download
              specific light texture sizes, only.
              Furthermore download option is given only, if files are not
              already locally available.

        Return value:
        Tuple of two bools, one of them is _guruanteed_ to be True:
        Tuple[0]: True, if exr is needed
        Tuple[1]: True, if jpg is needed
        """

        if not self.vSettings["hdri_use_jpg_bg"]:
            # Old behavior, exr size is needed.
            # We should not be here, if the exr is already local.
            return True, False

        if size_exr == size_jpg:
            # There's no reason to download the jpg
            return True, False

        need_exr = True
        need_jpg = True
        for path_asset in asset_data["files"]:
            filename = os.path.basename(path_asset)
            is_exr = filename.lower().endswith(".exr")
            is_jpg = filename.lower().endswith(".jpg")
            is_jpg &= "_JPG" in filename

            if is_exr and size_exr in filename:
                need_exr = False
            elif is_jpg and size_jpg in filename:
                need_jpg = False
        if not need_exr and not need_jpg:
            # we should not be here, fallback old behavior
            need_exr = True
        return need_exr, need_jpg

    def get_download_data(self, asset_data: Dict, size=None) -> Dict:
        """Construct the data needed for the download.

        Args:
            asset_data: Original asset data structure.
            size: Intended download size like '4K', fallback to pref default.
        """

        api_convention = asset_data["api_convention"]

        sizes = [size]

        if size in ["", None]:
            if asset_data["type"] == "Textures":
                sizes = [self.vSettings["res"]]
            elif asset_data["type"] == "Models":
                sizes = [self.vSettings["mres"]]
            elif asset_data["type"] == "HDRIs":
                need_exr, need_jpg = self.check_need_hdri_sizes(asset_data,
                                                                self.vSettings["hdri"],
                                                                self.vSettings["hdrib"])
                if need_exr and need_jpg:
                    sizes = [self.vSettings["hdri"], self.vSettings["hdrib"]]
                elif need_exr:
                    sizes = [self.vSettings["hdri"]]
                elif need_jpg:
                    sizes = [self.vSettings["hdrib"]]

            elif asset_data["type"] == "Brushes":
                sizes = [self.vSettings["brush"]]

            with self.lock_download:
                self.vDownloadQueue[asset_data["id"]]["size"] = sizes[0]

        elif asset_data["type"] == "HDRIs":
            need_exr, need_jpg = self.check_need_hdri_sizes(asset_data,
                                                            size,
                                                            self.vSettings["hdrib"])
            if not need_exr and need_jpg:
                sizes = [self.vSettings["hdrib"]]
            elif need_jpg:
                sizes.append(self.vSettings["hdrib"])

        download_data = {
            "assets": [
                {
                    "id": asset_data["id"],
                    "name": asset_data["name"]
                }
            ]
        }

        if asset_data["type"] in ["Textures", "HDRIs"]:
            if api_convention == 0:
                asset_workflows = asset_data["workflows"]
                if "METALNESS" in asset_workflows:
                    download_workflows = ["METALNESS"]
                elif "REGULAR" in asset_workflows:
                    download_workflows = ["REGULAR"]
                elif "SPECULAR" in asset_workflows:
                    download_workflows = ["SPECULAR"]
                else:
                    download_workflows = []
                download_data["assets"][0]["workflows"] = download_workflows

                maps = self.get_maps_by_workflow(
                    asset_data["maps"],
                    download_workflows[0])

                download_data["assets"][0]["type_codes"] = maps
            elif api_convention == 1:
                map_list = []
                for _map_dict in asset_data["maps"]:
                    file_format = "UNKNOWN"
                    map_type = _map_dict["type"]
                    for _ff in SUPPORTED_TEX_FORMATS:
                        if _ff in _map_dict["file_formats"]:
                            file_format = _ff
                            break
                    if file_format == "UNKNOWN":
                        msg = (f"UNKNWOWN file format for download; "
                               f"Asset Id: {asset_data.asset_id} Map: {map_type}")
                        self._api.report_message(
                            "download_invalid_format", msg, "error")
                        self.print_debug(0, msg)

                    map_dict = {
                        "type": map_type,
                        "format": file_format
                    }
                    map_list.append(map_dict)

                download_data["assets"][0]["maps"] = map_list

        elif asset_data["type"] == "Models":
            download_data["assets"][0]["lods"] = int(
                self.vSettings["download_lods"])

            if self.vSettings["download_prefer_blend"]:
                download_data["assets"][0]["softwares"] = ["Blender"]
                download_data["assets"][0]["renders"] = ["Cycles"]
            else:
                download_data["assets"][0]["softwares"] = ["ALL_OTHERS"]

        elif asset_data["type"] == "Brushes":
            # No special data needed for Brushes
            pass

        download_sizes = [
            _size for _size in sizes if _size in asset_data["sizes"]]
        if api_convention == 0:
            download_data["assets"][0]["sizes"] = download_sizes
            if len(download_data["assets"][0]["sizes"]) == 0:
                for _size in reversed(SIZES):
                    if _size in asset_data["sizes"]:
                        download_data["assets"][0]["sizes"] = [_size]
                        break
            if not download_data["assets"][0]["sizes"]:
                self.print_debug(0, "Missing sizes for download", download_data)
        elif api_convention == 1:
            download_data["assets"][0]["resolution"] = download_sizes[0]

        return download_data

    def store_last_downloaded_size(self,
                                   asset_name: str,
                                   asset_type: str,
                                   size: str
                                   ) -> None:
        if asset_type == "Brushes":
            size_pref = self.vSettings["brush"]
        elif asset_type == "HDRIs":
            size_pref = self.vSettings["hdri"]
        elif asset_type == "Models":
            size_pref = self.vSettings["mres"]
        elif asset_type == "Textures":
            size_pref = self.vSettings["res"]

        if size != size_pref and size is not None:
            self.last_texture_size[asset_name] = size
        elif asset_name in self.last_texture_size:
            del self.last_texture_size[asset_name]

    def get_last_downloaded_size(self,
                                 asset_name: str,
                                 size_default: str
                                 ) -> str:
        return self.last_texture_size.get(asset_name, size_default)

    def forget_last_downloaded_size(self,
                                    asset_name: str
                                    ) -> None:
        if asset_name in self.last_texture_size:
            del self.last_texture_size[asset_name]

    def get_destination_library_directory(self,
                                          asset_data: Dict
                                          ) -> Tuple[str,
                                                     List[str],
                                                     List[str]]:
        # Usually the asset will be downloaded into the primary library.
        # Exception: There are already files for this asset located in another
        #            library (and only in this, _not_ in primary).
        dbg = 0
        self.print_debug(dbg, "get_destination_library_directory")
        asset_name = asset_data["name"]
        asset_type = asset_data["type"]

        library_dir = self.vSettings["library"]  # primary library
        primary_files = []
        add_files = []
        with self.lock_assets:
            if asset_name not in self.vAssets["local"][asset_type].keys():
                return library_dir, primary_files, add_files

            for file in self.vAssets["local"][asset_type][asset_name]["files"]:
                if not f_Ex(file):
                    continue
                if file.split(asset_name, 1)[0] == library_dir:
                    primary_files.append(file)
                else:
                    add_files.append(file)

        self.print_debug(dbg, "get_destination_library_directory",
                         "Found asset files in primary library:",
                         primary_files)

        if len(primary_files) == 0 and len(add_files) > 0:
            # Asset must be located in an additional directory
            #
            # Always download new maps to the highest-level directory
            # containing asset name, regardless of any existing (sub)
            # structure within that directory
            file = add_files[0]
            if asset_name in os.path.dirname(file):
                library_dir = file.split(asset_name, 1)[0]
                self.print_debug(dbg,
                                 "get_destination_library_directory",
                                 library_dir)

        self.print_debug(dbg, "get_destination_library_directory DONE")
        return library_dir, primary_files, add_files

    def get_download_list(self,
                          asset_id: int,
                          download_data: Dict,
                          is_retry: bool,
                          convention: int
                          ) -> Tuple[List[api.FileDownload],
                                     str,
                                     api.AssetDownload]:
        dbg = 0
        self.print_debug(dbg, "get_download_list")
        dl_list = []
        # Fake AssetDownload, will be filled for dynamic file purposed later on
        asset_download = api.AssetDownload(asset_data=None)
        uuid = ""
        res = self._api.download_asset_get_urls(asset_id,
                                                download_data,
                                                convention=convention,
                                                is_retry=is_retry)
        cf_ray = res.body.get("CF-RAY", "")
        if res.ok:
            dl_list = res.body.get("downloads", [])

            dynamic_files_resp = res.body.get("dynamic_files", [])
            self._api.get_dynamic_files(asset_download, dynamic_files_resp)

            uuid = res.body.get("uuid", "")
            if uuid == "":
                msg = (f"{asset_id}: No UUID."
                       f"\nCF-RAY: {cf_ray}")
                reporting.capture_message(
                    "download_asset_no_uuid", msg, "error")
                self.print_debug(dbg, "get_download_list No UUID")
            if len(dl_list) == 0:
                msg = (f"{asset_id}: Empty download list."
                       f"\nCF-RAY: {cf_ray}")
                reporting.capture_message(
                    "download_asset_empty_download_list", msg, "error")
                self.print_debug(
                    dbg,
                    "get_download_list Empty download list despite success")
        else:
            msg = (f"{asset_id}: Failed to get download list."
                   f"\nCF-RAY: {cf_ray}",
                   f"\n{res.error}")
            reporting.capture_message(
                "download_asset_error_download_list", msg, "error")
            # Error is handled outside, including retries
            self.print_debug(dbg, "get_download_list URL retrieve error")

        self.print_debug(dbg, "get_download_list DONE")
        return dl_list, uuid, asset_download

    def calc_asset_size_bytes(self, dl_list: List[api.FileDownload]) -> int:
        size_asset = 0
        for download in dl_list:
            size_asset += download.size_expected
        return size_asset

    def avoid_specular_textures(self,
                                dl_list: List[api.FileDownload]
                                ) -> List[api.FileDownload]:
        dl_list_non_specular = [dl
                                for dl in dl_list
                                if "_specular" not in dl.filename.lower()]
        return dl_list_non_specular

    def schedule_downloads(self,
                           tpe: ThreadPoolExecutor,
                           dl_list: List[api.FileDownload],
                           directory: str
                           ) -> None:
        dbg = 0
        self.print_debug(dbg, "schedule_downloads")
        dl_list.sort(key=lambda dl: dl.size_expected)

        for download in dl_list:
            download.directory = directory
            # Note: We could also check here, if already DONE and not start
            # the thread at all.
            # Yet, it was decided to prefer it handled by the download thread
            # itself. In this way the code flow is always identical.
            download.status = api.DownloadStatus.WAITING
            self.print_debug(
                dbg, "schedule_downloads SUBMIT", download.filename)
            download.fut = tpe.submit(self._api.download_asset_file,
                                      download=download)
            download.fut.add_done_callback(print_exc)
        self.print_debug(dbg, "schedule_downloads DONE")

    def append_ui_error(self, ui_err: DisplayError) -> None:
        error_exists = False
        for error in self.ui_errors:
            if error.asset_id != ui_err.asset_id:
                continue
            if error.button_label != ui_err.button_label:
                continue
            error_exists = True
            break
        if not error_exists:
            self.ui_errors.append(ui_err)

    def reset_ui_errors(self, asset_id: int) -> None:
        for error in self.ui_errors.copy():
            if error.asset_id != asset_id:
                continue

            self.ui_errors.remove(error)

    def handle_download_error(self, download: api.FileDownload) -> None:
        """Decides whether to sentry report and what to tell user on error."""

        asset_id = download.asset_id
        asset_name = os.path.basename(download.directory)
        fut = download.fut
        filename = download.filename

        try:
            excp = fut.exception()
        except CancelledError:
            excp = None
        if excp is not None:  # Unhandled exception?
            err = api.construct_error(
                download.url,
                f"Streaming error during download of {asset_id} ({excp})",
                str(download))
            res = api.ApiResponse({"error": excp}, False, err)
        else:
            res = fut.result()

        if res.ok or download.status == api.DownloadStatus.CANCELLED:
            return
        elif res.error == api.ERR_USER_CANCEL_MSG:
            msg = f"{res.error}, but download.status {download.status}"
            reporting.capture_message(
                "download_asset_cancelled", res.error, "error")
            return

        if res.error == api.ERR_URL_EXPIRED and download.retries > 0:
            # A download URL expired, but we can still retry
            return

        generic_label = "Failed, retry"  # Must fit inside grid view button.
        generic_description = ("Error during download, please try again\n"
                               f"({res.error})")
        if res.error == api.ERR_OS_NO_SPACE:
            # No need to report to sentry no space errors.
            ui_err = DisplayError(
                asset_id=asset_id,
                asset_name=asset_name,
                button_label="No space",
                description="No disk space left on default library drive."
            )
        elif res.error == api.ERR_OS_NO_PERMISSION:
            # No need to report to sentry permission errors.
            ui_err = DisplayError(
                asset_id=asset_id,
                asset_name=asset_name,
                button_label="Access error",
                description=("Access error while downloading asset, \n"
                             "try running blender as an admin.")
            )
        elif res.error in api.SKIP_REPORT_ERRS:
            # Provide the retry message without reporting to sentry.
            ui_err = DisplayError(
                asset_id=asset_id,
                asset_name=asset_name,
                button_label=generic_label,
                description=generic_description
            )
        elif res.error in api.ERR_URL_EXPIRED:
            ui_err = DisplayError(
                asset_id=asset_id,
                asset_name=asset_name,
                button_label=generic_label,
                description=generic_description
            )
        elif res.error in api.ERR_FILESIZE_MISMATCH:
            cf_ray = res.body.get("CF-RAY", "")
            msg = (f"{asset_id}: {res.error} {filename} "
                   f"{download.size_expected} {download.size_downloaded}"
                   f"\nCF-RAY: {cf_ray}")
            reporting.capture_message(
                "download_asset_filesize_mismatch", msg, "error")
            ui_err = DisplayError(
                asset_id=asset_id,
                asset_name=asset_name,
                button_label=generic_label,
                description=generic_description
            )
        else:
            # An unhandled download issue, capture in general sentry message.
            # Since exception was unexpected, likely nobody set the error flag
            download.set_status_error()
            cf_ray = res.body.get("CF-RAY", "")
            msg = f"{asset_id}: {res.error}\nCF-RAY: {cf_ray}"
            reporting.capture_message(
                "download_asset_failed", msg, "error")
            ui_err = DisplayError(
                asset_id=asset_id,
                asset_name=asset_name,
                button_label=generic_label,
                description=generic_description
            )

        # Store in FileDownload for download quality tracking
        # Note: Only needed here in P4B as in addon-core this is handled by
        #       downloader automatically.
        download.error = res.error

        self.append_ui_error(ui_err)
        self.vRedraw = 1

    def check_downloads(self,
                        dl_list: List[api.FileDownload]
                        ) -> Tuple[bool,
                                   bool,
                                   int,
                                   Optional[api.FileDownload]]:
        any_error = False
        all_done = True
        size_downloaded = 0
        error_dl = None
        for download in dl_list:
            size_downloaded += download.size_downloaded

        for download in dl_list:
            fut = download.fut
            if not fut.done():
                all_done = False
                continue

            try:
                excp = fut.exception()
            except CancelledError:
                excp = None
            had_excp = excp is not None
            if not had_excp:
                res = fut.result()
            else:
                asset_id = download.asset_id
                err = api.construct_error(
                    download.url,
                    f"Streaming error during download of {asset_id} ({excp})",
                    str(download))
                res = api.ApiResponse(
                    {"error": "Unhandled Exception"}, False, err)
            if not res.ok or had_excp:
                # TODO(Andreas): What other errors would we want to let continue?
                if res.error not in [api.ERR_URL_EXPIRED]:
                    download.retries = 0  # Force abort
                if download.retries <= 0:
                    self.handle_download_error(download)
                any_error = True
                all_done = False
                error_dl = download
                break
        return all_done, any_error, size_downloaded, error_dl

    def cancel_downloads(self, dl_list: List[api.FileDownload]) -> None:
        dbg = 0
        # cancel all download threads
        self.print_debug(dbg, "cancel_downloads")
        for download in dl_list:
            download.set_status_cancelled()
            download.fut.cancel()
        # wait for threads to actually return
        self.print_debug(dbg, "cancel_downloads WAITING")
        for download in dl_list:
            if download.fut.cancelled():
                continue
            try:
                download.fut.result(timeout=60)
            except FutureTimeoutError as e:
                reporting.capture_exception(e)
            except BaseException as e:
                reporting.capture_exception(e)
                self.print_debug(dbg, f"Unexpected {e}, {type(e)}")
        self.print_debug(dbg, "cancel_downloads DONE")

    def rename_downloads(self, dl_list: List[api.FileDownload]) -> None:
        dbg = 0
        self.print_debug(dbg, "rename_downloads")
        for download in dl_list:
            if download.status != api.DownloadStatus.DONE:
                self.print_debug(dbg,
                                 "rename_downloads: conflicting DONE state")
            path_temp = download.get_path(temp=True)
            temp_exists = os.path.exists(path_temp)
            path_final = download.get_path(temp=False)
            final_exists = os.path.exists(path_final)

            if temp_exists and final_exists:
                # In case the same file got listed twice (and somehow slipped
                # through), we delete the new one, as the useer might have the
                # existing one already in use.
                try:
                    os.remove(path_temp)
                except BaseException as e:
                    # Do not fail here, we do have the file we want.
                    # Just one extra we did not want!
                    reporting.capture_exception(e)
            elif temp_exists:
                try:
                    os.rename(path_temp, path_final)
                except BaseException as e:
                    # TODO(Andreas): Not sure, what else we can do in this case
                    reporting.capture_exception(e)
            elif final_exists:
                # As pre-existing files do not get filtered from the download
                # list, but instead the download thread will return immediately
                # with success upon detecting a file exists, we may encounter
                # non-temporary files (files with final name) here.
                # Nothing to do for these here.
                pass  # deliberate pass
            else:
                msg = f"{download.asset_id}: Downloaded file missing: {path_temp}"
                reporting.capture_message(
                    "asset_downloaded_file_missing", msg, "error")
                self.print_debug(dbg, msg)
        self.print_debug(dbg, "rename_downloads DONE")

    def retry_file_download(self,
                            asset_id: int,
                            dl_list: List[api.FileDownload],
                            tpe: ThreadPoolExecutor,
                            download_data: Dict,
                            retries: int,
                            convention: int
                            ) -> bool:
        dl_list_new, uuid, _ = self.get_download_list(
            asset_id, download_data, is_retry=True, convention=convention)

        retries_exhausted = False
        for dl in dl_list:
            if dl.status != api.DownloadStatus.ERROR:
                continue
            if dl.retries <= 0:
                retries_exhausted = True
                break
            url_base = dl.url.split("?")[0]
            for dl_new in dl_list_new:
                if not dl_new.url.startswith(url_base):
                    continue
                dl.retries -= 1
                dl.url = self._api.patch_download_url_increment_version(
                    dl_new.url)
                dl.status = api.DownloadStatus.WAITING
                dl.fut = tpe.submit(self._api.download_asset_file,
                                    download=dl)
                dl.fut.add_done_callback(print_exc)
                break
        return retries_exhausted

    def poll_download_result(self,
                             asset_id: int,
                             size_asset: int,
                             dl_list: List[api.FileDownload],
                             tpe: ThreadPoolExecutor,
                             download_data: Dict,
                             retries: int,
                             uuid: str,
                             convention: int
                             ) -> Tuple[bool, bool, bool, int, float]:
        """Polling all downloads belonging to a single asset. We even stay in
        this loop, if one or more single files need to be restarted.
        """

        dbg = 0

        all_done = False
        any_error = False
        user_cancel = False
        dq_got_tracked = False

        self.print_debug(dbg, "poll_download_result POLL LOOP")
        while not all_done and not user_cancel:
            if not self.quitting:
                time.sleep(DOWNLOAD_POLL_INTERVAL)

            (all_done,
             any_error,
             size_downloaded,
             error_dl) = self.check_downloads(dl_list)

            # Get user cancel and update progress UI
            percent_downloaded = max(size_downloaded / size_asset, 0.001)
            user_cancel = not self.download_update(asset_id,
                                                   size_asset,
                                                   percent_downloaded)
            if all_done and not any_error:
                self.print_debug(dbg, "poll_download_result ALL DONE")
                if not dq_got_tracked:
                    self._api.track_download_quality(
                        uuid=uuid,
                        status=api.DQStatus.SUCCESS,
                        error="")
                retries = 0
                break
            elif user_cancel:
                self.print_debug(dbg, "poll_download_result CANCELLING")
                self.cancel_downloads(dl_list)
                if not dq_got_tracked:
                    self._api.track_download_quality(
                        uuid=uuid,
                        status=api.DQStatus.CANCELED,
                        error="User cancelled download")
                    dq_got_tracked = True
                break
            elif any_error:
                self.print_debug(dbg, "poll_download_result ERROR")
                if not dq_got_tracked:
                    msg = (f"Error: {error_dl.error}, File: {error_dl.url},"
                           f" CF-ray: {error_dl.cf_ray}")
                    self._api.track_download_quality(
                        uuid=uuid,
                        status=api.DQStatus.FAILED,
                        error=msg)
                    # Track only first retry of first failing file,
                    # entire asset download gets tracked as a failure
                    dq_got_tracked = True  # avoid consecutive tracking

                retries_exhausted = self.retry_file_download(
                    asset_id, dl_list, tpe, download_data, retries, convention)
                if retries_exhausted:
                    self.cancel_downloads(dl_list)
                    break

        return all_done, any_error, user_cancel, retries, percent_downloaded

    def update_asset_data(self,
                          asset_name: str,
                          asset_type: str,
                          download_dir: str,
                          primary_files: List[str],
                          add_files: List[str]
                          ) -> None:
        dbg = 0
        self.print_debug(dbg, "update_asset_data")
        if not f_Ex(download_dir):
            self.print_debug(dbg, "update_asset_data NO DIR")
            return
        asset_files = []
        for path, dirs, files in os.walk(download_dir):
            asset_files += [os.path.join(path, file)
                            for file in files
                            if not file.endswith(api.DOWNLOAD_TEMP_SUFFIX)]
        if len(asset_files) == 0:
            self.print_debug(dbg, "update_asset_data NO FILES")
            return
        # Ensure previously found asset files are added back
        asset_files += primary_files + add_files
        asset_files = list(set(asset_files))

        asset_data = self.build_local_asset_data(
            asset_name, asset_type, asset_files)
        with self.lock_assets:
            self.vAssets["local"][asset_type][asset_name] = asset_data
        self.print_debug(dbg, "update_asset_data DONE")

    @reporting.handle_function(silent=True)
    @run_threaded(tm.PoolKeys.ASSET_DL, MAX_PARALLEL_ASSET_DOWNLOADS)
    def download_asset_thread(self,
                              asset_id: int
                              ) -> None:
        """Thread to download an asset (all files thereof)"""

        dbg = 0
        # A queued download (user started more than MAX_PARALLEL_ASSET_DOWNLOADS)
        # may have been cancelled again before we reach this point
        with self.lock_download:
            user_cancel = asset_id in self.vDownloadCancelled
            if user_cancel:
                self.print_debug(
                    dbg, "download_asset_thread CANCEL BEFORE START")
                del self.vDownloadQueue[asset_id]
                self.vDownloadCancelled.remove(asset_id)
                self.vRedraw = 1
                return
            if asset_id not in self.vDownloadQueue:
                self.print_debug(
                    dbg, "download_asset_thread DOWNLOAD NOT QUEUED")
                self.vRedraw = 1
                return
            asset_size = self.vDownloadQueue[asset_id]["size"]
            asset_data = self.vDownloadQueue[asset_id]["data"]
            asset_name = asset_data["name"]
            asset_type = asset_data["type"]
            api_convention = asset_data["api_convention"]

        t_start = time.monotonic()

        download_data = self.get_download_data(asset_data, size=asset_size)

        result_tuple = self.get_destination_library_directory(asset_data)
        library_dir, primary_files, add_files = result_tuple
        download_dir = os.path.join(library_dir, asset_name)
        if not os.path.isdir(download_dir):
            os.mkdir(download_dir)

        self.print_debug(
            dbg, "download_asset_thread downloading to:", download_dir)

        tpe = ThreadPoolExecutor(max_workers=MAX_PARALLEL_DOWNLOADS_PER_ASSET)

        size_asset = 0
        retries = MAX_DOWNLOAD_RETRIES
        all_done = False
        any_error = False
        user_cancel = False
        dl_list = None
        asset_download = None
        percent_downloaded = 0.001  # progress bar split does not like zero

        self.print_debug(dbg, "download_asset_thread LOOP")
        while not all_done and not user_cancel and retries > 0:
            self.reset_ui_errors(asset_id)

            user_cancel = not self.download_update(
                asset_id, 1, percent_downloaded)  # Init progress bar

            t_start_urls = time.monotonic()

            is_retry = retries != MAX_DOWNLOAD_RETRIES
            dl_list, uuid, asset_download = self.get_download_list(
                asset_id, download_data, is_retry, convention=api_convention)
            t_end_urls = time.monotonic()
            duration_urls = t_end_urls - t_start_urls

            dl_list = self.avoid_specular_textures(dl_list)
            size_asset = self.calc_asset_size_bytes(dl_list)

            user_cancel = not self.download_update(
                asset_id, size_asset, percent_downloaded)
            if user_cancel:
                self.print_debug(dbg, "download_asset_thread USER CANCEL")
                break
            elif len(dl_list) == 0:
                self.print_debug(
                    dbg, "download_asset_thread URL RETRIEVE -> no downloads")
                retries -= 1
                continue  # retry

            self.print_debug(
                dbg, f"=== Requesting URLs took {duration_urls:.3f} s.")

            self.schedule_downloads(tpe, dl_list, download_dir)

            (all_done,
             any_error,
             user_cancel,
             retries,
             percent_downloaded) = self.poll_download_result(asset_id,
                                                             size_asset,
                                                             dl_list,
                                                             tpe,
                                                             download_data,
                                                             retries,
                                                             uuid,
                                                             api_convention)
            # If the asset download failed/is incomplete, we'll start another
            # attempt (if retries left). The next retry will be "download
            # quality tracked" as a new separate download with new UUID.
            retries -= 1

        no_files = dl_list is None or len(dl_list) == 0

        if all_done and not any_error and not user_cancel and not no_files:
            if asset_download is not None:
                # Used inside store_dynamic_files to get location of mtlx file
                fake_download = api.FileDownload(
                    asset_id=asset_id,
                    url="",  # not needed
                    filename="",  # not needed
                    convention=api_convention,
                    size_expected=1,  # not needed
                    size_downloaded=1,  # not needed
                    resolution_size=asset_size,
                    directory=download_dir
                )
                asset_download.download_list = [fake_download]
                # Used inside store_dynamic_files for reporting
                fake_asset_data = AssetData(
                    asset_id=asset_id,
                    asset_type=AssetType.TEXTURE,
                    asset_name=asset_name
                )
                asset_download.asset_data = fake_asset_data

                # Let entire download fail, if not able to write dynamic file
                all_done = self._api.store_dynamic_files(
                    asset_download, expose_api_error=False)

        if all_done and not any_error and not user_cancel and not no_files:
            self.rename_downloads(dl_list)
            self.store_last_downloaded_size(asset_name, asset_type, asset_size)

        self.update_asset_data(asset_name, asset_type, download_dir,
                               primary_files, add_files)

        self.print_debug(dbg, "download_asset_thread REMOVE FROM DL QUEUE")
        with self.lock_download:
            try:
                del self.vDownloadQueue[asset_id]
            except BaseException:
                pass  # Already removed or never existed.
            try:
                self.vDownloadCancelled.remove(asset_id)
            except BaseException:
                pass  # Already removed or never existed.

        # Don't even think about using refresh_ui(),
        # we are in thread context here!
        self.vRedraw = 1

        t_end = time.monotonic()
        if all_done and not any_error and not user_cancel:
            duration = t_end - t_start
            size_MB = size_asset / (1024 * 1024)
            speed = size_MB / duration
            self.print_debug(dbg, f"=== Successfully downloaded {asset_name}")
            self.print_debug(dbg, f"    ENTIRE ASSET : {size_MB:.2f} MB, {duration:.3f} s, {speed:.2f} MB/s")
            for download in dl_list:
                size_MB = download.size_downloaded / (1024 * 1024)
                speed = size_MB / download.duration
                self.print_debug(dbg, f"    {download.filename} : {size_MB:.2f} MB, {download.duration:.3f} s, {speed:.2f} MB/s")

    def should_continue_asset_download(self, asset_id: int) -> bool:
        """Check for any user cancel presses."""

        with self.lock_download:
            should_continue = asset_id not in self.vDownloadCancelled
        return should_continue and not self.quitting

    def download_update(self,
                        asset_id: int,
                        download_size: int,
                        download_percent: float = 0.001
                        ) -> bool:
        """Updates info for download progress bar, return false to cancel."""
        with self.lock_download:
            if asset_id in self.vDownloadQueue.keys():
                self.vDownloadQueue[asset_id]["download_size"] = download_size
                self.vDownloadQueue[asset_id]["download_percent"] = download_percent
        self.refresh_ui()
        return self.should_continue_asset_download(asset_id)

    def reset_asset_error(self,
                          asset_id: Optional[int] = None,
                          asset_name: Optional[str] = None
                          ) -> None:
        """Resets any prior errors for this asset, such as download issue."""

        for err in self.ui_errors:
            if asset_id and err.asset_id == asset_id:
                self.ui_errors.remove(err)
                self.print_debug(0, "Reset error from id", err)
            elif asset_name and err.asset_name == asset_name:
                self.ui_errors.remove(err)
                self.print_debug(0, "Reset error from name", err)

    # .........................................................................

    def f_BuildBackplate(self,
                         vAsset: str,
                         vName: str,
                         vFile: str,
                         reuse: bool = True):
        """Create the backplate material and apply to existing or a new obj."""
        dbg = 0
        self.print_separator(dbg, "f_BuildBackplate")

        vMat = None
        vImage = None

        # See if the material and its image already exist.
        if vName in bpy.data.materials:
            vMat = bpy.data.materials[vName]
            for node in vMat.node_tree.nodes:
                if node.type == "TEX_IMAGE":
                    vImage = node.image
                    break

        if reuse and vMat is not None and vImage is not None:
            # Already successfuly fetched the material and image to reuse.
            pass
        else:
            # TODO(SOFT-763): Migrate to blender core when ready.
            vMat = bpy.data.materials.new(vName)

            vMat.use_nodes = 1

            vMNodes = vMat.node_tree.nodes
            vMLinks = vMat.node_tree.links

            for node in vMNodes:
                if node.type == "BSDF_PRINCIPLED":
                    vMNodes.remove(node)

            vCoords = vMNodes.new(type="ShaderNodeTexCoord")
            vCoords.location = mathutils.Vector((-650, 360))

            vTex = vMNodes.new("ShaderNodeTexImage")
            vTex.name = "DIFF"
            vTex.label = "DIFF"
            vTex.location = mathutils.Vector((-450, 360))

            vMix = vMNodes.new("ShaderNodeMixShader")
            vMix.location = mathutils.Vector((60, 300))

            vTransparent = vMNodes.new("ShaderNodeBsdfTransparent")
            vTransparent.location = mathutils.Vector((-145, 230))

            vEmission = vMNodes.new("ShaderNodeEmission")
            vEmission.location = mathutils.Vector((-145, 120))
            vEmission.inputs["Strength"].default_value = 1.0

            if vName in bpy.data.images:
                vImage = bpy.data.images[vName]
            else:
                vImage = bpy.data.images.load(vFile)
                vImage.name = vName
            vTex.image = vImage

            vMLinks.new(vCoords.outputs["UV"], vTex.inputs["Vector"])
            vMLinks.new(vTex.outputs["Color"], vEmission.inputs["Color"])
            vMLinks.new(vTex.outputs["Alpha"], vMix.inputs[0])
            vMLinks.new(vTransparent.outputs[0], vMix.inputs[1])
            vMLinks.new(vEmission.outputs[0], vMix.inputs[2])
            vMLinks.new(vMix.outputs[0], vMNodes["Material Output"].inputs[0])

            vMat.blend_method = 'HASHED'
            vMat.shadow_method = 'HASHED'

        vMat.poliigon = "Textures;" + vAsset

        vMat.poliigon_props.asset_name = vAsset
        asset_id = -1
        with self.lock_assets:
            my_assets_textures = self.vAssets["my_assets"]["Textures"]
            if vAsset in my_assets_textures.keys():
                asset_id = my_assets_textures[vAsset].get("id", -1)
        vMat.poliigon_props.asset_id = asset_id
        vMat.poliigon_props.asset_type = "Textures"
        vMat.poliigon_props.size = ""
        for size in SIZES:
            if size in vFile:
                vMat.poliigon_props.size = size
                break
        vMat.poliigon_props.mapping = "FLAT"
        vMat.poliigon_props.scale = 1.0
        vMat.poliigon_props.displacement = 0.0
        vMat.poliigon_props.use_16bit = False
        vMat.poliigon_props.mode_disp = "NORMAL"
        vMat.poliigon_props.is_backplate = True

        if bpy.context.selected_objects:
            # If there are objects selected, apply backplate to those
            vObjs = list(bpy.context.selected_objects)

        else:
            # Otherwise, create a new object.
            prior_objs = [vO for vO in bpy.data.objects]

            bpy.ops.mesh.primitive_plane_add(
                size=1.0, enter_editmode=False,
                location=bpy.context.scene.cursor.location, rotation=(0, 0, 0)
            )

            vObj = [vO for vO in bpy.data.objects if vO not in prior_objs][0]
            vObjs = [vObj]  # For assignment of material later.
            vObj.name = vName

            vObj.rotation_euler = mathutils.Euler((radians(90.0), 0.0, 0.0),
                                                  "XYZ")

            vRatio = vImage.size[0] / vImage.size[1]

            vH = 5.0
            if bpy.context.scene.unit_settings.length_unit == "KILOMETERS":
                vH = 5.0 / 1000
            elif bpy.context.scene.unit_settings.length_unit == "CENTIMETERS":
                vH = 5.0 * 100
            elif bpy.context.scene.unit_settings.length_unit == "MILLIMETERS":
                vH = 5.0 * 1000
            elif bpy.context.scene.unit_settings.length_unit == "MILES":
                vH = 16.0 / 5280
            elif bpy.context.scene.unit_settings.length_unit == "FEET":
                vH = 16.0
            elif bpy.context.scene.unit_settings.length_unit == "INCHES":
                vH = 16.0 * 12

            vW = vH * vRatio

            vObj.dimensions = mathutils.Vector((vW, vH, 0))

            vObj.delta_scale[0] = 1
            vObj.delta_scale[1] = 1
            vObj.delta_scale[2] = 1

            bpy.ops.object.select_all(action="DESELECT")
            try:
                vObj.select_set(True)
            except Exception:
                vObj.select = True

            bpy.ops.object.transform_apply(
                location=False, rotation=False, scale=True)

        for obj in vObjs:
            obj.active_material = vMat

    # .........................................................................

    def f_GetLocalAssets(self, force: bool = False):
        dbg = 0
        self.print_separator(dbg, "f_GetLocalAssets")

        # This function was taking 3.5s to run at startup, so thrown it into a thread

        if not force and (time.monotonic() - self.vGotLocalAssets) < 60 * 5:
            return

        if not self.vGettingLocalAssets:
            self.vGettingLocalAssets = 1

            vThread = threading.Thread(target=self.f_GetLocalAssetsThread)
            vThread.daemon = 1
            vThread.start()
            self.vThreads.append(vThread)
        else:
            self.print_debug(1, "Flagging to check local assets again.")
            self.vRerunGetLocalAssets = True

    def get_common_prefix(self, files: List[str]) -> str:
        name_candidates = []
        for filename in files:
            if filename.startswith("."):
                continue  # Ignore hidden system files like .DS_Store
            name, ext = f_FNameExt(filename)
            if ext in ["", ".zip"]:
                continue
            name_parts = name.split("_")
            if name_parts[0] == "Poliigon":
                name_candidate = "_".join(name_parts[0:2])
            else:
                name_candidate = name_parts[0]
            name_candidates.append(name_candidate)
        return os.path.commonprefix(name_candidates)

    def validate_asset_name(self, asset_name: str) -> str:
        if len(asset_name) <= 5:
            return ""
        if "_" in asset_name and not asset_name.startswith("Poliigon"):
            return asset_name.split("_")[0]
        return asset_name

    def reduce_by_allowed_subdirs(self, path: str) -> str:
        """Reduces a file path (e.g. texture) down to the actual asset path,
        removing any of the allowed sub folders inside an asset directory.
        """

        path_check = path
        allowed_subdirs = SIZES + TAGS_WORKFLOW + ["REGULAR"]
        while True:
            basename = os.path.basename(path_check)
            if basename not in allowed_subdirs:
                break
            path_check = os.path.dirname(path_check)
        return path_check

    def guess_asset_name(self, path: str, files: List[str]) -> str:
        """Determines asset name as a majority decision of multiple guesses:
        - common filename prefix
        - folder name
        - common filename prefix of model files
        - common prefix of texture files
        E.g. "SomePie001_2K.png" and "SomePie_Berry.fbx" results in "SomePie"
        """

        if len(files) == 0:
            return ""

        asset_name_common = self.validate_asset_name(
            self.get_common_prefix(files))

        path_check = self.reduce_by_allowed_subdirs(path)
        asset_name_path = self.validate_asset_name(os.path.basename(path_check))

        files_preview = [filename for filename in files
                         if AssetIndex.check_if_preview(filename)]
        files_no_preview = [filename for filename in files
                            if filename not in files_preview]

        files_model = [filename for filename in files_no_preview
                       if f_FNameExt(filename)[1] in self.vModExts]
        files_tex = [filename for filename in files_no_preview
                     if f_FNameExt(filename)[1] in self.vTexExts]

        asset_name_files_model_common = self.validate_asset_name(
            self.get_common_prefix(files_model))
        asset_name_files_tex_common = self.validate_asset_name(
            self.get_common_prefix(files_tex))

        # Identify the best matching asset name based on the most aligned name
        # where the highest subset of asset name matches is supposed to be an
        # indicator for confidence.
        if asset_name_path == asset_name_common == asset_name_files_model_common == asset_name_files_tex_common:
            asset_name = asset_name_path
        elif asset_name_path == asset_name_common == asset_name_files_model_common:
            asset_name = asset_name_path
        elif asset_name_path == asset_name_common == asset_name_files_tex_common:
            asset_name = asset_name_path
        elif asset_name_path == asset_name_files_model_common:
            asset_name = asset_name_path
        elif asset_name_path == asset_name_files_tex_common:
            asset_name = asset_name_path
        elif len(files_model) > 0:
            if len(asset_name_files_model_common) > len(asset_name_path):
                asset_name = asset_name_files_model_common
            else:
                asset_name = asset_name_path
        else:
            if len(asset_name_files_tex_common) > len(asset_name_path):
                asset_name = asset_name_files_tex_common
            else:
                asset_name = asset_name_path

        return asset_name

    @reporting.handle_function(silent=True)
    def f_GetLocalAssetsThread(self):
        dbg = 0
        self.print_separator(dbg, "f_GetLocalAssetsThread")

        with self.lock_assets:
            for vType in self.vAssetTypes:
                self.vAssets["local"][vType] = {}

        vGetAssets = {}
        vModels = []
        vHDRIs = []
        vBrushes = []

        gLatest = {}
        for vDir in [self.vSettings["library"]] + self.vSettings["add_dirs"]:
            if vDir in self.vSettings["disabled_dirs"]:
                continue

            for vPath, vDirs, vFiles in os.walk(vDir):
                if len(vFiles) <= 1:
                    # Assumption here: A valid asset always has at least one
                    # preview and one texture file.
                    continue
                if vPath == vDir:
                    continue

                vPath = vPath.replace("\\", "/")

                if "Software" in vPath and "Blender" not in vPath:
                    continue

                vName = self.guess_asset_name(vPath, vFiles)

                # In case above loop results in a "funny" name,
                # we'll fall back to the old behavior
                if len(vName) > 5:  # assuming no assets with only five chars
                    use_name_per_file = False
                else:
                    use_name_per_file = True  # fallback

                for vF in vFiles:
                    if vF.startswith("."):
                        continue  # Ignore hidden system files like .DS_Store
                    if f_FExt(vF) in ["", ".zip"]:
                        continue
                    if vF.endswith(api.DOWNLOAD_TEMP_SUFFIX):
                        continue

                    vNamePerFile, vExt = f_FNameExt(vF)
                    if use_name_per_file:
                        vName = vNamePerFile

                    if vName.startswith("Hdr"):
                        vHDRIs.append(vName)

                    elif vName.startswith("Brush"):
                        vBrushes.append(vName)

                    if "_LIB." in vF:
                        asset_name = vName.replace("_LIB", "")
                        if asset_name not in vGetAssets.keys():
                            vGetAssets[asset_name] = []
                        # no path, this file will be filtered in build_local_asset_data()
                        vGetAssets[asset_name].append(vF)
                        continue

                    elif any(
                        f_FName(vF).lower().endswith(vS)
                        for vS in [
                            "_atlas",
                            "_sphere",
                            "_cylinder",
                            "_fabric",
                            "_preview1",
                        ]
                    ):
                        if vName not in vGetAssets.keys():
                            vGetAssets[vName] = []

                        vGetAssets[vName].append(vPath + "/" + vF)

                        vFTime = os.path.getctime(vPath + "/" + vF)

                        if vName not in gLatest.keys():
                            gLatest[vName] = vFTime
                        elif gLatest[vName] < vFTime:
                            gLatest[vName] = vFTime

                    elif vExt.lower() in self.vTexExts:
                        anymap_conv0 = any(vM in vF for vM in self.vMaps)
                        anymap_conv1 = any(vM in vF for vM in self.maps_convention1)
                        anymap = anymap_conv0 or anymap_conv1
                        if anymap or "Backdrop" in vF:
                            if vName not in vGetAssets.keys():
                                vGetAssets[vName] = []

                            vGetAssets[vName].append(vPath + "/" + vF)

                    elif vExt.lower() in self.vModExts:
                        if vName not in vGetAssets.keys():
                            vGetAssets[vName] = []

                        vGetAssets[vName].append(vPath + "/" + vF)

                        vGetAssets[vName] += [
                            vPath + "/" + vFl
                            for vFl in vFiles
                            if f_FExt(vFl) in self.vTexExts
                        ]

                        if vName not in vModels:
                            vModels.append(vName)

        # Special behavior for Vases and Foot Rests
        for vA in sorted(list(vGetAssets.keys())):
            if any(vS in vA for vS in self.vModSecondaries):
                vPrnt = vA
                for vS in self.vModSecondaries:
                    vPrnt = vPrnt.replace(vS, "")

                if vPrnt in list(vGetAssets.keys()):
                    vGetAssets[vPrnt] += vGetAssets[vA]

                    del vGetAssets[vA]

        for vA in sorted(list(vGetAssets.keys())):
            vType = "Textures"
            if vA in vModels:
                vType = "Models"
            elif vA in vHDRIs:
                vType = "HDRIs"
            elif vA in vBrushes:
                vType = "Brushes"

            asset_data = self.build_local_asset_data(vA, vType, vGetAssets[vA])

            with self.lock_assets:
                if vType not in self.vAssets["local"].keys():
                    self.vAssets["local"][vType] = {}
                # updating global asset dict here for better UI responsiveness
                self.vAssets["local"][vType][vA] = asset_data

        vSLatest = {}
        for vK in gLatest.keys():
            vSLatest[gLatest[vK]] = vK

        gLatest = [vSLatest[vK] for vK in reversed(sorted(vSLatest.keys()))]

        # Need to tag redraw, can't directly call refresh_ui since
        # this runs on startup.
        self.vRedraw = 1

        self.vGettingLocalAssets = 0

        self.vGotLocalAssets = time.monotonic()
        if self.vRerunGetLocalAssets:
            self.vRerunGetLocalAssets = False
            self.f_GetLocalAssets()  # TODO(Andreas): Shouldn't this have force=True ?

    def filter_asset_files(self, files: List[str]) -> List[str]:
        files = sorted(list(set(files)))

        files_existing = []
        for file in files:
            if not os.path.exists(file):
                continue
            if "_SOURCE" in file:
                continue
            files_existing.append(file)
        return files_existing

    def build_local_asset_data(self, asset, type, files):
        """Builds data dict for asset"""

        maps = []
        maps_convention1 = []
        lods = []
        sizes = []
        vars = []
        preview = None

        file_asset_browser = None
        files_existing = self.filter_asset_files(files)

        for file in files_existing:
            if "_LIB." in file:
                file_asset_browser = file
                continue
            elif AssetIndex.check_if_preview(file):
                preview = file
            else:
                filename_parts = f_FName(file).split("_")
                filename_ext = f_FExt(file)
                is_model = filename_ext == ".fbx" or filename_ext == ".blend"
                map_conv0 = [map for map in self.vMaps if map in filename_parts]
                maps += map_conv0
                map_conv1 = [map
                             for map in self.maps_convention1
                             if map in filename_parts]
                maps_convention1 += map_conv1
                lods += [
                    lod for lod in self.vLODs
                    if lod in filename_parts and is_model
                ]
                map_size = [
                    size for size in SIZES
                    if size in filename_parts
                ]
                if len(map_size) == 0:
                    path_size = os.path.basename(os.path.dirname(file))
                    if path_size in SIZES:
                        map_size = [path_size]
                sizes += map_size
                vars += [var for var in self.vVars if var in filename_parts]

        asset_data = {}
        asset_data["name"] = asset
        # asset_data["id"] = 0  # Don't populate id, it's not available here.
        asset_data["type"] = type
        if file_asset_browser is not None:
            files_existing.remove(file_asset_browser)
            asset_data["in_asset_browser"] = True
        else:
            asset_data["in_asset_browser"] = False
        asset_data["files"] = files_existing
        asset_data["maps"] = sorted(list(set(maps)))
        asset_data["lods"] = [lod for lod in self.vLODs if lod in lods]  # TODO: sort
        asset_data["sizes"] = [size for size in SIZES if size in sizes]  # TODO: sort
        asset_data["vars"] = sorted(list(set(vars)))
        modified_times = [os.path.getctime(file)
                          for file in files_existing
                          if file != file_asset_browser]
        if modified_times:
            asset_data["date"] = max(modified_times)
        else:
            asset_data["date"] = 0
        asset_data["credits"] = None
        asset_data["preview"] = preview
        asset_data["thumbnails"] = [preview]
        asset_data["quick_preview"] = []

        return asset_data

    def f_GetSceneAssets(self):
        dbg = 0
        self.print_separator(dbg, "f_GetSceneAssets")

        vImportedAssets = {}
        for vType in self.vAssetTypes:
            vImportedAssets[vType] = {}

        for vM in bpy.data.materials:
            try:
                vType, vAsset = vM.poliigon.split(";")
                if vAsset.startswith("Poliigon_"):
                    parts = vAsset.split("_")
                    vAsset = "_".join(parts[0:3])
                else:
                    vAsset = vAsset.split("_")[0]

                if vType == "Textures" and vAsset != "":
                    self.print_debug(dbg, "f_GetSceneAssets", vAsset)

                    if vAsset not in vImportedAssets["Textures"].keys():
                        vImportedAssets["Textures"][vAsset] = []

                    if vM not in vImportedAssets["Textures"][vAsset]:
                        vImportedAssets["Textures"][vAsset].append(vM)
            except Exception:
                pass

        for vO in bpy.data.objects:
            try:
                vType, vAsset = vO.poliigon.split(";")
                vAsset = vAsset.split("_")[0]
                if vType == "Models" and vAsset != "":
                    self.print_debug(dbg, "f_GetSceneAssets", vAsset)

                    if vAsset not in vImportedAssets["Models"].keys():
                        vImportedAssets["Models"][vAsset] = []

                    if vO not in vImportedAssets["Models"][vAsset]:
                        vImportedAssets["Models"][vAsset].append(vO)
            except Exception:
                pass

        for vI in bpy.data.images:
            try:
                vType, vAsset = vI.poliigon.split(";")
                vAsset = vAsset.split("_")[0]
                if vType in ["HDRIs", "Brushes"] and vAsset != "":
                    self.print_debug(dbg, "f_GetSceneAssets", vAsset)

                    if vAsset not in vImportedAssets[vType].keys():
                        vImportedAssets[vType][vAsset] = []

                    vImportedAssets[vType][vAsset].append(vI)
            except Exception:
                pass

        self.imported_assets = vImportedAssets

    def f_GetActiveData(self):
        dbg = 0
        self.print_separator(dbg, "f_GetActiveData")

        self.vActiveMatProps = {}
        self.vActiveTextures = {}
        self.vActiveMixProps = {}

        if self.vActiveMat is None:
            return

        vMat = bpy.data.materials[self.vActiveMat]

        if self.vActiveMode == "mixer":
            vMNodes = vMat.node_tree.nodes
            vMLinks = vMat.node_tree.links
            for vN in vMNodes:
                if vN.type == "GROUP":
                    if "Mix Texture Value" in [vI.name for vI in vN.inputs]:
                        vMat1 = None
                        vMat2 = None
                        vMixTex = None
                        for vL in vMLinks:
                            if vL.to_node == vN:
                                if vL.to_socket.name in ["Base Color1",
                                                         "Base Color2"]:
                                    vProps = {}
                                    for vI in vL.from_node.inputs:
                                        if vI.is_linked:
                                            continue
                                        if vI.type == "VALUE":
                                            vProps[vI.name] = vL.from_node

                                    if vL.to_socket.name == "Base Color1":
                                        vMat1 = [vL.from_node, vProps]
                                    elif vL.to_socket.name == "Base Color2":
                                        vMat2 = [vL.from_node, vProps]
                                elif vL.to_socket.name == "Mix Texture":
                                    if vN.inputs["Mix Texture"].is_linked:
                                        vMixTex = vL.from_node

                        vProps = {}
                        for vI in vN.inputs:
                            if vI.is_linked:
                                continue
                            if vI.type == "VALUE":
                                vProps[vI.name] = vN

                        self.vActiveMixProps[vN.name] = [
                            vN,
                            vMat1,
                            vMat2,
                            vProps,
                            vMixTex,
                        ]

            if self.vSettings["mix_props"] == []:
                vK = list(self.vActiveMatProps.keys())[0]
                self.vSettings["mix_props"] = list(self.vActiveMatProps[vK][3].keys())
        else:
            vMNodes = vMat.node_tree.nodes
            for vN in vMNodes:
                if vN.type == "GROUP":
                    for vI in vN.inputs:
                        if vI.type == "VALUE":
                            self.vActiveMatProps[vI.name] = vN
                elif vN.type == "BUMP" and vN.name == "Bump":
                    for vI in vN.inputs:
                        if vI.type == "VALUE" and vI.name == "Strength":
                            self.vActiveMatProps[vI.name] = vN

            if self.vSettings["mat_props"] == []:
                self.vSettings["mat_props"] = list(self.vActiveMatProps.keys())

            if vMat.use_nodes:
                for vN in vMat.node_tree.nodes:
                    if vN.type == "TEX_IMAGE":
                        if vN.image is None:
                            continue
                        vFile = vN.image.filepath.replace("\\", "/")
                        if f_Ex(vFile):
                            # pType = [vT for vT in vTypes if vT in f_FName(vFile).split('_')]
                            vType = vN.name
                            if vType == "COLOR":
                                vType = "COL"
                            elif vType == "DISPLACEMENT":
                                vType = "DISP"
                            elif vType == "NORMAL":
                                vType = "NRM"
                            elif vType == "OVERLAY":
                                vType = "OVERLAY"

                            self.vActiveTextures[vType] = vN

                    elif vN.type == "GROUP":
                        for vN1 in vN.node_tree.nodes:
                            if vN1.type == "TEX_IMAGE":
                                if vN1.image is None:
                                    continue
                                vFile = vN1.image.filepath.replace("\\", "/")
                                if f_Ex(vFile):
                                    # pType = [vT for vT in vTypes if vT in f_FName(vFile).split('_')]
                                    vType = vN1.name
                                    if vType == "COLOR":
                                        vType = "COL"
                                    if vType == "OVERLAY":
                                        vType = "OVERLAY"
                                    elif vType == "DISPLACEMENT":
                                        vType = "DISP"
                                    elif vType == "NORMAL":
                                        vType = "NRM"
                                    self.vActiveTextures[vType] = vN1
                            elif vN1.type == "BUMP" and vN1.name == "Bump":
                                for vI in vN1.inputs:
                                    if vI.type == "VALUE" and vI.name == "Distance":
                                        self.vActiveMatProps[vI.name] = vN1

    def f_CheckAssets(self):
        dbg = 0
        self.print_separator(dbg, "f_CheckAssets")

        if time.monotonic() - self.vTimer < 5:
            return
        self.vTimer = time.monotonic()

        vAssetNames = []
        with self.lock_assets:
            for vType in self.vAssets["my_assets"].keys():
                vAssetNames += self.vAssets["my_assets"][vType]

        self.print_debug(dbg, "f_CheckAssets", "New Assets :")

        vZips = []
        self.vNewAssets = []
        for vDir in [self.vSettings["library"]] + self.vSettings["add_dirs"]:
            for vPath, vDirs, vFiles in os.walk(vDir):
                vPath = vPath.replace("\\", "/")
                for vF in vFiles:
                    if vF.endswith(".zip"):
                        if vPath + vF not in vZips:
                            vZips.append(vPath + vF)

                    elif "COL" in vF or vF.startswith("Back"):
                        vName = f_FName(vF).split("_")
                        if vName[-1] == "SPECULAR":
                            continue
                        if vName[0] not in vAssetNames:
                            self.vNewAssets.append(vName[0])

                            # if vDBG : print(vDBGi,"-",vName[0])

        if len(vZips) and self.vSettings["unzip"]:
            self.print_debug(dbg, "f_CheckAssets", "Zips :")

            for vZFile in vZips:
                vName = f_FName(vZFile).split("_")[0]
                self.print_debug(
                    dbg, "f_CheckAssets", "-", vName, " from ", vZFile)

                gLatest = 0
                vFDate = 0
                vZDate = 0

                asset_files = None
                with self.lock_assets:
                    for vType in self.vAssets["local"].keys():
                        if vName in self.vAssets["local"][vType].keys():
                            asset_files = self.vAssets["local"][vType][vName]["files"]

                if asset_files is not None:
                    for vF in asset_files:
                        try:
                            vFDate = datetime.datetime.fromtimestamp(
                                os.path.getctime(vF)
                            )
                            vFDate = str(vFDate).split(" ")[0].replace("-", "")
                            vFDate = int(vFDate)
                            if vFDate > gLatest:
                                gLatest = vFDate
                        except Exception:
                            pass

                    try:
                        vZDate = int(
                            (
                                str(
                                    datetime.datetime.fromtimestamp(
                                        os.path.getctime(vF)
                                    )
                                ).split(" ")[0]
                            ).replace("-", "")
                        )
                    except Exception:
                        pass
                    if vZDate < vFDate:
                        continue

        self.f_GetLocalAssets()

    # .........................................................................

    def f_Label(
            self, vWidth, vText, vContainer, vIcon=None, vAddPadding=False):
        """Text wrap a label based on indicated width."""
        # TODO: Move this to UI class ideally.
        dbg = 0
        self.print_separator(dbg, "f_Label")

        vWords = [vW.replace("!@#", " ") for vW in vText.split(" ")]
        vContainerRow = vContainer.row()
        vParent = vContainerRow.column(align=True)
        vParent.scale_y = 0.8  # To make vertical height more natural for text.
        if vAddPadding:
            vParent.label(text="")

        if vIcon:
            vWidth -= 25 * self.get_ui_scale()

        vLine = ""
        vFirst = True
        for vW in vWords:
            vLW = 15
            vLineN = vLine + vW + " "
            for vC in vLineN:
                if vC in "ABCDEFGHKLMNOPQRSTUVWXYZmw":
                    vLW += 9
                elif vC in "abcdeghknopqrstuvxyz0123456789":
                    vLW += 6
                elif vC in "IJfijl .":
                    vLW += 3

            vLW *= self.get_ui_scale()

            if vLW > vWidth:
                if vFirst:
                    if vIcon is None:
                        vParent.label(text=vLine)
                    else:
                        vParent.label(text=vLine, icon=vIcon)
                    vFirst = False

                else:
                    if vIcon is None:
                        vParent.label(text=vLine)
                    else:
                        vParent.label(text=vLine, icon="BLANK1")

                vLine = vW + " "

            else:
                vLine += vW + " "

        if vLine != "":
            if vIcon is None:
                vParent.label(text=vLine)
            else:
                if vFirst:
                    vParent.label(text=vLine, icon=vIcon)
                else:
                    vParent.label(text=vLine, icon="BLANK1")
        if vAddPadding:
            vParent.label(text="")

    # .........................................................................

    def f_GetThumbnailPath(self, asset, index):
        """Return the best fitting thumbnail preview for an asset.

        The primary grid UI preview will be named asset_preview1.png,
        all others will be named such as asset_preview1_1K.png
        """
        if index == 0:
            # 0 is the small grid preview version of _preview1.

            # Support legacy option of loading .jpg files, check that first.
            thumb = os.path.join(self.gOnlinePreviews, asset + "_preview1.jpg")
            if not os.path.exists(thumb):
                thumb = os.path.join(
                    self.gOnlinePreviews, asset + "_preview1.png")
        else:
            thumb = os.path.join(
                self.gOnlinePreviews,
                asset + f"_preview{index}_1K.png")
        return thumb

    def f_GetPreview(self, vAsset, index=0, load_image=True):
        """Queue download for a preview if not already local.

        Use a non-zero index to fetch another preview type thumbnail.
        """
        dbg = 0
        self.print_separator(dbg, "f_GetPreview")

        if vAsset == "dummy":
            return

        with self.lock_previews:
            if vAsset in self.vPreviews:
                # TODO(SOFT-447): See if there's another way at this moment to
                # inspect whether the icon we are returning here is gray or not.
                # TODO(Andreas): While SOFT-447 is marked done, the actual problem
                #                of grey thumbs still persists. Thus this TODO
                #                is still valid.
                # print(
                #     "Returning icon id",
                #     vAsset,
                #     self.vPreviews[vAsset].image_size[:])
                return self.vPreviews[vAsset].icon_id

        f_MDir(self.gOnlinePreviews)

        vPrev = self.f_GetThumbnailPath(vAsset, index)

        if os.path.exists(vPrev):
            if not load_image:  # special case used by thumb prefetcher
                return None

            with self.lock_previews:
                try:
                    self.vPreviews.load(vAsset, vPrev, "IMAGE")
                except KeyError:
                    self.vPreviews[vAsset].reload()

                self.print_debug(dbg, "f_GetPreview", vPrev)

                return self.vPreviews[vAsset].icon_id

        with self.lock_previews:
            if vAsset not in self.vPreviewsDownloading:
                self.vPreviewsDownloading.append(vAsset)
                self.f_QueuePreview(vAsset, index)

        return None

    def f_GetClosestSize(self, vSizes, vSize):
        if vSize not in vSizes:
            x = SIZES.index(vSize)
            for i in range(len(SIZES)):
                if x - i >= 0:
                    if SIZES[x - i] in vSizes:
                        vSize = SIZES[x - i]
                        break
                if x + i < len(SIZES):
                    if SIZES[x + i] in vSizes:
                        vSize = SIZES[x + i]
                        break

        return vSize

    def f_GetSize(self, vName):
        for vSz in SIZES:
            if vSz in vName.split('_'):
                return vSz

        return None

    def f_GetClosestLod(self, vLods, vLod):
        if vLod in vLods:
            return vLod

        if vLod == "NONE":
            return vLod

        x = self.vLODs.index(vLod)
        for i in range(len(self.vLODs)):
            if x - i >= 0:
                if self.vLODs[x - i] in vLods:
                    vLod = self.vLODs[x - i]
                    break
            if x + i < len(self.vLODs):
                if self.vLODs[x + i] in vLods:
                    vLod = self.vLODs[x + i]
                    break

        return vLod

    def f_GetLod(self, vName):
        for vL in self.vLODs:
            if vL in vName:
                return vL
        return None

    def f_GetVar(self, vName):
        vVar = None
        for vV in self.vVars:
            if vV in vName:
                return vV
        return vVar

    # .........................................................................
    def get_verbose(self) -> bool:
        """Returns verbosity setting from prefs."""
        prefs = self.get_prefs()
        if prefs is not None:
            return prefs.verbose_logs
        else:
            return False

    def get_prefs(self):
        """User preferences call wrapper, separate to support test mocking."""

        # TODO(SOFT-958): Remove this member function and change all calls to
        #                 calls of global get_prefs()
        return get_prefs()

    @reporting.handle_function(silent=True, transact=False)
    def print_separator(self, dbg, logvalue):
        """Print out a separator log line with a string value logvalue.

        Cache based on args up to a limit, to avoid excessive repeat prints.
        All args must be flat values, such as already casted to strings, else
        an error will be thrown.
        """
        if self.get_verbose() or dbg > 0:
            self._cached_print("-" * 50 + "\n" + str(logvalue))

    @reporting.handle_function(silent=True, transact=False)
    def print_debug(self, dbg, *args):
        """Print out a debug statement with no separator line.

        Cache based on args up to a limit, to avoid excessive repeat prints.
        All args must be flat values, such as already casted to strings, else
        an error will be thrown.
        """
        if self.quitting:
            return
        if self.get_verbose() or dbg > 0:
            # Ensure all inputs are hashable, otherwise lru_cache fails.
            stringified = [str(arg) for arg in args]
            self._cached_print(*stringified)

    @lru_cache(maxsize=32)
    def _cached_print(self, *args):
        """A safe-to-cache function for printing."""
        print(*args)

    def interval_check_update(self):
        """Checks with an interval delay for any updated files.

        Used to identify if an update has occurred. Note: If the user installs
        and updates by manually pasting files in place, or even from install
        addon via zip in preferences, and the addon is already active, there
        is no event-based function ran to let us know. Hence we use this
        polling method instead.
        """
        interval = 10
        now = time.time()
        if self.last_update_addon_files_check + interval > now:
            return
        self.last_update_addon_files_check = now
        self.update_files(self.gScriptDir)

    def update_files(self, path):
        """Updates files in the specified path within the addon."""
        dbg = 0
        update_key = "_update"
        files_to_update = [f for f in os.listdir(path)
                           if os.path.isfile(os.path.join(path, f))
                           and os.path.splitext(f)[0].endswith(update_key)]

        for f in files_to_update:
            f_split = os.path.splitext(f)
            tgt_file = f_split[0][:-len(update_key)] + f_split[1]

            try:
                os.replace(os.path.join(path, f), os.path.join(path, tgt_file))
                self.print_debug(dbg, f"Updated {tgt_file}")
            except PermissionError as e:
                reporting.capture_message("file_permission_error", e, "error")
            except OSError as e:
                reporting.capture_message("os_error", e, "error")

        any_updates = len(files_to_update) > 0

        # If the intial register already completed, then this must be the
        # second time we have run the register function. If files were updated,
        # it means this was a fresh update install.
        # Thus: We must notify users to restart.
        if any_updates and self.initial_register_complete:
            self.notify_restart_required()

        return any_updates

    def notify_restart_required(self):
        """Creates a UI-blocking banner telling users they need to restart.

        This will occur if the user has installed an updated version of the
        addon but has not yet restarted Blender. This is important to avoid
        errors caused by only paritally reloaded modules.
        """
        rst_id = "RESTART_POST_UPDATE"
        if rst_id in [ntc.notification_id for ntc in self.notifications]:
            # Already registered.
            return
        notice = build_restart_notification()
        self.register_notification(notice)

    def check_update_callback(self):
        """Callback run by the updater instance."""
        # Hack to force it to think update is available
        fake_update = False
        if fake_update:
            self.updater.update_ready = True
            self.updater.update_data = updater.VersionData(
                version=(1, 0, 0),
                url="https://poliigon.com/blender")

        # Build notifications and refresh UI.
        if self.updater.update_ready:
            notice = build_update_notification(self.updater)
            self.register_notification(notice)
        elif self.updater.alert_notice:
            notice = rebuild_core_notification(self.updater.alert_notice)
            if notice:
                self.register_notification(notice)
        self.refresh_ui()

    def update_api_status_banners(self, status_name):
        """Updates notifications according the to the form of the API event.

        This is called by API's event_listener when API events occur.
        """
        reset_ids = [
            "PROXY_CONNECTION_ERROR",
            "NO_INTERNET_CONNECTION"
        ]
        if status_name == api.ApiStatus.CONNECTION_OK:
            for existing in self.notifications:
                if existing.notification_id in reset_ids:
                    self.notifications.remove(existing)

        elif status_name == api.ApiStatus.NO_INTERNET:
            notice = build_no_internet_notification()
            self.register_notification(notice)

        elif status_name == api.ApiStatus.PROXY_ERROR:
            notice = build_proxy_notification()
            self.register_notification(notice)

    def _any_local_assets(self) -> bool:
        """Returns True, if there are local assets"""
        with self.lock_assets:
            for asset_type in self.vAssets["local"]:
                if len(self.vAssets["local"][asset_type]) > 0:
                    return True
        return False

    def _get_datetime_now(self):
        return datetime.datetime.now(datetime.timezone.utc)

    def _add_survey_notifcation(self):
        """Registers a survey notification, if conditions are met.

        NOTE: To be call via self.f_add_survey_notifcation_once().
              This function will overwrite this member variable
              in order to deactivate itself.
        """

        # Temporary conditions, do before disabling the function
        if len(self.notifications) != 0:
            # Never compete with other notifications
            return
        if self.vUser["is_free_user"] is None:
            # We can't decide correct URL until we know, if free user or not
            return

        # DISABLE this very function we are in.
        self.f_add_survey_notifcation_once = lambda: None

        if not self._any_local_assets():
            # Do not bother users, who haven't downloaded anything, yet
            return

        already_asked = "last_nps_ask" in self.vSettings
        already_opened = "last_nps_open" in self.vSettings
        if already_asked or already_opened:
            # Never bother the user twice
            return

        # 7 day period starts after first local assets got detected
        time_now = self._get_datetime_now()
        if "first_local_asset" not in self.vSettings:
            self.vSettings["first_local_asset"] = time_now.timestamp()
            self.f_SaveSettings()
            return

        ts_first_local = self.vSettings["first_local_asset"]
        time_first_local = datetime.datetime.fromtimestamp(
            ts_first_local, datetime.timezone.utc)
        time_since = time_now - time_first_local
        if time_since.days < 7:
            return
        if self.vUser["is_free_user"] == 1:
            url = "https://www.surveymonkey.com/r/p4b-addon-ui-03"
            notification_id = "NPS_INAPP_FREE"
        else:
            url = "https://www.surveymonkey.com/r/p4b-addon-ui-02"
            notification_id = "NPS_INAPP_ACTIVE"

        notice = build_survey_notification(notification_id, url)
        self.register_notification(notice)
        self.vSettings["last_nps_ask"] = time_now.timestamp()
        self.f_SaveSettings()


# ::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::


def f_tick_handler() -> int:
    """Called on by blender timer handlers to check toolbox status.

    The returned value signifies how long until the next execution.
    """
    next_call_s = 60  # Long to prevent frequent checks for updates.
    if cTB.vRunning:  # and not self.vExit
        cTB.vExit = 0

        # Thread cleanup.
        for vT in list(cTB.vThreads):
            if not vT.is_alive():
                cTB.vThreads.remove(vT)

        # Updater callback.
        if cTB.prefs and cTB.prefs.auto_check_update:
            if cTB.updater.has_time_elapsed(hours=24):
                cTB.updater.async_check_for_update(
                    cTB.check_update_callback,
                    create_notifications=True)

    return next_call_s


def f_download_handler() -> int:
    """Called on by blender timer handlers to redraw the UI while downloading.

    The returned value signifies how long until the next execution.
    """
    next_call_s = 1
    with cTB.lock_download:
        queued_asset_ids = list(cTB.vDownloadQueue.keys())
    combined_keys = queued_asset_ids + list(cTB.vQuickPreviewQueue.keys())

    if len(combined_keys) == 0 and not cTB.vRedraw:
        return next_call_s

    cTB.vRedraw = 0
    next_call_s = 0.1
    cTB.refresh_ui()

    # Automatic import after download
    with cTB.lock_download:
        imports = [
            asset_id
            for asset_id in queued_asset_ids
            if asset_id in cTB.vDownloadQueue and "import" in cTB.vDownloadQueue[asset_id].keys()
        ]
    if len(imports) == 0:
        return next_call_s

    # TODO(Andreas): I doubt this code is currently in use.
    asset_id = imports[0]
    with cTB.lock_download:
        asset_data = cTB.vDownloadQueue[asset_id]
        del cTB.vDownloadQueue[asset_id]
    asset_name = asset_data["name"]
    asset_type = asset_data["data"]["type"]
    asset_size = asset_data["size"]
    if asset_type == "Textures":
        bpy.ops.poliigon.poliigon_material(
            "INVOKE_DEFAULT",
            vAsset=asset_name,
            vSize=asset_size,
            mapping="UV",
            use_16bit=cTB.vSettings["use_16"],
            reuse_material=True,
            vData="@_@_",
            vType=asset_type,
            vApply=0)
    elif asset_type == "HDRIs":
        if cTB.vSettings["hdri_use_jpg_bg"]:
            size_bg = f"{cTB.vSettings['hdrib']}_JPG"
        else:
            size_bg = f"{cTB.vSettings['hdri']}_EXR"
        bpy.ops.poliigon.poliigon_hdri(
            "INVOKE_DEFAULT",
            vAsset=asset_name,
            vSize=asset_size,
            size_bg=size_bg)

    return next_call_s


@persistent
def f_load_handler(*args):
    """Runs when a new file is opened to refresh data"""
    if cTB.vRunning:
        cTB.f_GetSceneAssets()


def f_login_with_website_handler() -> float:
    next_time_tick_s = None
    if cTB.login_state == LoginStates.IDLE:
        cTB._start_login_thread(cTB.f_Login_with_website_init)
        cTB.login_state = LoginStates.WAIT_FOR_INIT
        next_time_tick_s = 0.5

    elif cTB.login_state == LoginStates.WAIT_FOR_INIT:
        if cTB.login_cancelled:
            cTB.vLoginError = cTB.login_res.error
            cTB.login_cancelled = False
            cTB.refresh_ui()
            cTB.login_state = LoginStates.IDLE
            next_time_tick_s = None
        elif cTB.login_res is not None and cTB.login_res.ok:
            cTB.login_res = None
            cTB._start_login_thread(cTB.f_Login_with_website_check)
            cTB.login_state = LoginStates.WAIT_FOR_LOGIN
            next_time_tick_s = 0.25
        elif cTB.login_res is None:
            cTB.login_state = LoginStates.WAIT_FOR_INIT
            next_time_tick_s = 0.25
        else:
            reporting.capture_message(
                "login_with_website_initiation_error",
                f"{cTB.login_res.ok}: {cTB.login_res.error}",
                "error")
            # TODO(SOFT-603): Evaluate error, as soon as we have info which
            #                 errors may occur.
            #                 There're sibling TODOs in
            #                 addon-core/api.py:log_in_with_website() and
            #                                   check_login_with_website_success()
            cTB.refresh_ui()
            cTB.login_state = LoginStates.IDLE
            next_time_tick_s = None

    elif cTB.login_state == LoginStates.WAIT_FOR_LOGIN:
        if cTB.login_cancelled:
            cTB.vLoginError = cTB.login_res.error
            cTB.login_cancelled = False
            cTB.refresh_ui()
            cTB.login_state = LoginStates.IDLE
            next_time_tick_s = None
        elif cTB.login_res is not None and cTB.login_res.ok:
            cTB.login_cancelled = False
            cTB.login_finish(cTB.login_res)
            cTB.login_finalization()
            cTB.login_state = LoginStates.IDLE
            next_time_tick_s = None
        else:
            if cTB.login_thread is None:
                cTB._start_login_thread(cTB.f_Login_with_website_check)
            t = time.time()
            duration = t - cTB.login_time_start
            if duration < 15.0:
                cTB.login_state = LoginStates.WAIT_FOR_LOGIN
                next_time_tick_s = 1.0
            elif duration < 30.0:
                cTB.login_state = LoginStates.WAIT_FOR_LOGIN
                next_time_tick_s = 2.0
            elif duration < 600.0:
                cTB.login_state = LoginStates.WAIT_FOR_LOGIN
                next_time_tick_s = 5.0
            else:
                cTB.login_cancelled = False
                cTB.login_res = api.ApiResponse(body="",
                                                ok=False,
                                                error=ERR_LOGIN_TIMEOUT)
                cTB.login_finish(cTB.login_res)
                cTB.login_finalization()
                cTB._api.invalidated = True
                cTB.login_state = LoginStates.IDLE
                next_time_tick_s = None

    return next_time_tick_s

# ::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::


cTB = c_Toolbox()


def shutdown_asset_browser_client() -> None:
    """Shuts down client Blender process"""

    cTB.asset_browser_jobs_cancelled = True
    cTB.asset_browser_quitting = True

    # Needed for some unit tests to not cause exception on shutdown
    # if ran in "single" mode
    if not cTB.register_success:
        return

    if cTB.proc_blender_client is not None:
        cTB.proc_blender_client.terminate()


def shutdown_thumb_prefetch():
    cTB.thread_prefetch_running = False

    # Avoid issues with Blender exit during unit tests
    if not hasattr(cTB, "queue_thumb_prefetch"):
        return
    if not hasattr(cTB, "gOnlinePreviews"):
        return
    # Just put something into queue, in order to have
    # thread return immediately, instead of waiting for timeout
    cTB.enqueue_thumb_prefetch("quit")


def shutdown_all_downloads():
    immediate_cancel = []
    download_not_done = []

    # Needed for some unit tests to not cause exception on shutdown
    # if ran in "single" mode
    if not cTB.register_success:
        return

    with cTB.lock_download:
        for asset_id in cTB.vDownloadQueue:
            future = cTB.vDownloadQueue[asset_id].get("future", None)
            if future is not None:
                if future.cancel():
                    immediate_cancel.append(asset_id)
                elif not future.done():
                    download_not_done.append(asset_id)
            else:
                # print_debug does nothing during shutdown :(
                # cTB.print_debug(dbg, "No future in download queue")
                immediate_cancel.append(asset_id)
        for asset_id in immediate_cancel:
            del cTB.vDownloadQueue[asset_id]
        for asset_id in download_not_done:
            cTB.vDownloadCancelled.add(asset_id)


@atexit.register
def blender_quitting():
    # CAREFUL! When this exit handler gets called, many Blender data structures
    # are already destructed. We must not use any Blender resources inside here.
    global cTB

    cTB.quitting = True
    shutdown_all_downloads()
    shutdown_thumb_prefetch()
    shutdown_asset_browser_client()

    cTB.vRunning = 0


def register(bl_info):
    addon_version = ".".join([str(vV) for vV in bl_info["version"]])
    cTB.register(addon_version)

    cTB.vRunning = 1

    bpy.app.timers.register(
        f_tick_handler, first_interval=0.05, persistent=True)

    bpy.app.timers.register(
        f_download_handler, first_interval=1, persistent=True)

    if f_load_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(f_load_handler)


def unregister():
    cTB.quitting = True
    reporting.unregister()
    shutdown_all_downloads()
    shutdown_thumb_prefetch()
    shutdown_asset_browser_client()

    if bpy.app.timers.is_registered(f_tick_handler):
        bpy.app.timers.unregister(f_tick_handler)

    if bpy.app.timers.is_registered(f_download_handler):
        bpy.app.timers.unregister(f_download_handler)

    if f_load_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(f_load_handler)

    cTB.vRunning = 0

    # Don't block unregister or closing blender.
    # for vT in cTB.vThreads:
    #    vT.join()

    cTB.vIcons.clear()
    try:
        bpy.utils.previews.remove(cTB.vIcons)
    except KeyError:
        pass

    with cTB.lock_previews:
        cTB.vPreviews.clear()

        try:
            bpy.utils.previews.remove(cTB.vPreviews)
        except KeyError:
            pass
