bl_info = {
    'name': 'AnyModel Avatar Tools', 'author': 'Codex', 'version': (1, 0, 0),
    'blender': (4, 2, 0), 'location': 'View3D > Sidebar > AnyModel / File > Export',
    'description': 'Build Minecraft avatars and export editable animations, visibility and sound',
    'category': 'Import-Export',
}

import bpy
import math
import json
import struct
import zipfile
import tempfile
import os
import uuid
import time
from pathlib import Path
from bpy.props import StringProperty, BoolProperty, IntProperty, EnumProperty
from bpy_extras.io_utils import ExportHelper
from mathutils import Matrix


def armature_for(context):
    obj = context.active_object
    if obj and obj.get('anymodel_physics_helper'):
        parent=obj.parent
        while parent:
            if parent.type=='ARMATURE':return parent
            parent=parent.parent
    if obj and obj.type == 'ARMATURE':
        return obj
    if obj:
        arm = obj.find_armature() if obj.type == 'MESH' else None
        if arm:
            return arm
    arms = [o for o in context.scene.objects if o.type == 'ARMATURE' and not o.get('anymodel_preview')]
    if len(arms) == 1:
        return arms[0]
    raise ValueError('Select the avatar armature (or a mesh bound to it).')


def fcurves(action, slot=None):
    if not action:
        return
    if not getattr(action, 'is_action_layered', False):
        try:
            yield from action.fcurves
        except (AttributeError, RuntimeError):
            pass
    for layer in getattr(action, 'layers', []):
        for strip in layer.strips:
            for bag in getattr(strip, 'channelbags', []):
                if slot is not None and bag.slot_handle != slot.handle:
                    continue
                yield from bag.fcurves


def keyed_bounds(action, slot=None, paths=None):
    """Use authored keys, including repeated final values; never infer 'idle' poses."""
    start, end = None, None
    for curve in fcurves(action, slot):
        if curve.mute or (paths is not None and curve.data_path not in paths):
            continue
        for points in (curve.keyframe_points, curve.sampled_points):
            for point in points:
                frame = float(point.co[0])
                if math.isfinite(frame):
                    start = frame if start is None else min(start, frame)
                    end = frame if end is None else max(end, frame)
    if start is None:
        return None
    return start, end


def export_bounds(scene, arm, vis_objects, scope=None):
    """Resolve one timeline interval shared by poses, visibility and sound."""
    if scene.avatar_action_range == 'SCENE':
        return scene.frame_start, scene.frame_end, 'Scene range'
    scope = scope or scene.avatar_action_scope
    # Preserve the stored OVERLAY enum for older .blend files, but skeletal
    # actions now always own the complete evaluated rig.
    scope = scope if scope in {'EVENTS','FACE'} else 'FULL'
    data = arm.animation_data if scope != 'EVENTS' else None
    action = data.action if data else None
    bounds = keyed_bounds(action, getattr(data, 'action_slot', None))
    # An explicitly enabled Action range can intentionally include a hold
    # beyond the last key, or restrict a larger Action to a particular clip.
    if action and getattr(action, 'use_frame_range', False):
        bounds = float(action.frame_start), float(action.frame_end)
        source = 'Action manual range: '+action.name
    elif bounds is not None:
        source = 'Action keys: '+action.name
    else:
        intervals = []
        # With no active keyed Action, an NLA strip's scene-time bounds preserve
        # its placement, scale, repeats and deliberately extended strip duration.
        tracks = list(data.nla_tracks) if data and data.use_nla else []
        solo = any(track.is_solo for track in tracks)
        for track in tracks:
            if track.mute or (solo and not track.is_solo):
                continue
            for strip in track.strips:
                if strip.mute or strip.type != 'CLIP' or not strip.action:
                    continue
                if keyed_bounds(strip.action, getattr(strip, 'action_slot', None)) is not None:
                    intervals.append((float(strip.frame_start), float(strip.frame_end)))
        source = 'NLA strips'
        # Shape-only clips also have meaningful automatic bounds.
        if not intervals:
            from .morphs import animated_targets
            for descriptor, key, curve, complex_ in animated_targets(arm):
                if curve:
                    points=[p.co.x for p in curve.keyframe_points]
                    points += [p.co.x for p in curve.sampled_points]
                    if points:intervals.append((float(min(points)),float(max(points))))
            if intervals:source='Shape-key animation keys'
        if not intervals:
            paths = {'hide_render', 'hide_viewport', 'avatar_visible'}
            for obj in vis_objects:
                animation = obj.animation_data
                if animation:
                    interval = keyed_bounds(animation.action, getattr(animation, 'action_slot', None), paths)
                    if interval is not None:
                        intervals.append(interval)
            source = 'Visibility keys'
        if not intervals:
            return scene.frame_start, scene.frame_end, 'Scene range (no keyed Action)'
        bounds = min(a for a, _ in intervals), max(b for _, b in intervals)
    start, end = math.floor(bounds[0]), math.ceil(bounds[1])
    if end < start:
        raise ValueError('Invalid animation frame range.')
    return start, end, source


def animated_visibility(obj):
    data = obj.animation_data
    if not data:
        return False
    actions = [data.action]
    actions += [s.action for t in data.nla_tracks for s in t.strips]
    paths = {f.data_path for a in actions for f in fcurves(a)}
    paths |= {f.data_path for f in data.drivers}
    return bool(paths & {'hide_render', 'hide_viewport', 'avatar_visible'})


def sound_cues(scene, start, end, fps):
    cues = []
    editor = scene.sequence_editor
    strips = getattr(editor, 'strips_all', getattr(editor, 'sequences_all', [])) if editor else []
    for strip in strips:
        if strip.type != 'SOUND' or strip.mute:
            continue
        a, b = float(strip.frame_final_start), float(strip.frame_final_end)
        if a <= end and b > start:
            cues.append({'name': strip.name, 'start': (max(start, a)-start)/fps,
                         'end': (min(end+1, b)-start)/fps,
                         'source': Path(bpy.path.abspath(strip.sound.filepath)).name,
                         'volume': float(strip.volume)})
    for obj in scene.objects:
        if obj.type == 'SPEAKER' and obj.data.sound and not obj.data.muted:
            cues.append({'name': obj.name, 'start': 0, 'end': (end-start+1)/fps,
                         'source': Path(bpy.path.abspath(obj.data.sound.filepath)).name,
                         'speaker': True})
    return cues


def export_action(context, filepath, *, name=None, profile=None, mode=None, scope=None, audio=None, rate=None, force_scene=False):
    from .face_tools import stop_preview
    stop_preview()
    """Public entry point, also usable from background Blender and automated tests."""
    started=time.perf_counter()
    from . import physics_preview
    physics_preview.stop()
    scene = context.scene
    arm = armature_for(context)
    from .avatar_export import space_for, local_rests, engine_matrix
    space=space_for(arm)
    scope = scope or scene.avatar_action_scope
    scope = scope if scope in {'EVENTS','FACE'} else 'FULL'
    key, default_name = export_identity(scene, arm, scope)
    imported = imported_info(scene, arm, scope)
    switched = bool(imported and scene.avatar_action_id != key)
    name = name or default_name
    profile = profile or (space['profile'] if space else imported['profile'] if switched else scene.avatar_action_profile)
    mode = mode or (imported['mode'].upper() if switched else scene.avatar_action_mode)
    loop = bool(imported['loop']) if switched else scene.avatar_action_loop
    audio = (bool(imported.get('audio')) if switched else scene.avatar_action_audio) if audio is None else audio
    rate = rate or scene.avatar_action_rate
    vis_objects = [o for o in scene.objects if o.type == 'MESH' and (o.avatar_track_visibility or animated_visibility(o))]
    start, end, range_source = export_bounds(scene, arm, vis_objects, scope)
    fps = scene.render.fps / scene.render.fps_base
    duration = (end-start+1)/fps
    count = int(math.ceil(duration*rate))+1
    bones = list(arm.data.bones)
    if count < 2 or count*len(bones)*40 > 64*1024*1024:
        raise ValueError('Action exceeds 64 MiB of poses. Shorten the frame range or lower Sample FPS.')
    names = [str(b.get('avatar_bone', b.name)) for b in bones]
    if len(set(names)) != len(names):
        raise ValueError('Duplicate target bone names.')
    if space and set(space['bones'])!=set(b.name for b in bones):
        raise ValueError('Rig changed after avatar export. Build the AnyModel Avatar again first.')
    rest = local_rests(arm,bones,space)
    rest_inv = [m.inverted() for m in rest]
    tracks = {str(o.get('avatar_mesh', o.name)): [] for o in vis_objects}
    if len(tracks) != len(vis_objects):
        raise ValueError('Duplicate target mesh names.')
    selected = list(range(len(bones))) if scope == 'FULL' else [i for i,b in enumerate(bones) if b.name in {arm.anymodel_face.left,arm.anymodel_face.right}] if scope=='FACE' else []
    data = bytearray()
    from .fast_action import prepare
    sampler, sampling = prepare(arm,bones,space,rest_inv,vis_objects,scope) if not force_scene else (None,'Forced scene evaluation')
    from .morphs import ActionMorphs
    morphs=ActionMorphs(arm,count)
    if morphs.scene:sampler=None;sampling='Scene evaluation (driven/NLA blendshapes)'
    print('AnyModel export: '+sampling,flush=True)
    context.window_manager.progress_begin(0,count)
    old_frame, old_sub = scene.frame_current, scene.frame_subframe
    old_start, old_end = scene.frame_start, scene.frame_end
    try:
        if sampler is None:scene.frame_set(start)
        reference_world = arm.matrix_world.copy().inverted()
        for sample in range(count):
            frame = min(end, start + min(duration, sample/rate)*fps)
            if sampler is not None:
                values=sampler.sample(frame)
                for name,visible in sampler.visible(frame).items():tracks[name].append(visible)
            else:
                scene.frame_set(math.floor(frame), subframe=frame-math.floor(frame))
                depsgraph = context.evaluated_depsgraph_get()
                evaluated = arm.evaluated_get(depsgraph)
                object_delta = reference_world @ evaluated.matrix_world
                values = []
                for i, bone in enumerate(bones):
                    if space:
                        pose=engine_matrix(space,evaluated.matrix_world,evaluated.pose.bones[bone.name].matrix)
                        parent=engine_matrix(space,evaluated.matrix_world,evaluated.pose.bones[bone.parent.name].matrix) if bone.parent else None
                        local=parent.inverted() @ pose if parent else pose
                    else:
                        pose = evaluated.pose.bones[bone.name].matrix
                        parent = evaluated.pose.bones[bone.parent.name].matrix if bone.parent else None
                        local = parent.inverted() @ pose if parent else object_delta @ pose
                    delta = rest_inv[i] @ local
                    t, q, scale = delta.decompose()
                    q.normalize()
                    row = [*t, q.x, q.y, q.z, q.w, *scale]
                    if any(not math.isfinite(v) for v in row):
                        raise ValueError('Non-finite bone transform: '+bone.name)
                    if any(v < .00001 or v > 100 for v in scale):
                        raise ValueError('Zero/negative/extreme bone scale: '+bone.name+'. Use visibility tracks to hide meshes.')
                    values.append(row)
                for obj in vis_objects:
                    ev = obj.evaluated_get(depsgraph)
                    visible = bool(ev.avatar_visible) if obj.avatar_track_visibility else not (ev.hide_render or ev.hide_viewport)
                    tracks[str(obj.get('avatar_mesh', obj.name))].append(visible)
            morphs.sample(frame)
            for index in selected:
                row=values[index]
                if any(not math.isfinite(v) for v in row):raise ValueError('Non-finite bone transform: '+bones[index].name)
                if any(v<.00001 or v>100 for v in row[7:]):raise ValueError('Zero/negative/extreme bone scale: '+bones[index].name)
                data.extend(struct.pack('<10f',*row))
            if sample%30==0 or sample==count-1:context.window_manager.progress_update(sample+1)
            if sample%600==0 or sample==count-1:print(f'AnyModel export: {sample+1}/{count} samples',flush=True)
        pose_seconds=time.perf_counter()-started
        audio_info={'method':'No audio','seconds':0.0}
        with tempfile.TemporaryDirectory(prefix='avatar_action_') as temp:
            wav = Path(temp)/'soundtrack.wav'
            cues = []
            if audio:
                with export_audio_scope(scene, arm, scope, switched and bool(imported.get('audioStrip'))) as imported_audio:
                    cues = retained_cues(scene, imported_audio, start, end, fps, sound_cues(scene, start, end, fps))
                    if cues:
                        from .audio_export import mixdown
                        result,audio_info=mixdown(context,scene,wav,start,end)
                        if 'FINISHED' not in result or not wav.is_file():
                            raise ValueError('Blender sound mixdown failed.')
                        if wav.stat().st_size > 64*1024*1024:
                            raise ValueError('Soundtrack exceeds 64 MiB. Shorten the action.')
            if not key:
                key = uuid.uuid4().hex
                scene.avatar_action_id = key
            manifest = {'version': 1, 'id': key, 'name': name, 'profile': profile,
                'mode': mode.lower(), 'scope': scope.lower(), 'loop': loop, 'origin': 'floor',
                'duration': duration, 'sampleRate': rate, 'frameCount': count,
                'bones': [{'name': names[i], 'parent': str(bones[i].parent.get('avatar_bone', bones[i].parent.name)) if bones[i].parent else '',
                           'rest': [float(rest[i][r][c]) for r in range(3) for c in range(4)]} for i in selected],
                'morphs':morphs.manifest(),
                'visibility': tracks, 'audio': {'file': 'soundtrack.wav', 'cues': cues} if wav.is_file() else None,
                'source': {'application': 'Blender', 'version': bpy.app.version_string, 'startFrame': start, 'endFrame': end, 'fps': fps,
                           'rangeMode': scene.avatar_action_range, 'rangeSource': range_source, 'poseSampling': sampling,
                           'audioSampling':audio_info['method'],'timings':{'poses':pose_seconds,'audio':audio_info['seconds']}}}
            for descriptor,index in zip(manifest['bones'],selected):
                bone=bones[index]
                if bone.name not in {arm.anymodel_face.left,arm.anymodel_face.right}:continue
                side='left' if bone.name==arm.anymodel_face.left else 'right'
                descriptor['eyeSide']=side
                descriptor['faceNeutral']=list(getattr(arm.anymodel_face,side+'_neutral'))
                from .avatar_export import affine
                descriptor['faceRestGlobal']=affine(engine_matrix(space,arm.matrix_world,bone.matrix_local) if space else bone.matrix_local)
            target = Path(filepath).with_suffix('.avanim')
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix('.avanim.tmp')
            packing_started=time.perf_counter()
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as pack:
                pack.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
                pack.writestr('pose.bin', data)
                if morphs.rows:pack.writestr('morph.bin',morphs.data)
                if wav.is_file():
                    pack.write(wav, 'soundtrack.wav')
            os.replace(temporary, target)
            print(f"AnyModel export timings: poses={pose_seconds:.2f}s audio={audio_info['seconds']:.2f}s packing={time.perf_counter()-packing_started:.2f}s",flush=True)
            current_action = arm.animation_data.action if arm.animation_data and scope != 'EVENTS' else None
            if current_action:
                current_action['avatar_action_id'], current_action['avatar_action_name'] = key, name
                if imported:
                    imported.update({'id':key, 'name':name, 'profile':profile, 'mode':mode.lower(), 'scope':scope.lower(), 'loop':loop})
                    current_action['avatar_bridge_import'] = json.dumps(imported, ensure_ascii=False)
            scene.avatar_action_id, scene.avatar_action_name = key, name
        return manifest
    finally:
        context.window_manager.progress_end()
        scene.frame_start, scene.frame_end = old_start, old_end
        scene.frame_set(old_frame, subframe=old_sub)


class AVATAR_OT_export(bpy.types.Operator, ExportHelper):
    bl_idname = 'export_scene.avatar_action'
    bl_label = 'Export AnyModel Animation'
    filename_ext = '.avanim'
    filter_glob: StringProperty(default='*.avanim', options={'HIDDEN'})
    def execute(self, context):
        try:
            result = export_action(context, self.filepath)
            self.report({'INFO'}, f"Exported {result['name']}: {result['frameCount']} samples, {len(result['bones'])} bones")
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class AVATAR_OT_new_id(bpy.types.Operator):
    bl_idname = 'avatar_action.new_id'
    bl_label = 'New Action ID'
    def execute(self, context):
        context.scene.avatar_action_id = uuid.uuid4().hex
        try:
            arm = armature_for(context)
            action = arm.animation_data.action if arm.animation_data else None
            if action and action.get('avatar_action_id'):
                action['avatar_action_id'] = context.scene.avatar_action_id
                action['avatar_action_name'] = context.scene.avatar_action_name
        except ValueError:
            pass
        return {'FINISHED'}


class AVATAR_PT_export(bpy.types.Panel):
    bl_label = 'AnyModel Actions'
    bl_idname = 'AVATAR_PT_action_bridge'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AnyModel'
    def draw(self, context):
        layout, scene = self.layout, context.scene
        layout.prop(scene, 'avatar_action_name')
        layout.prop(scene, 'avatar_action_profile')
        layout.prop(scene, 'avatar_action_mode')
        layout.prop(scene, 'avatar_action_scope')
        layout.prop(scene, 'avatar_action_loop')
        layout.prop(scene, 'avatar_action_rate')
        layout.prop(scene, 'avatar_action_morph_drivers')
        layout.prop(scene, 'avatar_action_audio')
        if scene.avatar_action_audio:
            layout.prop(scene, 'avatar_action_audio_source')
        layout.prop(scene, 'avatar_action_range')
        try:
            arm = armature_for(context)
            vis_objects = [o for o in scene.objects if o.type == 'MESH' and (o.avatar_track_visibility or animated_visibility(o))]
            start, end, source = export_bounds(scene, arm, vis_objects)
            layout.label(text=f'Export frames {start}–{end}')
            layout.label(text=source)
        except ValueError as error:
            layout.label(text=str(error))
        layout.label(text='Animated Shape Keys are included.')
        layout.operator('export_scene.avatar_action')
        layout.operator('import_scene.avatar_action')
        layout.operator('avatar_action.new_id', icon='FILE_REFRESH')
        if scene.get('avatar_bridge_previous_setup'):
            layout.operator('avatar_action.restore_setup')
        obj = context.active_object
        if obj and obj.type == 'MESH':
            box = layout.box()
            box.label(text='Visibility track: '+obj.name)
            box.prop(obj, 'avatar_track_visibility')
            if obj.avatar_track_visibility:
                box.prop(obj, 'avatar_visible')
                box.label(text='Keyframe Visible to create on/off events.')
        layout.label(text='Retarget foreign actions onto the avatar rig first.')


from .roundtrip import (AVATAR_OT_import, AVATAR_OT_restore_setup,
    export_identity, imported_info, export_audio_scope, retained_cues)

CLASSES = (AVATAR_OT_export, AVATAR_OT_import, AVATAR_OT_restore_setup, AVATAR_OT_new_id, AVATAR_PT_export)
def menu_export(self, context):
    self.layout.operator(AVATAR_OT_export.bl_idname, text='AnyModel Animation (.avanim)')

def menu_import(self, context):
    self.layout.operator(AVATAR_OT_import.bl_idname, text='AnyModel Animation (.avanim)')

def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.avatar_action_name = StringProperty(name='Name', default='New action')
    bpy.types.Scene.avatar_action_profile = StringProperty(name='Avatar ID', default='nazuna')
    bpy.types.Scene.avatar_action_id = StringProperty(name='Action ID', default='')
    bpy.types.Scene.avatar_action_mode = EnumProperty(name='Control', items=[('BUTTON','Play button','Timed action'),('SLIDER','Float / radial slider','Scrub forward and backward')], default='BUTTON')
    bpy.types.Scene.avatar_action_scope = EnumProperty(name='Pose', items=[('FULL','Full body','Bake every bone, including unkeyed bones','NONE',0),('FACE','Face only (eyes + shapes)','Animate the assigned eyes and blendshapes while gameplay body motion continues','NONE',3),('OVERLAY','Full body (legacy setting)','Older overlay projects now export the complete body','NONE',1),('EVENTS','Objects and sound only','No bone pose; useful for clothing parameters','NONE',2)], default='FULL')
    bpy.types.Scene.avatar_action_loop = BoolProperty(name='Loop playback', default=False)
    bpy.types.Scene.avatar_action_morph_drivers = BoolProperty(name='Include driven shape keys',description='Sample shape-key drivers too; requires evaluated scene sampling. Ordinary keyed facial curves always export directly',default=False)
    bpy.types.Scene.avatar_action_audio = BoolProperty(name='Include scene sound', default=True)
    bpy.types.Scene.avatar_action_audio_source = EnumProperty(name='Audio source', items=[
        ('SCENE','Scene mix','Mix all unmuted scene audio'),
        ('IMPORTED','Imported soundtrack','Mix only the editable soundtrack imported with this Action'),
    ], default='SCENE')
    bpy.types.Scene.avatar_action_import_metadata = StringProperty(default='', options={'HIDDEN'})
    bpy.types.Scene.avatar_action_rate = IntProperty(name='Sample FPS', min=5, max=60, default=30)
    bpy.types.Scene.avatar_action_range = EnumProperty(name='Frame range', items=[
        ('AUTO', 'Automatic (Action keys)', 'Use active Action keys or its explicit manual range; fall back to NLA, visibility keys, then scene'),
        ('SCENE', 'Scene range', 'Use the scene frame range explicitly, including holds or external procedural/IK animation'),
    ], default='AUTO')
    bpy.types.Object.avatar_track_visibility = BoolProperty(name='Export visibility', default=False)
    bpy.types.Object.avatar_visible = BoolProperty(name='Visible', default=True, options={'ANIMATABLE'})
    bpy.types.TOPBAR_MT_file_export.append(menu_export)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    from . import avatar_export
    avatar_export.register()
    from . import bone_physics
    bone_physics.register()
    from . import mmd_converter
    mmd_converter.register()
    from . import face_tools
    face_tools.register()

def unregister():
    from . import face_tools
    face_tools.unregister()
    from . import mmd_converter
    mmd_converter.unregister()
    from . import bone_physics
    bone_physics.unregister()
    from . import avatar_export
    avatar_export.unregister()
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    for name in ('avatar_action_name','avatar_action_profile','avatar_action_id','avatar_action_mode','avatar_action_scope','avatar_action_loop','avatar_action_morph_drivers','avatar_action_audio','avatar_action_audio_source','avatar_action_import_metadata','avatar_action_rate','avatar_action_range'):
        delattr(bpy.types.Scene, name)
    for name in ('avatar_track_visibility','avatar_visible'):
        delattr(bpy.types.Object, name)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)

if __name__ == '__main__':
    register()
