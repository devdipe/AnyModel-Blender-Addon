"""Evaluate certified baked pose Actions without evaluating scene geometry.

Fall back for constraints, drivers, NLA, animated parents and frame handlers.
The regular exporter remains the authority for procedural/rigged animations.
"""
import bpy
from mathutils import Matrix,Vector,Quaternion
from .avatar_export import ordered_bones,engine_matrix


def _driven(data):
    return data and any(not curve.mute for curve in data.drivers)


def _constrained(owner):
    return any(not c.mute and c.influence!=0 for c in owner.constraints)


class BakedSampler:
    def __init__(self,arm,bones,space,rest_inverse,visibility):
        from . import fcurves
        self.arm,self.bones,self.space,self.rest_inverse=arm,bones,space,rest_inverse
        self.world=arm.matrix_world.copy();self.ordered=ordered_bones(arm)
        self.channels={};self.defaults={};self.visibility=[]
        allowed={}
        for pb in arm.pose.bones:
            self.defaults[pb.name]=([*pb.location],[*pb.rotation_quaternion],[*pb.scale])
            for part,size in [('location',3),('rotation_quaternion',4),('scale',3)]:
                allowed[pb.path_from_id(part)]=(pb.name,part,size)
        data=arm.animation_data
        for curve in fcurves(data.action,getattr(data,'action_slot',None)):
            if curve.mute or getattr(curve.group,'mute',False):continue
            entry=allowed.get(curve.data_path)
            if entry is None or not 0<=curve.array_index<entry[2]:raise ValueError('Non-pose Action channel')
            self.channels.setdefault(entry[0],{}).setdefault(entry[1],{})[curve.array_index]=curve
        self.compiled=[];self.by_name={};unit=space['unitScale'] if space else 1
        for i,bone in enumerate(bones):
            channels=self.channels.get(bone.name,{});fields=[]
            for part,default in zip(('location','rotation_quaternion','scale'),self.defaults[bone.name]):
                curves=channels.get(part,{})
                for channel,value in enumerate(default):
                    curve=curves.get(channel)
                    if curve and len(curve.keyframe_points)==2 and not len(curve.modifiers):
                        a,b=curve.keyframe_points
                        if a.co[1]==b.co[1] and a.interpolation in ('LINEAR','CONSTANT'):curve=None;value=a.co[1]
                    fields.append(curve if curve else float(value))
            standard=bone.use_inherit_rotation and bone.use_local_location and bone.inherit_scale=='FULL'
            if space:
                if not bone.parent:standard=False
                else:
                    expected=bone.parent.matrix_local.inverted()@bone.matrix_local
                    expected.translation*=unit
                    actual=rest_inverse[i].inverted()
                    standard=standard and all(abs(expected[r][c]-actual[r][c])<1e-5*max(1,abs(expected[r][c])) for r in range(4) for c in range(4))
            item=(bone,tuple(fields),standard,i);self.compiled.append(item);self.by_name[bone.name]=item
        for obj in visibility:
            fields={name:bool(getattr(obj,name)) for name in ('avatar_visible','hide_render','hide_viewport')}
            curves={}
            ad=obj.animation_data
            if _driven(ad) or ad and ad.use_nla and len(ad.nla_tracks):raise ValueError('Driven/NLA visibility')
            if ad and ad.action:
                for fc in fcurves(ad.action,getattr(ad,'action_slot',None)):
                    if not fc.mute and fc.data_path in fields:curves[fc.data_path]=fc
            self.visibility.append((str(obj.get('avatar_mesh',obj.name)),obj.avatar_track_visibility,fields,curves))

    def sample(self,frame):
        values={};globals_={};matrices={};inverse_parents={};unit=self.space['unitScale'] if self.space else 1
        for bone,fields,standard,i in self.compiled:
            v=[item if isinstance(item,float) else item.evaluate(frame) for item in fields]
            if bone.parent and bone.use_connect:v[:3]=[0,0,0]
            q=Quaternion(v[3:7]);q.normalize();v[3:7]=q
            values[bone.name]=v
        def pose_for(bone):
            if bone.name not in globals_:
                v=values[bone.name];basis=Matrix.LocRotScale(Vector(v[:3]),Quaternion(v[3:7]),Vector(v[7:]))
                kw={'parent_matrix':pose_for(bone.parent),'parent_matrix_local':bone.parent.matrix_local} if bone.parent else {}
                globals_[bone.name]=bone.convert_local_to_pose(basis,bone.matrix_local,**kw)
            return globals_[bone.name]
        def matrix_for(bone):
            if bone.name not in matrices:
                pose=pose_for(bone);matrices[bone.name]=engine_matrix(self.space,self.world,pose) if self.space else pose
            return matrices[bone.name]
        result=[]
        for bone,fields,standard,i in self.compiled:
            if standard:
                v=values[bone.name];result.append([*(x*unit for x in v[:3]),v[4],v[5],v[6],v[3],*v[7:]]);continue
            pose=matrix_for(bone)
            if bone.parent:
                inverse=inverse_parents.get(bone.parent.name)
                if inverse is None:inverse=matrix_for(bone.parent).inverted();inverse_parents[bone.parent.name]=inverse
                local=inverse@pose
            else:local=pose
            t,q,scale=(self.rest_inverse[i]@local).decompose();q.normalize()
            result.append([*t,q.x,q.y,q.z,q.w,*scale])
        return result

    def visible(self,frame):
        result={}
        for name,tracked,defaults,curves in self.visibility:
            values={key:(curves[key].evaluate(frame)>=.5 if key in curves else value) for key,value in defaults.items()}
            result[name]=values['avatar_visible'] if tracked else not(values['hide_render'] or values['hide_viewport'])
        return result


def prepare(arm,bones,space,rest_inverse,visibility,scope):
    if scope!='FULL':return None,'Events-only Action'
    ad=arm.animation_data
    if not ad or not ad.action or not ad.action.get('anymodel_retarget_report'):return None,'Action is not a certified baked import'
    if arm.data.pose_position!='POSE' or ad.use_tweak_mode or ad.action_blend_type!='REPLACE' or abs(ad.action_influence-1)>1e-6:return None,'Nonstandard pose/Action blend'
    if _driven(ad) or _driven(arm.data.animation_data) or ad.use_nla and len(ad.nla_tracks):return None,'Active drivers or NLA'
    if arm.data.animation_data and arm.data.animation_data.action:return None,'Animated armature data'
    if _constrained(arm) or any(_constrained(pb) or pb.rotation_mode!='QUATERNION' for pb in arm.pose.bones):return None,'Live rig constraints or Euler pose'
    parent=arm.parent
    while parent:
        data=parent.animation_data
        if _constrained(parent) or data and (data.action or len(data.nla_tracks) or _driven(data)):return None,'Animated parent transform'
        parent=parent.parent
    if bpy.app.handlers.frame_change_pre or bpy.app.handlers.frame_change_post:return None,'Scene frame handlers'
    try:return BakedSampler(arm,bones,space,rest_inverse,visibility),'Direct baked Action'
    except ValueError as error:return None,str(error)
