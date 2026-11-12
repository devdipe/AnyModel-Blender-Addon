"""Retarget evaluated MMD/humanoid motion to an editable humanoid Action.

No source rig, animation, mesh, rest pose or avatar export space is rewritten.
VMD import is delegated to the user's MMD Tools installation, not approximated.
"""
import bpy
import json
import math
import uuid
from array import array
from pathlib import Path
from mathutils import Matrix, Vector, Quaternion
from bpy.props import (StringProperty, BoolProperty, FloatProperty, IntProperty,
                       PointerProperty, CollectionProperty)
from bpy_extras.io_utils import ImportHelper
from . import avatar_export

REPORT_KEY = 'anymodel_retarget_report'


def japanese_aliases(role):
    centre = {'Hips': ['下半身', '腰'], 'Spine': ['上半身'],
              'Chest': ['上半身2', '上半身２', '上半身'],
              'UpperChest': ['上半身3', '上半身３', '上半身2', '上半身２', '上半身'],
              'Neck': ['首'], 'Head': ['頭']}
    if role in centre:
        return centre[role]
    side = '左' if role.startswith('Left') else '右'
    part = role[4:] if role.startswith('Left') else role[5:]
    words = {'Shoulder': '肩', 'UpperArm': '腕', 'LowerArm': 'ひじ',
             'Hand': '手首', 'UpperLeg': '足', 'LowerLeg': 'ひざ',
             'Foot': '足首', 'Toes': 'つま先'}
    if part in words:
        return [side+words[part]]
    for finger, word in [('Thumb', '親指'), ('Index', '人指'), ('Middle', '中指'),
                         ('Ring', '薬指'), ('Little', '小指')]:
        prefix = 'Hand'+finger
        if part.startswith(prefix):
            n = int(part[-1])-(1 if finger == 'Thumb' else 0)
            return [side+word+str(n), side+word+'０１２３'[n],
                    *([side+'人差指'+str(n), side+'人差指'+'０１２３'[n]] if finger == 'Index' else [])]
    return []


def detect_mapping(arm, mmd=False):
    result = {}
    if not arm or arm.type != 'ARMATURE':
        return result
    names = {}
    for bone in arm.pose.bones:
        names[bone.name] = bone.name
        prop = getattr(bone, 'mmd_bone', None)
        if prop:
            for field in ('name_j', 'name_e'):
                value = getattr(prop, field, '')
                if value:
                    names.setdefault(value, bone.name)
    for role in avatar_export.all_roles():
        if mmd:
            for name in japanese_aliases(role):
                if name in names:
                    result[role] = names[name]
                    break
        if role not in result:
            candidates = [b.name for b in arm.data.bones
                          if str(b.get('avatar_bone', b.name)) == role]
            if not candidates:
                candidates = list(dict.fromkeys(actual for name, actual in names.items()
                    if avatar_export.normalized(name) in avatar_export.aliases(role)))
            if len(candidates) == 1:
                result[role] = candidates[0]
    # Some PMX rigs omit the thumb metacarpal (0). Keep its fallback fixed;
    # do not apply the same animated joint to two consecutive target joints.
    for side in ('Left', 'Right'):
        if mmd and side+'HandThumb1' not in result:
            result.pop(side+'HandThumb1', None)
    return result


def mappings(scene, source, target):
    src, dst = detect_mapping(source, True), avatar_export.mapping_for(scene, target)
    if scene.get('anymodel_map_source') == source.name and scene.get('anymodel_map_target') == target.name:
        for item in scene.anymodel_retarget_map:
            if item.source in source.data.bones and item.target in target.data.bones:
                src[item.role], dst[item.role] = item.source, item.target
            else:
                src.pop(item.role,None);dst.pop(item.role,None)
    return {role: (src[role], dst[role]) for role in src.keys() & dst.keys()}


def _body_frame(rest, pairs, index):
    def point(role):
        return rest[pairs[role][index]].translation
    x = point('RightUpperArm')-point('LeftUpperArm')
    up = point('Head')-point('Hips')
    if x.length < 1e-6 or up.length < 1e-6:
        raise ValueError('Degenerate humanoid rest pose: shoulders/head/hips overlap.')
    x.normalize()
    up -= x*up.dot(x)
    if up.length < 1e-6:
        raise ValueError('Cannot infer humanoid up axis from the rest pose.')
    up.normalize()
    return Matrix((x, up, x.cross(up))).transposed()


def _leg_length(rest, pairs, index):
    lengths = []
    for side in ('Left', 'Right'):
        points = [rest[pairs[side+p][index]].translation for p in ('UpperLeg','LowerLeg','Foot')]
        lengths.append((points[0]-points[1]).length+(points[1]-points[2]).length)
    return sum(lengths)/2


def retarget(context, source, target, *, start=None, end=None, step=1,
             name='MMD → Humanoid', root_motion=True):
    """Bake a new Action. Sampling happens before modifying the target setup."""
    from . import fcurves
    from .curve_reduce import retained_indices,tolerance_for
    from .roundtrip import _new_action, _prepare_setup, restore_setup
    scene = context.scene
    if not source or not target or source.type != 'ARMATURE' or target.type != 'ARMATURE' or source == target:
        raise ValueError('Choose two different armatures: animated source and humanoid avatar.')
    if source.mode == 'EDIT' or target.mode == 'EDIT':
        raise ValueError('Leave Edit Mode before converting an animation.')
    pairs = mappings(scene, source, target)
    missing = [role for role in avatar_export.REQUIRED if role not in pairs]
    if missing:
        raise ValueError('Map these source/target bones first: '+', '.join(missing))
    target_names = [pair[1] for pair in pairs.values()]
    if len(set(target_names)) != len(target_names):
        raise ValueError('Each humanoid role must use a different target bone.')
    start = scene.frame_start if start is None else int(start)
    end = scene.frame_end if end is None else int(end)
    step = max(1, int(step))
    if end <= start or end-start > 36000:
        raise ValueError('Choose a range of 2–36001 frames.')
    sample_frames = list(range(start, end+1, step))
    if sample_frames[-1] != end:
        sample_frames.append(end)
    bones = avatar_export.ordered_bones(target)
    old_frame, old_sub = scene.frame_current, scene.frame_subframe
    try:
        scene.frame_set(start)
        source_world, target_world = source.matrix_world.copy(), target.matrix_world.copy()
    finally:
        scene.frame_set(old_frame,subframe=old_sub)
    inverse_target = target_world.inverted_safe()
    sr = {b.name: source_world@b.matrix_local for b in source.data.bones}
    tr = {b.name: target_world@b.matrix_local for b in bones}
    alignment = _body_frame(tr, pairs, 1)@_body_frame(sr, pairs, 0).transposed()
    align_q = alignment.to_quaternion()
    inverse_align_q = align_q.inverted()
    source_length, target_length = _leg_length(sr, pairs, 0), _leg_length(tr, pairs, 1)
    if source_length < 1e-6 or target_length < 1e-6:
        raise ValueError('Humanoid legs have zero length.')
    ratio = target_length/source_length
    source_rot = {name: matrix.to_quaternion() for name, matrix in sr.items()}
    target_rot = {name: matrix.to_quaternion() for name, matrix in tr.items()}
    children = {'UpperArm':'LowerArm', 'LowerArm':'Hand', 'UpperLeg':'LowerLeg',
                'LowerLeg':'Foot', 'Foot':'Toes', 'Hand':'HandMiddle1'}
    calibration = {}
    for side in ('Left', 'Right'):
        for parent, child in children.items():
            role, next_role = side+parent, side+child
            if role not in pairs or next_role not in pairs:
                continue
            s, t = pairs[role]; sn, tn = pairs[next_role]
            a, b = tr[tn].translation-tr[t].translation, alignment@(sr[sn].translation-sr[s].translation)
            if a.length > 1e-6 and b.length > 1e-6:
                calibration[t] = a.normalized().rotation_difference(b.normalized())
    mapped = {target_name: source_name for source_name, target_name in pairs.values()}
    hips_src, hips_dst = pairs['Hips']
    rests = {b.name: b.parent.matrix_local.inverted_safe()@b.matrix_local
             if b.parent else b.matrix_local.copy() for b in bones}
    rows, previous = {b.name:array('f') for b in bones}, {}
    root_samples = []
    context.window_manager.progress_begin(0, len(sample_frames))
    try:
        for i, frame in enumerate(sample_frames):
            scene.frame_set(frame)
            evaluated = source.evaluated_get(context.evaluated_depsgraph_get())
            sp = {name:evaluated.matrix_world@evaluated.pose.bones[name].matrix for name in set(mapped.values())}
            globals_ = {}
            for bone in bones:
                pose = globals_[bone.parent.name]@rests[bone.name] if bone.parent else tr[bone.name].copy()
                if bone.name in mapped:
                    name_src = mapped[bone.name]
                    delta = align_q@sp[name_src].to_quaternion()@source_rot[name_src].inverted()@inverse_align_q
                    rotation = delta@calibration.get(bone.name, Quaternion())@target_rot[bone.name]
                    position = pose.translation.copy()
                    if bone.name == hips_dst and root_motion:
                        position = tr[hips_dst].translation+alignment@(sp[hips_src].translation-sr[hips_src].translation)*ratio
                    pose = Matrix.LocRotScale(position, rotation, tr[bone.name].to_scale())
                globals_[bone.name] = pose
                native = inverse_target@pose
                parent = inverse_target@globals_[bone.parent.name] if bone.parent else None
                kw = {'parent_matrix':parent,'parent_matrix_local':bone.parent.matrix_local} if bone.parent else {}
                basis = bone.convert_local_to_pose(native, bone.matrix_local, invert=True, **kw)
                location, rotation, scale = basis.decompose()
                rotation.normalize()
                if bone.name in previous and rotation.dot(previous[bone.name]) < 0:
                    rotation.negate()
                previous[bone.name] = rotation.copy()
                values = [*location, *rotation, *scale]
                if not all(math.isfinite(v) for v in values):
                    raise ValueError(f'Non-finite pose at frame {frame}: {bone.name}')
                rows[bone.name].extend(values)
            root_samples.append(list(globals_[hips_dst].translation))
            context.window_manager.progress_update(i+1)
            if i % 600 == 0 or i == len(sample_frames)-1:
                print(f'AnyModel retarget: {i+1}/{len(sample_frames)} frames',flush=True)
    finally:
        scene.frame_set(old_frame, subframe=old_sub)
        context.window_manager.progress_end()
    old_action = target.animation_data.action if target.animation_data else None
    old_world = target.matrix_world.copy()
    old_slot = target.animation_data.action_slot if target.animation_data and hasattr(target.animation_data,'action_slot') else None
    old_basis = {pb.name:(pb.matrix_basis.copy(),pb.rotation_mode) for pb in target.pose.bones}
    action = None
    try:
        _prepare_setup(scene, target, [], True)
        action = _new_action(target, name)
        target.matrix_world = target_world
        for bone in bones:
            pb, data = target.pose.bones[bone.name], rows[bone.name]
            pb.rotation_mode = 'QUATERNION'
            # Explicit identity keys are necessary: unkeyed bones can retain the
            # previously active Action's pose, even with constraints muted.
            for path, offset, channels in [('location',0,3),('rotation_quaternion',3,4),('scale',7,3)]:
                setattr(pb, path, list(data[offset:offset+channels]))
                pb.keyframe_insert(data_path=path, frame=start, group=bone.name)
                curves = {fc.array_index:fc for fc in fcurves(action,getattr(target.animation_data,'action_slot',None))
                          if fc.data_path == pb.path_from_id(path)}
                for channel in range(channels):
                    values = data[offset+channel::10]
                    indices = retained_indices(sample_frames,values,tolerance_for('.'+path,max(abs(v) for v in target_world.to_scale())))
                    coords = array('f')
                    for n in indices:
                        coords.extend([sample_frames[n], values[n]])
                    fc = curves[channel]
                    fc.keyframe_points.clear(); fc.keyframe_points.add(len(coords)//2)
                    fc.keyframe_points.foreach_set('co',coords)
                    for key in fc.keyframe_points:
                        key.interpolation = 'LINEAR'
                    fc.update()
        action.use_frame_range = True
        action.frame_start, action.frame_end = start,end
        profile = (avatar_export.space_for(target) or {}).get('profile', scene.avatar_action_profile)
        key = uuid.uuid4().hex
        action['avatar_action_id'], action['avatar_action_name'] = key,name
        scene.avatar_action_id, scene.avatar_action_name = key,name
        scene.avatar_action_profile, scene.avatar_action_scope, scene.avatar_action_range = profile,'FULL','AUTO'
        scene.avatar_action_mode = 'BUTTON'
        scene.avatar_action_audio_source = 'SCENE'
        scene.avatar_action_rate = max(5,min(60,round(scene.render.fps/scene.render.fps_base)))
        report = dict(source=source.name,target=target.name,action=action.name,previousAction=old_action.name if old_action else None,
                      start=start,end=end,samples=len(sample_frames),scaleRatio=ratio,rootMotion=root_motion,
                      rootMin=[min(p[a] for p in root_samples) for a in range(3)],
                      rootMax=[max(p[a] for p in root_samples) for a in range(3)],
                      mapping={role:dict(source=s,target=t) for role,(s,t) in pairs.items()})
        action[REPORT_KEY] = json.dumps(report,ensure_ascii=False)
        scene[REPORT_KEY] = action[REPORT_KEY]
        scene.frame_set(start)
        return action,report
    except Exception:
        restore_setup(scene)
        if target.animation_data:
            target.animation_data.action = old_action
            if old_action and old_slot:
                target.animation_data.action_slot = old_slot
        for name_pb,(basis,mode) in old_basis.items():
            target.pose.bones[name_pb].rotation_mode = mode
            target.pose.bones[name_pb].matrix_basis = basis
        target.matrix_world = old_world
        if action:
            bpy.data.actions.remove(action)
        scene.frame_set(old_frame,subframe=old_sub)
        raise


def _armature(self,obj):
    return obj.type == 'ARMATURE' and not obj.get('anymodel_preview')


def _mmd_operator(name):
    """Support both official MMD Tools and CATS' bundled MMD Tools."""
    for namespace in ('mmd_tools','mmd_tools_local'):
        op=getattr(getattr(bpy.ops,namespace),name)
        try:
            op.get_rna_type()
            return op
        except (AttributeError,RuntimeError,KeyError):
            pass
    raise RuntimeError('Enable MMD Tools or CATS in Blender Preferences → Add-ons.')


def prepare_reference_torso(context,arm):
    """Complete a mesh-free imported MMD reference, before loading its VMD."""
    names={getattr(p.mmd_bone,'name_j',''):p.name for p in arm.pose.bones}
    if '上半身2' in names or '上半身２' in names or '上半身' not in names:
        return False
    if any(o.type=='MESH' and o.find_armature()==arm for o in context.scene.objects):
        raise ValueError('Automatic torso completion only supports a mesh-free reference rig.')
    context.view_layer.objects.active=arm;arm.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        upper=arm.data.edit_bones[names['上半身']]
        children=list(upper.children);tail=upper.tail.copy();roll=upper.roll
        bone=arm.data.edit_bones.new('上半身2')
        bone.head=upper.head.lerp(tail,.5);bone.tail=tail;bone.roll=roll;bone.parent=upper
        new_name=bone.name
        for child in children:child.parent=bone
        upper.tail=bone.head
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
    arm.pose.bones[new_name].mmd_bone.name_j='上半身2'
    return True


class ANYMODEL_PG_retarget_bone(bpy.types.PropertyGroup):
    role: StringProperty()
    source: StringProperty(name='MMD bone')
    target: StringProperty(name='Avatar bone')


class ANYMODEL_OT_retarget_map(bpy.types.Operator):
    bl_idname = 'anymodel.retarget_map'
    bl_label = 'Auto-map MMD → Humanoid'
    bl_options = {'REGISTER','UNDO'}
    def execute(self,context):
        scene=context.scene;s=scene.anymodel_mmd_source;t=scene.anymodel_mmd_target
        if not s or not t or s==t:
            self.report({'ERROR'},'Choose two different armatures.');return {'CANCELLED'}
        src,dst=detect_mapping(s,True),avatar_export.mapping_for(scene,t)
        scene.anymodel_retarget_map.clear()
        scene['anymodel_map_source'],scene['anymodel_map_target']=s.name,t.name
        for role in avatar_export.all_roles():
            item=scene.anymodel_retarget_map.add();item.role=role
            item.source=src.get(role,'');item.target=dst.get(role,'')
        missing=[r for r in avatar_export.REQUIRED if r not in src or r not in dst]
        self.report({'WARNING'} if missing else {'INFO'},'Map missing bones: '+', '.join(missing) if missing else 'Required humanoid bones mapped. Optional fingers can be edited below.')
        return {'FINISHED'}


class ANYMODEL_OT_retarget(bpy.types.Operator):
    bl_idname='anymodel.retarget_mmd'
    bl_label='Convert to Humanoid Action'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        scene=context.scene
        try:
            source,target=scene.anymodel_mmd_source,scene.anymodel_mmd_target
            action,report=retarget(context,source,target,name=scene.anymodel_mmd_name,
                step=scene.anymodel_mmd_step,root_motion=scene.anymodel_mmd_root)
            if context.object and context.object.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.select_all(action='DESELECT');target.select_set(True)
            context.view_layer.objects.active=target
            self.report({'INFO'},f"Created {action.name}: {report['samples']} samples. Preview, then Export AnyModel Animation.")
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'},str(error));return {'CANCELLED'}


class ANYMODEL_OT_import_mmd(bpy.types.Operator,ImportHelper):
    bl_idname='anymodel.import_mmd_reference'
    bl_label='Import MMD Reference Rig'
    bl_options={'REGISTER','UNDO'}
    filter_glob:StringProperty(default='*.pmx;*.pmd',options={'HIDDEN'})
    def execute(self,context):
        try:
            importer=_mmd_operator('import_model')
            before=set(context.scene.objects)
            if context.object and context.object.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.select_all(action='DESELECT')
            result=importer(filepath=self.filepath,types={'ARMATURE'},
                scale=context.scene.anymodel_mmd_scale,rename_bones=False,log_level='ERROR')
            arms=[o for o in context.scene.objects if o not in before and o.type=='ARMATURE']
            if result!={'FINISHED'} or len(arms)!=1:raise ValueError('MMD Tools did not import one reference armature.')
            # Some CATS PMD importers ignore types={'ARMATURE'}. The reference
            # needs only the rig: remove only meshes created by this import.
            for obj in list(context.scene.objects):
                if obj not in before and obj.type=='MESH':
                    mesh=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
                    if mesh.users==0:bpy.data.meshes.remove(mesh)
            context.scene.anymodel_mmd_source=arms[0]
            if context.scene.anymodel_mmd_torso and prepare_reference_torso(context,arms[0]):
                self.report({'INFO'},'Added upper-body-2 to the mesh-free reference, before VMD import.')
            return {'FINISHED'}
        except (AttributeError,RuntimeError) as error:
            self.report({'ERROR'},'Enable MMD Tools to import PMX/PMD. An already imported animated rig also works. '+str(error));return {'CANCELLED'}
        except Exception as error:
            self.report({'ERROR'},str(error));return {'CANCELLED'}


class ANYMODEL_OT_import_vmd(bpy.types.Operator,ImportHelper):
    bl_idname='anymodel.import_vmd'
    bl_label='Load VMD on Source Rig'
    bl_options={'REGISTER','UNDO'}
    filter_glob:StringProperty(default='*.vmd',options={'HIDDEN'})
    def execute(self,context):
        source=context.scene.anymodel_mmd_source
        if not source:
            self.report({'ERROR'},'Import or choose the MMD reference armature first.');return {'CANCELLED'}
        old_active=context.view_layer.objects.active;old_selection=list(context.selected_objects)
        try:
            if context.object and context.object.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.select_all(action='DESELECT');source.select_set(True);context.view_layer.objects.active=source
            if source.animation_data and source.animation_data.action:source.animation_data.action.use_fake_user=True
            path=Path(self.filepath)
            importer=_mmd_operator('import_vmd')
            properties=importer.get_rna_type().properties
            options={k:v for k,v in {'create_new_action':True,'use_nla':False}.items() if k in properties}
            return importer(filepath=str(path),directory=str(path.parent),files=[{'name':path.name}],
                scale=context.scene.anymodel_mmd_scale,margin=0,bone_mapper='PMX',rename_bones=False,
                update_scene_settings=True,log_level='ERROR',**options)
        except (AttributeError,RuntimeError) as error:
            self.report({'ERROR'},'Enable MMD Tools for VMD import. '+str(error));return {'CANCELLED'}
        finally:
            bpy.ops.object.select_all(action='DESELECT')
            for obj in old_selection:
                if obj.name in context.view_layer.objects:obj.select_set(True)
            context.view_layer.objects.active=old_active


class ANYMODEL_PT_mmd(bpy.types.Panel):
    bl_label='AnyModel · MMD Animations'
    bl_idname='ANYMODEL_PT_mmd'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='AnyModel'
    bl_order=-30
    def draw(self,context):
        layout,scene=self.layout,context.scene
        from .mmd_quick import selected_target
        try:
            target=selected_target(context)
            layout.label(text='Avatar: '+target.name,icon='ARMATURE_DATA')
            layout.operator('anymodel.vmd_to_avatar',icon='ACTION')
        except ValueError:
            layout.label(text='Select an avatar armature or its mesh.',icon='INFO')
            row=layout.row();row.enabled=False;row.operator('anymodel.vmd_to_avatar')
        layout.label(text='VMD → Action on the selected armature.')
        layout.operator('export_scene.avatar_action',text='Export animation (.avanim)')
        layout.prop(scene,'anymodel_mmd_advanced',text='Advanced: custom MMD reference')
        if not scene.anymodel_mmd_advanced:return
        layout.separator()
        layout.label(text='1. Reference rig with MMD animation')
        layout.prop(scene,'anymodel_mmd_source')
        row=layout.row(align=True);row.operator('anymodel.import_mmd_reference',text='PMX / PMD…');row.operator('anymodel.import_vmd',text='VMD…')
        layout.prop(scene,'anymodel_mmd_scale')
        layout.prop(scene,'anymodel_mmd_torso')
        layout.label(text='MMD Tools / CATS · evaluated IK is baked.')
        layout.label(text='2. Destination avatar')
        layout.prop(scene,'anymodel_mmd_target')
        layout.operator('anymodel.retarget_map',icon='BONE_DATA')
        layout.prop(scene,'anymodel_mmd_show_map')
        if scene.anymodel_mmd_show_map and scene.anymodel_mmd_source and scene.anymodel_mmd_target:
            box=layout.box();box.label(text='Role · source · target')
            for item in scene.anymodel_retarget_map:
                row=box.row(align=True);row.label(text=item.role)
                row.prop_search(item,'source',scene.anymodel_mmd_source.data,'bones',text='')
                row.prop_search(item,'target',scene.anymodel_mmd_target.data,'bones',text='')
        layout.label(text='3. Bake editable Action, then export')
        layout.prop(scene,'anymodel_mmd_name')
        row=layout.row(align=True);row.prop(scene,'frame_start',text='From');row.prop(scene,'frame_end',text='To')
        layout.prop(scene,'anymodel_mmd_step');layout.prop(scene,'anymodel_mmd_root')
        layout.operator('anymodel.retarget_mmd',icon='ACTION')
        layout.operator('export_scene.avatar_action',text='Export AnyModel Animation (.avanim)')
        layout.label(text='Sound: add to Sequencer; enable Include scene sound.')
        if scene.get('avatar_bridge_previous_setup'):
            layout.operator('avatar_action.restore_setup')


from .mmd_quick import ANYMODEL_OT_vmd_avatar

CLASSES=(ANYMODEL_PG_retarget_bone,ANYMODEL_OT_retarget_map,ANYMODEL_OT_retarget,ANYMODEL_OT_vmd_avatar,
         ANYMODEL_OT_import_mmd,ANYMODEL_OT_import_vmd,ANYMODEL_PT_mmd)
PROPS=('anymodel_mmd_source','anymodel_mmd_target','anymodel_retarget_map',
       'anymodel_mmd_name','anymodel_mmd_step','anymodel_mmd_root','anymodel_mmd_scale','anymodel_mmd_show_map','anymodel_mmd_torso','anymodel_mmd_advanced')


def register():
    for cls in CLASSES:bpy.utils.register_class(cls)
    bpy.types.Scene.anymodel_mmd_source=PointerProperty(type=bpy.types.Object,name='MMD source',poll=_armature)
    bpy.types.Scene.anymodel_mmd_target=PointerProperty(type=bpy.types.Object,name='Humanoid avatar',poll=_armature)
    bpy.types.Scene.anymodel_retarget_map=CollectionProperty(type=ANYMODEL_PG_retarget_bone)
    bpy.types.Scene.anymodel_mmd_name=StringProperty(name='Action name',default='MMD Humanoid')
    bpy.types.Scene.anymodel_mmd_step=IntProperty(name='Sample every N frames',default=1,min=1,max=10)
    bpy.types.Scene.anymodel_mmd_root=BoolProperty(name='Preserve root motion (XYZ)',default=True)
    bpy.types.Scene.anymodel_mmd_scale=FloatProperty(name='MMD import scale',default=.08,min=.0001,max=10)
    bpy.types.Scene.anymodel_mmd_show_map=BoolProperty(name='Edit bone mapping',default=False)
    bpy.types.Scene.anymodel_mmd_advanced=BoolProperty(name='Advanced reference workflow',default=False)
    bpy.types.Scene.anymodel_mmd_torso=BoolProperty(name='Complete reference upper-body-2',default=True,
        description='Add the chest joint required by many MMD motions, only to newly imported mesh-free reference rigs')


def unregister():
    for prop in PROPS:delattr(bpy.types.Scene,prop)
    for cls in reversed(CLASSES):bpy.utils.unregister_class(cls)
