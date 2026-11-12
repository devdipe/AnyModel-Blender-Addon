"""Selected humanoid + VMD -> editable Action, without an artist reference rig.

The temporary PMX is generated from target measurements using a conventional
humanoid hierarchy. It has no vertices, textures, materials or external assets.
MMD Tools/CATS remains an external VMD decoder/IK evaluator, never bundled.
"""
import bpy
import math
import struct
import tempfile
import unicodedata
from pathlib import Path
from mathutils import Vector
from bpy.props import StringProperty
from bpy_extras.io_utils import ImportHelper
from . import avatar_export


def normalized(name):
    return unicodedata.normalize('NFKC',name).replace(' ','').lower()


def inspect_motion(filepath):
    path=Path(filepath)
    if path.stat().st_size>256*1024*1024:
        raise ValueError('VMD exceeds 256 MiB.')
    data=path.read_bytes()
    if len(data)<44 or not data[:30].startswith(b'Vocaloid Motion Data'):
        raise ValueError('Choose a valid VMD motion file.')
    offset=30+(20 if b'0002' in data[:30] else 10)
    def count(at,stride):
        if at+4>len(data):raise ValueError('Truncated VMD section.')
        n=struct.unpack_from('<I',data,at)[0]
        if n>2_000_000 or at+4+n*stride>len(data):raise ValueError('Invalid VMD section length.')
        return n,at+4+n*stride
    n,end=count(offset,111)
    if not n:raise ValueError('This VMD has no skeletal animation.')
    tracks={};first=36001;last=0
    for at in range(offset+4,end,111):
        name=data[at:at+15].split(b'\0')[0].decode('cp932',errors='replace')
        frame=struct.unpack_from('<I',data,at+15)[0]
        if frame>36000:raise ValueError('VMD exceeds the supported 36000-frame range.')
        first=min(first,frame);last=max(last,frame)
        tracks[name]=tracks.get(name,0)+1
    morph_count=0
    if end<len(data):morph_count,end=count(end,23)
    return dict(start=first,end=max(first+1,last),tracks=tracks,morphKeys=morph_count)


def selected_target(context):
    from . import armature_for
    try:arm=armature_for(context)
    except ValueError:
        arm=context.scene.anymodel_mmd_target
    if not arm or arm.type!='ARMATURE' or arm.get('anymodel_preview'):
        raise ValueError('Select an avatar armature or a mesh bound to it.')
    mapping=avatar_export.mapping_for(context.scene,arm)
    missing=[r for r in avatar_export.REQUIRED if r not in mapping]
    if missing:
        raise ValueError('Unrecognized avatar bones: '+', '.join(missing)+'. Check Detect Humanoid Bones.')
    return arm


def reference_bones(scene,target,motion):
    """Own procedural MMD reference; dimensions come from this target only."""
    mapping=avatar_export.mapping_for(scene,target)
    rest={r:target.matrix_world@target.data.bones[n].matrix_local for r,n in mapping.items()}
    def pos(role):return rest[role].translation
    right=(pos('RightUpperArm')-pos('LeftUpperArm')).normalized()
    up=pos('Head')-pos('Hips');up-=right*up.dot(right)
    if up.length<1e-6:raise ValueError('Cannot infer humanoid up axis.')
    up.normalize()
    def length(a,b,fallback):
        return (pos(a)-pos(b)).length if a in rest and b in rest else fallback
    leg=sum(length(s+'UpperLeg',s+'LowerLeg',0)+length(s+'LowerLeg',s+'Foot',0) for s in ('Left','Right'))/2
    if leg<1e-6:raise ValueError('Humanoid leg length is zero.')
    floor=min(pos(r).dot(up) for r in ('LeftFoot','RightFoot','LeftToes','RightToes') if r in rest)-leg*.06
    height=(pos('Head').dot(up)-floor)/.90
    if height<1e-6:raise ValueError('Humanoid rest height is zero.')
    unit=20/height
    def y(role,fallback):return (pos(role).dot(up)-floor)*unit if role in rest else fallback
    def segment(a,b,fallback):return length(a,b,fallback/unit)*unit
    # MMD file-space convention: X left, Y up, model front toward -Z.
    names_by_normal={normalized(n):n for n in motion['tracks']}
    def choose(aliases):
        present=[names_by_normal[normalized(a)] for a in aliases if normalized(a) in names_by_normal]
        return max(present,key=lambda n:motion['tracks'][n]) if present else aliases[0]
    bones=[];by_role={}
    def add(name,position,parent=None,role=None,ik=None):
        index=len(bones);name=choose([name]) if isinstance(name,str) else choose(name)
        bones.append(dict(name=name,position=tuple(position),parent=-1 if parent is None else parent,role=role,ik=ik))
        if role:by_role[role]=index
        return index
    hips=y('Hips',10);spine=y('Spine',hips+.7);neck=y('Neck',17.2)
    chest=y('Chest',(spine+neck)*.5);upper_chest=y('UpperChest',(chest+neck)*.5)
    root=add(['全ての親','全親'],(0,0,0));centre=add('センター',(0,hips,0),root)
    groove=add('グルーブ',(0,hips,0),centre);waist=add('腰',(0,hips,0),groove)
    lower=add('下半身',(0,hips,0),waist,'Hips')
    torso=add('上半身',(0,spine,0),waist,'Spine')
    torso2=add(['上半身2','上半身２'],(0,chest,0),torso,'Chest')
    torso3=add(['上半身3','上半身３'],(0,upper_chest,0),torso2,'UpperChest')
    neck_id=add('首',(0,neck,0),torso3,'Neck')
    add('頭',(0,y('Head',18),0),neck_id,'Head')
    for side,jp,sign in [('Left','左',1),('Right','右',-1)]:
        arm=side+'UpperArm';low=side+'LowerArm';hand=side+'Hand'
        half_arm=abs((pos(arm)-pos('Hips')).dot(right))*unit
        shoulder_inner=abs((pos(side+'Shoulder')-pos('Hips')).dot(right))*unit if side+'Shoulder' in rest else half_arm*.65
        shoulder=add(jp+'肩',(sign*shoulder_inner,y(side+'Shoulder',y(arm,16)),0),torso3,side+'Shoulder')
        p=Vector((sign*half_arm,y(arm,16),0));direction=Vector((sign*math.cos(math.radians(35)),-math.sin(math.radians(35)),0))
        upper=add(jp+'腕',p,shoulder,arm)
        upper_length=segment(arm,low,3);lower_length=segment(low,hand,2.7)
        twist=add([jp+'腕捩',jp+'腕捩り'],p+direction*upper_length*.5,upper)
        elbow_pos=p+direction*upper_length
        elbow=add([jp+'ひじ',jp+'肘'],elbow_pos,twist,low)
        wrist_twist=add([jp+'手捩',jp+'手捩り'],elbow_pos+direction*lower_length*.5,elbow)
        wrist_pos=elbow_pos+direction*lower_length
        wrist=add(jp+'手首',wrist_pos,wrist_twist,hand)
        palm=segment(hand,side+'HandMiddle1',.65)
        for finger,word,spread in [('Thumb','親指',-.65),('Index','人指',-.25),('Middle','中指',0),('Ring','薬指',.23),('Little','小指',.44)]:
            parent=wrist;root_pos=wrist_pos+direction*palm+Vector((0,0,spread))
            finger_dir=(direction+Vector((0,0,-.7))).normalized() if finger=='Thumb' else direction
            if finger=='Thumb':root_pos=wrist_pos+direction*palm*.25+Vector((0,0,-.36))
            for n in (1,2,3):
                role=side+'Hand'+finger+str(n);index=n-1 if finger=='Thumb' else n
                aliases=[jp+word+str(index),jp+word+'０１２３'[index]]
                if finger=='Index':aliases += [jp+'人差指'+str(index),jp+'人差指'+'０１２３'[index]]
                parent=add(aliases,root_pos,parent,role)
                root_pos+=finger_dir*segment(role,side+'Hand'+finger+str(n+1),.38 if n==1 else .28)
        hip_x=abs((pos(side+'UpperLeg')-pos('Hips')).dot(right))*unit
        hip_y=y(side+'UpperLeg',hips);upper_length=segment(side+'UpperLeg',side+'LowerLeg',4.6)
        lower_length=segment(side+'LowerLeg',side+'Foot',4.5)
        leg_upper=add([jp+'足',jp+'足D'],(sign*hip_x,hip_y,0),lower,side+'UpperLeg')
        knee=add([jp+'ひざ',jp+'膝',jp+'ひざD'],(sign*hip_x,hip_y-upper_length,-.04),leg_upper,side+'LowerLeg')
        ankle_pos=Vector((sign*hip_x,hip_y-upper_length-lower_length,0))
        ankle=add([jp+'足首',jp+'足首D'],ankle_pos,knee,side+'Foot')
        toe_length=segment(side+'Foot',side+'Toes',1.1)
        toe_pos=ankle_pos+Vector((0,-max(.1,y(side+'Foot',.8)-y(side+'Toes',.4)),-toe_length))
        toe=add(jp+'つま先',toe_pos,ankle,side+'Toes')
        goal=add([jp+'足ＩＫ',jp+'足IK',jp+'足ＩＫ親'],ankle_pos,root,
                 ik=dict(target=ankle,links=[(knee,True),(leg_upper,False)]))
        add([jp+'つま先ＩＫ',jp+'つま先IK'],toe_pos,goal,ik=dict(target=toe,links=[(ankle,False)]))
    return bones,dict(unitScale=unit,canonicalHeight=20,armRestDegrees=35)


def write_reference(path,bones):
    """Minimal PMX 2.0 skeleton. No material or mesh sections are populated."""
    data=bytearray(b'PMX ')+struct.pack('<fB8B',2.0,8,1,0,1,1,1,2,1,1)
    def put(fmt,*values):data.extend(struct.pack('<'+fmt,*values))
    def text(value):
        raw=value.encode('utf-8');put('i',len(raw));data.extend(raw)
    for value in ('AnyModel temporary reference','AnyModel generated humanoid','',''):text(value)
    for _ in range(4):put('i',0) # vertices, indices, textures, materials
    put('i',len(bones))
    for index,bone in enumerate(bones):
        children=[i for i,b in enumerate(bones) if b['parent']==index]
        connection=children[0] if children else None
        text(bone['name']);text(bone['role'] or '')
        put('3fhiH',*bone['position'],bone['parent'],1 if bone['ik'] else 0,0x1e|(0x20 if bone['ik'] else 0)|(1 if connection is not None else 0))
        if connection is not None:put('h',connection)
        else:
            direction=Vector(bone['position'])-Vector(bones[bone['parent']]['position']) if bone['parent']>=0 else Vector((0,1,0))
            if direction.length<.001:direction=Vector((0,1,0))
            put('3f',*(direction.normalized()*.2))
        if bone['ik']:
            ik=bone['ik'];put('hifi',ik['target'],40,.5,len(ik['links']))
            for index,knee in ik['links']:
                put('hB',index,int(knee))
                if knee:put('6f',-math.pi,0,0,-.001,0,0)
    for _ in range(4):put('i',0) # morphs, display frames, rigid bodies, joints
    Path(path).write_bytes(data)


def load_to_avatar(context,target,filepath):
    from . import mmd_converter as converter
    motion=inspect_motion(filepath)
    converter._mmd_operator('import_model');converter._mmd_operator('import_vmd')
    mapping=avatar_export.mapping_for(context.scene,target)
    missing=[r for r in avatar_export.REQUIRED if r not in mapping]
    if missing:raise ValueError('Unrecognized bones: '+', '.join(missing))
    scene=context.scene
    # Avatar rest transforms are measured without evaluating/rewriting its pose.
    bones,dimensions=reference_bones(scene,target,motion)
    objects_before=set(bpy.data.objects);arms_before=set(bpy.data.armatures);meshes_before=set(bpy.data.meshes);actions_before=set(bpy.data.actions)
    selected_before=list(context.selected_objects);old_active=context.view_layer.objects.active
    old_mode=context.object.mode if context.object else 'OBJECT'
    old_source,old_target=scene.anymodel_mmd_source,scene.anymodel_mmd_target
    old_scale=scene.anymodel_mmd_scale
    frame_state=(scene.frame_start,scene.frame_end,scene.frame_current,scene.frame_subframe,scene.render.fps,scene.render.fps_base)
    action=None;success=False
    try:
        if context.object and context.object.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        with tempfile.TemporaryDirectory(prefix='anymodel-vmd-') as directory:
            reference=Path(directory)/'AnyModel-generated-reference.pmx';write_reference(reference,bones)
            result=converter._mmd_operator('import_model')(filepath=str(reference),types={'ARMATURE'},scale=.08,rename_bones=False,log_level='ERROR')
            source_arms=[o for o in bpy.data.objects if o not in objects_before and o.type=='ARMATURE']
            if result!={'FINISHED'} or len(source_arms)!=1:raise ValueError('Cannot create the temporary motion reference.')
            source=source_arms[0];source['anymodel_temporary_reference']=True
            by_name={p.mmd_bone.name_j:p.name for p in source.pose.bones}
            for spec in bones:
                if spec['role'] and spec['name'] in by_name:source.data.bones[by_name[spec['name']]]['avatar_bone']=spec['role']
            # No procedural mesh or morph helper belongs in the resulting scene.
            for obj in list(bpy.data.objects):
                if obj not in objects_before and obj.type=='MESH':bpy.data.objects.remove(obj,do_unlink=True)
            bpy.ops.object.select_all(action='DESELECT');source.select_set(True);context.view_layer.objects.active=source
            importer=converter._mmd_operator('import_vmd');properties=importer.get_rna_type().properties
            options={k:v for k,v in {'create_new_action':True,'use_nla':False}.items() if k in properties}
            path=Path(filepath)
            result=importer(filepath=str(path),directory=str(path.parent),files=[{'name':path.name}],scale=.08,
                margin=0,bone_mapper='PMX',rename_bones=False,update_scene_settings=False,log_level='ERROR',**options)
            if result!={'FINISHED'} or not source.animation_data or not source.animation_data.action:
                raise ValueError('VMD did not produce a skeletal Action.')
            scene.render.fps=30;scene.render.fps_base=1
            scene.frame_start=motion['start'];scene.frame_end=motion['end']
            action,report=converter.retarget(context,source,target,start=motion['start'],end=motion['end'],
                step=1,name=path.stem[:90],root_motion=True)
            from .face_vmd import import_face
            facial=import_face(context,target,filepath,arm_action=action)
            report['face']=facial
            report.update(automatic=True,reference='procedural humanoid, generated from target measurements',
                inputFile=str(path),referenceBoneCount=len(bones),dimensions=dimensions,morphKeysNotConverted=len(facial['unmappedMorphs']))
            action[converter.REPORT_KEY]=__import__('json').dumps(report,ensure_ascii=False)
            scene[converter.REPORT_KEY]=action[converter.REPORT_KEY]
            scene.avatar_action_loop=False
            success=True
            return action,report
    finally:
        # Remove only datablocks created during this import; keep the new avatar Action.
        if context.object and context.object.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
        for obj in list(bpy.data.objects):
            if obj not in objects_before:bpy.data.objects.remove(obj,do_unlink=True)
        for collection,previous in [(bpy.data.armatures,arms_before),(bpy.data.meshes,meshes_before)]:
            for block in list(collection):
                if block not in previous and block.users==0:collection.remove(block)
        for block in list(bpy.data.actions):
            if block not in actions_before and block!=action and block.users<=int(block.use_fake_user):bpy.data.actions.remove(block)
        scene.anymodel_mmd_source,scene.anymodel_mmd_target=old_source,old_target
        scene.anymodel_mmd_scale=old_scale
        bpy.ops.object.select_all(action='DESELECT')
        if success:
            target.select_set(True);context.view_layer.objects.active=target
            scene.frame_set(motion['start'])
            if old_mode=='POSE':bpy.ops.object.mode_set(mode='POSE')
        else:
            scene.frame_start,scene.frame_end,frame,sub,scene.render.fps,scene.render.fps_base=frame_state
            scene.frame_set(frame,subframe=sub)
            for obj in selected_before:
                if obj.name in context.view_layer.objects:obj.select_set(True)
            context.view_layer.objects.active=old_active
            if old_mode!='OBJECT' and old_active:
                bpy.ops.object.mode_set(mode=old_mode)


class ANYMODEL_OT_vmd_avatar(bpy.types.Operator,ImportHelper):
    bl_idname='anymodel.vmd_to_avatar'
    bl_label='Add VMD Animation'
    bl_description='Choose a VMD file to turn its motion into an editable Action on the selected avatar'
    bl_options={'REGISTER','UNDO'}
    filter_glob:StringProperty(default='*.vmd',options={'HIDDEN'})
    target_name:StringProperty(options={'HIDDEN'})
    def invoke(self,context,event):
        try:self.target_name=selected_target(context).name
        except ValueError as error:
            self.report({'ERROR'},str(error));return {'CANCELLED'}
        return ImportHelper.invoke(self,context,event)
    def execute(self,context):
        try:
            target=bpy.data.objects.get(self.target_name) if self.target_name else selected_target(context)
            if not target:raise ValueError('The selected armature no longer exists.')
            action,report=load_to_avatar(context,target,self.filepath)
            self.report({'INFO'},f"Done: {action.name}, {report['samples']} frames. Edit the Action in Pose Mode.")
            return {'FINISHED'}
        except Exception as error:
            self.report({'ERROR'},str(error));return {'CANCELLED'}
