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

bl_info = {
    "name": "Poliigon",
    "author": "Poliigon",
    "version": (1, 7, 0),
    "blender": (2, 83, 0),
    "location": "3D View",
    "description": "Load models, textures, and more from Poliigon and locally",
    "doc_url": "https://help.poliigon.com/en/articles/6342599-poliigon-blender-addon-2023?utm_source=blender&utm_medium=addon",  # noqa: E501
    "tracker_url": "https://help.poliigon.com/en/?utm_source=blender&utm_medium=addon",  # noqa: E501
    "category": "3D View",
}


if "bpy" in locals():
    import importlib
    import bpy

    importlib.reload(operators)  # noqa: F821
    importlib.reload(preferences)  # noqa: F821
    importlib.reload(props)  # noqa: F821
    importlib.reload(reporting)  # noqa: F821
    importlib.reload(toolbox)  # noqa: F821
    importlib.reload(ui)  # noqa: F821
    if bpy.app.version >= (3, 0):
        importlib.reload(asset_browser_sync_commands)  # noqa: F821
        importlib.reload(asset_browser)  # noqa: F821
        importlib.reload(asset_browser_ui)  # noqa: F821
        importlib.reload(asset_browser_operators)  # noqa: F821
    importlib.reload(api)  # noqa: F821
    importlib.reload(env)  # noqa: F821
    importlib.reload(updater)  # noqa: F821
else:
    import bpy

    from . import operators
    from . import preferences
    from . import props
    from . import reporting  # noqa: F401
    from . import toolbox
    from . import ui
    if bpy.app.version >= (3, 0):
        from . import asset_browser_sync_commands  # noqa: F401
        from . import asset_browser  # noqa: F401
        from . import asset_browser_ui
        from . import asset_browser_operators
    from .modules.poliigon_core import api  # noqa: F401, needed for tests
    from .modules.poliigon_core import env  # noqa: F401, needed for tests
    from .modules.poliigon_core import updater  # noqa: F401, needed for tests


def register():
    props.register()
    preferences.register()
    toolbox.register(bl_info)
    operators.register()
    ui.register()
    if bpy.app.version >= (3, 0):
        asset_browser_operators.register()
        asset_browser_ui.register()


def unregister():
    # Reverse order of register.
    if bpy.app.version >= (3, 0):
        asset_browser_ui.unregister()
        asset_browser_operators.unregister()
    ui.unregister()
    operators.unregister()
    toolbox.unregister()
    preferences.unregister()
    props.unregister()


if __name__ == "__main__":
    register()
