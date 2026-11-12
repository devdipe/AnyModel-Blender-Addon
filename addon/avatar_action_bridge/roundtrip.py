"""Editable, data-only AVANIM import. No embedded scripts are executed."""

import bpy
import json
import math
import re
import struct
import hashlib
import io
import wave
import zipfile
from contextlib import contextmanager
from pathlib import Path
from bpy.props import StringProperty, BoolProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Vector, Quaternion

META_KEY = 'avatar_bridge_import'
SETUP_KEY = 'avatar_bridge_previous_setup'
MAX_BYTES = 64 * 1024 * 1024


def _read_entry(archive, name, limit):
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise ValueError('Package entry too large: '+name)
    with archive.open(info) as stream:
        result = stream.read(limit+1)
    if len(result) > limit:
        raise ValueError('Package entry too large: '+name)
    return result


def _package(filepath):
    if Path(filepath).stat().st_size > 128*1024*1024:
        raise ValueError('Package exceeds 128 MiB.')
    with zipfile.ZipFile(filepath) as archive:
        manifest = json.loads(_read_entry(archive, 'manifest.json', 2*1024*1024))
        data = _read_entry(archive, 'pose.bin', MAX_BYTES)
        audio = _read_entry(archive, 'soundtrack.wav', MAX_BYTES) if manifest.get('audio') else None
    if manifest.get('version') != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', manifest.get('id', '')):
        raise ValueError('Unsupported AVANIM version or invalid action ID.')
    rate, count, duration = float(manifest['sampleRate']), int(manifest['frameCount']), float(manifest['duration'])
    if not math.isfinite(rate) or not 5 <= rate <= 60 or not 2 <= count <= 36002:
        raise ValueError('Invalid sample rate/frame count.')
    if not math.isfinite(duration) or not 0 < duration <= 600 or abs((count-1)/rate-duration) > .21:
        raise ValueError('Invalid action duration.')
    bones = manifest['bones']
    if len(bones) > 512 or len(data) != count*len(bones)*40:
        raise ValueError('Invalid pose data length.')
    names = [bone['name'] for bone in bones]
    if len(set(names)) != len(names):
        raise ValueError('Duplicate bone names.')
    for values in struct.iter_unpack('<10f', data):
        if not all(math.isfinite(v) for v in values) or not .000001 <= sum(v*v for v in values[3:7]) <= 100:
            raise ValueError('Invalid pose transform.')
        if not all(.00001 <= v <= 100 for v in values[7:]):
            raise ValueError('Zero/negative bone scale is not supported.')
    tracks = manifest['visibility']
    if len(tracks) > 256 or any(len(track) != count or any(type(v) is not bool for v in track) for track in tracks.values()):
        raise ValueError('Invalid visibility tracks.')
    if audio:
        with wave.open(io.BytesIO(audio), 'rb') as wav:
            if wav.getsampwidth() != 2 or wav.getnchannels() not in (1, 2) or wav.getcomptype() != 'NONE':
                raise ValueError('Imported soundtrack must be PCM16 WAV.')
            if wav.getframerate() <= 0 or wav.getnframes() > wav.getframerate()*601:
                raise ValueError('Invalid soundtrack length.')
    return manifest, data, audio


def _ordered_bones(arm):
    ordered, seen = [], set()
    def visit(bone):
        if bone.name in seen:
            return
        if bone.parent:
            visit(bone.parent)
        seen.add(bone.name)
        ordered.append(bone)
    for bone in arm.data.bones:
        visit(bone)
    return ordered


def _strips(scene):
    editor = scene.sequence_editor
    if not editor:
        return []
    strips = getattr(editor, 'strips_all', None)
    return list(strips if strips is not None else editor.sequences_all)


def _strip_collection(scene):
    editor = scene.sequence_editor_create()
    strips = getattr(editor, 'strips', None)
    return strips if strips is not None else editor.sequences


def _json(value):
    try:
        return json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}


def imported_info(scene, arm, scope):
    action = arm.animation_data.action if arm.animation_data else None
    if scope != 'EVENTS' and action:
        return _json(action.get(META_KEY))
    return _json(scene.avatar_action_import_metadata)


def export_identity(scene, arm, scope):
    """Switching imported Actions must not silently export the previous one's ID."""
    action = arm.animation_data.action if arm.animation_data and scope != 'EVENTS' else None
    key = action.get('avatar_action_id') if action else None
    if key:
        name = scene.avatar_action_name if scene.avatar_action_id == key else action.get('avatar_action_name', action.name)
        return key, name
    return scene.avatar_action_id, scene.avatar_action_name


@contextmanager
def export_audio_scope(scene, arm, scope, force_imported=False):
    """Solo the editable imported mix only for mixdown; always restore scene mutes."""
    if scene.avatar_action_audio_source != 'IMPORTED' and not force_imported:
        yield None
        return
    metadata = imported_info(scene, arm, scope)
    strip_name = metadata.get('audioStrip')
    if not strip_name or not any(strip.name == strip_name for strip in _strips(scene)):
        raise ValueError('Imported soundtrack not found. Choose Audio source: Scene mix.')
    restore = []
    try:
        for strip in _strips(scene):
            if strip.type == 'SOUND':
                restore.append((strip, 'mute', strip.mute))
                strip.mute = strip.name != strip_name
        for obj in scene.objects:
            if obj.type == 'SPEAKER':
                restore.append((obj.data, 'muted', obj.data.muted))
                obj.data.muted = True
        yield metadata
    finally:
        for owner, attr, value in restore:
            setattr(owner, attr, value)


def retained_cues(scene, metadata, start, end, fps, fallback):
    """Keep original source cue names/times when the imported mix is unshifted."""
    if not metadata:
        return fallback
    strip = next((s for s in _strips(scene) if s.name == metadata.get('audioStrip')), None)
    original_start = metadata.get('start')
    if strip is None or original_start is None or abs(strip.frame_start-original_start) > .001:
        return fallback
    for attr, value in metadata.get('audioState', {}).items():
        if abs(float(getattr(strip, attr, value))-value) > .001:
            return fallback
    result = []
    offset, duration = (original_start-start)/fps, (end-start+1)/fps
    for cue in metadata.get('audio', {}).get('cues', []):
        a, b = float(cue['start'])+offset, float(cue['end'])+offset
        if a < duration and b > 0:
            result.append(dict(cue, start=max(0, a), end=min(duration, b)))
    return result or fallback


def restore_setup(scene):
    """Restore only constraints/drivers/NLA/audio mutes, keeping editable Actions."""
    backup = _json(scene.get(SETUP_KEY))
    for entry in backup.get('shapes',[]):
        keys=bpy.data.shape_keys.get(entry['data'])
        if keys and keys.animation_data:
            keys.animation_data.use_nla=entry['useNla']
            for item in entry['drivers']:
                for driver in keys.animation_data.drivers:
                    if driver.data_path==item['path'] and driver.array_index==item['index']:driver.mute=item['mute']
    for entry in backup.get('objects', []):
        obj = bpy.data.objects.get(entry['name'])
        if obj and obj.animation_data:
            obj.animation_data.use_nla = entry['useNla']
    for entry in backup.get('constraints', []):
        obj = bpy.data.objects.get(entry['object'])
        owner = obj.pose.bones.get(entry['bone']) if obj and entry['bone'] and obj.type == 'ARMATURE' else obj
        constraint = owner.constraints.get(entry['name']) if owner else None
        if constraint:
            constraint.mute = entry['mute']
    for entry in backup.get('drivers', []):
        obj = bpy.data.objects.get(entry['object'])
        for curve in obj.animation_data.drivers if obj and obj.animation_data else []:
            if curve.data_path == entry['path'] and curve.array_index == entry['index']:
                curve.mute = entry['mute']
    strips = {strip.name: strip for strip in _strips(scene)}
    for entry in backup.get('audio', []):
        if entry['kind'] == 'strip' and entry['name'] in strips:
            strips[entry['name']].mute = entry['mute']
        elif entry['kind'] == 'speaker':
            obj = bpy.data.objects.get(entry['name'])
            if obj and obj.type == 'SPEAKER':
                obj.data.muted = entry['mute']
    if SETUP_KEY in scene:
        del scene[SETUP_KEY]


def _prepare_setup(scene, arm, meshes, mute_constraints):
    restore_setup(scene)
    backup = {'objects': [], 'constraints': [], 'drivers': [], 'audio': []}
    for obj in ([arm] if arm else [])+meshes:
        data = obj.animation_data_create()
        backup['objects'].append({'name': obj.name, 'useNla': data.use_nla})
        data.use_nla = False
        for curve in data.drivers:
            if (obj == arm and (curve.data_path.startswith('pose.bones[') or curve.data_path in {'location','rotation_euler','rotation_quaternion','scale'})) or (obj != arm and curve.data_path in {'avatar_visible','hide_render','hide_viewport'}):
                backup['drivers'].append({'object': obj.name, 'path': curve.data_path, 'index': curve.array_index, 'mute': curve.mute})
                curve.mute = True
    if mute_constraints and arm:
        for owner, bone in [(arm, ''), *[(pb, pb.name) for pb in arm.pose.bones]]:
            for constraint in owner.constraints:
                backup['constraints'].append({'object': arm.name, 'bone': bone, 'name': constraint.name, 'mute': constraint.mute})
                constraint.mute = True
    scene[SETUP_KEY] = json.dumps(backup)
    return backup


def _new_action(obj, name):
    data = obj.animation_data_create()
    if data.action:
        data.action.use_fake_user = True
    action = bpy.data.actions.new(name=name)
    action.use_fake_user = True
    data.action = action
    if hasattr(action, 'slots'):
        data.action_slot = action.slots.new(id_type='OBJECT', name=obj.name)
    return action


def import_action(context, filepath, mute_constraints=True, solo_audio=True):
    from . import armature_for, fcurves
    scene, arm = context.scene, armature_for(context)
    from .avatar_export import space_for,local_rests
    space=space_for(arm)
    manifest, data, audio = _package(filepath)
    count, rate = int(manifest['frameCount']), float(manifest['sampleRate'])
    bones = _ordered_bones(arm)
    aliases = {str(b.get('avatar_bone', b.name)): b for b in bones}
    if len(aliases) != len(bones):
        raise ValueError('Duplicate target bone aliases.')
    source_index = {}
    for index, source in enumerate(manifest['bones']):
        bone = aliases.get(source['name'])
        if bone is None:
            continue
        parent = str(bone.parent.get('avatar_bone', bone.parent.name)) if bone.parent else ''
        rest = local_rests(arm,[bone],space)[0]
        if source['parent'] != parent or len(source['rest']) != 12:
            raise ValueError('Rig hierarchy mismatch: '+bone.name)
        if any(not math.isfinite(v) or abs(v-rest[r][c]) > .03 for (r,c),v in zip(((r,c) for r in range(3) for c in range(4)),source['rest'])):
            raise ValueError('Rest pose mismatch: '+bone.name)
        source_index[bone.name] = index
    if manifest['bones'] and not source_index:
        raise ValueError('No matching avatar bones. Select the rig used to export this avatar.')
    mesh_map = {}
    for obj in scene.objects:
        if obj.type == 'MESH' and str(obj.get('avatar_mesh', obj.name)) in manifest['visibility']:
            key = str(obj.get('avatar_mesh', obj.name))
            if key in mesh_map:
                raise ValueError('Duplicate target mesh: '+key)
            mesh_map[key] = obj
    missing = set(manifest['visibility'])-mesh_map.keys()
    if missing:
        raise ValueError('Missing avatar meshes: '+', '.join(sorted(missing)))
    source = manifest.get('source', {})
    fps = float(source.get('fps', rate))
    if not math.isfinite(fps) or not 1 <= fps <= 240:
        raise ValueError('Unsupported source FPS.')
    start = int(source.get('startFrame', 1))
    end = int(source.get('endFrame', start+round(float(manifest['duration'])*fps)-1))
    if end < start or abs((end-start+1)/fps-float(manifest['duration'])) > .21:
        raise ValueError('Inconsistent source frame range.')
    key, name = manifest['id'], str(manifest['name'])
    if not name or len(name) > 100:
        raise ValueError('Invalid action name.')
    channel = max((strip.channel for strip in _strips(scene)), default=0)+1
    if audio and channel > 128:
        raise ValueError('No free VSE channel for the imported soundtrack.')
    for obj in [arm, *mesh_map.values()]:
        if obj.animation_data and obj.animation_data.use_tweak_mode:
            raise ValueError('Exit NLA tweak mode before importing: '+obj.name)
    # All package/rig validation above precedes changes to animation data.
    backup = _prepare_setup(scene, arm if manifest['bones'] else None, list(mesh_map.values()), mute_constraints)
    scene.render.fps = max(1, round(fps))
    scene.render.fps_base = scene.render.fps/fps
    scene.frame_start, scene.frame_end = start, end
    action = _new_action(arm, name) if manifest['bones'] else None
    rest_local = dict(zip((b.name for b in bones),local_rests(arm,bones,space)))
    previous_quat = {}
    if action:
        for sample_index in range(count):
            frame = min(end, start+sample_index*fps/rate)
            globals_ = {};native_globals={}
            for bone in bones:
                index = source_index.get(bone.name)
                delta = Matrix.Identity(4)
                if index is not None:
                    values = struct.unpack_from('<10f', data, (sample_index*len(manifest['bones'])+index)*40)
                    q = Quaternion((values[6], *values[3:6])).normalized()
                    delta = Matrix.LocRotScale(Vector(values[:3]), q, Vector(values[7:]))
                local = rest_local[bone.name] @ delta
                parent_pose = globals_.get(bone.parent.name) if bone.parent else None
                pose = parent_pose @ local if parent_pose is not None else local
                globals_[bone.name] = pose
                native_pose=pose
                native_parent=parent_pose
                if space:
                    scaled=pose.copy()
                    for r in range(3):
                        for c in range(3):scaled[r][c]*=space['unitScale']
                    native_pose=arm.matrix_world.inverted() @ Matrix(space['conversion']).inverted() @ scaled
                    native_parent=native_globals.get(bone.parent.name) if bone.parent else None
                    native_globals[bone.name]=native_pose
                kwargs = {'parent_matrix': native_parent, 'parent_matrix_local': bone.parent.matrix_local} if bone.parent else {}
                basis = bone.convert_local_to_pose(native_pose, bone.matrix_local, invert=True, **kwargs)
                location, quaternion, scale = basis.decompose()
                if bone.name in previous_quat and quaternion.dot(previous_quat[bone.name]) < 0:
                    quaternion.negate()
                previous_quat[bone.name] = quaternion.copy()
                pb = arm.pose.bones[bone.name]
                pb.rotation_mode = 'QUATERNION'
                pb.location, pb.rotation_quaternion, pb.scale = location, quaternion, scale
                for path in ('location', 'rotation_quaternion', 'scale'):
                    pb.keyframe_insert(data_path=path, frame=frame, group=bone.name)
        for curve in fcurves(action, getattr(arm.animation_data, 'action_slot', None)):
            for point in curve.keyframe_points:
                point.interpolation = 'LINEAR'
    for key_name, values in manifest['visibility'].items():
        obj = mesh_map[key_name]
        visibility_action = _new_action(obj, name+' / '+key_name)
        obj.avatar_track_visibility = True
        obj.hide_set(False)
        last = None
        for index, visible in enumerate(values):
            if index not in (0,count-1) and visible == last:
                continue
            frame = min(end, start+index*fps/rate)
            obj.avatar_visible = visible
            obj.hide_render = obj.hide_viewport = not visible
            for path in ('avatar_visible','hide_render','hide_viewport'):
                obj.keyframe_insert(data_path=path, frame=frame)
            last = visible
        for curve in fcurves(visibility_action, getattr(obj.animation_data, 'action_slot', None)):
            for point in curve.keyframe_points:
                point.interpolation = 'CONSTANT'
    from .morphs import import_tracks
    import_tracks(context,filepath,manifest,arm,start,fps)
    metadata = {field: manifest.get(field) for field in ('id','name','profile','mode','scope','loop','source')}
    metadata.update({'start': start, 'end': end, 'fps': fps, 'audio': manifest.get('audio') or {}})
    if audio:
        # Use a fixed, sanitized cache path, then pack the sound into the .blend.
        cache = Path(bpy.utils.user_resource('DATAFILES', path='avatar_action_bridge', create=True))/key
        cache.mkdir(parents=True, exist_ok=True)
        wav = cache/(hashlib.sha256(audio).hexdigest()+'.wav')
        wav.write_bytes(audio)
        collection = _strip_collection(scene)
        prefix = 'AVATAR::'+key+'::'
        for strip in list(collection):
            if strip.type == 'SOUND' and strip.name.startswith(prefix):
                collection.remove(strip)
        sound = collection.new_sound(prefix+name, str(wav), channel=channel, frame_start=start)
        sound.frame_final_end = min(sound.frame_final_end, end+1)
        sound.sound.pack()
        metadata['audioStrip'] = sound.name
        metadata['audioState'] = {attr: float(getattr(sound, attr, default)) for attr,default in (
            ('frame_start',start),('frame_offset_start',0),('frame_offset_end',0),
            ('sound_offset',0),('pitch',1),('speed_factor',1))}
        if solo_audio:
            for strip in _strips(scene):
                if strip.type == 'SOUND' and strip != sound:
                    backup['audio'].append({'kind':'strip','name':strip.name,'mute':strip.mute})
                    strip.mute = True
            for obj in scene.objects:
                if obj.type == 'SPEAKER':
                    backup['audio'].append({'kind':'speaker','name':obj.name,'mute':obj.data.muted})
                    obj.data.muted = True
        scene.avatar_action_audio_source = 'IMPORTED'
    else:
        scene.avatar_action_audio_source = 'SCENE'
    scene[SETUP_KEY] = json.dumps(backup)
    text = json.dumps(metadata, ensure_ascii=False)
    if action:
        action['avatar_action_id'], action['avatar_action_name'] = key, name
        action[META_KEY] = text
    scene.avatar_action_import_metadata = text
    scene.avatar_action_id, scene.avatar_action_name = key, name
    scene.avatar_action_profile = str(manifest['profile'])
    scene.avatar_action_mode = 'SLIDER' if manifest['mode'] == 'slider' else 'BUTTON'
    scene.avatar_action_scope = 'FACE' if manifest.get('scope')=='face' else 'FULL' if manifest['bones'] else 'EVENTS'
    scene.avatar_action_loop = bool(manifest['loop'])
    scene.avatar_action_audio = audio is not None
    scene.avatar_action_rate = round(rate)
    scene.avatar_action_range = 'AUTO'
    scene.frame_set(start)
    return manifest


class AVATAR_OT_import(bpy.types.Operator, ImportHelper):
    bl_idname = 'import_scene.avatar_action'
    bl_label = 'Import AnyModel Animation'
    bl_options = {'REGISTER', 'UNDO'}
    filename_ext = '.avanim'
    filter_glob: StringProperty(default='*.avanim', options={'HIDDEN'})
    mute_constraints: BoolProperty(name='Mute baked rig constraints', default=True,
        description='Avoid applying IK/constraints twice to already baked poses')
    solo_audio: BoolProperty(name='Solo imported soundtrack', default=True)
    def execute(self, context):
        try:
            manifest = import_action(context, self.filepath, self.mute_constraints, self.solo_audio)
            self.report({'INFO'}, 'Imported editable action: '+manifest['name'])
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class AVATAR_OT_restore_setup(bpy.types.Operator):
    bl_idname = 'avatar_action.restore_setup'
    bl_label = 'Restore Rig / Audio Setup'
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        restore_setup(context.scene)
        self.report({'INFO'}, 'Previous constraints, drivers, NLA and audio mutes restored; imported Actions kept.')
        return {'FINISHED'}
