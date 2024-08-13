from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .modules.poliigon_core import notifications
from .modules.poliigon_core.updater import SoftwareUpdater, t2v


@dataclass
class Notification:
    """Container object for a user notification."""
    class ActionType(Enum):
        OPEN_URL = 1
        UPDATE_READY = 2
        POPUP_MESSAGE = 3
        RUN_OPERATOR = 4

    notification_id: str  # Unique id for this specific kind of notice.
    title: str  # Main title, should be short
    action: ActionType  # Indicator of how to structure and draw notification.
    allow_dismiss: bool = True  # Allow the user to dismiss the notification.
    auto_dismiss: bool = False  # Dismiss after user interacted with the notification
    tooltip: Optional[str] = None  # Hover-over tooltip, if there is a button
    icon: Optional[str] = None  # Blender icon enum to use.
    viewed: bool = False  # False until actually drawn

    # Treat below as a "oneof" where only set if the given action is assigned.

    # OPEN_URL
    ac_open_url_address: Optional[str] = None
    ac_open_url_label: Optional[str] = None

    # UPDATE_READY
    ac_update_ready_download_url: Optional[str] = None
    ac_update_ready_download_label: Optional[str] = None
    ac_update_ready_logs_url: Optional[str] = None
    ac_update_ready_logs_label: Optional[str] = None

    # POPUP_MESSAGE
    # If url is populated, opens the given url in a webbrowser, otherwise
    # this popup can just be dismissed.
    ac_popup_message_body: Optional[str] = None
    ac_popup_message_url: Optional[str] = None
    ac_popup_message_alert: bool = True

    # RUN_OPERATOR
    # Where the message leads to a popup with an OK button that leads to an
    # execution of some kind.
    ac_run_operator_ops_name: Optional[str] = None


def build_update_notification(
        p4b_updater: SoftwareUpdater) -> Optional[Notification]:
    """Constructs the update notification if available."""
    if not p4b_updater.update_ready:
        return None

    this_update = p4b_updater.update_data
    vstring = t2v([str(x) for x in this_update.version])
    logs = "https://poliigon.com/blender"

    update_notice = Notification(
        notification_id="UPDATE_READY_MANUAL_INSTALL",
        title="Update ready:",
        action=Notification.ActionType.UPDATE_READY,
        tooltip=f"Download the {vstring} update",
        allow_dismiss=True,
        ac_update_ready_download_url=this_update.url,
        ac_update_ready_download_label="Install",
        ac_update_ready_logs_url=logs,
        ac_update_ready_logs_label="Logs"
    )
    return update_notice


def rebuild_core_notification(
        core_notice: notifications.NotificationSystem
) -> Optional[Notification]:
    """Translates a core notification into the local notice format."""
    # TODO(Patrick): Actually adopt the poliigon_core NotificationSystem.

    if isinstance(core_notice, notifications.NotificationPopup):
        action = Notification.ActionType.POPUP_MESSAGE
        notice = Notification(
            notification_id=core_notice.id_notice,
            title=core_notice.title,
            action=action,
            tooltip=core_notice.tooltip,
            allow_dismiss=core_notice.auto_dismiss,
            ac_popup_message_body=core_notice.body,
            ac_popup_message_url=core_notice.url,
            ac_popup_message_alert=core_notice.alert)
    elif isinstance(core_notice, notifications.NotificationOpenUrl):
        action = Notification.ActionType.OPEN_URL
        notice = Notification(
            notification_id=core_notice.id_notice,
            title=core_notice.title,
            action=action,
            tooltip=core_notice.tooltip,
            allow_dismiss=core_notice.auto_dismiss,
            ac_open_url_address=core_notice.url,
            ac_open_url_label=core_notice.title)
    else:
        print("Notification type not implemented")
        print(core_notice)
        print(type(core_notice))
        return None

    print("DEBUG: generated notice", notice)
    return notice


def build_no_internet_notification():
    msg = (
        "Please connect to the internet to continue using the Poliigon "
        "Addon."
    )
    notice = Notification(
        notification_id="NO_INTERNET_CONNECTION",
        title="No internet access",
        action=Notification.ActionType.POPUP_MESSAGE,
        tooltip=msg,
        allow_dismiss=False,
        ac_popup_message_body=msg
    )
    return notice


def build_material_template_error_notification():
    msg = ("Failed to load the material template file.\n"
           "Please remove the addon, restart blender,\n"
           "and re-install the latest version of the addon.\n"
           "Please reach out to support if you continue to have issues at help.poliigon.com")
    notice = Notification(
        notification_id="MATERIAL_TEMPLATE_ERROR",
        title="Material template error",
        action=Notification.ActionType.POPUP_MESSAGE,
        tooltip=msg,
        allow_dismiss=True,
        ac_popup_message_body=msg
    )
    return notice


def build_proxy_notification():
    msg = ("Error: Blender cannot connect to the internet.\n"
           "Disable network proxy or firewalls.")
    notice = Notification(
        notification_id="PROXY_CONNECTION_ERROR",
        title="Encountered proxy error",
        action=Notification.ActionType.POPUP_MESSAGE,
        tooltip=msg,
        allow_dismiss=False,
        ac_popup_message_body=msg
    )
    return notice


def build_restart_notification():
    notice = Notification(
        notification_id="RESTART_POST_UPDATE",
        title="Restart Blender",
        action=Notification.ActionType.RUN_OPERATOR,
        tooltip="Please restart Blender to complete the update",
        allow_dismiss=False,
        ac_run_operator_ops_name="wm.quit_blender"
    )
    return notice


def build_survey_notification(notification_id, url):
    notice = Notification(
        notification_id=notification_id,
        title="How's the addon?",
        action=Notification.ActionType.OPEN_URL,
        tooltip="Share your feedback so we can improve this addon for you",
        allow_dismiss=True,
        auto_dismiss=True,
        ac_open_url_address=url,
        ac_open_url_label="Let us know"
    )
    return notice


def build_writing_settings_failed_notification(error_string: str):
    msg = f"Error: Failed to write its settings: {error_string}"
    notice = Notification(
        notification_id="SETTINGS_WRITE_ERROR",
        title="Failed to write settings",
        action=Notification.ActionType.POPUP_MESSAGE,
        tooltip=msg,
        allow_dismiss=True,
        ac_popup_message_body=msg
    )
    return notice
