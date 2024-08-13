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

from typing import List, Tuple
import datetime
import json
import mathutils
import os
import re
import threading
import time
import webbrowser
from math import pi

import addon_utils
from bpy.types import Operator
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
import bpy.utils.previews
import bmesh


from .modules.poliigon_core.assets import SIZES
from .asset_browser import create_poliigon_library
from .material_import_utils import (filter_textures_by_workflow,
                                    find_identical_material,
                                    load_poliigon_node_group,
                                    replace_tex_size)
from .toolbox import (cTB,
                      DisplayError,
                      f_login_with_website_handler,
                      LoginStates,
                      MAX_MY_ASSETS_REQUEST_SIZE)
from .utils import (construct_model_name,
                    f_Ex,
                    f_FExt,
                    f_FName,
                    f_FSplit,
                    f_MDir,
                    open_dir)
from . import ui
from . import reporting


USER_COMMENT_LENGTH = 512  # Max length for user submitted error messages.


def fill_size_drop_down(asset_name: str, asset_type: str):
    """Returns a list of enum items with locally available sizes."""

    # Get list of locally available sizes
    with cTB.lock_assets:
        assets_local = cTB.vAssets["local"]
        if asset_type not in assets_local.keys():
            return []
        assets_local_type = assets_local[asset_type]
        if asset_name not in assets_local_type.keys():
            return []

        asset_data = assets_local_type[asset_name]

    local_sizes = asset_data["sizes"]

    # Populate dropdown items
    items_size = []
    for size in local_sizes:
        # Tuple: (id, name, description, icon, enum value)
        items_size.append((size, size, size))
    return items_size


class POLIIGON_OT_setting(Operator):
    bl_idname = "poliigon.poliigon_setting"
    bl_label = ""
    bl_description = "Edit Poliigon Addon Settings"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_options = {"REGISTER", "INTERNAL"}

    vTooltip: bpy.props.StringProperty(default="", options={"HIDDEN"})  # noqa: F722, F821
    vMode: bpy.props.StringProperty(default="", options={"HIDDEN"})  # noqa: F722, F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @reporting.handle_operator()
    def execute(self, context):
        global cTB

        cTB.get_ui_scale()  # Force update DPI check for scale.
        cTB.print_debug(0, "POLIIGON_OT_setting", self.vMode)

        props = bpy.context.window_manager.poliigon_props

        # .....................................................................

        vUpdate = 0
        vClearCache = 0

        if self.vMode in ["none", ""]:
            return {"FINISHED"}

        # .....................................................................

        elif self.vMode == "set_library":
            tmp_path = cTB.vSettings["set_library"]
            cTB.register(cTB.version)  # Force reloading as if startup.
            cTB.vSettings["set_library"] = tmp_path
            cTB.vWorking["startup"] = 1

            cTB.vSettings["library"] = cTB.vSettings["set_library"]
            if bpy.app.version >= (3, 0):
                create_poliigon_library(force=True)

            vUpdate = 1

            f_MDir(cTB.vSettings["set_library"])

        # .....................................................................

        elif self.vMode.startswith("area_"):
            with cTB.lock_previews:
                cTB.vPreviews.clear()
            cTB.vSettings["area"] = self.vMode.replace("area_", "")

            vUpdate = 2

            # This caused a delay when switching between Poliigon/My Assets
            # vClearCache = 1

            cTB.vSettings["show_settings"] = 0
            cTB.vSettings["show_user"] = 0
            cTB.vActiveAsset = None

            cTB.track_screen_from_area()

        # .....................................................................

        elif self.vMode == "my_account":
            cTB.vSettings["show_settings"] = 0
            cTB.vSettings["show_user"] = 1
            cTB.vActiveAsset = None
            # cTB.vPage = 0
            # cTB.vPages = 1
            cTB.vGoTop = 1
            cTB.vRedraw = 1

            return {"FINISHED"}

        # .....................................................................

        elif self.vMode == "settings":
            cTB.vSettings["show_settings"] = 1
            cTB.vSettings["show_user"] = 0
            cTB.vActiveAsset = None
            cTB.vGoTop = 1

            return {"FINISHED"}

        # .....................................................................

        elif self.vMode.startswith("category_"):
            vFlt = self.vMode.split("_")
            i = int(vFlt[1])
            vF = vFlt[2]
            if i < len(cTB.vSettings["category"][cTB.vSettings["area"]]):
                cTB.vSettings["category"][cTB.vSettings["area"]][i] = vF
            else:
                cTB.vSettings["category"][cTB.vSettings["area"]].append(vF)
            cTB.vSettings["category"][cTB.vSettings["area"]] = cTB.vSettings[
                "category"
            ][cTB.vSettings["area"]][: i + 1]

            categories = cTB.vSettings["category"][cTB.vSettings["area"]]
            if len(categories) > 1 and categories[-1].startswith("All "):
                categories = categories[:-1]
            cTB.vSettings["category"][cTB.vSettings["area"]] = categories

            vUpdate = 1

            cTB.vActiveAsset = None
            cTB.vActiveMat = None
            cTB.vActiveMode = None
            # Do we want to clear searches when switching between areas?
            cTB.vSearch["poliigon"] = props.search_poliigon
            cTB.vSearch["my_assets"] = props.search_my_assets
            cTB.vSearch["imported"] = props.search_imported

        # .....................................................................

        elif self.vMode.startswith("page_"):
            with cTB.lock_previews:
                cTB.vPreviews.clear()
            vP = self.vMode.split("_")[-1]
            if cTB.vPage[cTB.vSettings["area"]] == vP:
                return {"FINISHED"}

            elif vP == "-":
                if cTB.vPage[cTB.vSettings["area"]] > 0:
                    cTB.vPage[cTB.vSettings["area"]] -= 1

            elif vP == "+":
                if cTB.vPage[cTB.vSettings["area"]] < cTB.vPages[cTB.vSettings["area"]]:
                    cTB.vPage[cTB.vSettings["area"]] += 1

            else:
                cTB.vPage[cTB.vSettings["area"]] = int(vP)

            vUpdate = 2

        # .....................................................................

        elif self.vMode.startswith("page@"):
            vPerPage = int(self.vMode.split("@")[1])
            if cTB.vSettings["page"] != vPerPage:
                cTB.vSettings["page"] = vPerPage

                vUpdate = 1
                vClearCache = 1

        # .....................................................................

        elif self.vMode.startswith("clear_search_"):
            if self.vMode.endswith("poliigon"):
                bpy.context.window_manager.poliigon_props.search_poliigon = ""
            elif self.vMode.endswith("my_assets"):
                bpy.context.window_manager.poliigon_props.search_my_assets = ""
            elif self.vMode.endswith("imported"):
                bpy.context.window_manager.poliigon_props.search_imported = ""
            cTB.flush_thumb_prefetch_queue()

        # .....................................................................

        elif self.vMode == "clear_email":
            bpy.context.window_manager.poliigon_props.vEmail = ""

        # .....................................................................

        elif self.vMode == "clear_pass":
            bpy.context.window_manager.poliigon_props.vPassHide = ""
            bpy.context.window_manager.poliigon_props.vPassShow = ""

        # .....................................................................

        # Can be removed if we're not going use the "show password" button
        elif self.vMode == "show_pass":
            if cTB.vSettings["show_pass"]:
                bpy.context.window_manager.poliigon_props.vPassHide = (
                    bpy.context.window_manager.poliigon_props.vPassShow
                )
            else:
                bpy.context.window_manager.poliigon_props.vPassShow = (
                    bpy.context.window_manager.poliigon_props.vPassHide
                )

            cTB.vSettings["show_pass"] = not cTB.vSettings["show_pass"]

        # .....................................................................

        elif self.vMode.startswith("thumbsize@"):
            size = self.vMode.split("@")[1]
            if cTB.vSettings["thumbsize"] != size:
                cTB.vSettings["thumbsize"] = size

                cTB.vRedraw = 1
        # .....................................................................

        elif self.vMode in [
            "apply_subdiv",
            "auto_download",
            "download_lods",
            "download_prefer_blend",
            "download_link_blend",
            "hdri_use_jpg_bg",
            "mat_props_edit",
            "new_top",
            "show_active",
            "show_add_dir",
            "show_asset_info",
            "show_credits",
            "show_default_prefs",
            "show_display_prefs",
            "show_import_prefs",
            "show_asset_browser_prefs",
            "show_mat_ops",
            "show_mat_props",
            "show_mat_texs",
            "show_plan",
            "show_feedback",
            "show_settings",
            "show_user",
            "use_16",
        ]:
            cTB.vSettings[self.vMode] = not cTB.vSettings[self.vMode]

            # Update the session reference of this setting too.
            if self.vMode == "download_link_blend":
                cTB.link_blend_session = cTB.vSettings[self.vMode]
            elif self.vMode == "download_prefer_blend":
                cTB.vRedraw = 1
                cTB.refresh_ui()
            elif self.vMode == "hdri_use_jpg_bg":
                vUpdate = 1

        # .....................................................................

        elif self.vMode.startswith("default_"):
            vK = self.vMode.split("_")[1]
            vR = self.vMode.split("_")[2]
            cTB.vSettings[vK] = vR

            if self.vMode.startswith("default_hdri"):
                idx_size_exr = cTB.HDRI_RESOLUTIONS.index(
                    cTB.vSettings["hdri"])
                idx_size_jpg = cTB.HDRI_RESOLUTIONS.index(
                    cTB.vSettings["hdrib"])
                if idx_size_jpg <= idx_size_exr:
                    idx_size_jpg_new = min(idx_size_exr + 1,
                                           len(cTB.HDRI_RESOLUTIONS) - 1)
                    cTB.vSettings["hdrib"] = cTB.HDRI_RESOLUTIONS[idx_size_jpg_new]

                vUpdate = 1

        # .....................................................................

        elif self.vMode.startswith("disable_dir_"):
            vDir = self.vMode.replace("disable_dir_", "")
            if vDir in cTB.vSettings["disabled_dirs"]:
                cTB.vSettings["disabled_dirs"].remove(vDir)
                cTB.print_debug(0, "Enabled directory: ", vDir)
            else:
                cTB.vSettings["disabled_dirs"].append(vDir)
                cTB.print_debug(0, "Disabled directory: ", vDir)
            cTB.f_GetLocalAssets(force=True)

        # .....................................................................

        elif self.vMode.startswith("del_dir_"):
            vDir = self.vMode.replace("del_dir_", "")
            if vDir in cTB.vSettings["add_dirs"]:
                cTB.vSettings["add_dirs"].remove(vDir)
            cTB.f_GetLocalAssets(force=True)

        # .....................................................................

        elif self.vMode.startswith("prop@"):
            vProp = self.vMode.split("@")[1]
            if vProp in cTB.vSettings["mat_props"]:
                cTB.vSettings["mat_props"].remove(vProp)
            else:
                cTB.vSettings["mat_props"].append(vProp)

        # .....................................................................

        elif self.vMode.startswith("preset@"):
            if cTB.vEditPreset == self.vMode.split("@")[1]:
                try:
                    cTB.vPresets[cTB.vEditPreset] = [
                        float(vV)
                        for vV in context.scene.vEditText.replace(" ", "").split(";")
                    ]
                    cTB.vEditPreset = None
                except Exception:
                    return {"FINISHED"}
            else:
                cTB.vEditPreset = self.vMode.split("@")[1]
                context.scene.vEditText = self.vMode.split("@")[2]
                return {"FINISHED"}

        # .....................................................................

        elif self.vMode == "view_more":
            prev_area = cTB.vSettings["area"]
            cTB.vSettings["area"] = "poliigon"
            cat_area = cTB.vSettings["category"][prev_area]
            cTB.vSettings["category"][cTB.vSettings["area"]] = cat_area

            vUpdate = 2

            cTB.vSettings["show_settings"] = 0
            cTB.vSettings["show_user"] = 0
            cTB.vSearch["poliigon"] = cTB.vSearch[prev_area]
            props.search_poliigon = cTB.vSearch[prev_area]
            cTB.vActiveAsset = None

        # .....................................................................

        else:
            reporting.capture_message("invalid_setting_mode", self.vMode)
            self.report({"WARNING"}, f"Invalid setting mode {self.vMode}")
            return {'CANCELLED'}

        # .....................................................................

        if vClearCache:
            with cTB.lock_asset_index:
                cTB.vAssetsIndex["poliigon"] = {}
                cTB.vAssetsIndex["my_assets"] = {}
                cTB.vAssetsIndex["imported"] = {}

        if vUpdate:
            cTB.flush_thumb_prefetch_queue()

            if vUpdate == 1:
                cTB.vPage[cTB.vSettings["area"]] = 0
                cTB.vPages[cTB.vSettings["area"]] = 1

            # Not setting cursor as it can lead to being stuck on "wait".
            # bpy.context.window.cursor_set("WAIT")

            # TODO(SOFT-762): refactor to cache raw API request, also validate
            # if this needs re-requesting (has calls to f_GetCategoryChildren).
            cTB.f_GetCategories()

            cTB.vInterrupt = time.monotonic()
            # cTB.vGettingData = 1

            cTB.f_GetSceneAssets()

            if cTB.vSettings["area"] == "poliigon":
                cTB.f_GetAssets()

            elif cTB.vSettings["area"] == "my_assets":
                cTB.f_GetLocalAssets()
                cTB.f_GetAssets()

            cTB.vGoTop = 1
            cTB.vRedraw = 1

        # .....................................................................

        cTB.f_SaveSettings()

        return {"FINISHED"}


class POLIIGON_OT_user(Operator):
    bl_idname = "poliigon.poliigon_user"
    bl_label = ""
    bl_description = ""
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_options = {"REGISTER", "INTERNAL"}

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    vMode: StringProperty(options={"HIDDEN"})  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @reporting.handle_operator()
    def execute(self, context):
        global cTB

        props = bpy.context.window_manager.poliigon_props
        cTB.vRedraw = 1

        if self.vMode == "login":
            if "@" not in props.vEmail or len(props.vPassHide) < 6:
                cTB.clear_user_invalidated()
                cTB.vLoginError = cTB.ERR_CREDS_FORMAT
                return {"CANCELLED"}

        if self.vMode == "login_with_website":
            cTB.print_debug(0, "Sending login with website request")
            if cTB.login_state != LoginStates.IDLE or cTB.login_thread is not None:
                print("MUST NOT OCCUR, a previous login still ongoing?")

            if bpy.app.timers.is_registered(f_login_with_website_handler):
                bpy.app.timers.unregister(f_login_with_website_handler)

            cTB.login_state = LoginStates.IDLE
            cTB.login_res = None
            cTB.login_time_start = time.time()

            bpy.app.timers.register(f_login_with_website_handler,
                                    first_interval=0.1,
                                    persistent=True)

        elif self.vMode == "login_cancel":
            cTB.login_cancelled = True

        elif self.vMode == "login_switch_to_email":
            cTB.vLoginError = None
            cTB.login_via_browser = False

        elif self.vMode == "login_switch_to_browser":
            cTB.login_via_browser = True

        else:  # login or logout
            if bpy.app.timers.is_registered(f_login_with_website_handler):
                bpy.app.timers.unregister(f_login_with_website_handler)
                cTB.login_state = LoginStates.IDLE
                cTB.login_res = None
                cTB.login_cancelled = True

            cTB.vWorking["login"] = 1

            cTB.print_debug(0, "Sending login request")
            bpy.context.window.cursor_set("WAIT")

            vThread = threading.Thread(target=cTB.f_Login, args=(self.vMode,))
            vThread.daemon = 1
            vThread.start()
            cTB.vThreads.append(vThread)

        return {"FINISHED"}


class POLIIGON_OT_link(Operator):
    bl_idname = "poliigon.poliigon_link"
    bl_label = ""
    bl_description = "(Find asset on Poliigon.com in your default browser)"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_options = {"REGISTER", "INTERNAL"}

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    vMode: StringProperty(options={"HIDDEN"})  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    def open_asset_url(self, asset_id: int) -> None:
        # TODO(Andreas): Use addon.open_asset_url() after AssetIndex refactor

        dbg = 0

        asset_data = cTB.get_data_for_asset_id(asset_id)
        cTB.print_debug(dbg, "Data for asset id:", str(asset_data))

        # URL should be populated, but if missing, fallback to search
        url = asset_data.get("url")
        if url is None:
            reporting.capture_message("asset_lacking_url", asset_id, "error")
            name = asset_data.get("name", asset_id)
            url_base = cTB._api.get_base_url(cTB.env.env_name)
            url = f"{url_base}/search/{name}"
        url = cTB._api.add_utm_suffix(url)
        webbrowser.open(url)

    @reporting.handle_operator()
    def execute(self, context):
        global cTB

        notification_id = ""
        if self.vMode.startswith("notify"):
            _, vURL, action, notification_id = self.vMode.split("@")
            cTB.click_notification(notification_id, action)

            open_free_survey = notification_id == "NPS_INAPP_FREE"
            open_paying_survey = notification_id == "NPS_INAPP_ACTIVE"
            if open_free_survey or open_paying_survey:
                time_now = cTB._get_datetime_now()
                cTB.vSettings["last_nps_open"] = time_now.timestamp()
                cTB.f_SaveSettings()
            webbrowser.open(vURL)
        elif self.vMode == "survey":
            cTB._api.open_poliigon_link(self.vMode, env_name=cTB.env.env_name)
            cTB.click_notification("FEEDBACK_SURVEY_LINK", "survey")
        elif self.vMode in cTB._api._url_paths:
            cTB._api.open_poliigon_link(self.vMode, env_name=cTB.env.env_name)
        else:
            # Assume passed in asset id, open asset page.
            asset_id = int(self.vMode)
            self.open_asset_url(asset_id)

        cTB.finish_notification(notification_id)
        return {"FINISHED"}


class POLIIGON_OT_download(Operator):
    bl_idname = "poliigon.poliigon_download"
    bl_label = ""
    bl_description = "(Download Asset from Poliigon.com)"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_options = {"REGISTER", "INTERNAL"}

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    vAsset: StringProperty(options={"HIDDEN"})  # noqa: F821
    vType: StringProperty(options={"HIDDEN"})  # noqa: F821
    vMode: StringProperty(options={"HIDDEN"})  # noqa: F821
    vSize: StringProperty(options={"HIDDEN"})  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @reporting.handle_operator()
    def execute(self, context):

        if self.vMode == "download":
            cTB.reset_asset_error(asset_name=self.vAsset)
            if ";" in self.vSize:
                # Presumed previously supported multi size downloads using ;
                # as a separator. No longer allowed.
                reporting.capture_message(
                    "reached_legacy_multi_vsize_for_dl",
                    f"vSize contained unexpected `;` {self.vSize}")
                self.report({"ERROR"}, "Failed to download, multiple sizes specified")
                return {"CANCELLED"}

            with cTB.lock_assets:
                asset_data = cTB.vAssets[cTB.vSettings["area"]][self.vType][self.vAsset]

            asset_id = asset_data["id"]

            size = None
            if self.vSize != '':
                size = self.vSize

            download_dict = {
                "data": asset_data,
                "size": size,
                "download_size": None
            }
            with cTB.lock_download:
                cTB.vDownloadQueue[asset_id] = download_dict

            cTB.print_debug(0, f"Queue download asset {asset_id}")
            fut = cTB.download_asset_thread(asset_id)
            download_dict["future"] = fut

        elif self.vMode == "purchase":
            asset_name, asset_id = self.vAsset.split("@")
            asset_id = int(asset_id)
            cTB.reset_asset_error(asset_id=asset_id)

            with cTB.lock_assets:
                asset_data = None
                assets_by_type = cTB.vAssets[cTB.vSettings["area"]][self.vType]
                if asset_name in assets_by_type:
                    asset_data = assets_by_type[asset_name]

            if asset_data is not None:
                # keeping this for access to the data
                cTB.print_debug(0, f"Purchase asset {asset_id}")
                cTB.queue_purchase(asset_id, asset_data)
            else:
                bpy.ops.poliigon.refresh_data()
                err_desc = "Error during purchase, please try again (key error)"
                err = DisplayError(button_label="Failed, retry",
                                   description=err_desc,
                                   asset_id=asset_id,
                                   asset_name=asset_name)
                cTB.ui_errors.append(err)
                cTB.vRedraw = 1
                cTB.refresh_ui()
                return {"CANCELLED"}

        cTB.refresh_ui()

        return {"FINISHED"}


class POLIIGON_OT_cancel_download(Operator):
    bl_idname = "poliigon.cancel_download"
    bl_label = "Cancel download"
    bl_description = "Cancel downloading this asset"
    bl_options = {"INTERNAL"}

    asset_id: IntProperty(default=0, options={'SKIP_SAVE'})  # noqa: F821

    @reporting.handle_operator(silent=True)
    def execute(self, context):
        if self.asset_id == 0:
            return {'CANCELLED'}
        immediate_cancel = False
        download_done = False

        with cTB.lock_download:
            if self.asset_id in cTB.vDownloadQueue:
                future = cTB.vDownloadQueue[self.asset_id].get("future", None)
                if future is not None:
                    immediate_cancel = future.cancel()
                    download_done = future.done()
                else:
                    cTB.print_debug(0, "No future in download queue")
                    immediate_cancel = True
                    download_done = False
            if immediate_cancel:
                del cTB.vDownloadQueue[self.asset_id]
                cTB.refresh_ui()
            elif not download_done:
                cTB.vDownloadCancelled.add(self.asset_id)
        cTB.print_debug(0, "Cancelled download", self.asset_id)
        self.report({'WARNING'}, "Cancelling download")
        return {'FINISHED'}


class POLIIGON_OT_options(Operator):
    bl_idname = "poliigon.poliigon_asset_options"
    bl_label = ""
    bl_description = "Asset Options"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_options = {"REGISTER", "INTERNAL"}

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    vType: StringProperty(options={"HIDDEN"})  # noqa: F821
    vData: StringProperty(options={"HIDDEN"})  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @reporting.handle_operator()
    def execute(self, context):
        global cTB

        if self.vData.startswith("size@"):
            cTB.vSetup["size"] = self.vData.split("@")[1]

        elif self.vData == "disp":
            cTB.vSetup["disp"] = not cTB.vSetup["disp"]

        else:
            vAsset, vMode = self.vData.split("@")

            with cTB.lock_assets:
                vAData = cTB.vAssets["my_assets"][self.vType][vAsset]

            if vMode == "dir":
                vDirs = sorted(
                    list(set([os.path.dirname(vF) for vF in vAData["files"]]))
                )
                for i in range(len(vDirs)):
                    if vAsset in vDirs[i]:
                        vDirs[i] = vDirs[i].split(vAsset)[0] + vAsset
                vDirs = sorted(list(set(vDirs)))

                for vDir in vDirs:
                    open_dir(vDir)

            elif vMode == "link":
                vName = vAsset
                vAssetType = "T"
                vMods = [
                    vF for vF in vAData["files"] if f_FExt(vF) in [".fbx", ".blend"]
                ]
                if len(vMods):
                    vAssetType = "M"
                vName = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", vName)
                vName = (
                    (re.sub(r"(?<=[a-z])(?=[0-9])", " ", vName))
                    .lower()
                    .replace(" ", "-")
                )
                if vAssetType == "T":
                    vURL = "https://www.poliigon.com/texture/" + vName
                elif vAssetType == "M":
                    vURL = "https://www.poliigon.com/model/" + vName
                webbrowser.open(vURL)

        return {"FINISHED"}


class POLIIGON_OT_active(Operator):
    bl_idname = "poliigon.poliigon_active"
    bl_label = ""
    bl_description = "Set Active Asset"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_options = {"REGISTER", "INTERNAL"}

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    vMode: StringProperty(options={"HIDDEN"})  # noqa: F821
    vType: StringProperty(options={"HIDDEN"})  # noqa: F821
    vData: StringProperty(options={"HIDDEN"})  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @reporting.handle_operator()
    def execute(self, context):
        global cTB

        if self.vData == "":
            cTB.vActiveType = None
            cTB.vActiveAsset = None
            cTB.vActiveMat = None
            cTB.vActiveMode = None

        elif self.vMode == "asset":
            cTB.vActiveType = self.vType
            cTB.vActiveAsset = self.vData
            if cTB.vActiveAsset in cTB.imported_assets["Textures"].keys():
                cTB.vActiveMat = cTB.imported_assets["Textures"][cTB.vActiveAsset][0].name
                context.scene.vEditMatName = cTB.vActiveMat
            else:
                cTB.vActiveMat = None
            cTB.vActiveMode = "asset"
            cTB.vSettings["show_active"] = 1
            cTB.vGoTop = 1

        elif self.vMode == "mat":
            cTB.vActiveType = self.vType
            if "@" in self.vData:
                cTB.vActiveAsset, cTB.vActiveMat = self.vData.split("@")
            else:
                cTB.vActiveMat = self.vData
            cTB.vActiveMode = "asset"

        elif self.vMode == "mixer":
            cTB.vActiveType = self.vType
            cTB.vActiveAsset = self.vData
            context.scene.vEditMatName = cTB.vActiveAsset
            cTB.vActiveMat = self.vData
            cTB.vActiveMode = "mixer"
            cTB.vSettings["show_active"] = 1
            cTB.vGoTop = 1

        elif self.vMode == "mix":
            cTB.vActiveMode = "mixer"
            cTB.vActiveMix = self.vData

        elif self.vMode == "mixmat":
            cTB.vActiveMode = "mixer"
            cTB.vActiveMixMat = self.vData

        elif self.vMode == "poliigon":
            cTB.vActiveMode = "poliigon"
            cTB.vActiveAsset = self.vData

        elif self.vMode == "settings":
            # f_Settings()
            return {"FINISHED"}

        cTB.f_GetActiveData()

        return {"FINISHED"}


class POLIIGON_OT_detail(Operator):
    bl_idname = "poliigon.poliigon_detail"
    bl_label = ""
    bl_description = "Reset Property to Default"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_options = {"REGISTER", "INTERNAL"}

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    vData: StringProperty(options={"HIDDEN"})  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    @reporting.handle_operator()
    def execute(self, context):
        if context.object.cycles.dicing_rate != context.scene.vDispDetail:
            context.object.cycles.dicing_rate = context.scene.vDispDetail
            context.object.modifiers["Subdivision"].subdivision_type = "SIMPLE"

        return {"FINISHED"}


class POLIIGON_OT_folder(Operator):
    bl_idname = "poliigon.poliigon_folder"
    bl_label = "Open Asset Folder"
    bl_description = "Open Asset Folder in system browser"
    bl_options = {"INTERNAL"}

    vAsset: StringProperty(options={"HIDDEN"})  # noqa: F821

    @reporting.handle_operator()
    def execute(self, context):
        asset_data = cTB.get_data_for_asset_name(self.vAsset,
                                                 area_order=["local",
                                                             "my_assets",
                                                             "poliigon"])
        files = asset_data["files"]

        if not files:
            msg = f"No files found to open for {self.vAsset}"
            avail_keys = str(asset_data.keys())
            self.report({"ERROR"}, msg)
            reporting.capture_message(
                "open_folder_failed",
                f"{msg}, data: {avail_keys}")
            cTB.print_debug(0, msg)
            return {'CANCELLED'}

        dirs = [os.path.dirname(path) for path in files]
        dirs = list(set(dirs))
        if len(dirs) > 1:
            cTB.print_debug(0, "Opening more than one directory:", dirs)

        if self.vAsset.startswith("Poliigon_"):
            dirs_all = dirs.copy()
            dirs = []
            for _dir in dirs_all:
                path_base, name_dir_last = os.path.split(_dir)
                if name_dir_last in SIZES:
                    dirs.append(path_base)
                else:
                    dirs.append(_dir)
            dirs = list(set(dirs))

        # Open folder, different methods for different operating systems.
        did_open = False
        for vDir in dirs:
            _this_did_open = open_dir(vDir)
            did_open = did_open or _this_did_open

        if not did_open:
            reporting.capture_message("open_folder_failed", vDir)
            self.report({"ERROR"}, f"Open folder here: {vDir}")
            return {'CANCELLED'}

        return {"FINISHED"}


class POLIIGON_OT_library(Operator):
    bl_idname = "poliigon.poliigon_library"
    bl_label = "Poliigon Library"
    bl_description = "(Set Poliigon Library Location)"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_options = {"REGISTER", "INTERNAL"}

    _enum_items = [
        ("set_library", "set_library", "Set path on first load"),
        ("update_library", "update_library", "Update path from preferences")]

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    directory: StringProperty(subtype="DIR_PATH")  # noqa: F821
    vMode: EnumProperty(items=_enum_items, options={"HIDDEN"})  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @reporting.handle_operator()
    def execute(self, context):
        global cTB

        vDir = self.directory.replace("\\", "/")

        if self.vMode == "set_library":
            # Stage for confirmation on startup (or after deleted)
            cTB.vSettings["set_library"] = vDir
        else:
            # Update_library, from user preferences
            cTB.vSettings["library"] = vDir
            cTB.f_GetLocalAssets(force=True)
            if bpy.app.version >= (3, 0):
                create_poliigon_library(force=True)

            cTB.vRedraw = 1

        cTB.f_SaveSettings()

        # if os.path.exists(vDir):
        #    bpy.ops.poliigon.poliigon_setting("INVOKE_DEFAULT")

        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class POLIIGON_OT_directory(Operator):
    bl_idname = "poliigon.poliigon_directory"
    bl_label = "Add Additional Directory"
    bl_description = "Add Additional Directory to search for assets"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_options = {"REGISTER", "INTERNAL"}

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    directory: StringProperty(subtype="DIR_PATH")  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @reporting.handle_operator()
    def execute(self, context):
        global cTB

        vDir = self.directory.replace("\\", "/")
        cTB.print_debug(0, vDir)

        if not os.path.exists(vDir) or vDir in cTB.vSettings["add_dirs"]:
            return {"FINISHED"}

        cTB.vSettings["add_dirs"].append(vDir)
        if vDir in cTB.vSettings["disabled_dirs"]:
            cTB.vSettings["disabled_dirs"].remove(vDir)

        cTB.f_GetLocalAssets(force=True)

        bpy.ops.poliigon.poliigon_setting("INVOKE_DEFAULT")

        return {"FINISHED"}

    def invoke(self, context, event):
        cTB.print_debug(0, self.directory)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class POLIIGON_OT_category(Operator):
    bl_idname = "poliigon.poliigon_category"
    bl_label = "Select a Category"
    bl_description = "Select a Category"
    bl_options = {"REGISTER", "INTERNAL"}

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    vData: StringProperty(options={"HIDDEN"})  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    def execute(self, context):
        vIdx = self.vData.split("@")[0]
        vFlts = self.vData.split("@")[1:]

        ui.show_categories_menu(
            cTB,
            categories=vFlts,
            index=vIdx
        )

        return {"FINISHED"}


# ::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::


class POLIIGON_OT_preview(Operator):
    """Quick asset preview by downloading download and applying watermark."""
    bl_idname = "poliigon.poliigon_preview"
    bl_label = ""
    bl_description = "Preview Material"
    bl_options = {"GRAB_CURSOR", "BLOCKING", "REGISTER", "INTERNAL", "UNDO"}

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    vType: StringProperty(options={"HIDDEN"})  # noqa: F821
    vAsset: StringProperty(options={"HIDDEN"})  # noqa: F821

    _is_backplate = None
    _name = None
    _asset_id = None

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @reporting.handle_operator()
    def execute(self, context):
        cTB.reset_asset_error(asset_name=self.vAsset)
        t_start = time.time()
        files = self.download_preview(context)
        t_downloaded = time.time()

        if not files:
            self.report({'ERROR'}, "Failed to download preview files")
            struc_str = f"asset_name: {self.vAsset}"
            reporting.capture_message(
                "quick_preview_download_failed", struc_str, "error")
            return {'CANCELLED'}

        # Warn user on viewport change before post process, so that any later
        # warnings will popup and take priority to be visible to user. Blender
        # shows the last "self.report" message only (but all print to console).
        self.report_viewport(context)
        cTB.refresh_ui()

        if self._is_backplate:
            res = self.post_process_backplate(context, files)
        else:
            res = self.post_process_material(context, files)

        t_post_processed = time.time()
        total_time = t_post_processed - t_start
        download_time = t_downloaded - t_start

        debug_str = f"Preview Total time: {total_time}, download: {download_time}"
        cTB.print_debug(0, "POLIIGON_OT_preview", debug_str)

        cTB.signal_preview_asset(asset_id=self._asset_id)
        return res

    def download_preview(self, context):
        """Download a preview and return expected files."""
        vAData = cTB.get_poliigon_asset(self.vType, self.vAsset)
        self.vAData = vAData.copy()  # needed for new material importer
        self._is_backplate = cTB.check_backplate(self.vAsset)
        self._asset_id = vAData.get("id", 0)

        self._name = "PREVIEW_" + self.vAsset

        if self._is_backplate:
            bpy.context.window.cursor_set("WAIT")
            vFile = os.path.join(
                cTB.gOnlinePreviews, self.vAsset + "_quickpreview.jpg")

            files = [vFile]
            if not os.path.exists(vFile):
                with cTB.lock_assets:
                    vURL = cTB.vAssets["poliigon"][self.vType][self.vAsset]["preview"]
                vTemp = os.path.join(
                    cTB.gOnlinePreviews, self.vAsset + "_quickpreviewX.jpg")
                self.run_download_backplate(vURL, vTemp, vFile)
            return files

        elif len(vAData["quick_preview"]):
            bpy.context.window.cursor_set("WAIT")
            vDir = os.path.join(
                cTB.gSettingsDir.replace("Blender", "OnlinePreviews"),
                self.vAsset)
            f_MDir(vDir)

            files = []
            download_files = []
            for vURL in vAData["quick_preview"]:
                vFName = os.path.basename(vURL.split("?")[0])

                # Might need to skip certain maps to improve performance
                # if any(vM in vFName for vM in ['BUMP','DISP']+[f'VAR{i}' for i in range(2,9)]) :
                #    continue

                vFile = os.path.join(vDir, vFName)
                if not os.path.exists(vFile):
                    download_files.append([vURL, vFile])
                files.append(vFile)

            if download_files:
                # cTB.vQuickPreviewQueue[vAsset] = [vD[1] for vD in vDownload]
                self.run_download_material(download_files)
            return files
        else:
            return []

    def run_download_material(self, download_files):
        """Synchronous function to download material preview."""

        urls = []
        files = []
        for preview_set in download_files:
            urls.append(preview_set[0])
            basename, ext = f_FSplit(preview_set[1])
            tmp_name = f"{basename}X{ext}"
            files.append(tmp_name)

        req = cTB._api.pooled_preview_download(urls, files)

        if not req.ok:
            self.report({"ERROR"}, req.error)
            # Continue, as some may have worked.

        for vURL, vFile in download_files:
            vTemp, vExt = f_FSplit(vFile)
            vTemp += "X" + vExt

            try:
                vFile_exists = os.path.exists(vFile)
                vTemp_exists = os.path.exists(vTemp)
                if vFile_exists and vTemp_exists:
                    os.remove(vFile)
                elif not vFile_exists and not vTemp_exists:
                    raise FileNotFoundError
                if vTemp_exists:
                    os.rename(vTemp, vFile)
            except FileNotFoundError:
                msg = f"Neither {vFile}, nor {vTemp} exist"
                reporting.capture_message(
                    "download_mat_existing_file", msg, "error")
                self.report({"ERROR"}, msg)
            except FileExistsError:
                msg = f"File {vFile} already exists, failed to rename"
                reporting.capture_message(
                    "download_mat_rename", msg, "error")
                self.report({"ERROR"}, msg)
            except Exception as e:
                reporting.capture_exception(e)
                self.report({"ERROR"}, "Failed to rename file")

    def run_download_backplate(self, vURL, vTemp, vFile):
        """Synchronous function to download backplate preview."""
        vReq = cTB._api.download_preview(vURL, vTemp, self.vAsset)
        if vReq.ok:
            try:
                if os.path.exists(vFile):
                    os.remove(vFile)
                os.rename(vTemp, vFile)
            except FileExistsError:
                msg = f"File {vFile} already exists"
                reporting.capture_message(
                    "download_backplate_exists", msg, "error")
                self.report({"ERROR"}, msg)
            except Exception as e:
                reporting.capture_exception(e)
                self.report({"ERROR"}, "Failed to rename file")
        else:
            reporting.capture_message(
                "download_backplate_resp_err", vReq.error, "error")
            self.report({"ERROR"}, vReq.error)

    def post_process_backplate(self, context, files):
        if not files:  # API error from above.
            reporting.capture_message(
                "preview_file_not_populated",
                f"Preview file not populated: {self.vAsset}",
                "error")
            return {'CANCELLED'}
        vFile = files[0]
        cTB.f_BuildBackplate(self.vAsset, self._name, vFile, reuse=True)
        bpy.context.window.cursor_set("DEFAULT")
        return {'FINISHED'}

    def post_process_material(self, context, files):
        """Run after the download has completed."""
        bpy.context.window.cursor_set("DEFAULT")
        # TODO(Andreas): Old code, revive to get WM import with legacy importer
        #                working again?
        # vMat = cTB.mat_import.f_BuildMat(
        #     self.vAsset, "PREVIEW", files, "Textures", self)
        self.vAData["files"] = files
        vMat = cTB.mat_import.import_material(
            asset_ref=self.vAData,
            asset_name=self.vAsset,  # only passed for legacy importer, not used in here
            do_apply=False,
            workflow="METALNESS",
            size="PREVIEW",
            size_bg=None,
            lod="",
            variant=None,
            name_material=None,
            name_mesh=None,
            ref_objs=None,
            projection="UV",
            use_16bit=True,
            mode_disp="NORMAL",
            translate_x=0.0,
            translate_y=0.0,
            scale=1.0,
            global_rotation=0.0,
            aspect_ratio=1.0,
            displacement=0.0,
            keep_unused_tex_nodes=False,
            reuse_existing=True,
            ref_operator=self  # only passed for legacy impoorter, MUST NOT be used in here
        )

        if vMat is None:
            self.report({"ERROR"}, "Material could not be created.")
            reporting.capture_message(
                "could_not_create_preview_mat", self.vAsset, "error")
            return {"CANCELLED"}

        vSel = [vObj for vObj in context.scene.objects if vObj.select_get()]
        if len(vSel):
            for vObj in vSel:
                vObj.active_material = vMat

        else:
            vObjs = [vO for vO in bpy.data.objects]

            # TODO: Remove operator calls in favor of direct creation/manip,
            # for speed and stability. This: bpy.data.objects.new()
            bpy.ops.mesh.primitive_plane_add(
                size=1.0, location=context.scene.cursor.location,
                enter_editmode=False, rotation=(0, 0, 0)
            )

            vObj = [vO for vO in bpy.data.objects if vO not in vObjs][0]

            vObj.active_material = vMat

            vImage = None
            for vI in bpy.data.images:
                if vI.filepath in files:
                    vImage = vI

            if vImage is not None:
                vW = vImage.size[0] / vImage.size[1]

                vObj.dimensions = mathutils.Vector((vW, 1.0, 0))

                vObj.delta_scale[0] = 1
                vObj.delta_scale[1] = 1
                vObj.delta_scale[2] = 1

                bpy.ops.object.select_all(action="DESELECT")
                vObj.select_set(True)

                bpy.ops.object.transform_apply(
                    location=False, rotation=False, scale=True
                )

        bpy.context.window.cursor_set("DEFAULT")
        return {"FINISHED"}

    def report_viewport(self, context):
        """Send the appropriate report based on the current shading mode."""
        any_mat_or_render = False
        for vA in context.screen.areas:
            if vA.type == "VIEW_3D":
                for vSpace in vA.spaces:
                    if vSpace.type == "VIEW_3D":
                        if vSpace.shading.type in ["MATERIAL", "RENDERED"]:
                            any_mat_or_render = True
        if not any_mat_or_render:
            self.report({'WARNING'},
                        ("Enter material or rendered mode to view applied "
                        "quick preview"))


class POLIIGON_OT_view_thumbnail(Operator):
    bl_idname = "poliigon.view_thumbnail"
    bl_label = ""
    bl_description = "View larger thumbnail"
    bl_options = {"INTERNAL"}

    tooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    asset: StringProperty(options={"HIDDEN"})  # noqa: F821
    thumbnail_index: IntProperty(min=0, options={"HIDDEN"})  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.tooltip

    @reporting.handle_operator()
    def execute(self, context):
        cTB.reset_asset_error(asset_name=self.asset)

        # Check if index count is valid, noting that index 0 is should be the
        # low resolution preview, and index 1 the same image at 1K.
        asset_data = cTB.get_data_for_asset_name(self.asset)
        count = len(asset_data["thumbnails"])
        if self.thumbnail_index > count + 1:
            msg = f"Invalid thumbnail index: {self.asset} {self.thumbnail_index}"
            self.report({"ERROR"}, msg)
            reporting.capture_message("bad_thumbnail_index", msg, 'info')
            return {'CANCELLED'}

        # We use the show render operator to hack setting an explicit size,
        # be sure to capture the resolution to revert back.
        render = bpy.context.scene.render
        init_res_x = render.resolution_x
        init_res_y = render.resolution_y
        if hasattr(context.preferences.view, "render_display_type"):
            init_display = bpy.context.preferences.view.render_display_type
        else:
            init_display = context.scene.render.display_mode

        # Don't modify by get_ui_scale(), as it uses physical pixel size. Also
        # making it smaller than 1024 to minimize margins around image.
        pixels = int(1000)

        try:
            # Modify render settings to force new window to appear.
            render.resolution_x = pixels
            render.resolution_y = pixels
            if hasattr(context.preferences.view, "render_display_type"):
                context.preferences.view.render_display_type = "WINDOW"
            else:
                context.scene.render.display_mode = "WINDOW"

            # Main loading steps
            area = self.create_window()
            self.download_thumbnail()
            res = self.load_preview(area)

        except Exception as e:
            # If exception occurs, will run after the finally block below.
            raise e

        finally:
            # Ensure we always restore render settings and preferences.
            render.resolution_x = init_res_x
            render.resolution_y = init_res_y
            if hasattr(context.preferences.view, "render_display_type"):
                context.preferences.view.render_display_type = init_display
            else:
                context.scene.render.display_mode = init_display

        cTB.track_screen("large_preview")

        return res

    def create_window(self):
        # Call image editor window
        bpy.ops.render.view_show("INVOKE_DEFAULT")

        # Set up the window as needed.
        area = None
        for window in bpy.context.window_manager.windows:
            this_area = window.screen.areas[0]
            if this_area.type == "IMAGE_EDITOR":
                area = this_area
                break
        if not area:
            return None

        # TODO: Showcase index/count once button exists to flip through.
        # asset_data = cTB.get_data_for_asset_name(self.asset)
        # i = self.thumbnail_index if self.thumbnail_index > 0 else 1
        # count = len(asset_data["thumbnails"])
        # area.header_text_set(f"Asset thumbnail: {self.asset} ({i}/{count})")
        area.header_text_set(f"Asset thumbnail: {self.asset}")
        area.show_menus = False
        return area

    def download_thumbnail(self):
        """Download the target thumbnail if not local, no threading."""
        cTB.print_debug(0, "Download thumbnail index", self.thumbnail_index)
        bpy.context.window.cursor_set("WAIT")
        try:
            res = cTB.f_DownloadPreview(self.asset, self.thumbnail_index)
            res.result()  # Wait until this specific thread completes.
        except Exception as e:
            raise e
        finally:
            bpy.context.window.cursor_set("DEFAULT")

    def load_preview(self, area):
        """Load in the image preview based on the area."""
        path = cTB.f_GetThumbnailPath(self.asset, self.thumbnail_index)

        if not os.path.isfile(path):
            self.report({'ERROR'}, "Could not find image preview")
            msg = f"{self.asset}: Could not find image preview {path}"
            reporting.capture_message("thumbnail_file_missing", msg, "error")
            return {'CANCELLED'}

        thumbnail = bpy.data.images.load(path)

        if area:
            area.spaces[0].image = thumbnail
        else:
            msg = "Open the image now loaded in an image viewer"
            self.report({"ERROR"}, msg)
            err = "Failed to open window for preview"
            reporting.capture_message("img_window_failed_open", err, 'info')

        # Tag this image with a property, could be used to trigger UI draws in
        # the viewer in the future.
        thumbnail["poliigon_thumbnail"] = True

        return {'FINISHED'}


class POLIIGON_OT_material(Operator):
    bl_idname = "poliigon.poliigon_material"
    bl_label = "Poliigon Material Import"
    bl_description = "Create Material"
    bl_options = {"GRAB_CURSOR", "BLOCKING", "REGISTER", "INTERNAL", "UNDO"}

    exec_count = 0

    _uvopts = [("UV", "UV", "UV"),
               ("MOSAIC", "Mosaic", "Poliigon Mosaic"),
               ("FLAT", "Flat", "Flat"),
               ("BOX", "Box", "Box"),
               ("SPHERE", "Sphere", "Sphere"),
               ("TUBE", "Tube", "Tube")]

    def _get_dispopts(self, context):
        options = [
            ("NORMAL", "Normal Only", "Use the Normal Map for surface details")
        ]
        is_cycles = context.scene.render.engine == "CYCLES"
        is_eevee_next = context.scene.render.engine == "BLENDER_EEVEE_NEXT"
        if is_cycles or is_eevee_next:
            # Only cycles and 4.2's eevee next support displacement
            options.append(("BUMP", "Bump Only", "Use the displacement map for surface details without displacement"))
            options.append(("DISP", "Displacement and Bump", "Use the displacement map for surface details and physical displacement"))

        if context.scene.render.engine == "CYCLES":
            # While Eevee next in blender 4.2 could have the above options,
            # below is still only relevnat in cycles.
            options.append((
                "MICRO",
                "Adaptive Displacement Only",
                ("Use the displacement map for physical displacement with adpative geometry subdivisions\n"
                 "Note: This has a high render performance cost!")
            ))
        return options

    # Hidden properties
    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    vAsset: StringProperty(options={"HIDDEN"})  # noqa: F821
    vType: StringProperty(options={"HIDDEN"})  # noqa: F821
    vData: StringProperty(options={"HIDDEN"})  # noqa: F821
    vApply: IntProperty(options={"HIDDEN"}, default=1)  # noqa: F821

    # Properties exposed in Redo Last
    def _fill_size_drop_down(self, context):
        return fill_size_drop_down(self.vAsset, self.vType)

    def _update_displacement_options(self, context):
        if self.vAsset.startswith("Poliigon_"):  # TODO(patrick): use conv=1
            self.displacement = 0.2
        else:
            self.displacement = 0.05

    vSize: EnumProperty(
        name="Texture",  # noqa: F821
        items=_fill_size_drop_down,
        description="Change size of assigned textures.")  # noqa: F722
    mapping: EnumProperty(name="Mapping",  # noqa: F821
                          items=_uvopts,
                          default="UV")  # noqa: F821
    mode_disp: EnumProperty(name="Displacement",  # noqa: F821
                            items=_get_dispopts,
                            default=0,  # noqa: F821
                            update=_update_displacement_options)
    displacement: FloatProperty(name="Displacement Strength", default=0.0)  # noqa: F722
    scale: FloatProperty(name="Scale", default=1.0)  # noqa: F821
    use_16bit: BoolProperty(name="16-Bit Textures (if any)", default=False)  # noqa: F722
    reuse_material: BoolProperty(name="Reuse Material", default=True)  # noqa: F722
    keep_unused_tex_nodes: BoolProperty(name="Keep Additional Texture Nodes", default=True)  # noqa: F722

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    def draw(self, context):
        is_backplate = cTB.check_backplate(self.vAsset)
        is_cycles = context.scene.render.engine == "CYCLES"
        # Blender 4.2's Eevee Next supports displacement (but not adaptive)
        is_eevee_next = context.scene.render.engine == "BLENDER_EEVEE_NEXT"

        col = self.layout.column()
        col.prop(self, "vSize")
        row = col.row()
        row.prop(self, "mapping")
        row.enabled = not is_backplate
        row.enabled = not is_backplate
        if is_cycles or is_eevee_next:
            row = col.row()
            row.prop(self, "mode_disp")
            row.enabled = not is_backplate
        if self.mode_disp != "NORMAL" and (is_cycles or is_eevee_next):
            row = col.row()
            row.prop(self, "displacement")
            row.enabled = not is_backplate
        row = col.row()
        row.prop(self, "scale")  # TODO(Andreas): implement for backplate?
        row = col.row()
        row.prop(self, "use_16bit")
        row.enabled = not is_backplate
        col.prop(self, "reuse_material")
        row = col.row()
        row.prop(self, "keep_unused_tex_nodes")
        row.enabled = not self.reuse_material

    @reporting.handle_operator()
    def execute(self, context):
        if not self.properties.is_property_set("mode_disp"):
            try:
                self.mode_disp = cTB.prefs.mode_disp
            except TypeError:
                # Could be Eevee active while assigning a displacement value
                self.mode_disp = "NORMAL"
            except AttributeError as e:
                reporting.capture_exception(e)
                self.mode_disp = "NORMAL"

        cTB.reset_asset_error(asset_name=self.vAsset)

        if self.vData == "rename":
            vMat = bpy.data.materials[cTB.vActiveMat]
            cTB.vActiveMat = bpy.context.scene.vEditMatName
            vMat.name = cTB.vActiveMat
            return {"FINISHED"}

        vSel = [vO for vO in context.selected_objects]

        if bpy.context.scene.render.engine == "CYCLES" and self.mode_disp == "MICRO":
            vAddDisp = [vO for vO in vSel if "Subdivision" not in vO.modifiers]
            bpy.context.scene.cycles.feature_set = "EXPERIMENTAL"
        else:
            vAddDisp = []

        vAsset = self.vAsset
        vSize = self.vSize
        vAData = None

        vBackplate = cTB.check_backplate(vAsset)

        msg_error_local = None
        msg_error_my_assets = None
        with cTB.lock_assets:
            if self.vType not in cTB.vAssets["local"]:
                msg_error_local = (f"Asset type {self.vType} not loaded, "
                                   "try searching for asset first")
            elif vAsset not in cTB.vAssets["local"][self.vType]:
                msg_error_local = (f"Asset {vAsset} not loaded,"
                                   " try searching for asset first")
            else:
                vAData = cTB.vAssets["local"][self.vType][vAsset]

                if self.vType in cTB.vAssets["my_assets"].keys():
                    vSizes = vAData["sizes"]
                    if vSize not in vSizes:
                        msg_error_my_assets = f"Use quick menu to download {vSize}"

        if msg_error_local is not None:
            self.report({"ERROR"}, msg_error_local)
            return {'CANCELLED'}
        if msg_error_my_assets is not None:
            cTB.print_debug(0, "apply_mat_size_not_local", vAData, vSize)
            self.report({"ERROR"}, msg_error_my_assets)
            reporting.capture_message("apply_mat_size_not_local", vAsset)
            return {"CANCELLED"}

        cTB.print_debug(0, "Size :", vSize)

        if self.reuse_material:
            # TODO(Andreas): I see little use for this branch with new importer
            identical_mat = find_identical_material(
                vAsset,
                "Textures",
                vSize,
                self.mapping,
                self.scale,
                self.displacement,
                self.use_16bit,
                self.mode_disp,
                vBackplate)
        else:
            identical_mat = None

        if identical_mat is not None:
            # prevent duplicate materials from being created unintentionally
            # but we probably want to provide an option for that at some point.
            cTB.print_debug(0, "Applying existing material :", identical_mat.name)
            self.report({"WARNING"}, "Applying existing material")

            rtn = bpy.ops.poliigon.poliigon_apply(
                "INVOKE_DEFAULT", vAsset=vAsset, vMat=identical_mat.name
            )
            if rtn == {"CANCELLED"}:
                self.report({"WARNING"}, "Could not apply materials to selection")
            else:
                self.signal_import()

            return {"FINISHED"}

        vTexs = []
        for _filepath in vAData["files"]:
            if vSize in os.path.basename(_filepath):
                vTexs.append(_filepath)
            else:
                dir_parent = os.path.basename(os.path.dirname(_filepath))
                if dir_parent == vSize:
                    vTexs.append(_filepath)
        if not len(vTexs):
            self.report({"WARNING"}, "No Textures found.")
            reporting.capture_message("apply_mat_tex_not_found", vAsset)
            return {"CANCELLED"}

        # BACKPLATE ...........................................................

        if vBackplate:
            cTB.f_BuildBackplate(vAsset, vAsset, vTexs[0], reuse=self.reuse_material)
            self.signal_import()
            return {"FINISHED"}

        # .....................................................................
        vMat = cTB.mat_import.import_material(
            asset_ref=vAData,
            asset_name=vAsset,  # only passed for legacy importer
            do_apply=False,
            workflow="METALNESS",
            size=vSize,
            lod="",
            projection=self.mapping,
            use_16bit=self.use_16bit,
            mode_disp=self.mode_disp,
            translate_x=0.0,
            translate_y=0.0,
            scale=self.scale,
            global_rotation=0.0,
            aspect_ratio=1.0,
            displacement=self.displacement,
            keep_unused_tex_nodes=self.keep_unused_tex_nodes,
            reuse_existing=False,  # we already checked for reusable mats before
            ref_operator=self  # only passed for legacy importer
        )

        if vMat is None:
            reporting.capture_message(
                "could_not_create_mat", vAsset, "error")
            self.report({"ERROR"}, "Material could not be created.")
            return {"CANCELLED"}

        for obj in vAddDisp:
            obj.cycles.use_adaptive_subdivision = True
            modifier = obj.modifiers.new("Subdivision", "SUBSURF")
            if modifier is None:
                continue
            modifier.subdivision_type = "SIMPLE"
            modifier.levels = 0  # Don't do subdiv in viewport

        cTB.f_GetSceneAssets()

        cTB.vActiveType = self.vType
        cTB.vActiveAsset = vAsset
        cTB.vActiveMat = vMat.name

        bpy.ops.poliigon.poliigon_active(
            vMode="mat", vType=cTB.vActiveType, vData=cTB.vActiveMat
        )

        cTB.forget_last_downloaded_size(vAsset)
        self.signal_import()

        if not self.vApply:
            cTB.vGoTop = 1
            return {"FINISHED"}

        rtn = bpy.ops.poliigon.poliigon_apply(
            "INVOKE_DEFAULT", vAsset=vAsset, vMat=vMat.name
        )
        if rtn == {"CANCELLED"}:
            self.report({"WARNING"}, "Could not apply materials to selection")

        cTB.vGoTop = 1
        return {"FINISHED"}

    def signal_import(self) -> None:
        if self.exec_count == 0:
            # Attempt to fetch id.
            data = cTB.get_data_for_asset_name(self.vAsset)
            if data and data.get("id"):
                cTB.signal_import_asset(asset_id=data["id"])
            else:
                cTB.signal_import_asset(asset_id=0)
        self.exec_count += 1


class POLIIGON_OT_show_quick_menu(Operator):
    bl_idname = "poliigon.show_quick_menu"
    bl_label = ""
    bl_description = "Show quick menu"

    asset_data_s: StringProperty(options={"HIDDEN"})  # noqa: F821
    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    vSizes: StringProperty(options={"HIDDEN"})  # e.g. 1K;2K;HIGHRES # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @reporting.handle_operator()
    def execute(self, context):
        if bpy.app.background:
            return {'CANCELLED'}  # Don't popup menus when running headless.
        asset_data = json.loads(self.asset_data_s)
        sizes = self.vSizes.split(";") if self.vSizes else []
        ui.show_quick_menu(cTB,
                           asset_data_tab=asset_data,
                           sizes=sizes)
        return {'FINISHED'}


class POLIIGON_OT_apply(Operator):
    bl_idname = "poliigon.poliigon_apply"
    bl_label = "Apply Material :"
    bl_description = "Apply Material to Selection"
    bl_options = {"REGISTER", "INTERNAL"}

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    vType: StringProperty(options={"HIDDEN"})  # noqa: F821
    vAsset: StringProperty(options={"HIDDEN"})  # noqa: F821
    vMat: StringProperty(options={"HIDDEN"})  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @reporting.handle_operator()
    def execute(self, context):
        vSel = [vO for vO in context.selected_objects]
        if not len(vSel):
            for vObj in context.scene.objects:
                if vObj.mode == "EDIT":
                    vMesh = vObj.data
                    vBMesh = bmesh.from_edit_mesh(vMesh)
                    for vF in vBMesh.faces:
                        if vF.select:
                            vSel.append(vObj)
                            break

        vAddDisp = [vO for vO in vSel if "Subdivision" not in vO.modifiers]

        vAsset = self.vAsset
        vMatN = self.vMat
        vSubdiv = 0

        # ...............................................................................................

        if vMatN != "":
            vMat = bpy.data.materials[vMatN]

        elif vAsset in cTB.imported_assets["Textures"].keys():
            if len(cTB.imported_assets["Textures"][vAsset]) == 1:
                vMat = cTB.imported_assets["Textures"][vAsset][0]
                vMatN = vMat.name

            else:
                # Unexpected need to download local, should avoid happening.
                reporting.capture_message(
                    "triggered_popup_mid_apply", vAsset, level='info')
                return {"CANCELLED"}

        # ...............................................................................................

        if cTB.vSettings["use_disp"] and len(vAddDisp):
            if cTB.prefs and cTB.prefs.mode_disp == "MICRO":
                vSubdiv = 1

        # ...............................................................................................

        if vSubdiv or len(vSel) > len(vAddDisp):
            bpy.context.scene.render.engine = "CYCLES"
            bpy.context.scene.cycles.feature_set = "EXPERIMENTAL"

            vMNodes = vMat.node_tree.nodes
            for vN in vMNodes:
                if vN.type != "GROUP":
                    continue
                for vI in vN.inputs:
                    if vI.type != "VALUE":
                        continue
                    elif vI.name == "Displacement Strength":
                        vN.inputs[vI.name].default_value = vMat.poliigon_props.displacement

        vAllFaces = []
        for vK in cTB.vActiveFaces.keys():
            vAllFaces += cTB.vActiveFaces[vK]

        valid_objects = 0
        for vObj in vSel:
            if hasattr(vObj.data, "materials"):
                valid_objects += 1
            else:
                continue

            if vObj.mode != "EDIT":
                vObj.active_material = vMat
            else:
                vMats = [vM.material for vM in vObj.material_slots
                         if vM is not None]
                if vMat not in vMats:
                    vObj.data.materials.append(vMat)
                vMesh = vObj.data
                vBMesh = bmesh.from_edit_mesh(vMesh)
                for i in range(len(vObj.material_slots)):
                    if vObj.material_slots[i].material == vMat:
                        vObj.active_material_index = i
                        bpy.ops.object.material_slot_assign()

            if vSubdiv and vObj in vAddDisp:
                vMod = vObj.modifiers.new(name="Subdivision", type="SUBSURF")
                vMod.subdivision_type = "SIMPLE"
                vMod.levels = 0  # Don't do subdiv in viewport
                vObj.cycles.use_adaptive_subdivision = 1

            # Scale ...........................................................

            vDimen = "?"
            if vDimen != "?":
                vScaleMult = bpy.context.scene.unit_settings.scale_length

                if bpy.context.scene.unit_settings.length_unit == "KILOMETERS":
                    vScaleMult *= 1000.0
                elif bpy.context.scene.unit_settings.length_unit == "CENTIMETERS":
                    vScaleMult *= 1.0 / 100.0
                elif bpy.context.scene.unit_settings.length_unit == "MILLIMETERS":
                    vScaleMult *= 1.0 / 1000.0
                elif bpy.context.scene.unit_settings.length_unit == "MILES":
                    vScaleMult *= 1.0 / 0.000621371
                elif bpy.context.scene.unit_settings.length_unit == "FEET":
                    vScaleMult *= 1.0 / 3.28084
                elif bpy.context.scene.unit_settings.length_unit == "INCHES":
                    vScaleMult *= 1.0 / 39.3701

                vScale = (vObj.scale * vScaleMult) / vDimen

                vMNodes = vMat.node_tree.nodes
                for vN in vMNodes:
                    if vN.type != "GROUP":
                        continue
                    for vI in vN.inputs:
                        if vI.type != "VALUE":
                            continue
                        elif vI.name == "Scale":
                            vN.inputs[vI.name].default_value = vScale[0]

        # ...............................................................................................

        cTB.vActiveType = self.vType
        cTB.vActiveAsset = vAsset
        cTB.vActiveMat = vMat.name
        bpy.ops.poliigon.poliigon_active(
            vMode="mat", vType=self.vType, vData=cTB.vActiveMat
        )
        return {"FINISHED"}


# ::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::

LOD_DESCS = {
    "NONE": "med. poly",
    "LOD0": "high poly",
    "LOD1": "med. poly",
    "LOD2": "low poly",
    "LOD3": "lower poly",
    "LOD4": "min. poly",
}
LOD_NAME = "{0} ({1})"
LOD_DESCRIPTION_FBX = "Import the {0} level of detail (LOD) FBX file"


class POLIIGON_OT_model(Operator):
    bl_idname = "poliigon.poliigon_model"
    bl_label = "Import model"
    bl_description = "Import Model"
    bl_options = {"REGISTER", "INTERNAL", "UNDO"}

    exec_count = 0

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    vType: StringProperty(options={"HIDDEN"})  # noqa: F821
    vAsset: StringProperty(options={"HIDDEN"})  # noqa: F821
    vUseCollection: BoolProperty(
        name="Import as collection",  # noqa: F722
        description="Instance model from a reusable collection",  # noqa: F722
        default=False)
    vReuseMaterials: BoolProperty(
        name="Reuse materials",  # noqa: F722
        description="Reuse already imported materials to avoid duplicates",  # noqa: F722
        default=False)
    vLinkBlend: BoolProperty(
        name="Link .blend file",  # noqa: F722
        description="Link the .blend file instead of appending",  # noqa: F722
        default=False)

    def _fill_size_drop_down(self, context):
        return fill_size_drop_down(self.vAsset, self.vType)

    vSize: EnumProperty(
        name="Texture",  # noqa: F821
        items=_fill_size_drop_down,
        description="Change size of assigned textures.")  # noqa: F722

    def _fill_lod_drop_down(self, context):
        # Get list of locally available sizes
        local_lods = []

        with cTB.lock_assets:
            asset_data = None
            assets_local = cTB.vAssets["local"]
            if self.vType in assets_local.keys():
                assets_local_type = assets_local[self.vType]
                if self.vAsset in assets_local_type.keys():
                    asset_data = assets_local_type[self.vAsset]

        if asset_data is not None:
            asset_files = asset_data["files"]
            fbx_filenames = [
                os.path.basename(path) for path in asset_files
                if f_FExt(path) == ".fbx"
            ]
            for lod in cTB.vLODs:
                for filename in fbx_filenames:
                    if lod in filename:
                        local_lods.append(lod)

        items_lod = [("NONE",
                      LOD_NAME.format("NONE", LOD_DESCS["NONE"]),
                      "Import the med. poly level of detail (LOD) .blend file")]
        for lod in local_lods:
            # Tuple: (id, name, description[, icon, [enum value]])
            lod_tuple = (lod,
                         LOD_NAME.format(lod, LOD_DESCS[lod]),
                         LOD_DESCRIPTION_FBX.format(LOD_DESCS[lod]))
            # Note: Usually we rather do a list(set()) afterwards,
            #       but in this case order is important!
            if lod_tuple not in items_lod:
                items_lod.append(lod_tuple)

        return items_lod

    vLod: EnumProperty(
        name="LOD",  # noqa: F821
        items=_fill_lod_drop_down,
        description="Change LOD of the Model.")  # noqa: F722

    def __init__(self):
        """Runs once per operator call before drawing occurs."""

        # Infer the default value on each press from the cached session
        # setting, which will be updated by redo last but not saved to prefs.
        self.vLinkBlend = cTB.link_blend_session

        self.blend_exists = False
        self.lod_import = False

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    def draw(self, context):
        prefer_blend = cTB.vSettings["download_prefer_blend"] and self.blend_exists
        if not self.blend_exists:
            label = "No local .blend file :"
            row_link_enabled = False
            row_sizes_enabled = True
        elif not prefer_blend:
            label = "Enable preference 'Download + Import .blend' :"
            row_link_enabled = False
            row_sizes_enabled = True
        elif self.lod_import:
            label = "Set 'LOD' to 'NONE' to load .blend :"
            row_link_enabled = False
            row_sizes_enabled = True
        else:
            label = None
            row_link_enabled = True
            row_sizes_enabled = not self.vLinkBlend

        row = self.layout.row()
        row.prop(self, "vLod")
        row.enabled = True

        row = self.layout.row()
        row.prop(self, "vSize")
        row.enabled = row_sizes_enabled

        self.layout.prop(self, "vUseCollection")

        row = self.layout.row()
        row.prop(self, "vReuseMaterials")
        row.enabled = not (row_link_enabled and self.vLinkBlend)

        if label is not None:
            self.layout.label(text=label)

        row = self.layout.row()
        row.prop(self, "vLinkBlend")
        row.enabled = row_link_enabled

    @reporting.handle_operator()
    def execute(self, context):
        """Runs at least once before first draw call occurs."""

        vAsset = self.vAsset
        cTB.reset_asset_error(asset_name=vAsset)

        # Save any updated preference of to link or not, if changed via the
        # redo last menu (without changing the saved preferences value).
        cTB.link_blend_session = self.vLinkBlend

        err = None
        with cTB.lock_assets:
            if vAsset not in cTB.vAssets["local"][self.vType].keys():
                err = f"Asset not found locally: {vAsset}"
        if err is not None:
            reporting.capture_message("model_texture_missing", err, "error")
            self.report({'ERROR'}, err)
            return {"CANCELLED"}

        asset_id, project_files, textures, size, lod = self.get_model_data()

        asset_name = construct_model_name(vAsset, size, lod)
        did_fresh_import = False
        inst = None
        new_objs = []

        # Import the model.
        blend_import = False
        coll_exists = bpy.data.collections.get(asset_name) is not None
        if self.vUseCollection is False or not coll_exists:
            ok, new_objs, blend_import, fbx_fail = self.run_fresh_import(
                context,
                project_files,
                textures,
                size,
                lod)
            if fbx_fail:
                return {'CANCELLED'}
            did_fresh_import = True

        # If imported, perform these steps regardless of collection or not.
        if did_fresh_import:
            empty = self.setup_empty_parent(context, new_objs)
            if blend_import and not self.vUseCollection:
                empty.location = bpy.context.scene.cursor.location
        else:
            empty = None

        if self.vUseCollection is True:
            # Move the objects into a subcollection, and place in scene.
            if did_fresh_import:
                # Always create a new collection if did a fresh import.
                cache_coll = bpy.data.collections.new(asset_name)
                # Ensure all objects are only part of this new collection.
                for obj in [empty] + new_objs:
                    for vC in obj.users_collection:
                        vC.objects.unlink(obj)
                    cache_coll.objects.link(obj)

                # Now add the cache collection to the layer, but unchecked.
                layer = context.view_layer.active_layer_collection
                layer.collection.children.link(cache_coll)
                for ch in layer.children:
                    if ch.collection == cache_coll:
                        ch.exclude = True
            else:
                cache_coll = bpy.data.collections.get(asset_name)

            # Now finally add the instance to the scene.
            if cache_coll:
                inst = self.create_instance(context, cache_coll, size, lod)
                # layer = context.view_layer.active_layer_collection
                # layer.collection.objects.link(inst)
            else:
                # Raise error
                err = "Failed to get new collection to instance"
                self.report({"ERROR"}, err)
                return {"CANCELLED"}

        elif not blend_import:
            # Make sure the objects imported are all part of the same coll.
            self.append_cleanup(context, empty)

        # Final notifications and reporting.
        cTB.f_GetSceneAssets()

        if blend_import:
            # Fix import info message containing LOD info
            asset_name = construct_model_name(vAsset, size, "")

        if did_fresh_import is True:
            self.report({"INFO"}, "Model Imported : " + asset_name)
        elif self.vUseCollection and inst is not None:
            self.report({"INFO"}, "Instance created : " + inst.name)
        else:
            err = f"Failed to import model correctly: {asset_name}"
            self.report({"ERROR"}, err)
            reporting.capture_message("import-model-failed", err, "error")
            return {"CANCELLED"}

        cTB.forget_last_downloaded_size(vAsset)

        if self.exec_count == 0:
            cTB.signal_import_asset(asset_id=asset_id)
        self.exec_count += 1
        return {"FINISHED"}

    def _filter_lod_fbxs(self,
                         file_list: List[str]
                         ) -> List[str]:
        """Returns a list with all FBX files with LOD tag in filename"""

        all_lod_fbxs = []
        for asset_path in file_list:
            if f_FExt(asset_path) != ".fbx":
                continue
            filename_parts = f_FName(asset_path).split("_")
            for lod_level in cTB.vLODs:
                if lod_level not in filename_parts:
                    continue
                all_lod_fbxs.append(asset_path)
                break
        return all_lod_fbxs

    def get_model_data(self):
        with cTB.lock_assets:
            vAData = cTB.vAssets["local"][self.vType][self.vAsset]
        self.vAData = vAData  # needed for passing to new material importer
        asset_id = vAData.get("id", 0)
        if asset_id == 0:
            # Try to fetch id, safely.
            with cTB.lock_assets:
                # TODO(Andreas): This looks fishy, area is not used inside loop
                for area in cTB.vAssets:
                    if cTB.vAssets["my_assets"].get(self.vType) is None:
                        continue
                    my_asset_data = cTB.vAssets["my_assets"][self.vType].get(self.vAsset)
                    if my_asset_data is None:
                        continue
                    asset_id = my_asset_data.get("id", 0)
                    if asset_id == 0:
                        cTB.print_debug(0, f"Could not fetch {self.vAsset} asset id")

        # Get the intended material size and LOD import to use.
        if self.vSize:
            vSize = cTB.f_GetClosestSize(vAData["sizes"], self.vSize)
        else:
            vSize = cTB.f_GetClosestSize(vAData["sizes"],
                                         cTB.vSettings["mres"])

        prefer_blend = cTB.vSettings["download_prefer_blend"]

        vLod = self.vLod
        if vLod == "NONE" and not prefer_blend:
            vLod = "LOD1"

        if len(vAData["lods"]):
            vLod = cTB.vSettings["lod"] if vLod == "NONE" else vLod
            vLod = cTB.f_GetClosestLod(vAData["lods"], vLod)
            if vLod == "NONE":
                vLod = None
        else:
            vLod = None

        all_lod_fbxs = self._filter_lod_fbxs(vAData["files"])

        vLODFBXs = [
            vF for vF in all_lod_fbxs
            if f_FExt(vF) == ".fbx" and str(vLod) in f_FName(vF).split("_")
        ]
        # Most likely redundant, but rather safe than sorry
        vLODFBXs = list(set(vLODFBXs))

        vFBXs = []
        vBlendFiles = []
        for vF in vAData["files"]:
            filename_ext = f_FExt(vF)
            is_fbx = filename_ext == ".fbx"
            is_blend = filename_ext == ".blend" and "_LIB.blend" not in vF

            if not is_fbx and not is_blend:
                continue

            if is_fbx and vLod is not None and len(vLODFBXs):
                if vF not in vLODFBXs:
                    continue

            if is_fbx and vLod is None and vF in all_lod_fbxs:
                continue

            if is_fbx:
                vFBXs.append(vF)
            elif is_blend:
                vBlendFiles.append(vF)

            cTB.print_debug(0, os.path.basename(vF))

        self.blend_exists = len(vBlendFiles) > 0
        self.lod_import = vLod is not None and self.vLod != "NONE"

        if prefer_blend and self.blend_exists and not self.lod_import:
            vProjectFiles = vBlendFiles
        elif not prefer_blend and not vFBXs and self.blend_exists:
            # Settings specify to import fbx files, but only blend exists.
            # Needed for asset browser imports and right-click
            # TODO(Patrick): Migrate to using a use_blend operator arg, so it can also
            # be a backdoor option.
            vProjectFiles = vBlendFiles
        else:
            vProjectFiles = vFBXs

        vTextures = []
        for vT in vAData["files"]:
            if f_FExt(vT) not in cTB.vTexExts:
                continue

            if vSize not in f_FName(vT).split("_"):
                continue

            if any(
                vL in f_FName(vT).split("_") for vL in cTB.vLODs
            ) and vLod not in f_FName(vT).split("_"):
                continue

            vTextures.append(vT)

        return asset_id, vProjectFiles, vTextures, vSize, vLod

    def _load_blend(self, path_proj: str):
        with bpy.data.libraries.load(path_proj,
                                     link=self.vLinkBlend
                                     ) as (data_from,
                                           data_to):
            data_to.objects = data_from.objects
        return data_to.objects

    def _cut_identity_counter(self, s: str) -> str:
        """Reduces strings like 'walter.042' to 'walter'"""

        splits = s.rsplit(".", maxsplit=1)
        if len(splits) > 1 and splits[1].isdecimal():
            s = splits[0]
        return s

    def _reuse_materials(self, imported_objs: List, imported_mats: List):
        """Re-uses previously imported materials after a .blend import"""

        if not self.vReuseMaterials or self.vLinkBlend:
            return

        # Mark all materials from this import
        PROP_FRESH_IMPORT = "poliigon_fresh_import"
        for mat in imported_mats:
            mat[PROP_FRESH_IMPORT] = True
        # Find any previously imported materials with same name
        # and make the objects use those
        mats_remap = []  # list of tuples (from_mat, to_mat)
        for obj in imported_objs:
            mat_on_obj = obj.active_material
            if mat_on_obj is None:
                continue
            materials = reversed(sorted(list(bpy.data.materials.keys())))
            for name_mat in materials:
                # Unfortunately we seem to have little control,
                # where and when Blender adds counter suffixes for
                # identically named materials.
                # Therefore we compare names without any counter suffix.
                name_on_obj_cmp = self._cut_identity_counter(mat_on_obj.name)
                name_mat_cmp = self._cut_identity_counter(name_mat)
                if name_on_obj_cmp != name_mat_cmp:
                    continue
                mat_reuse = bpy.data.materials[name_mat]
                is_fresh = mat_reuse.get(PROP_FRESH_IMPORT, False)
                if is_fresh:
                    continue
                if (mat_on_obj, mat_reuse) not in mats_remap:
                    mats_remap.append((mat_on_obj, mat_reuse))
                    break
        # Remove previously added marker
        for mat in imported_mats:
            if PROP_FRESH_IMPORT in mat.keys():
                del mat[PROP_FRESH_IMPORT]
        # Finally remap the materials and remove those freshly imported ones
        did_send_sentry = False
        for from_mat, to_mat in mats_remap:
            from_mat.user_remap(to_mat)
            from_mat.user_clear()
            if from_mat in imported_mats:
                imported_mats.remove(from_mat)
            if from_mat.users != 0 and not did_send_sentry:
                msg = ("User count not zero on material replaced by reuse: "
                       f"Asset: {self.vAsset}, Material: {from_mat.name}")
                reporting.capture_message(
                    "import_model_mat_reuse_user_count",
                    msg,
                    "info")
                did_send_sentry = True
                continue
            try:
                bpy.data.materials.remove(from_mat)
            except Exception as e:
                reporting.capture_exception(e)
                self.report({"WARNING"},
                            "Failed to remove material after reuse.")

    def run_fresh_import(self,
                         context,
                         project_files: List[str],
                         vTextures: List[str],
                         vSize: str,
                         vLod: str
                         ) -> Tuple[bool,
                                    List[bpy.types.Object],
                                    bool,
                                    bool]:
        """Performs a fresh import of the whole model.

        There can be multiple FBX models, therefore we want to import all of
        them and verify each one was properly imported.

        Return values:
        Tuple[0] - True, if all FBX files have been imported successfully
        Tuple[1] - List of all imported mesh objects
        Tuple[2] - True, if it was a .blend import instead of an FBX import
        Tuple[3] - True, if an error occurred during FBX loading
        """

        PROP_LIBRARY_LINKED = "poliigon_linked"
        vAllMeshes = []
        vAllMaterials = {}
        imported_proj = []
        blend_import = False
        for path_proj in project_files:
            filename_base = f_FName(path_proj)

            if not f_Ex(path_proj):
                err = f"Couldn't load project file: {self.vAsset} {path_proj}"
                self.report({"ERROR"}, err)
                reporting.capture_message("model_fbx_missing", err, "info")
                continue

            vObjs = list(context.scene.objects)

            ext_proj = f_FExt(path_proj)
            if ext_proj == ".blend":
                cTB.print_debug(0, "BLEND IMPORT")
                filename = filename_base + ".blend"

                if self.vLinkBlend and filename in bpy.data.libraries.keys():
                    lib = bpy.data.libraries[filename]
                    if lib[PROP_LIBRARY_LINKED]:
                        linked_objs = []
                        for obj in bpy.data.objects:
                            if obj.library == lib:
                                linked_objs.append(obj)

                        imported_objs = []
                        for obj in linked_objs:
                            imported_objs.append(obj.copy())
                    else:
                        imported_objs = self._load_blend(path_proj)
                else:
                    imported_objs = self._load_blend(path_proj)

                    if filename in bpy.data.libraries.keys():
                        lib = bpy.data.libraries[filename]
                        lib[PROP_LIBRARY_LINKED] = self.vLinkBlend

                for obj in context.view_layer.objects:
                    obj.select_set(False)
                layer = context.view_layer.active_layer_collection
                imported_mats = []
                for obj in imported_objs:
                    if obj is None:
                        continue
                    obj_copy = obj.copy()
                    layer.collection.objects.link(obj_copy)
                    obj_copy.select_set(True)
                    if obj_copy.active_material is None:
                        pass
                    elif obj_copy.active_material not in imported_mats:
                        imported_mats.append(obj_copy.active_material)

                with cTB.lock_assets:
                    asset_data = cTB.vAssets["local"][self.vType][self.vAsset]
                asset_files = asset_data["files"]

                replace_tex_size(
                    imported_mats,
                    asset_files,
                    vSize,
                    self.vLinkBlend
                )
                self._reuse_materials(imported_objs, imported_mats)

                blend_import = True
            else:
                cTB.print_debug(0, "FBX IMPORT")
                if "fbx" not in dir(bpy.ops.import_scene):
                    try:
                        bpy.ops.preferences.addon_enable(module="io_scene_fbx")
                        self.report({"INFO"},
                                    "FBX importer addon enabled for import")
                    except RuntimeError:
                        self.report({"ERROR"},
                                    "Built-in FBX importer could not be found, check Blender install")
                        did_full_import = False
                        vAllMeshes = []
                        blend_import = False
                        fbx_error = True
                        return did_full_import, vAllMeshes, blend_import, fbx_error
                try:
                    # Note on use_custom_normals parameter:
                    # It always defaulted to True. But in Blender 4.0+
                    # it started to cause issues.
                    # Mateusz sync'ed with Stephen and recommended to turn it
                    # off regardless of Blender version.
                    bpy.ops.import_scene.fbx(filepath=path_proj,
                                             axis_up="-Z",
                                             use_custom_normals=False)
                except Exception as e:
                    self.report({"ERROR"},
                                "FBX importer exception:" + str(e))
                    did_full_import = False
                    vAllMeshes = []
                    blend_import = False
                    fbx_error = True
                    return did_full_import, vAllMeshes, blend_import, fbx_error

            imported_proj.append(path_proj)
            vMeshes = [vM for vM in list(context.scene.objects)
                       if vM not in vObjs]

            vAllMeshes += vMeshes

            if ext_proj == ".blend":
                for vMesh in vMeshes:
                    # Ensure we can identify the mesh & LOD even on name change
                    vMesh.poliigon = f"Models;{self.vAsset}"
                    if vLod is not None:
                        vMesh.poliigon_lod = vLod
                continue

            for vMesh in vMeshes:
                if vMesh.type == "EMPTY":
                    continue

                name_mat_imported = ""
                if vMesh.active_material is not None:
                    name_mat_imported = vMesh.active_material.name

                # Note: Of course the check if "_mat" is contained could be
                #       written in one line. But I wouldn't consider "_mat"
                #       unlikely in arbitrary filenames. Thus I chose to
                #       explicitly compare for "_mat" at the end and
                #       additionally check if "_mat_" is contained, in order
                #       to at least reduce the chance of false positives a bit.
                name_mat_imported_lower = name_mat_imported.lower()
                name_tex_remastered = ""
                name_tex_on_obj = ""
                ends_remastered = name_mat_imported_lower.endswith("_mat")
                contains_remastered = "_mat_" in name_mat_imported_lower
                if ends_remastered or contains_remastered:
                    pos_remastered = name_mat_imported_lower.rfind("_mat", 1)
                    name_tex_remastered = name_mat_imported[:pos_remastered]
                else:
                    name_tex_on_obj = name_mat_imported.split("_")[0]

                vMeshName = vMesh.name.split(".")[0].split("_")[0]

                vMVar = cTB.f_GetVar(vMesh.name)
                variant_name = vMVar
                if vMVar is None:
                    # This is a fallback for models,
                    # where the object name does not contain a variant indicator.
                    # As None is covered explicitly in the loop below,
                    # this should do no harm.
                    vMVar = "VAR1"
                    variant_name = ""

                vMatName = vMeshName
                vTexs = []
                if len(name_tex_remastered) > 0:
                    # Remastered textures
                    vTexs = [
                        vT
                        for vT in vTextures
                        if os.path.basename(vT).startswith(name_tex_remastered)
                    ]
                    vMatName = name_mat_imported
                else:
                    for vCheck in [vMeshName,
                                   filename_base.split("_")[0],
                                   self.vAsset,
                                   name_tex_on_obj]:
                        vTexs = [
                            vT
                            for vT in vTextures
                            if os.path.basename(vT).startswith(vCheck)
                            if cTB.f_GetVar(f_FName(vT)) in [None, vMVar]
                        ]
                        vMatName = vCheck
                        if len(vTexs) > 0:
                            break

                if len(vTexs) == 0:
                    err = f"No Textures found for: {self.vAsset} {vMesh.name}"
                    reporting.capture_message(
                        "model_texture_missing", err, "info")
                    continue

                vMatName += f"_{vSize}"

                if vSize not in vMatName:
                    vMatName += f"_{vSize}"

                if variant_name != "":
                    vMatName += f"_{variant_name}"

                # TODO(Andreas): Not sure, why these lines are commented out.
                #                Looks reasonable to me.
                # if vMatName in bpy.data.materials and self.vReuseMaterials:
                #     vMat = bpy.data.materials[vMatName]
                # el
                # TODO(Andreas): Not sure, this should also be dependening on self.vReuseMaterials?
                if vMatName in vAllMaterials:
                    # Already built in previous iteration
                    vMat = vAllMaterials[vMatName]
                else:
                    # TODO(Andreas): Old code, revive to get WM import with
                    #                legacy importer working again?
                    # vMat = cTB.f_BuildMat(
                    #     vMatName, vSize, vTexs, "Models", self,
                    #     lod=vLod, do_reuse=self.vReuseMaterials)
                    vMat = cTB.mat_import.import_material(
                        asset_ref=self.vAData,
                        asset_name=self.vAsset,  # only passed for legacy importer, not used in here
                        do_apply=False,
                        workflow="METALNESS",
                        size=vSize,
                        size_bg=None,
                        lod=vLod,
                        variant=variant_name if variant_name != "" else None,
                        name_material=vMatName,
                        name_mesh=vMeshName,
                        ref_objs=None,
                        projection="UV",
                        use_16bit=True,
                        mode_disp="NORMAL",  # We never want model dispalcement
                        translate_x=0.0,
                        translate_y=0.0,
                        scale=1.0,
                        global_rotation=0.0,
                        aspect_ratio=1.0,
                        displacement=0.0,
                        keep_unused_tex_nodes=False,
                        reuse_existing=self.vReuseMaterials,
                        ref_operator=self  # only passed for legacy impoorter, MUST NOT be used in here
                    )
                if vMat is None:
                    msg = f"{self.vAsset}: Failed to build matrial: {vMatName}"
                    reporting.capture_message(
                        "could_not_create_fbx_mat", msg, "error")
                    self.report({"ERROR"}, "Material could not be created.")
                    imported_proj.remove(path_proj)
                    break

                vAllMaterials[vMatName] = vMat

                # This sequence is important!
                # 1) Setting the material slot to None
                # 2) Changing the link mode
                # 3) Assigning our generated material
                # Any other order of these statements will get us into trouble
                # one way or another.
                vMesh.active_material = None
                if len(vMesh.material_slots) > 0:
                    vMesh.material_slots[0].link = "OBJECT"
                vMesh.active_material = vMat

                if vMVar is not None:
                    if len(vMesh.material_slots) == 0:
                        vMesh.data.materials.append(vMat)
                    else:
                        vMesh.material_slots[0].link = "OBJECT"
                        vMesh.material_slots[0].material = vMat
                    vMesh.material_slots[0].link = "OBJECT"

                # Ensure we can identify the mesh & LOD even on name change.
                vMesh.poliigon = f"Models;{self.vAsset}"
                if vLod is not None:
                    vMesh.poliigon_lod = vLod

                # Finally try to remove the originally imported materials
                if name_mat_imported in bpy.data.materials:
                    mat_imported = bpy.data.materials[name_mat_imported]
                    if mat_imported.users == 0:
                        mat_imported.user_clear()
                        bpy.data.materials.remove(mat_imported)

        # There could have been multiple FBXs, consider fully imported
        # for user-popup reporting if all FBX files imported.
        did_full_import = len(imported_proj) == len(project_files)

        return did_full_import, vAllMeshes, blend_import, False

    def object_has_children(self, obj_parent: bpy.types.Object) -> bool:
        """Returns True, if obj_parent has children."""

        for _obj in bpy.data.objects:
            if _obj.parent == obj_parent:
                return True
        return False

    def setup_empty_parent(self,
                           context,
                           new_meshes: List[bpy.types.Object]
                           ) -> bpy.types.Object:
        """Parents newly imported objects to a central empty object."""

        radius = 0
        for vMesh in new_meshes:
            vBnds = vMesh.dimensions
            if vBnds.x > radius:
                radius = vBnds.x
            if vBnds.y > radius:
                radius = vBnds.y

        empty = bpy.data.objects.new(
            name=f"{self.vAsset}_Empty", object_data=None)
        if self.vUseCollection:
            empty.empty_display_size = 0.01
        else:
            empty.empty_display_size = radius * 0.5

        layer = context.view_layer.active_layer_collection
        layer.collection.objects.link(empty)

        for mesh in new_meshes.copy():
            if mesh.type == "EMPTY" and not self.object_has_children(mesh):
                bpy.data.objects.remove(mesh, do_unlink=True)
                new_meshes.remove(mesh)
                continue
            mesh.parent = empty

        return empty

    def create_instance(self, context, coll, size, lod):
        """Creates an instance of an existing collection int he active view."""
        inst_name = construct_model_name(self.vAsset, size, lod) + "_Instance"
        inst = bpy.data.objects.new(name=inst_name, object_data=None)
        inst.instance_collection = coll
        inst.instance_type = "COLLECTION"
        lc = context.view_layer.active_layer_collection
        lc.collection.objects.link(inst)
        inst.location = context.scene.cursor.location
        inst.empty_display_size = 0.01

        # Set selection and active object.
        for obj in context.scene.collection.all_objects:
            obj.select_set(False)
        inst.select_set(True)
        context.view_layer.objects.active = inst

        return inst

    def append_cleanup(self, context, root_empty):
        """Performs selection and placement cleanup after an import/append."""
        if not root_empty:
            print("root_empty was not a valid object, exiting cleanup")
            return

        # Set empty location
        root_empty.location = context.scene.cursor.location

        # Deselect all others in scene (faster than using operator call).
        for obj in context.scene.collection.all_objects:
            obj.select_set(False)

        # Make empty active and select it + children.
        root_empty.select_set(True)
        context.view_layer.objects.active = root_empty
        for obj in root_empty.children:
            obj.select_set(True)


class POLIIGON_OT_select(Operator):
    bl_idname = "poliigon.poliigon_select"
    bl_label = ""
    bl_description = "Select Model"
    bl_options = {"REGISTER", "INTERNAL", "UNDO"}

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    vMode: StringProperty(options={"HIDDEN"})  # noqa: F821
    vData: StringProperty(options={"HIDDEN"})  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @reporting.handle_operator()
    def execute(self, context):
        global cTB

        if self.vMode == "faces":
            vObj = context.active_object

            self.deselect(context)

            i = int(self.vData)
            vMat = vObj.material_slots[i].material

            bpy.ops.object.mode_set(mode="EDIT")

            vMesh = vObj.data
            vBMesh = bmesh.from_edit_mesh(vMesh)
            for vF in vBMesh.faces:
                vF.select = 0

            vObj.active_material_index = i
            bpy.ops.object.material_slot_select()

        elif self.vMode == "object":
            self.deselect(context)
            vObj = context.scene.objects[self.vData]
            try:
                vObj.select_set(True)
            except RuntimeError:
                pass  # Might not be in view layer

        elif "@" in self.vData:
            vSplit = self.vData.split("@")
            self.deselect(context)
            try:
                context.scene.objects[vSplit[1]].select_set(1)
            except RuntimeError:
                pass  # Might not be in view layer

        elif self.vMode == "model":
            if not context.mode == "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")

            self.deselect(context)
            to_select = []

            key = self.vData
            for obj in context.scene.objects:
                split = obj.name.rsplit("_")[0]

                # For instance collections
                if split == key and obj.instance_type == "COLLECTION":
                    to_select.append(obj)
                # For empty parents
                ref_key = key + "_empty"
                if obj.name.lower().startswith(ref_key.lower()):
                    to_select.append(obj)
                # For the objects within the empty tree
                if key in obj.poliigon.split(";")[-1]:
                    to_select.append(obj)

            for obj in to_select:
                try:
                    obj.select_set(True)
                except RuntimeError:
                    pass  # Might not be in view layer

        elif self.vMode == "mat_objs":
            vMat = bpy.data.materials[self.vData]

            vObjs = [vO for vO in context.scene.objects if vO.active_material == vMat]

            if len(vObjs) == 1:
                self.deselect(context)
                try:
                    vObjs[0].select_set(True)
                except RuntimeError:
                    pass  # Might not be in view layer

            else:
                # TODO(Andreas): Looks like this branch is never used.
                #                This operator seems to be only used in "model" mode
                reporting.capture_message(
                    "reached_legacy_f_DropdownSelect", vObj.name)
                return {"FINISHED"}

        return {"FINISHED"}

    def deselect(self, context):
        """Deselects objects in a lower api, faster, context-invaraint way."""
        for obj in context.scene.collection.all_objects:
            try:
                obj.select_set(False)
            except RuntimeError:
                pass  # Might not be in view layer


# ::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::


class POLIIGON_OT_hdri(Operator):
    bl_idname = "poliigon.poliigon_hdri"
    bl_label = "HDRI Import"
    bl_description = "Import HDRI"
    bl_options = {"GRAB_CURSOR", "BLOCKING", "REGISTER", "INTERNAL", "UNDO"}

    exec_count = 0

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    vAsset: StringProperty(options={"HIDDEN"})  # noqa: F821
    # If do_apply is set True, the sizes are ignored and set internally
    do_apply: BoolProperty(options={"HIDDEN"}, default=False)  # noqa: F821

    def _fill_light_size_drop_down(self, context):
        # Get list of locally available sizes
        with cTB.lock_assets:
            assets_local = cTB.vAssets["local"]
            if "HDRIs" not in assets_local.keys():
                return []
            assets_local_hdri = assets_local["HDRIs"]
            if self.vAsset not in assets_local_hdri.keys():
                return []
            asset_data = assets_local_hdri[self.vAsset]
        asset_files = asset_data["files"]
        # Populate dropdown items
        local_exr_sizes = []
        for path_asset in asset_files:
            filename = os.path.basename(path_asset)
            if not filename.endswith(".exr"):
                continue
            match_object = re.search(r"_(\d+K)[_\.]", filename)
            if match_object:
                local_exr_sizes.append(match_object.group(1))
        # Sort by comparing integer size without "K"
        local_exr_sizes.sort(key=lambda s: int(s[:-1]))
        items_size = []
        for size in local_exr_sizes:
            # Tuple: (id, name, description, icon, enum value)
            items_size.append((size, f"{size} EXR", f"{size} EXR"))
        return items_size

    vSize: EnumProperty(
        name="Light Texture",  # noqa F722
        items=_fill_light_size_drop_down,
        description="Change size of light texture.")  # noqa F722

    def _fill_bg_size_drop_down(self, context):
        # Get list of locally available sizes
        with cTB.lock_assets:
            assets_local = cTB.vAssets["local"]
            if "HDRIs" not in assets_local.keys():
                return []
            assets_local_hdri = assets_local["HDRIs"]
            if self.vAsset not in assets_local_hdri.keys():
                return []
            asset_data = assets_local_hdri[self.vAsset]
        asset_files = asset_data["files"]
        # Populate dropdown items
        local_exr_sizes = []
        local_jpg_sizes = []
        for path_asset in asset_files:
            filename = os.path.basename(path_asset)
            is_exr = filename.endswith(".exr")
            is_jpg = filename.lower().endswith(".jpg")
            is_jpg &= "_JPG" in filename
            if not is_exr and not is_jpg:
                continue
            match_object = re.search(r"_(\d+K)[_\.]", filename)
            if not match_object:
                continue
            local_size = match_object.group(1)
            if is_exr:
                local_exr_sizes.append(f"{local_size}_EXR")
            elif is_jpg:
                local_jpg_sizes.append(f"{local_size}_JPG")

        local_sizes = local_exr_sizes + local_jpg_sizes
        # Sort by comparing integer size without "K_JPG" or "K_EXR"
        local_sizes.sort(key=lambda s: int(s[:-5]))
        items_size = []
        for size in local_sizes:
            # Tuple: (id, name, description, icon, enum value)
            label = size.replace("_", " ")
            items_size.append((size, label, label))
        return items_size

    # This is not a pure size, but is a string like "4K_JPG"
    size_bg: EnumProperty(
        name="Background Texture",  # noqa F722
        items=_fill_bg_size_drop_down,
        description="Change size of background texture.")  # noqa F722

    hdr_strength: FloatProperty(
        name="HDR Strength",  # noqa F722
        description="Strength of Light and Background textures",  # noqa F722
        soft_min=0.0,
        step=10,
        default=1.0)
    rotation: FloatProperty(
        name="Z-Rotation",  # noqa: F821
        description="Z-Rotation",  # noqa: F821
        unit="ROTATION",  # noqa: F821
        soft_min=-2.0 * pi,
        soft_max=2.0 * pi,
        # precision needed here, otherwise Redo Last and node show different values
        precision=3,
        step=10,
        default=0.0)

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @reporting.handle_operator()
    def execute(self, context):
        vAsset = self.vAsset
        vSize = self.vSize

        cTB.reset_asset_error(asset_name=vAsset)

        vAData = cTB.get_data_for_asset_name(vAsset)
        if not vAData:
            # Force non-threaded fetch to load local assets.
            cTB.f_GetLocalAssetsThread()
            vAData = cTB.get_data_for_asset_name(vAsset)
            reporting.capture_message(
                "hdri_force_fetched_data", vAsset, "info")

        if not vAData:
            msg = f"Failed to load data for {vAsset}"
            reporting.capture_message("failed_load_data_hdri", msg)
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}

        # Must ensure we copy the dict, otherwise we are inadvertently updating
        # the other datastructures (poliigon, my_assets) with data from (local)
        vAData = vAData.copy()

        with cTB.lock_assets:
            if cTB.vAssets["local"]["HDRIs"].get(vAsset):
                vAData.update(cTB.vAssets["local"]["HDRIs"].get(vAsset))
        vLIName = vAsset + "_Light"
        vBIName = vAsset + "_Background"

        cTB.print_debug(0, vAData, vLIName, vBIName)

        if "HDRIs" in cTB.imported_assets:
            existing = cTB.imported_assets["HDRIs"].get(vAsset)
        else:
            existing = None

        # Whenever an HDR is loaded, it fully replaces the prior loaded
        # resolutions. Thus, if we are "applying" an already imported one,
        # we don't need to worry about resolution selection.

        if not self.do_apply or not existing:
            # Remove existing images to force load this resolution.
            if vLIName in bpy.data.images.keys():
                bpy.data.images.remove(bpy.data.images[vLIName])

            if vBIName in bpy.data.images.keys():
                bpy.data.images.remove(bpy.data.images[vBIName])
        elif self.do_apply:
            if vLIName in bpy.data.images.keys():
                path_light = bpy.data.images[vLIName].filepath
                filename = os.path.basename(path_light)
                match_object = re.search(r"_(\d+K)[_\.]", filename)
                size_light = match_object.group(1) if match_object else cTB.vSettings['hdri']
                self.vSize = size_light
                vSize = self.vSize
            if vBIName in bpy.data.images.keys():
                path_bg = bpy.data.images[vBIName].filepath
                filename = os.path.basename(path_bg)
                file_type = "JPG" if "_JPG" in filename else "EXR"
                match_object = re.search(r"_(\d+K)[_\.]", filename)
                size_bg = match_object.group(1) if match_object else cTB.vSettings['hdri']
                self.size_bg = f"{size_bg}_{file_type}"

        light_exists = vLIName in bpy.data.images.keys()
        bg_exists = vBIName in bpy.data.images.keys()
        if not light_exists or not bg_exists:
            if not self.vSize or self.do_apply:
                # Edge case that shouldn't occur as the resolution should be
                # explicitly set, or just applying a local tex already,
                # but fallback if needed.
                vSize = cTB.vSettings["hdri"]

            vLSize = cTB.f_GetClosestSize(vAData["sizes"], vSize)

            vLTex = [vF for vF in vAData["files"]
                     if vLSize in os.path.basename(vF) and vF.lower().endswith(".exr")]

            if not len(vLTex):
                cTB.f_GetLocalAssets()  # Refresh local assets data structure.
                msg = (f"Unable to locate image {vLIName} with size {vLSize}, "
                       f"try downloading {self.vAsset} again.")
                reporting.capture_message(
                    "failed_load_light_hdri", msg, "error")
                self.report({"ERROR"}, msg)
                return {"CANCELLED"}
            vLTex = vLTex[0]

            try:
                if "_" not in self.size_bg:
                    raise ValueError
                size_bg_eff, filetype_bg = self.size_bg.split("_")
            except Exception:
                msg = f"POLIIGON_OT_hdri: Wrong size_bg format ({self.size_bg}), expected '4K_JPG' or '1K_EXR'"
                raise ValueError(msg)

            if cTB.vSettings["hdri_use_jpg_bg"] and filetype_bg == "JPG":
                vBSize = cTB.f_GetClosestSize(vAData["sizes"], size_bg_eff)

                vBTex = [vF for vF in vAData["files"]
                         if vBSize in os.path.basename(vF) and vF.lower().endswith(".jpg")]

                if not len(vBTex):
                    cTB.f_GetLocalAssets()  # Refresh local assets data structure.
                    msg = (f"Unable to locate image {vBIName} with size {vBSize} (JPG), "
                           f"try downloading {self.vAsset} again.")
                    reporting.capture_message(
                        "failed_load_bg_jpg", msg, "error")
                    self.report({"ERROR"}, msg)
                    return {"CANCELLED"}
                vBTex = vBTex[0]
            elif vLSize != size_bg_eff:
                vBSize = size_bg_eff
                vBTex = [vF for vF in vAData["files"]
                         if size_bg_eff in os.path.basename(vF) and vF.lower().endswith(".exr")]
                if not len(vBTex):
                    cTB.f_GetLocalAssets()  # Refresh local assets data structure.
                    msg = (f"Unable to locate image {vBIName} with size {vBSize} (EXR), "
                           f"try downloading {self.vAsset} again")
                    reporting.capture_message(
                        "failed_load_bg_hdri", msg, "error")
                    self.report({"ERROR"}, msg)
                    return {"CANCELLED"}
                vBTex = vBTex[0]
            else:
                vBSize = vLSize
                vBTex = vLTex

        # Reset apply for Redo Last menu to work properly
        self.do_apply = False

        # ...............................................................................................

        vTCoordNode = None
        vMapNode = None

        vEnvNodeL = None
        vBGNodeL = None

        vEnvNodeB = None
        vBGNodeB = None

        vMixNode = None
        vLightNode = None

        vWorldNode = None

        if not bpy.context.scene.world:
            bpy.ops.world.new()
            bpy.context.scene.world = bpy.data.worlds[-1]

        context.scene.world.use_nodes = True

        vWNodes = context.scene.world.node_tree.nodes
        vWLinks = context.scene.world.node_tree.links
        for vN in vWNodes:
            if vN.type == "TEX_COORD":
                if vN.label == "Mapping":
                    vTCoordNode = vN

            elif vN.type == "MAPPING":
                if vN.label == "Mapping":
                    vMapNode = vN

            elif vN.type == "TEX_ENVIRONMENT":
                if vN.label == "Lighting":
                    vEnvNodeL = vN
                elif vN.label == "Background":
                    vEnvNodeB = vN

            elif vN.type == "BACKGROUND":
                if vN.label == "Lighting":
                    vBGNodeL = vN
                elif vN.label == "Background":
                    vBGNodeB = vN
                elif len(vWNodes) == 2:
                    vBGNodeL = vN
                    vBGNodeL.label = "Lighting"
                    vBGNodeL.location = mathutils.Vector((-110, 200))

            elif vN.type == "MIX_SHADER":
                vMixNode = vN

            elif vN.type == "LIGHT_PATH":
                vLightNode = vN

            elif vN.type == "OUTPUT_WORLD":
                vWorldNode = vN

        if vTCoordNode is None:
            vTCoordNode = vWNodes.new("ShaderNodeTexCoord")
            vTCoordNode.label = "Mapping"
            vTCoordNode.location = mathutils.Vector((-1080, 420))

        if vMapNode is None:
            vMapNode = vWNodes.new("ShaderNodeMapping")
            vMapNode.label = "Mapping"
            vMapNode.location = mathutils.Vector((-870, 420))

        if vEnvNodeL is None:
            vEnvNodeL = vWNodes.new("ShaderNodeTexEnvironment")
            vEnvNodeL.label = "Lighting"
            vEnvNodeL.location = mathutils.Vector((-470, 420))

        if vEnvNodeB is None:
            vEnvNodeB = vWNodes.new("ShaderNodeTexEnvironment")
            vEnvNodeB.label = "Background"
            vEnvNodeB.location = mathutils.Vector((-470, 100))

        if vBGNodeL is None:
            vBGNodeL = vWNodes.new("ShaderNodeBackground")
            vBGNodeL.label = "Lighting"
            vBGNodeL.location = mathutils.Vector((-110, 200))

        if vBGNodeB is None:
            vBGNodeB = vWNodes.new("ShaderNodeBackground")
            vBGNodeB.label = "Background"
            vBGNodeB.location = mathutils.Vector((-110, 70))

        if vMixNode is None:
            vMixNode = vWNodes.new("ShaderNodeMixShader")
            vMixNode.location = mathutils.Vector((110, 300))

        if vLightNode is None:
            vLightNode = vWNodes.new("ShaderNodeLightPath")
            vLightNode.location = mathutils.Vector((-110, 550))

        if vWorldNode is None:
            vWorldNode = vWNodes.new("ShaderNodeOutputWorld")
            vWorldNode.location = mathutils.Vector((370, 300))

        vWLinks.new(vTCoordNode.outputs["Generated"], vMapNode.inputs["Vector"])
        vWLinks.new(vMapNode.outputs["Vector"], vEnvNodeL.inputs["Vector"])
        vWLinks.new(vEnvNodeL.outputs["Color"], vBGNodeL.inputs["Color"])
        vWLinks.new(vBGNodeL.outputs[0], vMixNode.inputs[1])

        vWLinks.new(vTCoordNode.outputs["Generated"], vMapNode.inputs["Vector"])
        vWLinks.new(vMapNode.outputs["Vector"], vEnvNodeB.inputs["Vector"])
        vWLinks.new(vEnvNodeB.outputs["Color"], vBGNodeB.inputs["Color"])
        vWLinks.new(vBGNodeB.outputs[0], vMixNode.inputs[2])

        vWLinks.new(vLightNode.outputs[0], vMixNode.inputs[0])

        vWLinks.new(vMixNode.outputs[0], vWorldNode.inputs[0])

        if vLIName in bpy.data.images.keys():
            vImageL = bpy.data.images[vLIName]

        else:
            vImageL = bpy.data.images.load(vLTex)
            vImageL.name = vLIName
            vImageL.poliigon = "HDRIs;" + vAsset

        if vBIName in bpy.data.images.keys():
            vImageB = bpy.data.images[vBIName]

        else:
            vImageB = bpy.data.images.load(vBTex)
            vImageB.name = vBIName

        if "Rotation" in vMapNode.inputs:
            vMapNode.inputs["Rotation"].default_value[2] = self.rotation
        else:
            vMapNode.rotation[2] = self.rotation

        vEnvNodeL.image = vImageL
        vBGNodeL.inputs["Strength"].default_value = self.hdr_strength

        vEnvNodeB.image = vImageB
        vBGNodeB.inputs["Strength"].default_value = self.hdr_strength

        context.scene.world.poliigon_props.asset_name = vAsset
        asset_id = -1
        with cTB.lock_assets:
            assets_local_hdri = cTB.vAssets["my_assets"]["HDRIs"]
            if vAsset in assets_local_hdri.keys():
                asset_id = assets_local_hdri.get("id", -1)
        context.scene.world.poliigon_props.asset_id = asset_id
        context.scene.world.poliigon_props.asset_type = "HDRIs"
        context.scene.world.poliigon_props.size = self.vSize
        context.scene.world.poliigon_props.size_bg = self.size_bg
        context.scene.world.poliigon_props.hdr_strength = self.hdr_strength
        context.scene.world.poliigon_props.rotation = self.rotation

        cTB.f_GetSceneAssets()

        cTB.forget_last_downloaded_size(vAsset)

        if self.exec_count == 0:
            cTB.signal_import_asset(asset_id=vAData.get("id", 0))
        self.exec_count += 1
        self.report({"INFO"}, "HDRI Imported : " + vAsset)
        return {"FINISHED"}


# ::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::


class POLIIGON_OT_brush(Operator):
    bl_idname = "poliigon.poliigon_brush"
    bl_label = ""
    bl_description = "Import Brush"
    bl_options = {"GRAB_CURSOR", "BLOCKING", "REGISTER", "INTERNAL", "UNDO"}

    exec_count = 0

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    vAsset: StringProperty(options={"HIDDEN"})  # noqa: F821
    vSize: StringProperty(options={"HIDDEN"})  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @reporting.handle_operator()
    def execute(self, context):
        global cTB

        vAsset = self.vAsset
        vSize = self.vSize

        cTB.reset_asset_error(asset_name=vAsset)

        if vSize == "apply":
            vTex = cTB.imported_assets["Brushes"][vAsset].filepath

        else:
            with cTB.lock_assets:
                vAData = cTB.vAssets["my_assets"]["Brushes"][vAsset]

            vTex = [vF for vF in vAData["files"] if vSize in os.path.basename(vF)]

            if not len(vTex):
                return {"FINISHED"}

            vTex = vTex[0]

        # ...............................................................................................

        vName = f_FName(vTex)

        if "Poliigon" in bpy.data.textures.keys():
            vTexture = bpy.data.textures["Poliigon"]
        else:
            vTexture = bpy.data.textures.new("Poliigon", "IMAGE")

        if vName in bpy.data.images.keys():
            vImage = bpy.data.images[vName]
        else:
            vImage = bpy.data.images.load(vTex)
            vImage.name = vName

        vImage.poliigon = "Brushes;" + vAsset

        vTexture.image = vImage

        if "Poliigon" in bpy.data.brushes:
            vBrush = bpy.data.brushes["Poliigon"]
        else:
            vBrush = bpy.data.brushes.new("Poliigon")
            vBrush.strength = 0.3
            vBrush.blend = "MIX"
            vBrush.texture_slot.map_mode = "VIEW_PLANE"
            vBrush.stroke_method = "AIRBRUSH"

        vBrush.texture = vTexture

        cTB.f_GetSceneAssets()

        valid_context = context.object and context.object.type == "MESH"
        if valid_context:
            if context.mode not in ["SCULPT"]:
                bpy.ops.sculpt.sculptmode_toggle()

            for vA in context.screen.areas:
                if vA.type == "PROPERTIES":
                    for vR in vA.spaces:
                        if vR.type == "PROPERTIES":
                            vR.context = "TOOL"

            context.tool_settings.sculpt.brush = vBrush
            context.tool_settings.sculpt.use_symmetry_x = False
            context.tool_settings.unified_paint_settings.size = 200
        else:
            msg = (
                "Select a mesh object first to activate sculpt mode "
                "with this brush."
            )
            self.report({"INFO"}, msg)

        cTB.forget_last_downloaded_size(vAsset)

        if self.exec_count == 0:
            cTB.signal_import_asset(asset_id=vAData.get("id", 0))
        self.exec_count += 1
        return {"FINISHED"}


class POLIIGON_OT_show_preferences(bpy.types.Operator):
    """Open user preferences and display Poliigon settings"""
    bl_idname = "poliigon.open_preferences"
    bl_label = "Show Poliigon preferences"

    _options = (
        ("skip", "Skip", "Open user preferences as-is without changing visible areas"),
        ("all", "All", "Expand all sections of user preferences"),
        ("show_add_dir", "Additional library", "Show additional library directory preferences"),
        ("show_display_prefs", "Display", "Show display preferences"),
        ("show_default_prefs", "Asset prefs", "Show asset preferences"))

    set_focus: EnumProperty(items=_options, options={"HIDDEN"})  # noqa: F821

    @reporting.handle_operator()
    def execute(self, context):
        prefs = cTB.get_prefs()
        if self.set_focus == "all":
            cTB.vSettings["show_add_dir"] = True
            cTB.vSettings["show_display_prefs"] = True
            cTB.vSettings["show_default_prefs"] = True
            cTB.vSettings["show_updater_prefs"] = True
            if prefs:
                prefs.show_updater_prefs = True
        elif self.set_focus != "skip":
            cTB.vSettings["show_add_dir"] = self.set_focus == "show_add_dir"
            cTB.vSettings["show_display_prefs"] = self.set_focus == "show_display_prefs"
            cTB.vSettings["show_default_prefs"] = self.set_focus == "show_default_prefs"
            if prefs:
                prefs.show_updater_prefs = False

        bpy.ops.screen.userpref_show('INVOKE_AREA')
        bpy.data.window_managers["WinMan"].addon_search = "Poliigon"
        prefs = context.preferences
        try:
            prefs.active_section = "ADDONS"
        except TypeError as err:
            reporting.capture_message(
                "assign_preferences_tab", str(err), "error")

        addons_ids = [
            mod for mod in addon_utils.modules(refresh=False)
            if mod.__name__ == __package__]
        if not addons_ids:
            msg = "Failed to directly load and open Poliigon preferences"
            reporting.capture_message(
                "preferences_open_no_id", msg, "error")
            return {'CANCELLED'}

        addon_blinfo = addon_utils.module_bl_info(addons_ids[0])
        if not addon_blinfo["show_expanded"]:
            has_prefs = hasattr(bpy.ops, "preferences")
            has_prefs = has_prefs and hasattr(bpy.ops.preferences, "addon_expand")

            if has_prefs:  # later 2.8 buids
                bpy.ops.preferences.addon_expand(module=__package__)
            else:
                self.report(
                    {"INFO"},
                    "Search for and expand the Poliigon addon in preferences")

        cTB.track_screen("settings")
        return {'FINISHED'}


class POLIIGON_OT_close_notification(Operator):
    bl_idname = "poliigon.close_notification"
    bl_label = ""
    bl_description = "Close notification"
    bl_options = {"INTERNAL"}

    notification_index: IntProperty(options={"HIDDEN"})  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return "Close notification"  # Avoids having an extra blank line.

    @reporting.handle_operator()
    def execute(self, context):
        if self.notification_index < 0:
            self.report(
                {'ERROR'},
                f"Invalid notification index {self.notification_index} to dismiss.")
            return {'CANCELLED'}
        if len(cTB.notifications) <= self.notification_index:
            self.report(
                {'ERROR'}, "Could not dismiss notificaiton, out of bounds.")
            return {'CANCELLED'}
        cTB.dismiss_notification(notification_index=self.notification_index)
        return {'FINISHED'}


class POLIIGON_OT_report_error(Operator):
    bl_idname = "poliigon.report_error"
    bl_label = "Report error"
    bl_description = "Report an error to the developers"
    bl_options = {"INTERNAL"}

    error_report: StringProperty(options={"HIDDEN"})  # noqa: F821
    user_message: StringProperty(
        default="",  # noqa: F722
        maxlen=USER_COMMENT_LENGTH,
        options={'SKIP_SAVE'})  # noqa: F821

    target_width = 600

    def invoke(self, context, event):
        width = self.target_width  # Blender handles scaling to ui.
        return context.window_manager.invoke_props_dialog(self, width=width)

    @reporting.handle_draw()
    def draw(self, context):
        layout = self.layout

        # Display the error message (no word wrapping in case too long)
        box = layout.box()
        box.scale_y = 0.5
        box_wrap = self.target_width * cTB.get_ui_scale()
        box_wrap -= 20 * cTB.get_ui_scale()
        lines = self.error_report.split("\n")
        if len(lines) > 10:  # Prefer the last few lines.
            lines = lines[-10:]
        for ln in lines:
            if not ln:
                continue
            box.label(text=ln)

        # Display instructions to submit a comment.
        label_txt = "(Optional) What were you doing when this error occurred?"
        target_wrap = self.target_width * cTB.get_ui_scale()
        target_wrap -= 10 * cTB.get_ui_scale()
        cTB.f_Label(target_wrap, label_txt, layout)
        layout.prop(self, "user_message", text="")

        cTB.f_Label(target_wrap, "Press OK to send report", layout)

    @reporting.handle_operator(silent=True)
    def execute(self, context):
        if bpy.app.background:  # No user to give feedback anyways.
            return {'CANCELLED'}
        reporting.user_report(self.error_report, self.user_message)
        self.report({"INFO"}, "Thanks for sharing this report")
        return {'FINISHED'}


class POLIIGON_OT_check_update(Operator):
    bl_idname = "poliigon.check_update"
    bl_label = "Check for update"
    bl_description = "Check for any addon updates"
    bl_options = {"INTERNAL"}

    @reporting.handle_operator(silent=True)
    def execute(self, context):
        cTB.print_debug(0, "Started check for update with",
                        cTB.updater.addon_version,
                        cTB.updater.software_version)
        cTB.updater.async_check_for_update(
            callback=cTB.check_update_callback, create_notifications=True)
        cTB.print_debug(0, "Update ready?",
                        cTB.updater.update_ready, cTB.updater.update_data)

        return {'FINISHED'}


class POLIIGON_OT_refresh_data(Operator):
    bl_idname = "poliigon.refresh_data"
    bl_label = "Refresh data"
    bl_description = "Refresh thumbnails and reload data"
    bl_options = {"INTERNAL"}

    @reporting.handle_operator(silent=True)
    def execute(self, context):
        cTB.refresh_data()
        return {'FINISHED'}


class POLIIGON_OT_popup_message(Operator):
    bl_idname = "poliigon.popup_message"
    bl_label = ""
    bl_options = {"INTERNAL"}

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    message_body: StringProperty(options={"HIDDEN"})  # noqa: F821
    message_url: StringProperty(options={"HIDDEN"})  # noqa: F821
    notice_id: StringProperty(options={"HIDDEN"})  # noqa: F821

    target_width = 400

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    def invoke(self, context, event):
        width = self.target_width  # Blender handles scaling to ui.
        cTB.click_notification(self.notice_id, "popup")
        return context.window_manager.invoke_props_dialog(self, width=width)

    @reporting.handle_draw()
    def draw(self, context):
        layout = self.layout
        target_wrap = self.target_width * cTB.get_ui_scale()
        target_wrap -= 10 * cTB.get_ui_scale()
        cTB.f_Label(target_wrap, self.message_body, layout)

    @reporting.handle_operator(silent=True)
    def execute(self, context):
        if self.message_url:
            bpy.ops.wm.url_open(url=self.message_url)
        cTB.finish_notification(self.notice_id)
        return {'FINISHED'}


class POLIIGON_OT_notice_operator(Operator):
    bl_idname = "poliigon.notice_operator"
    bl_label = ""
    bl_options = {"INTERNAL"}

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821
    notice_id: StringProperty(options={"HIDDEN"})  # noqa: F821
    ops_name: StringProperty(options={"HIDDEN"})  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @reporting.handle_operator(silent=True)
    def execute(self, context):
        # Execute the operator via breaking into parts e.g. "wm.quit_blender"
        atr = self.ops_name.split(".")
        if len(atr) != 2:
            reporting.capture_message("bad_notice_operator", self.ops_name)
            return {'CANCELLED'}

        # Safeguard to avoid injection.
        if self.ops_name not in ("wm.quit_blender"):
            cTB.print_debug(f"Unsupported operation: {self.ops_name}")
            return {'CANCELLED'}

        cTB.click_notification(self.notice_id, self.ops_name)
        cTB.print_debug(0, f"Running {self.ops_name}")

        # Using invoke acts like in the interface, so any "save?" dialogue
        # will pick up, for instance if a "quit" operator.
        getattr(getattr(bpy.ops, atr[0]), atr[1])('INVOKE_DEFAULT')
        cTB.finish_notification(self.notice_id)
        return {'FINISHED'}


# These need to be global to work in _fill_node_drop_down()
enum29 = (
    ("Poliigon_Mixer", "Principled mixer", "Principled mixer node"),
    ("Mosaic_UV_Mapping", "Mosaic mapping", "Poliigon Mosaic mapping node"),
)
enum28 = (
    ("Poliigon_Mixer", "Principled mixer", "Principled mixer node"),
)

# Needs to be global,
# as member variable can not be accessed in "items" function of EnumProperty
view_screen_tracked_nodes = False


class POLIIGON_OT_add_converter_node(bpy.types.Operator):
    bl_idname = "poliigon.add_converter_node"
    bl_label = "Converter node group"
    bl_description = "Adds a material converter node group"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    def _fill_node_drop_down(self, context):
        """Returns list of available nodes as EnumPropertyItems.
        While the enums are actually static, this function serves as a
        "draw detection" to track view screen.
        """

        # Called during class construction, we can not access a
        # member variable here
        global view_screen_tracked_nodes

        if not view_screen_tracked_nodes:
            cTB.track_screen("blend_node_add")
            view_screen_tracked_nodes = True

        if bpy.app.version >= (2, 90):
            return enum29
        else:
            return enum28

    node_type: EnumProperty(items=_fill_node_drop_down)

    @reporting.handle_operator(silent=True)
    def execute(self, context):
        if not self.node_type:
            self.report({"Error"}, "No node_type specified to add")
            return {'CANCELLED'}

        if not context.material:
            self.report({"ERROR"}, "No active material selected to add nodegroup")
            return {"CANCELLED"}

        for node in context.material.node_tree.nodes:
            node.select = False

        # TODO(Andreas): Not supposed to stay in legacy importer module
        node_group = load_poliigon_node_group(self.node_type)
        if node_group is None:
            self.report({"ERROR"}, "Failed to import nodegroup.")
            return {"CANCELLED"}

        mat = context.material
        node_mosaic = mat.node_tree.nodes.new("ShaderNodeGroup")
        node_mosaic.node_tree = node_group
        node_mosaic.name = node_group.name
        node_mosaic.width = 200
        if not node_mosaic.node_tree:
            self.report({"ERROR"}, "Failed to load nodegroup.")
            return {"CANCELLED"}

        # Use this built in modal for moving the added node around
        return bpy.ops.node.translate_attach('INVOKE_DEFAULT')


class POLIIGON_OT_get_local_asset_sync(Operator):
    bl_idname = "poliigon.get_local_asset_sync"
    bl_label = "For internal testing, only"
    bl_description = "For internal testing, only"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        cTB.f_GetLocalAssetsThread()
        cTB.f_GetAssets(vUseThread=False)
        cTB.f_GetAssets(
            vArea="my_assets", vMax=MAX_MY_ASSETS_REQUEST_SIZE, vBackground=1, vUseThread=False, force=True)
        return {"FINISHED"}


class POLIIGON_OT_load_asset_size_from_list(bpy.types.Operator):
    bl_idname = "poliigon.load_asset_size_from_list"
    bl_label = "Import list of files as an asset"
    bl_description = "Import an asset from a list of files"
    bl_options = {'REGISTER', 'INTERNAL'}

    # Use negative asset IDs for, now, caller to key track to keep unique within session.
    asset_id: IntProperty(default=-1, options={'SKIP_SAVE'})  # noqa: F821
    asset_name: StringProperty(options={"HIDDEN"})  # noqa: F821
    asset_type: StringProperty(options={"HIDDEN"})  # noqa: F821
    file_list_json: StringProperty(options={"HIDDEN"})  # noqa: F821
    size: StringProperty(options={"HIDDEN"})  # noqa: F821
    lod: StringProperty(options={"HIDDEN"})  # noqa: F821
    convention: IntProperty(default=1, options={'SKIP_SAVE'})  # noqa: F821

    def _validate_properties(self) -> None:
        """Validates received parameters.

        Raise ValueError, if validation failed.
        """

        if self.asset_id >= 0:
            msg = f"Only negative asset IDs allowed, for now (not {self.asset_id})"
            self.report({"ERROR"}, msg)
            raise ValueError(msg)
        if len(self.asset_name) == 0:
            msg = "Please specify an asset name"
            self.report({"ERROR"}, msg)
            raise ValueError(msg)
        if self.asset_type not in ["HDRIs", "Models", "Textures"]:
            msg = (f"Unknown asset type: {self.asset_type}\n"
                   "Known types: HDRIs, Models, Textures")
            self.report({"ERROR"}, msg)
            raise ValueError(msg)
        if len(self.size) > 0:
            msg = (f"Unknown size string: {self.size}\n"
                   "Expected something like: '256', '2K' or '16K'")
            try:
                if self.size[-1] == "K":
                    int(self.size[:-1])
                else:
                    int(self.size)
            except ValueError:
                raise ValueError(msg)
        if len(self.lod) > 0 and not self.lod.startswith("LOD"):
            msg = (f"Unknown LOD string format: {self.lod}\n"
                   "Expected something like: 'LOD0'")
            self.report({"ERROR"}, msg)
            raise ValueError(msg)
        # TODO(Andreas): Any additional validation needed?
        #                E.g. test if file types in file_list_json actually
        #                match file tags?`Like an "xyz.fbx" for COL channel?

    def _derive_properties_from_files(
            self,
            tex_maps: List[str]
    ) -> Tuple[List[str], List[str], List[str]]:
        """Derives workflows, sizes and LODs from filenames."""

        workflows = []
        sizes = []
        lods = []
        for path in tex_maps:
            dir_parent = os.path.basename(os.path.dirname(path))
            filename = os.path.basename(path)
            filename_no_ext, _ = os.path.splitext(filename)
            filename_parts = filename_no_ext.split("_")
            for part in filename_parts:
                match_size = re.search(r"(\d+K)", part)
                match_lod = re.search(r"(LOD\d)", part)

                if part in ["METALNESS", "SPECULAR", "REGULAR"]:
                    workflows.append(part)
                elif match_size is not None:
                    sizes.append(part)
                elif match_lod is not None:
                    lods.append(part)
                elif dir_parent in SIZES:
                    sizes.append(dir_parent)

        workflows = list(set(workflows))
        sizes = list(set(sizes))
        lods = list(set(lods))

        return workflows, sizes, lods

    @reporting.handle_operator()
    def execute(self, context):
        # Deliberately not catching ValueError, here.
        # Scripts using this operator are supposed to fail.
        self._validate_properties()

        file_list = json.loads(self.file_list_json)
        files = []  # List of _all_ files belonging to asset
        files_tex = []  # List of only texture files
        maps = []  # List of map types stored in asset data
        for single_file_dict in file_list:
            for key, path_file in single_file_dict.items():
                if key != "MODEL":
                    maps.append(key)
                    files_tex.append(path_file)
                files.append(path_file)
                break  # there's always only one entry

        maps = list(set(maps))

        # Filter maps to be of same workflow
        # Note: size and name_mat are not needed here and could be arbitrarily
        #       chosen.
        #       size is internally only used for a "PREVIEW" size check.
        #       name_mat is used for printing an error message, only.
        if self.asset_type != "HDRIs":
            files_tex, _ = filter_textures_by_workflow(
                files_tex, size=self.size, name_mat=self.asset_name)

        workflows, sizes, lods = self._derive_properties_from_files(files_tex)
        if len(workflows) == 0:
            print("WARN: No known workflow found in filenames!")
            # TODO(Andreas): Rather fail than this fallback?
            if self.asset_type == "HDRIs":
                workflows.append("REGULAR")
            else:
                workflows.append("METALNESS")
        if len(sizes) == 0:
            print("WARN: No sizes found in filenames!")
        if self.size not in sizes:
            raise ValueError(f"Size {self.size} not found in files!")
        if len(lods) == 0:
            print("WARN: No lods found in filenames!")
            lods = ["NONE"]
        if len(self.lod) > 0 and self.lod not in lods:
            raise ValueError(f"LOD {self.lod} not found in files!")

        date_now = datetime.datetime.now().strftime("%Y-%m-%d 00:00:00")

        asset_data = {}
        asset_data["name"] = self.asset_name
        asset_data["name_beauty"] = self.asset_name
        asset_data["id"] = self.asset_id
        asset_data["slug"] = self.asset_name
        asset_data["type"] = self.asset_type
        asset_data["files"] = files
        asset_data["maps"] = maps
        asset_data["lods"] = lods
        asset_data["sizes"] = sizes
        asset_data["workflows"] = workflows
        asset_data["vars"] = []
        asset_data["date"] = date_now
        asset_data["credits"] = 0
        asset_data["categories"] = [self.asset_type]
        asset_data["preview"] = ""
        asset_data["thumbnails"] = []
        asset_data["quick_preview"] = []
        asset_data["in_asset_browser"] = False
        asset_data["url"] = ""
        asset_data["api_convention"] = self.convention
        asset_data["local_convention"] = self.convention

        with cTB.lock_assets:
            if self.asset_name in cTB.vAssets["poliigon"][self.asset_type]:
                print(f"WARN: An asset with name {self.asset_name} already existed!")
            if self.asset_name in cTB.vAssets["my_assets"][self.asset_type]:
                print(f"WARN: A local asset with name {self.asset_name} already existed!")

            cTB.vAssets["poliigon"][self.asset_type][self.asset_name] = asset_data
            cTB.vAssets["my_assets"][self.asset_type][self.asset_name] = asset_data
            cTB.vAssets["local"][self.asset_type][self.asset_name] = asset_data

        cTB.vPurchased.append(self.asset_name)

        # TODO(Andreas): If we wanted the imported asset to appear in UI,
        #                we'd need to fill this (not exactly easy...):
        # with self.lock_asset_index:
        #     if vKey in cTB.vAssetsIndex[vArea].keys():
        #         self.vAssetsIndex[vArea][vKey][vIdx] = [asset_type, asset_data]

        return {"FINISHED"}


class POLIIGON_OT_unsupported_convention(bpy.types.Operator):
    bl_idname = "poliigon.unsupported_convention"
    bl_label = "Unsupported"
    bl_description = "Addon does not support asset convention, try updating the addon"
    bl_options = {'REGISTER', 'INTERNAL'}

    vTooltip: StringProperty(options={"HIDDEN"})  # noqa: F821

    @classmethod
    def description(cls, context, properties):
        return properties.vTooltip

    @reporting.handle_operator()
    def execute(self, context):
        msg = ("Asset not supported, please update plugin. "
               "This asset is published with newer conventions to improve render outputs.")
        ui.f_Popup(cTB,
                   vTitle="Addon Update Needed",
                   vMsg=msg,
                   vBtns=["OK", "Update"],
                   vCmds=["check_update", "open_p4b_url"],
                   vMode=None,
                   w_limit=210)
        return {"FINISHED"}


classes = (
    POLIIGON_OT_setting,
    POLIIGON_OT_user,
    POLIIGON_OT_link,
    POLIIGON_OT_download,
    POLIIGON_OT_cancel_download,
    POLIIGON_OT_options,
    POLIIGON_OT_active,
    POLIIGON_OT_detail,
    POLIIGON_OT_folder,
    POLIIGON_OT_library,
    POLIIGON_OT_directory,
    POLIIGON_OT_category,
    POLIIGON_OT_preview,
    POLIIGON_OT_view_thumbnail,
    POLIIGON_OT_material,
    POLIIGON_OT_show_quick_menu,
    POLIIGON_OT_apply,
    POLIIGON_OT_model,
    POLIIGON_OT_select,
    POLIIGON_OT_hdri,
    POLIIGON_OT_brush,
    POLIIGON_OT_show_preferences,
    POLIIGON_OT_close_notification,
    POLIIGON_OT_report_error,
    POLIIGON_OT_check_update,
    POLIIGON_OT_refresh_data,
    POLIIGON_OT_popup_message,
    POLIIGON_OT_notice_operator,
    POLIIGON_OT_add_converter_node,
    POLIIGON_OT_get_local_asset_sync,
    POLIIGON_OT_load_asset_size_from_list,
    POLIIGON_OT_unsupported_convention
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
