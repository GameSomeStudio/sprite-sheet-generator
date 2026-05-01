bl_info = {
    "name": "Sprite Sheet Generator",
    "author": "GameSome-mabaci",
    "version": (2, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Sprite Sheet",
    "description": "Create a Sprite Sheet with all Animation Actions in your model.",
    "category": "Render",
}

import bpy
import os
import re
import tempfile
import shutil
import numpy as np
from mathutils import Vector, Matrix, Euler
import math
import json
from bpy.props import (
    StringProperty,
    IntProperty,
    BoolProperty,
    EnumProperty,
    FloatProperty,
    PointerProperty,
    CollectionProperty,
)
from bpy.types import (
    Panel,
    Operator,
    PropertyGroup,
    UIList,
)

# ==================== UTILITY FUNCTIONS ====================

def sanitize_filename(name):
    """
    Remove invalid characters from filename.
    Windows invalid chars: < > : " / \ | ? *
    """
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'_+', '_', name)
    name = name.strip('_ ')
    if name.startswith('.'):
        name = '_' + name[1:]
    if not name:
        name = "unnamed"
    return name

def get_safe_temp_path(temp_dir, animation_name, angle, frame_index):
    """Create safe temporary file path."""
    safe_name = sanitize_filename(animation_name)
    filename = f"{safe_name}_a{angle}_f{frame_index:04d}.png"
    return os.path.join(temp_dir, filename)

class SpriteSheetCore:
    """Core functionality for sprite sheet generation."""
    
    @staticmethod
    def get_object_bounding_box_in_frame(obj, frame, camera=None):
        """
        Calculate object's bounding box in camera view for a specific frame.
        Returns NDC coordinates: (min_x, min_y, max_x, max_y)
        """
        if not obj:
            return (-1, -1, 1, 1)
        
        bpy.context.scene.frame_set(frame)
        
        # Find mesh objects
        if obj.type == 'ARMATURE':
            meshes = [child for child in obj.children if child.type == 'MESH']
            if not meshes:
                return (-1, -1, 1, 1)
            target_objects = meshes
        else:
            target_objects = [obj]
        
        if camera is None:
            camera = bpy.context.scene.camera
        
        if not camera:
            return (-1, -1, 1, 1)
        
        scene = bpy.context.scene
        render_width = scene.render.resolution_x
        render_height = scene.render.resolution_y
        
        modelview_matrix = camera.matrix_world.inverted()
        projection_matrix = camera.calc_matrix_camera(
            bpy.context.evaluated_depsgraph_get(),
            x=render_width,
            y=render_height
        )
        
        min_x, min_y = float('inf'), float('inf')
        max_x, max_y = float('-inf'), float('-inf')
        
        for mesh_obj in target_objects:
            bbox_corners = [mesh_obj.matrix_world @ Vector(corner) 
                          for corner in mesh_obj.bound_box]
            
            for corner in bbox_corners:
                view_corner = modelview_matrix @ corner
                proj_corner = projection_matrix @ view_corner.to_4d()
                
                if proj_corner.w != 0:
                    ndc = proj_corner.to_3d() / proj_corner.w
                    min_x = min(min_x, ndc.x)
                    max_x = max(max_x, ndc.x)
                    min_y = min(min_y, ndc.y)
                    max_y = max(max_y, ndc.y)
        
        if min_x == float('inf'):
            return (-1, -1, 1, 1)
        
        return (
            max(-1, min(min_x, 1)),
            max(-1, min(min_y, 1)),
            max(-1, min(max_x, 1)),
            max(-1, min(max_y, 1))
        )
    
    @staticmethod
    def calculate_animation_bounds(obj, action, frame_start, frame_end, sample_count=20):
        """
        Calculate maximum bounding box across an animation.
        Returns: (width_ndc, height_ndc, center_offset_y)
        """
        if not obj or not action:
            return (1.0, 1.0, 0.0)
        
        original_action = obj.animation_data.action if obj.animation_data else None
        if not obj.animation_data:
            obj.animation_data_create()
        obj.animation_data.action = action
        
        camera = bpy.context.scene.camera
        
        max_width = 0
        max_height = 0
        min_center_y = float('inf')
        max_center_y = float('-inf')
        
        frames_to_check = SpriteSheetCore.interpolate_frames(
            frame_start, frame_end, min(sample_count, frame_end - frame_start + 1)
        )
        
        for frame in frames_to_check:
            bbox = SpriteSheetCore.get_object_bounding_box_in_frame(obj, frame, camera)
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
            center_y = (bbox[1] + bbox[3]) / 2
            
            max_width = max(max_width, width)
            max_height = max(max_height, height)
            min_center_y = min(min_center_y, center_y)
            max_center_y = max(max_center_y, center_y)
        
        if obj.animation_data:
            obj.animation_data.action = original_action
        
        center_offset_y = (max_center_y + min_center_y) / 2
        
        return (max_width, max_height, center_offset_y)
    
    @staticmethod
    def get_all_actions_from_object(obj):
        """Get all actions associated with an object."""
        actions = []
        
        if not obj:
            return actions
        
        if obj.animation_data and obj.animation_data.nla_tracks:
            for track in obj.animation_data.nla_tracks:
                for strip in track.strips:
                    if strip.action and strip.action not in actions:
                        actions.append(strip.action)
        
        if obj.animation_data and obj.animation_data.action:
            if obj.animation_data.action not in actions:
                actions.append(obj.animation_data.action)
        
        for action in bpy.data.actions:
            if action.users > 0 and action not in actions:
                actions.append(action)
        
        return actions
    
    @staticmethod
    def get_action_frame_range(action):
        """Get the actual frame range of an action."""
        if not action or not action.fcurves:
            return (1, 24)
        
        frame_start = float('inf')
        frame_end = float('-inf')
        
        for fcurve in action.fcurves:
            if fcurve.keyframe_points:
                points = fcurve.keyframe_points
                frame_start = min(frame_start, points[0].co[0])
                frame_end = max(frame_end, points[-1].co[0])
        
        if frame_start == float('inf'):
            return (1, 24)
        
        return (int(frame_start), int(frame_end))
    
    @staticmethod
    def interpolate_frames(frame_start, frame_end, target_count):
        """Smart frame interpolation to reduce frame count."""
        if target_count <= 0:
            return []
        
        total_frames = frame_end - frame_start + 1
        
        if target_count >= total_frames:
            return list(range(frame_start, frame_end + 1))
        
        selected = []
        step = (total_frames - 1) / max(target_count - 1, 1)
        
        for i in range(target_count):
            frame = frame_start + round(i * step)
            selected.append(frame)
        
        return selected
    
    @staticmethod
    def apply_action_to_object(obj, action, frame_start, frame_end):
        """Apply an action to an object."""
        if not obj or not action:
            return False
        
        if not obj.animation_data:
            obj.animation_data_create()
        
        obj.animation_data.action = action
        
        scene = bpy.context.scene
        scene.frame_start = frame_start
        scene.frame_end = frame_end
        
        return True
    
    @staticmethod
    def rotate_armature(obj, angle_degrees):
        """
        Rotate armature around its Z-axis (vertical axis).
        Positive angle = counter-clockwise from top view.
        """
        if not obj or obj.type != 'ARMATURE':
            return
        
        # Convert to radians
        angle_rad = math.radians(angle_degrees)
        
        # Apply rotation to armature's Z-axis
        obj.rotation_euler.z += angle_rad
    
    @staticmethod
    def get_camera_base_params(camera, target_obj):
        """Get camera distance and height parameters."""
        if not camera or not target_obj:
            return 5.0, 0.0
        
        cam_pos = camera.matrix_world.translation.copy()
        obj_pos = target_obj.matrix_world.translation.copy()
        
        rel = cam_pos - obj_pos
        horizontal_dist = math.sqrt(rel.x**2 + rel.y**2)
        height = rel.z
        
        return horizontal_dist, height
    
    @staticmethod
    def position_camera_for_frame(camera, target_obj, vertical_offset=0.0):
        """
        Position camera to center on object with vertical offset.
        Camera stays in place, only adjusts aim point.
        """
        if not camera or not target_obj:
            return False
        
        obj_world_pos = target_obj.matrix_world.translation.copy()
        
        # Calculate aim point with vertical offset
        aim_point = obj_world_pos.copy()
        aim_point.z += vertical_offset
        
        # Keep camera position, just update rotation to aim at target
        direction = aim_point - camera.location
        rot_quat = direction.to_track_quat('-Z', 'Y')
        camera.rotation_euler = rot_quat.to_euler()
        
        return True
    
    @staticmethod
    def render_frame(scene, frame, output_path):
        """Render a single frame."""
        scene.frame_set(frame)
        scene.render.filepath = output_path
        bpy.ops.render.render(write_still=True)

# ==================== PROPERTY GROUPS ====================

class AnimationItem(PropertyGroup):
    """Animation item for sprite sheet."""
    name: StringProperty(name="Name", default="anim")
    action_name: StringProperty(name="Action", default="")
    frame_start: IntProperty(name="Start", default=1, min=1)
    frame_end: IntProperty(name="End", default=24, min=1)
    target_frames: IntProperty(name="Frames", default=10, min=1, max=100)
    enabled: BoolProperty(name="Active", default=True)
    auto_scale: BoolProperty(
        name="Auto Scale",
        description="Automatically calculate sprite size for this animation",
        default=True
    )
    calculated_scale: FloatProperty(name="Calculated Scale", default=1.0, min=0.5, max=2.0)

class SpriteSheetSettings(PropertyGroup):
    """Main sprite sheet settings."""
    
    active_animation_index: IntProperty(default=-1, min=-1)
    
    output_path: StringProperty(
        name="Output Folder",
        default="//",
        subtype='DIR_PATH'
    )
    
    output_filename: StringProperty(
        name="Filename",
        default="sprite_sheet"
    )
    
    # Base sprite size for normal animations
    base_sprite_width: IntProperty(name="Base Width", default=64, min=16, max=512)
    base_sprite_height: IntProperty(name="Base Height", default=64, min=16, max=512)
    
    # Dynamic sizing settings
    use_dynamic_sizing: BoolProperty(
        name="Dynamic Sizing",
        description="Auto-adjust sprite size for animations like jump",
        default=True
    )
    
    max_sprite_width: IntProperty(name="Max Width", default=128, min=64, max=512)
    max_sprite_height: IntProperty(name="Max Height", default=128, min=64, max=512)
    
    padding_percent: FloatProperty(
        name="Padding %",
        description="Padding around sprites",
        default=10.0,
        min=0.0,
        max=50.0
    )
    
    columns: IntProperty(name="Columns", default=10, min=1, max=50)
    
    # View angles (armature rotation)
    use_front: BoolProperty(name="Front (0°)", default=True)
    use_right: BoolProperty(name="Right (90°)", default=True)
    use_back: BoolProperty(name="Back (180°)", default=True)
    use_left: BoolProperty(name="Left (270°)", default=True)
    
    flip_y: BoolProperty(name="Flip Y Axis", default=True)
    
    track_origin: BoolProperty(
        name="Track Origin",
        description="Camera follows object origin point each frame",
        default=True
    )
    
    include_all_actions: BoolProperty(
        name="Include All Actions",
        default=True
    )
    
    animations: CollectionProperty(type=AnimationItem)

# ============= OPERATORS =============

class SPRITESHEET_OT_analyze_animation(Operator):
    """Analyze selected animation's bounding box"""
    bl_idname = "spritesheet.analyze_animation"
    bl_label = "Analyze"
    bl_description = "Analyze the selected animation's dimensions"
    
    @classmethod
    def poll(cls, context):
        settings = context.scene.spritesheet_settings
        return settings.active_animation_index >= 0 and context.active_object
    
    def execute(self, context):
        settings = context.scene.spritesheet_settings
        obj = context.active_object
        
        anim = settings.animations[settings.active_animation_index]
        action = bpy.data.actions.get(anim.action_name)
        
        if not action:
            self.report({'ERROR'}, "Action not found!")
            return {'CANCELLED'}
        
        width_ndc, height_ndc, center_offset = SpriteSheetCore.calculate_animation_bounds(
            obj, action, anim.frame_start, anim.frame_end
        )
        
        max_scale = max(width_ndc, height_ndc)
        anim.calculated_scale = max_scale
        
        padding_factor = 1 + (settings.padding_percent / 100)
        suggested_width = int(settings.base_sprite_width * max_scale * padding_factor)
        suggested_height = int(settings.base_sprite_height * max_scale * padding_factor)
        
        suggested_width = min(suggested_width, settings.max_sprite_width)
        suggested_height = min(suggested_height, settings.max_sprite_height)
        
        self.report({'INFO'}, 
            f"Analysis: Scale={max_scale:.2f}x | "
            f"Suggested: {suggested_width}x{suggested_height}px | "
            f"Offset: {center_offset:.2f}")
        
        print(f"\nAnimation Analysis: {anim.name}")
        print(f"  NDC Width: {width_ndc:.3f}")
        print(f"  NDC Height: {height_ndc:.3f}")
        print(f"  Scale Factor: {max_scale:.3f}")
        print(f"  Suggested Size: {suggested_width}x{suggested_height}px")
        print(f"  Vertical Offset: {center_offset:.3f}")
        
        return {'FINISHED'}

class SPRITESHEET_OT_detect_actions(Operator):
    """Scan and add all actions to the list"""
    bl_idname = "spritesheet.detect_actions"
    bl_label = "Detect Actions"
    bl_description = "Scan all actions and add to list (preserves existing list)"
    
    clear_existing: BoolProperty(default=False, options={'HIDDEN'})
    
    def execute(self, context):
        settings = context.scene.spritesheet_settings
        obj = context.active_object
        
        if not obj:
            self.report({'ERROR'}, "Please select an armature or object")
            return {'CANCELLED'}
        
        if settings.include_all_actions:
            all_actions = [a for a in bpy.data.actions if a.users > 0]
        else:
            all_actions = SpriteSheetCore.get_all_actions_from_object(obj)
        
        if not all_actions:
            self.report({'WARNING'}, "No actions found!")
            return {'CANCELLED'}
        
        if self.clear_existing:
            settings.animations.clear()
        
        existing_names = {item.action_name for item in settings.animations}
        
        added_count = 0
        for action in all_actions:
            if action.name not in existing_names:
                item = settings.animations.add()
                item.name = sanitize_filename(action.name)
                item.action_name = action.name
                frame_start, frame_end = SpriteSheetCore.get_action_frame_range(action)
                item.frame_start = frame_start
                item.frame_end = frame_end
                item.target_frames = min(10, frame_end - frame_start + 1)
                existing_names.add(action.name)
                added_count += 1
        
        if added_count > 0:
            self.report({'INFO'}, f"Added {added_count} new actions")
        else:
            self.report({'INFO'}, "All actions already in list")
        
        return {'FINISHED'}

class SPRITESHEET_OT_clear_list(Operator):
    """Clear the animation list"""
    bl_idname = "spritesheet.clear_list"
    bl_label = "Clear List"
    bl_description = "Remove all animations from the list"
    
    def execute(self, context):
        settings = context.scene.spritesheet_settings
        settings.animations.clear()
        settings.active_animation_index = -1
        self.report({'INFO'}, "List cleared")
        return {'FINISHED'}

class SPRITESHEET_OT_add_animation(Operator):
    """Add a new animation manually"""
    bl_idname = "spritesheet.add_animation"
    bl_label = "Add Animation"
    
    def execute(self, context):
        settings = context.scene.spritesheet_settings
        item = settings.animations.add()
        item.name = f"anim_{len(settings.animations)}"
        item.frame_start = 1
        item.frame_end = 24
        item.target_frames = 10
        settings.active_animation_index = len(settings.animations) - 1
        return {'FINISHED'}

class SPRITESHEET_OT_remove_animation(Operator):
    """Remove selected animation"""
    bl_idname = "spritesheet.remove_animation"
    bl_label = "Remove"
    
    @classmethod
    def poll(cls, context):
        settings = context.scene.spritesheet_settings
        return settings.active_animation_index >= 0
    
    def execute(self, context):
        settings = context.scene.spritesheet_settings
        idx = settings.active_animation_index
        
        if 0 <= idx < len(settings.animations):
            settings.animations.remove(idx)
            if idx >= len(settings.animations):
                settings.active_animation_index = len(settings.animations) - 1
        
        return {'FINISHED'}

class SPRITESHEET_OT_preview_animation(Operator):
    """Preview selected animation in viewport"""
    bl_idname = "spritesheet.preview_animation"
    bl_label = "Preview"
    
    @classmethod
    def poll(cls, context):
        settings = context.scene.spritesheet_settings
        return settings.active_animation_index >= 0 and context.active_object
    
    def execute(self, context):
        settings = context.scene.spritesheet_settings
        obj = context.active_object
        
        anim = settings.animations[settings.active_animation_index]
        action = bpy.data.actions.get(anim.action_name)
        
        if action:
            SpriteSheetCore.apply_action_to_object(obj, action, anim.frame_start, anim.frame_end)
            context.scene.frame_set(anim.frame_start)
            self.report({'INFO'}, f"'{anim.name}' loaded")
        else:
            self.report({'WARNING'}, f"'{anim.action_name}' not found")
        
        return {'FINISHED'}

class SPRITESHEET_OT_generate(Operator):
    """Generate sprite sheet with armature rotation"""
    bl_idname = "spritesheet.generate"
    bl_label = "Generate Sprite Sheet"
    bl_description = "Generate sprite sheet by rotating the armature for different angles"
    
    @classmethod
    def poll(cls, context):
        settings = context.scene.spritesheet_settings
        return len(settings.animations) > 0 and context.active_object
    
    def execute(self, context):
        settings = context.scene.spritesheet_settings
        obj = context.active_object
        scene = context.scene
        camera = scene.camera
        
        # Validations
        if not camera:
            self.report({'ERROR'}, "No camera in scene!")
            return {'CANCELLED'}
        
        if obj.type != 'ARMATURE':
            self.report({'ERROR'}, "Selected object must be an armature!")
            return {'CANCELLED'}
        
        active_animations = [a for a in settings.animations if a.enabled]
        if not active_animations:
            self.report({'ERROR'}, "Enable at least one animation!")
            return {'CANCELLED'}
        
        # Get view angles
        view_angles = []
        if settings.use_front: view_angles.append(0)
        if settings.use_right: view_angles.append(90)
        if settings.use_back: view_angles.append(180)
        if settings.use_left: view_angles.append(270)
        
        if not view_angles:
            view_angles = [0]
        
        angle_names = {0: "front", 90: "right", 180: "back", 270: "left"}
        
        print("\n" + "="*70)
        print(f"SPRITE SHEET GENERATOR v2.0 - ARMATURE ROTATION MODE")
        print("="*70)
        print(f"Active animations: {len(active_animations)}")
        print(f"View angles: {[f'{angle_names.get(a, a)} ({a}°)' for a in view_angles]}")
        print(f"Total rows: {len(active_animations) * len(view_angles)}")
        
        # PRE-ANALYZE ALL ANIMATIONS
        animation_analyses = {}
        max_sprite_width = settings.base_sprite_width
        max_sprite_height = settings.base_sprite_height
        
        if settings.use_dynamic_sizing:
            print("\nAnalyzing animations...")
            for anim in active_animations:
                if anim.auto_scale:
                    action = bpy.data.actions.get(anim.action_name)
                    if action:
                        width_ndc, height_ndc, center_offset = SpriteSheetCore.calculate_animation_bounds(
                            obj, action, anim.frame_start, anim.frame_end
                        )
                        
                        scale = max(width_ndc, height_ndc)
                        anim.calculated_scale = scale
                        
                        padding_factor = 1 + (settings.padding_percent / 100)
                        sprite_w = int(settings.base_sprite_width * scale * padding_factor)
                        sprite_h = int(settings.base_sprite_height * scale * padding_factor)
                        
                        sprite_w = min(sprite_w, settings.max_sprite_width)
                        sprite_h = min(sprite_h, settings.max_sprite_height)
                        
                        animation_analyses[anim.name] = {
                            'scale': scale,
                            'sprite_width': sprite_w,
                            'sprite_height': sprite_h,
                            'center_offset': center_offset
                        }
                        
                        max_sprite_width = max(max_sprite_width, sprite_w)
                        max_sprite_height = max(max_sprite_height, sprite_h)
                        
                        print(f"  {anim.name}: {scale:.2f}x -> {sprite_w}x{sprite_h}px")
        
        print(f"\nUnified sprite size: {max_sprite_width}x{max_sprite_height}px")
        
        # SAVE ORIGINAL STATE
        original_frame = scene.frame_current
        original_action = obj.animation_data.action if obj.animation_data else None
        original_rotation = obj.rotation_euler.copy()
        original_location = obj.location.copy()
        
        orig_res_x = scene.render.resolution_x
        orig_res_y = scene.render.resolution_y
        orig_percentage = scene.render.resolution_percentage
        
        # Setup render
        scene.render.resolution_x = max_sprite_width
        scene.render.resolution_y = max_sprite_height
        scene.render.resolution_percentage = 100
        scene.render.film_transparent = True
        scene.render.image_settings.file_format = 'PNG'
        scene.render.image_settings.color_mode = 'RGBA'
        
        temp_dir = tempfile.mkdtemp(prefix="spritesheet_")
        all_frames = []
        
        try:
            # PROCESS EACH ANIMATION
            for anim_idx, anim in enumerate(active_animations):
                print(f"\n{'='*50}")
                print(f"Animation {anim_idx+1}/{len(active_animations)}: {anim.name}")
                
                action = bpy.data.actions.get(anim.action_name)
                if not action:
                    print(f"  WARNING: '{anim.action_name}' not found, skipping!")
                    continue
                
                # Get sprite size for this animation
                if settings.use_dynamic_sizing and anim.name in animation_analyses:
                    analysis = animation_analyses[anim.name]
                    sprite_w = analysis['sprite_width']
                    sprite_h = analysis['sprite_height']
                    center_offset = analysis['center_offset']
                else:
                    sprite_w = max_sprite_width
                    sprite_h = max_sprite_height
                    center_offset = 0.0
                
                # Apply action
                SpriteSheetCore.apply_action_to_object(
                    obj, action, anim.frame_start, anim.frame_end
                )
                
                # Select frames
                frame_list = SpriteSheetCore.interpolate_frames(
                    anim.frame_start, anim.frame_end, anim.target_frames
                )
                
                print(f"  Frames: {len(frame_list)} [{anim.frame_start}-{anim.frame_end}]")
                
                # RENDER EACH ANGLE
                for angle_idx, angle in enumerate(view_angles):
                    angle_name = angle_names.get(angle, str(angle))
                    full_name = f"{anim.name}_{angle_name}"
                    
                    print(f"\n  [{angle_idx+1}/{len(view_angles)}] {angle_name} ({angle}°)")
                    
                    # ROTATE ARMATURE
                    obj.rotation_euler = original_rotation.copy()
                    SpriteSheetCore.rotate_armature(obj, angle)
                    
                    # Update scene to apply rotation
                    bpy.context.view_layer.update()
                    
                    frame_paths = []
                    
                    # RENDER EACH FRAME
                    for i, frame in enumerate(frame_list):
                        scene.frame_set(frame)
                        
                        # Track origin with camera
                        if settings.track_origin:
                            SpriteSheetCore.position_camera_for_frame(
                                camera, obj, center_offset
                            )
                        
                        temp_path = get_safe_temp_path(temp_dir, full_name, angle, i)
                        scene.render.resolution_x = sprite_w
                        scene.render.resolution_y = sprite_h
                        SpriteSheetCore.render_frame(scene, frame, temp_path)
                        frame_paths.append(temp_path)
                        
                        if (i + 1) % 5 == 0 or i == 0:
                            print(f"    Frame {i+1}/{len(frame_list)} rendered")
                    
                    all_frames.append((full_name, frame_paths, sprite_w, sprite_h))
                    print(f"  ✓ {len(frame_paths)} frames rendered")
                    
                    # RESET ARMATURE ROTATION
                    obj.rotation_euler = original_rotation.copy()
                    bpy.context.view_layer.update()
            
            # CREATE FINAL SPRITE SHEET
            if all_frames:
                self.create_final_sprite_sheet(all_frames, settings)
            else:
                self.report({'ERROR'}, "No frames were rendered!")
                
        except Exception as e:
            self.report({'ERROR'}, f"Error: {str(e)}")
            import traceback
            traceback.print_exc()
            
        finally:
            # RESTORE EVERYTHING
            print(f"\nRestoring original state...")
            
            if obj.animation_data:
                obj.animation_data.action = original_action
            
            obj.rotation_euler = original_rotation
            obj.location = original_location
            bpy.context.view_layer.update()
            
            scene.frame_set(original_frame)
            scene.render.resolution_x = orig_res_x
            scene.render.resolution_y = orig_res_y
            scene.render.resolution_percentage = orig_percentage
            
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
                print("Temporary files cleaned up")
            
            print(f"✓ Complete!")
        
        return {'FINISHED'}
    
    def create_final_sprite_sheet(self, all_frames, settings):
        """Combine all frames into final sprite sheet with variable sizes."""
        
        # Find maximum sprite dimensions
        max_w = max(frame_data[2] for frame_data in all_frames)
        max_h = max(frame_data[3] for frame_data in all_frames)
        
        # Ensure minimum size
        max_w = max(max_w, 1)
        max_h = max(max_h, 1)
        
        total_rows = len(all_frames)
        sheet_width = settings.columns * max_w
        sheet_height = total_rows * max_h
        
        print(f"\n{'='*50}")
        print(f"CREATING SPRITE SHEET")
        print(f"  Max sprite: {max_w}x{max_h}px")
        print(f"  Sheet: {sheet_width}x{sheet_height}px")
        print(f"  Rows: {total_rows}")
        
        # Create empty sheet (transparent background)
        sheet_pixels = np.zeros((sheet_height, sheet_width, 4), dtype=np.float32)
        
        for row, (anim_name, frame_paths, sprite_w, sprite_h) in enumerate(all_frames):
            placed = 0
            
            # Ensure valid dimensions
            actual_w = max(1, sprite_w)
            actual_h = max(1, sprite_h)
            
            for col, frame_path in enumerate(frame_paths):
                if col >= settings.columns:
                    break
                
                # Load and process frame
                img = bpy.data.images.load(frame_path)
                pixels = np.array(img.pixels)
                
                # Validate array size
                expected_size = actual_h * actual_w * 4
                if len(pixels) != expected_size:
                    print(f"  WARNING: Size mismatch for {anim_name} frame {col}")
                    print(f"    Expected: {expected_size}, Got: {len(pixels)}")
                    # Try to reshape with actual loaded dimensions
                    actual_pixels = len(pixels) // 4
                    actual_h = int(math.sqrt(actual_pixels * actual_h / actual_w))
                    actual_w = actual_pixels // actual_h
                    if actual_h <= 0 or actual_w <= 0:
                        actual_h = 1
                        actual_w = actual_pixels
                
                pixels = pixels.reshape((actual_h, actual_w, 4))
                
                # Flip Y if needed
                if settings.flip_y:
                    pixels = np.flipud(pixels)
                
                # Center sprite in max-sized cell
                y_offset = (max_h - actual_h) // 2
                x_offset = (max_w - actual_w) // 2
                
                y_start = row * max_h + y_offset
                y_end = y_start + actual_h
                x_start = col * max_w + x_offset
                x_end = x_start + actual_w
                
                # Ensure we don't exceed sheet boundaries
                y_end = min(y_end, sheet_height)
                x_end = min(x_end, sheet_width)
                
                # Adjust pixels if boundaries were clipped
                if y_end - y_start < actual_h:
                    pixels = pixels[:y_end - y_start, :, :]
                if x_end - x_start < actual_w:
                    pixels = pixels[:, :x_end - x_start, :]
                
                sheet_pixels[y_start:y_end, x_start:x_end] = pixels
                bpy.data.images.remove(img)
                placed += 1
            
            print(f"  Row {row}: {anim_name} ({placed} frames, {actual_w}x{actual_h}px)")
        
        # Save sprite sheet
        output_path = os.path.join(
            bpy.path.abspath(settings.output_path),
            f"{settings.output_filename}.png"
        )
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        sheet_img = bpy.data.images.new(
            name="SpriteSheet",
            width=sheet_width,
            height=sheet_height,
            alpha=True,
            float_buffer=True
        )
        
        sheet_img.pixels = sheet_pixels.flatten()
        sheet_img.filepath_raw = output_path
        sheet_img.file_format = 'PNG'
        sheet_img.save()
        bpy.data.images.remove(sheet_img)
        
        print(f"\n✓ Sprite sheet saved: {output_path}")
        print(f"  Total: {total_rows} rows, {sheet_width}x{sheet_height}px")
        
        # Create metadata file
        self.save_metadata(output_path, all_frames, max_w, max_h, settings)
    
    def save_metadata(self, output_path, all_frames, max_w, max_h, settings):
        """Save metadata JSON file alongside sprite sheet."""
        metadata_path = output_path.replace('.png', '_metadata.json')
        
        metadata = {
            'sprite_sheet': os.path.basename(output_path),
            'columns': settings.columns,
            'rows': len(all_frames),
            'max_sprite_width': max_w,
            'max_sprite_height': max_h,
            'animations': []
        }
        
        for row, (anim_name, frame_paths, sprite_w, sprite_h) in enumerate(all_frames):
            metadata['animations'].append({
                'name': anim_name,
                'row': row,
                'frame_count': len(frame_paths),
                'sprite_width': max(1, sprite_w),
                'sprite_height': max(1, sprite_h)
            })
        
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        print(f"✓ Metadata saved: {metadata_path}")

# ==================== UI LIST ====================

class SPRITESHEET_UL_animations(UIList):
    """Animation list UI."""
    
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "enabled", text="")
            row.prop(item, "name", text="", emboss=False)
            
            col = row.column()
            col.scale_x = 0.4
            col.label(text=f"[{item.frame_start}-{item.frame_end}]")
            
            row.prop(item, "target_frames", text="")
            
            if item.auto_scale and item.calculated_scale > 1.0:
                row.label(text=f"×{item.calculated_scale:.1f}", icon='FULLSCREEN_ENTER')
        
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text=item.name)

# ==================== PANEL ====================

class VIEW3D_PT_sprite_sheet(Panel):
    """Main sprite sheet panel."""
    bl_label = "Sprite Sheet Generator v2.0"
    bl_idname = "VIEW3D_PT_sprite_sheet"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Sprite Sheet"
    
    def draw(self, context):
        layout = self.layout
        settings = context.scene.spritesheet_settings
        
        # Object info
        box = layout.box()
        box.label(text="Selected Object:", icon='OBJECT_DATA')
        obj = context.active_object
        if obj:
            icon = 'ARMATURE_DATA' if obj.type == 'ARMATURE' else 'OBJECT_DATA'
            box.label(text=f"  {obj.name} ({obj.type})", icon=icon)
            if obj.type != 'ARMATURE':
                box.label(text="  Warning: Armature required!", icon='ERROR')
        else:
            box.label(text="  No object selected!", icon='ERROR')
        
        layout.separator()
        
        # Output settings
        box = layout.box()
        box.label(text="Output:", icon='EXPORT')
        box.prop(settings, "output_path")
        box.prop(settings, "output_filename")
        
        layout.separator()
        
        # Sprite size settings
        box = layout.box()
        box.label(text="Sprite Size:", icon='MESH_GRID')
        
        row = box.row(align=True)
        row.prop(settings, "base_sprite_width", text="Base W")
        row.prop(settings, "base_sprite_height", text="Base H")
        
        box.prop(settings, "use_dynamic_sizing", icon='AUTO')
        
        if settings.use_dynamic_sizing:
            row = box.row(align=True)
            row.prop(settings, "max_sprite_width", text="Max W")
            row.prop(settings, "max_sprite_height", text="Max H")
            box.prop(settings, "padding_percent", text="Padding %")
        
        box.prop(settings, "columns")
        box.prop(settings, "flip_y")
        
        layout.separator()
        
        # View angles (armature rotation)
        box = layout.box()
        box.label(text="View Angles (Armature Rotation):", icon='ORIENTATION_VIEW')
        
        row = box.row(align=True)
        row.prop(settings, "use_front", toggle=True, text="Front")
        row.prop(settings, "use_right", toggle=True, text="Right")
        row.prop(settings, "use_back", toggle=True, text="Back")
        row.prop(settings, "use_left", toggle=True, text="Left")
        
        box.prop(settings, "track_origin", icon='PIVOT_ACTIVE')
        
        layout.separator()
        
        # Animation list
        box = layout.box()
        box.label(text="Animations:", icon='ANIM')
        
        row = box.row(align=True)
        row.operator("spritesheet.detect_actions", text="Detect", icon='VIEWZOOM').clear_existing = False
        row.operator("spritesheet.add_animation", text="", icon='ADD')
        row.operator("spritesheet.clear_list", text="", icon='TRASH')
        
        row = box.row()
        row.template_list(
            "SPRITESHEET_UL_animations",
            "",
            settings,
            "animations",
            settings,
            "active_animation_index",
            rows=5
        )
        
        col = row.column(align=True)
        col.operator("spritesheet.remove_animation", text="", icon='REMOVE')
        col.operator("spritesheet.preview_animation", text="", icon='PLAY')
        col.operator("spritesheet.analyze_animation", text="", icon='VIEWZOOM')
        
        total = len(settings.animations)
        active = len([a for a in settings.animations if a.enabled])
        box.label(text=f"Active: {active}/{total} animations")
        
        layout.separator()
        
        # Generate button
        row = layout.row()
        row.scale_y = 2.0
        row.operator("spritesheet.generate", icon='RENDER_STILL')

# ==================== REGISTRATION ====================

classes = [
    AnimationItem,
    SpriteSheetSettings,
    SPRITESHEET_OT_analyze_animation,
    SPRITESHEET_OT_detect_actions,
    SPRITESHEET_OT_clear_list,
    SPRITESHEET_OT_add_animation,
    SPRITESHEET_OT_remove_animation,
    SPRITESHEET_OT_preview_animation,
    SPRITESHEET_OT_generate,
    SPRITESHEET_UL_animations,
    VIEW3D_PT_sprite_sheet,
]

def register():
    print("Sprite Sheet Generator v2.0 - Armature Rotation Mode")
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.spritesheet_settings = PointerProperty(type=SpriteSheetSettings)
    print("✓ Registered successfully!")

def unregister():
    print("Unregistering Sprite Sheet Generator...")
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.spritesheet_settings
    print("✓ Unregistered successfully!")

if __name__ == "__main__":
    register()