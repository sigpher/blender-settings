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

import os
import re
from typing import List, Tuple

import bpy

from .modules.poliigon_core.assets import SIZES
from . import reporting


def find_identical_material(asset_name: str,
                            asset_type: str,
                            size: str,
                            mapping: str,
                            scale: float,
                            displacement: float,
                            use_16bit: bool,
                            mode_disp: str,
                            is_backplate: bool = False
                            ) -> bpy.types.Material:
    """Tries to find an parameter-wise identical material in current scene."""

    identical_mat = None
    for mat in bpy.data.materials:
        if not mat.poliigon_props.asset_name.startswith(asset_name):
            continue
        if mat.poliigon_props.asset_type != asset_type:
            continue
        if mat.poliigon_props.size != size:
            continue
        if mat.poliigon_props.mapping != mapping:
            continue
        if mat.poliigon_props.scale != scale:
            continue
        if mat.poliigon_props.displacement != displacement:
            continue
        if mat.poliigon_props.use_16bit != use_16bit:
            continue
        if mat.poliigon_props.mode_disp != mode_disp:
            continue
        if mat.poliigon_props.is_backplate != is_backplate:
            continue
        identical_mat = mat
        break
    return identical_mat


def get_all_nodes(node_tree: bpy.types.NodeTree):
    nodes = list(node_tree.nodes)
    for node in node_tree.nodes:
        if node.bl_idname != "ShaderNodeGroup":
            continue
        elif not node.node_tree:
            continue
        nodes.extend(get_all_nodes(node.node_tree))
    return nodes


def get_node_by_type(
        group: bpy.types.Node, bl_idname: str) -> bpy.types.Node:
    """Returns first node of given type (bl_idname) found in group."""

    node_found = None
    for _node in group.node_tree.nodes:
        if _node.bl_idname != bl_idname:
            continue
        node_found = _node
        break
    return node_found


def get_node_by_name(group: bpy.types.Node, name: str) -> bpy.types.Node:
    """Returns first node with given name found in group."""

    node_found = None
    for _node in group.node_tree.nodes:
        if _node.name != name:
            continue
        node_found = _node
        break
    return node_found


def get_all_node_trees(node_tree: bpy.types.NodeTree,
                       include_root: bool = True):
    node_trees = [node_tree] if include_root else []
    for node in node_tree.nodes:
        if node.bl_idname != "ShaderNodeGroup":
            continue
        elif not node.node_tree:
            continue
        node_trees.extend(get_all_node_trees(node.node_tree))
    return node_trees


def mat_get_nodes(mat: bpy.types.Material,
                  node_idname: str = "ShaderNodeTexImage"):
    if mat is None:
        return []

    nodes = get_all_nodes(mat.node_tree)

    tex_nodes = [
        node for node in nodes
        if node.bl_idname == node_idname
    ]
    return tex_nodes


def regex_size_rename(name_old: str, size_new: str) -> str:
    """Returns a name with new_size, if a size is found in name_old"""

    # Match in order an underscore, digit number (also multiple digits),
    # immediately followed by K
    # group(1) contains the digit number (size) we are interested in.
    # Capturing group example: "whatever_4K" => 4
    name_new = name_old
    match_object = re.search(r"_(\d+K)", name_old)
    if match_object is not None:
        size_old = match_object.group(1)
        name_new = name_old.replace(size_old, size_new)
    return name_new


def rename_material_and_nodes(mat: bpy.types.Material,
                              size: str) -> None:
    # Rename material, first
    mat.name = regex_size_rename(mat.name, size)
    # Then rename all group nodes containing size in name
    nodes = get_all_nodes(mat.node_tree)
    for _node in nodes:
        if _node.bl_idname != "ShaderNodeGroup":
            continue
        _node.name = regex_size_rename(_node.name, size)
    # Finally rename all node trees containing size in name
    node_trees = get_all_node_trees(
        mat.node_tree, include_root=False)
    for _node_tree in node_trees:
        _node_tree.name = regex_size_rename(
            _node_tree.name, size)


def replace_tex_size(materials: List,
                     asset_files: List[str],
                     size: str,
                     link_blend: bool
                     ) -> None:
    """Changes the texture resolution of all materials in list."""

    if link_blend:
        return

    for mat in materials:
        tex_nodes = mat_get_nodes(
            mat, node_idname="ShaderNodeTexImage")
        replaced_tex = False
        for node in tex_nodes:
            if node is None or node.image is None:
                continue

            # Match in order an underscore, digit number (also multiple
            # digits), immediately followed by K,
            # followed by an underscore or a period.
            # group(1) contains the digit number we are interested in.
            # Capturing group examples: "_4K." or "_16K_METALLIC"
            path_tex = node.image.filepath
            match_object = re.search(r"_(\d+K)[_\.]", path_tex)
            dir_parent = os.path.basename(os.path.dirname(path_tex))
            if match_object is not None:
                imported_size = match_object.group(1)
            elif "HIRES" in node.image.filepath:
                imported_size = "HIRES"
            elif dir_parent in SIZES:
                imported_size = dir_parent
            else:
                print("Invalid filepath for parsing", node.image.filepath)
                continue
            if imported_size == size:
                continue

            directory, filename = os.path.split(node.image.filepath)
            filename_desired_size = filename.replace(imported_size, size)
            directory_desired_size = directory.replace(imported_size, size)
            path_desired_size = os.path.join(
                directory_desired_size, filename_desired_size)
            path_found = None
            for path_asset_file in asset_files:
                if path_asset_file == path_desired_size:
                    path_found = path_asset_file
                    break
            if path_found is not None:
                node.image.filepath = path_found
                node.image.name = os.path.basename(path_found)
                replaced_tex = True
        # Finally also change the material name to the new size
        if replaced_tex:
            rename_material_and_nodes(mat, size)


def print_node_inputs_outputs(node: bpy.types.Node) -> None:
    """Prints input and output ports of a node with their names and data
    type.
    """

    print(f"Node: {node.name}")
    print("Inputs:")
    for idx, _in in enumerate(node.inputs):
        print("  ", idx, _in.name, _in.type)
    print("Outputs:")
    for idx, _out in enumerate(node.outputs):
        print("  ", idx, _out.name, _out.type)


def load_poliigon_node_group(node_type: str) -> bpy.types.Node:
    """Loads the needed node group from template, if not already local."""

    if node_type in bpy.data.node_groups.keys():
        return bpy.data.node_groups[node_type]

    dir_script = os.path.join(os.path.dirname(__file__), "files")
    path_template = os.path.join(dir_script,
                                 "poliigon_material_template.blend")

    if not os.path.exists(path_template):
        msg = f"Material template file missing!\n{path_template}"
        reporting.capture_message(
            "add_converter_node_no_template", msg, "error")
        return None

    nodes_before = list(bpy.data.node_groups)

    with bpy.data.libraries.load(path_template, link=False) as (from_file,
                                                                into):
        into.node_groups = [
            node_group for node_group in from_file.node_groups
            if node_group in [node_type]
        ]

    nodes_after = list(bpy.data.node_groups)
    # Safely get the newly imported datablock, without referencing by name.
    nodes_imported = list(set(nodes_after) - set(nodes_before))
    if len(nodes_imported) == 0:
        raise RuntimeError("No new node groups imported")
    elif len(nodes_imported) > 1:
        # Not supposed to occur
        print("Warning, more than one??")
    node_mosaic = nodes_imported[0]  # but just return first if more than one
    node_mosaic.name = node_type  # pass in UI friendly name
    return node_mosaic


def filter_textures_by_workflow(textures: List[str],
                                size: str,
                                name_mat: str
                                ) -> Tuple[List[str], bool]:

    def parent_dir_name(path: str) -> str:
        return os.path.basename(os.path.dirname(path))

    def filename_no_ext(path: str) -> str:
        return os.path.splitext(os.path.basename(path))[0]

    textures_metallic = [
        tex
        for tex in textures
        if filename_no_ext(tex).endswith("METALNESS") or parent_dir_name(tex) == "METALNESS"
    ]
    textures_specular = [
        tex
        for tex in textures
        if filename_no_ext(tex).endswith("SPECULAR") or parent_dir_name(tex) == "SPECULAR"
    ]
    textures_dielectric = [
        tex
        for tex in textures
        if tex not in textures_metallic and tex not in textures_specular
    ]
    textures_overlay = [
        tex
        for tex in textures
        if "OVERLAY" in os.path.splitext(os.path.basename(tex))[0]
    ]

    has_col_or_alpha = False
    for tex in textures:
        filename = os.path.splitext(os.path.basename(tex))[0]
        has_col = "COL" in filename
        has_alpha = "ALPHA" in filename
        if has_col or has_alpha:
            has_col_or_alpha = True
            break

    only_overlay = False
    # TODO(Andreas): Dear reviewer, before refactoring, below if statement
    #                had this additional condition:
    #                and len(textures_overlay) <= len(textures)
    #                Seeing how textures_overlay is generated above,
    #                it is always true, isn't it?
    if not has_col_or_alpha and len(textures_overlay) > 0:
        # This is an overlay, not a full texture.
        only_overlay = True
        textures_workflow = textures
    elif len(textures_metallic) >= 4:
        textures_workflow = textures_metallic + textures_dielectric
    elif len(textures_specular) >= 4:
        textures_workflow = textures_specular + textures_dielectric
    elif len(textures_dielectric) >= 4:
        textures_workflow = textures_dielectric
    elif size == "PREVIEW":
        textures_workflow = textures
    elif has_col_or_alpha and len(textures_dielectric) > 0:
        # Likely decals or seafoam, which only have color information
        # but don't have OVERLAY as a map pass (only COL or ALPHAMASKED).
        textures_workflow = textures_dielectric
    elif has_col_or_alpha and len(textures_metallic) > 0:
        # Likely remastered asset with too few metalness textures
        textures_workflow = textures_metallic
    else:
        msg = (
            f"Wrong tex counts for {name_mat} to determine workflow - "
            f"metal:{len(textures_metallic)}, "
            f"specular:{len(textures_specular)}, "
            f"dielectric:{len(textures_dielectric)}"
        )
        reporting.capture_message(
            "build_mat_error_workflow", msg, "error")
        return None, only_overlay
    return textures_workflow, only_overlay
