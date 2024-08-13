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

import mathutils
from typing import Optional

import bpy

from .material_import_utils import load_poliigon_node_group


def create_node(
    group: bpy.types.Node,
    bl_idname: str,
    parent: Optional[bpy.types.Node],
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    select: bool = False,
    hide: bool = False
) -> bpy.types.Node:
    """Creates an arbitrary node of type bl_idname."""

    node = group.node_tree.nodes.new(bl_idname)
    if name is not None:
        node.label = name
        node.name = name
    if parent is not None:
        node.parent = parent
    if location is not None:
        node.location = location
    if width is not None:
        node.width = width
    if height is not None:
        node.height = height
    node.select = select
    node.hide = hide
    return node


def create_node_socket(
    node_group: bpy.types.Node,
    *,
    socket_type: str = "NodeSocketVector",
    in_out: str = "INPUT",
    name: str = "Vector",
    description: str = ""
) -> None:
    """Creates a new input or output socket on a group node."""

    if bpy.app.version >= (4, 0):
        node_group.node_tree.interface.new_socket(
            name,
            description=description,
            in_out=in_out,
            socket_type=socket_type,
            parent=None
        )
    elif bpy.app.version >= (3, 4):
        if in_out == "INPUT":
            node_group.node_tree.inputs.new(socket_type, name)
        else:
            node_group.node_tree.outputs.new(socket_type, name)
    else:
        if in_out == "INPUT":
            node_group.inputs.new(socket_type, name)
        else:
            node_group.outputs.new(socket_type, name)


def create_frame(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node],
    *,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates an arbitrary node of type bl_idname."""

    frame = create_node(
        group=group,
        bl_idname="NodeFrame",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    return frame


def create_add_shader_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates an 'Add Shader' node."""

    node_add_shader = create_node(
        group=group,
        bl_idname="ShaderNodeAddShader",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    return node_add_shader


def create_color_invert_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    factor: Optional[float] = 1.0,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates an 'Color Invert' node."""

    node_invert_color = create_node(
        group=group,
        bl_idname="ShaderNodeInvert",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    if factor is not None:
        node_invert_color.inputs[0].default_value = factor
    return node_invert_color


def create_combine_xyz_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    value_x: Optional[float] = 1.0,
    value_y: Optional[float] = 1.0,
    value_z: Optional[float] = 1.0,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates an 'Combine XYZ' node."""

    node_combine_xyz = create_node(
        group=group,
        bl_idname="ShaderNodeCombineXYZ",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    if value_x is not None:
        node_combine_xyz.inputs[0].default_value = value_x
    if value_y is not None:
        node_combine_xyz.inputs[1].default_value = value_y
    if value_z is not None:
        node_combine_xyz.inputs[2].default_value = value_z
    return node_combine_xyz


def create_displacement_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    midlevel: Optional[float] = 0.0,
    scale: Optional[float] = 0.0,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates a 'Displacement' node."""

    node_displacement = create_node(
        group=group,
        bl_idname="ShaderNodeDisplacement",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    if midlevel is not None:
        node_displacement.inputs[1].default_value = midlevel
    if scale is not None:
        node_displacement.inputs[2].default_value = scale
    return node_displacement


def create_fresnel_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    ior: Optional[float] = 1.150,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates a 'Fresnel' node."""

    node_fresnel = create_node(
        group=group,
        bl_idname="ShaderNodeFresnel",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    if ior is not None:
        node_fresnel.inputs[0].default_value = ior
    return node_fresnel


def create_group_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    node_tree: Optional[bpy.types.NodeTree] = None,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates a 'Group' node."""

    node_group = create_node(
        group=group,
        bl_idname="ShaderNodeGroup",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    if node_tree is not None:
        node_group.node_tree = node_tree
    else:
        node_tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
        node_group.node_tree = node_tree

        node_inputs = node_group.node_tree.nodes.new("NodeGroupInput")
        node_inputs.select = False
        node_outputs = node_group.node_tree.nodes.new("NodeGroupOutput")
        node_outputs.select = False
    return node_group


def create_mapping_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    scale: float = 1.0,
    name: Optional[str] = None,
    location: mathutils.Vector = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False,
) -> bpy.types.Node:
    """Creates a 'Mapping' node."""

    node_mapping = create_node(
        group=group,
        bl_idname="ShaderNodeMapping",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    node_mapping.inputs["Scale"].default_value = [scale] * 3
    return node_mapping


def create_math_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    operation: Optional[str] = "MULTIPLY",
    use_clamp: Optional[bool] = True,
    value1: Optional[float] = None,
    value2: Optional[float] = None,
    name: Optional[str] = None,
    location: mathutils.Vector = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False,
) -> bpy.types.Node:
    """Creates a 'Math' node."""

    node_math = create_node(
        group=group,
        bl_idname="ShaderNodeMath",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    if operation is not None:
        node_math.operation = operation
    if use_clamp is not None:
        node_math.use_clamp = use_clamp
    if value1 is not None:
        node_math.inputs[0].default_value = value1
    if value2 is not None:
        node_math.inputs[1].default_value = value2
    return node_math


def create_mix_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    data_type: Optional[str] = "RGBA",
    use_clamp: Optional[bool] = True,
    clamp_result: Optional[bool] = False,
    blend_type: Optional[str] = "MULTIPLY",
    blend_factor: Optional[float] = None,
    name: Optional[str] = None,
    location: mathutils.Vector = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates a 'Mix' (or 'MixRGB') node."""

    if bpy.app.version >= (3, 4):
        bl_idname = "ShaderNodeMix"
    else:
        bl_idname = "ShaderNodeMixRGB"
    node_mix = create_node(
        group=group,
        bl_idname=bl_idname,
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    if data_type is not None:
        if bpy.app.version >= (3, 4):
            node_mix.data_type = "RGBA"
    if use_clamp is not None:
        if bpy.app.version >= (3, 4):
            node_mix.clamp_factor = use_clamp
        else:
            node_mix.use_clamp = use_clamp
    if clamp_result is not None:
        if bpy.app.version >= (3, 4):
            node_mix.clamp_result = clamp_result
    if blend_type is not None:
        node_mix.blend_type = blend_type
    if blend_factor is not None:
        node_mix.inputs[0].default_value = blend_factor
    return node_mix


def create_mix_shader_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates a 'Mix Shader' node."""

    node_mix_shader = create_node(
        group=group,
        bl_idname="ShaderNodeMixShader",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    return node_mix_shader


def create_mosaic_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    scale: float = 1.0,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates a 'Poliigon Mosaic' node group."""

    node_group_mosaic = load_poliigon_node_group(
        "Mosaic_UV_Mapping")

    if name is None:
        name = node_group_mosaic.name

    node_mosaic = create_group_node(
        group=group,
        parent=parent,
        node_tree=node_group_mosaic,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    node_mosaic.inputs[1].default_value = scale
    return node_mosaic


def create_normal_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    space: Optional[str] = "TANGENT",
    strength: Optional[float] = None,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates a 'Normal' node."""

    node_normal = create_node(
        group=group,
        bl_idname="ShaderNodeNormalMap",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    if space is not None:
        node_normal.space = space
    if strength is not None:
        node_normal.inputs[0].default_value = strength
    return node_normal


def create_texture_coordinate_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates a 'Texture Coordinate' node."""

    node_tex_coord = create_node(
        group=group,
        bl_idname="ShaderNodeTexCoord",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    return node_tex_coord


def create_transparent_bsdf_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates a 'Transparent BSDF' node."""

    node_transparent_bsdf = create_node(
        group=group,
        bl_idname="ShaderNodeBsdfTransparent",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    return node_transparent_bsdf


def create_translucent_bsdf_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates a 'Translucent BSDF' node."""

    node_translucent_bsdf = create_node(
        group=group,
        bl_idname="ShaderNodeBsdfTranslucent",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    return node_translucent_bsdf


def create_value_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    value: Optional[float] = 0.0,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates a 'Value' node."""

    node_value = create_node(
        group=group,
        bl_idname="ShaderNodeValue",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    if value is not None:
        node_value.outputs[0].default_value = value
    return node_value


def create_vector_math_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    operation: str = "MULTIPLY",
    value1: Optional[mathutils.Vector] = None,
    value2: Optional[mathutils.Vector] = None,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates a 'Vector Math' node."""

    node_vector_math = create_node(
        group=group,
        bl_idname="ShaderNodeVectorMath",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    if operation is not None:
        node_vector_math.operation = operation
    if value1 is not None:
        node_vector_math.inputs[0].default_value = value1
    if value2 is not None:
        node_vector_math.inputs[1].default_value = value2
    return node_vector_math


def create_vector_rotate_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    angle_rad: Optional[mathutils.Vector] = None,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates a 'Vector Rotate' node."""

    node_vector_rotate = create_node(
        group=group,
        bl_idname="ShaderNodeVectorRotate",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    if angle_rad is not None:
        node_vector_rotate.inputs["Angle"].default_value = angle_rad
    return node_vector_rotate


def create_volume_absorption_node(
    group: bpy.types.Node,
    parent: Optional[bpy.types.Node] = None,
    *,
    density: Optional[float] = 100.0,
    name: Optional[str] = None,
    location: Optional[mathutils.Vector] = None,
    width: Optional[float] = None,
    height: Optional[float] = None,
    hide: bool = False
) -> bpy.types.Node:
    """Creates a 'Volume Absorption' node."""

    node_vol_abs = create_node(
        group=group,
        bl_idname="ShaderNodeVolumeAbsorption",
        parent=parent,
        name=name,
        location=location,
        width=width,
        height=height,
        hide=hide
    )
    if density is not None:
        node_vol_abs.inputs[1].default_value = density
    return node_vol_abs
