"""Native VMD mouth/blink/eye decoder and editable humanoid facial Actions.
No CATS/MMD Tools dependency. Files are bounded numeric data, never scripts.
"""
import bpy,struct,math,bisect,json,uuid
from pathlib import Path
from array import array
import numpy as np
from mathutils import Matrix,Vector,Quaternion
from bpy.props import StringProperty
from bpy_extras.io_utils import ImportHelper
from . import face_tools as face

EYES={'両目':'both','左目':'left','右目':'right','BothEyes':'both','LeftEye':'left','RightEye':'right'}

def read_vmd(filepath):
 path=Path(filepath)
 if path.stat().st_size>256*1024*1024:raise ValueError('VMD exceeds 256 MiB.')
 data=path.read_bytes()
 if len(data)<44 or not data[:30].startswith(b'Vocaloid Motion Data'):raise ValueError('Invalid VMD header.')
 at=50 if b'0002' in data[:30] else 40
 def section(stride):
  nonlocal at
  if at+4>len(data):raise ValueError('Truncated VMD section.')
  count=struct.unpack_from('<I',data,at)[0];at+=4;start=at;at+=count*stride
  if count>2_000_000 or at>len(data):raise ValueError('VMD section size limit.')
  return range(start,at,stride)
 eyes={};morphs={};first=36001;last=0;bone_count=0
 for offset in section(111):
  bone_count+=1;name=data[offset:offset+15].split(b'\0')[0].decode('cp932',errors='replace');frame=struct.unpack_from('<I',data,offset+15)[0]
  if frame>36000:raise ValueError('VMD exceeds frame 36000.')
  first=min(first,frame);last=max(last,frame)
  if name not in EYES:continue
  q=struct.unpack_from('<4f',data,offset+31)
  if not all(math.isfinite(v) for v in q) or sum(v*v for v in q)<1e-10:raise ValueError('Invalid VMD eye quaternion.')
  controls=tuple(data[offset+47+i]/127 for i in (48,52,56,60))
  if any(v>1 for v in controls):raise ValueError('Invalid VMD eye interpolation.')
  eyes.setdefault(EYES[name],{})[frame]=(q,controls)
 if at<len(data):
  for offset in section(23):
   name=data[offset:offset+15].split(b'\0')[0].decode('cp932',errors='replace');frame,value=struct.unpack_from('<If',data,offset+15)
   if frame>36000 or not math.isfinite(value) or abs(value)>4:raise ValueError('Invalid VMD face frame/weight.')
   if name not in morphs and len(morphs)>=256:raise ValueError('VMD exceeds 256 morph tracks.')
   morphs.setdefault(name,{})[frame]=value;first=min(first,frame);last=max(last,frame)
 return dict(eyes={name:sorted(rows.items()) for name,rows in eyes.items()},morphs={name:sorted(rows.items()) for name,rows in morphs.items()},start=0 if first>36000 else first,end=max(last,(0 if first>36000 else first)+1),boneKeys=bone_count)

def _bezier(t,controls):
 x1,y1,x2,y2=controls
 if abs(x1-y1)<1e-7 and abs(x2-y2)<1e-7:return t
 low,high=0.,1.
 for _ in range(24):
  u=(low+high)*.5;v=1-u;x=3*v*v*u*x1+3*v*u*u*x2+u*u*u
  if x<t:low=u
  else:high=u
 u=(low+high)*.5;v=1-u;return 3*v*v*u*y1+3*v*u*u*y2+u*u*u

def _rotation(track,frame):
 if not track:return Quaternion()
 frames=[x[0] for x in track];i=bisect.bisect_right(frames,frame)
 def convert(q):
  # MMD: +X avatar-left, +Y up, forward -Z. Engine: +X right.
  x,y,z,w=q;return Quaternion((w,x,-y,-z)).normalized()
 if i==0:return convert(track[0][1][0])
 if i>=len(track):return convert(track[-1][1][0])
 a,b=track[i-1],track[i];factor=(frame-a[0])/(b[0]-a[0]);return convert(a[1][0]).slerp(convert(b[1][0]),_bezier(factor,b[1][1]))

def _targets(arm,name):
 s=arm.anymodel_face;mesh=s.face_mesh;eye=s.eyelid_mesh or mesh
 aliases={'まばたき':s.blink,'瞬き':s.blink,'blink':s.blink,'ウィンク':s.blink_left,'ウィンク左':s.blink_left,'ウィンク右':s.blink_right,'ｳｨﾝｸ２':s.blink_left,'ウィンク２':s.blink_left}
 if name in aliases:
  key=face._key(eye,aliases[name],False)
  if face._nonempty(key):return [(eye,key,1.)]
  if name in {'まばたき','瞬き','blink'}:
   split=[(eye,key,1.) for key in [face._key(eye,s.blink_left,False),face._key(eye,s.blink_right,False)] if face._nonempty(key)]
   if len(split)==2:return split
 for obj in (mesh,eye):
  key=face._key(obj,name,False)
  if face._nonempty(key):return [(obj,key,1.)]
 if name in face.MMD_VOWELS:
  v=face.MMD_VOWELS[name];key=face._key(mesh,'vrc.v_'+v,False)
  if face._nonempty(key):return [(mesh,key,1.)]
  result=[]
  for source,weight in zip((s.mouth_a,s.mouth_o,s.mouth_ch),face.VISEMES[v]):
   key=face._key(mesh,source,False)
   if weight and face._nonempty(key):result.append((mesh,key,float(weight)*s.intensity))
  return result
 return []

def _write_curve(idblock,action,path,values,frames,group):
 from . import fcurves
 from .curve_reduce import retained_indices
 idblock.path_resolve(path)
 # Seed a layered Action channel through Blender's supported insertion API.
 if '[' in path and idblock.__class__.__name__=='Key':
  key=next(k for k in idblock.key_blocks if k.path_from_id('value')==path);key.value=float(values[0]);key.keyframe_insert(data_path='value',frame=frames[0],group=group)
 else:raise ValueError('Unsupported scalar channel')
 curve=next(c for c in fcurves(action,getattr(idblock.animation_data,'action_slot',None)) if c.data_path==path)
 indices=retained_indices(frames,values,1e-5);coords=array('f')
 for i in indices:coords.extend((frames[i],float(values[i])))
 curve.keyframe_points.clear();curve.keyframe_points.add(len(indices));curve.keyframe_points.foreach_set('co',coords)
 for point in curve.keyframe_points:point.interpolation='LINEAR'
 curve.update()

def import_face(context,target,filepath,*,arm_action=None,standalone=False):
 from . import fcurves
 from .roundtrip import _new_action,SETUP_KEY
 face.stop_preview();motion=read_vmd(filepath);settings=target.anymodel_face
 if not settings.face_mesh:face.auto_detect(target)
 frames=list(range(motion['start'],motion['end']+1));mixed={};missing=[];mapped=[]
 for name,track in motion['morphs'].items():
  destinations=_targets(target,name)
  if not destinations:missing.append(name);continue
  x=np.asarray([row[0] for row in track]);y=np.asarray([row[1] for row in track]);values=np.interp(frames,x,y).astype(np.float32)
  for obj,key,weight in destinations:
   identity=(obj.name,key.name)
   if identity not in mixed:mixed[identity]=np.zeros(len(frames),dtype=np.float32)
   mixed[identity]+=values*weight
  mapped.append(name)
 actions=[];by_keys={}
 backup=json.loads(context.scene.get(SETUP_KEY,'{}'))
 for (object_name,key_name),values in mixed.items():
  obj=bpy.data.objects[object_name];keys=obj.data.shape_keys;ad=keys.animation_data_create()
  if keys.name not in by_keys:
   if ad.action:ad.action.use_fake_user=True
   backup.setdefault('shapes',[]).append(dict(data=keys.name,useNla=ad.use_nla,drivers=[dict(path=d.data_path,index=d.array_index,mute=d.mute) for d in ad.drivers]))
   action=bpy.data.actions.new(Path(filepath).stem+' / '+obj.name+' face');action.use_fake_user=True;ad.action=action
   if hasattr(action,'slots'):ad.action_slot=action.slots.new(id_type='KEY',name=keys.name)
   ad.use_nla=False;by_keys[keys.name]=action;actions.append(action)
  key=keys.key_blocks[key_name]
  for driver in ad.drivers:
   if driver.data_path==key.path_from_id('value'):driver.mute=True
  values=np.clip(values,max(-4,key.slider_min),min(4,key.slider_max));_write_curve(keys,by_keys[keys.name],key.path_from_id('value'),values,frames,'MMD face')
 context.scene[SETUP_KEY]=json.dumps(backup)
 eye_count=0
 if motion['eyes'] and settings.head in target.data.bones:
  right,up=face._head_frame(target,settings.head);back=right.cross(up);basis=Matrix((right,up,back)).transposed().to_quaternion()
  for side in ('left','right'):
   bone_name=getattr(settings,side)
   if bone_name not in target.pose.bones:continue
   if arm_action is None:arm_action=_new_action(target,Path(filepath).stem+' / eyes');actions.append(arm_action)
   pb=target.pose.bones[bone_name];pb.rotation_mode='QUATERNION';rest=pb.bone.matrix_local.to_quaternion();neutral=Quaternion(getattr(settings,side+'_neutral'));rows=[];previous=None
   for frame in frames:
    q=_rotation(motion['eyes'].get('both'),frame)@_rotation(motion['eyes'].get(side),frame)
    local=rest.inverted()@basis@q@basis.inverted()@rest@neutral
    if previous and previous.dot(local)<0:local.negate()
    previous=local.copy();rows.append(tuple(local))
   for channel in range(4):
    path=pb.path_from_id('rotation_quaternion');pb.rotation_quaternion=Quaternion(rows[0]);pb.keyframe_insert(data_path='rotation_quaternion',index=channel,frame=frames[0],group='MMD eyes')
    curve=next(c for c in fcurves(arm_action,getattr(target.animation_data,'action_slot',None)) if c.data_path==path and c.array_index==channel)
    from .curve_reduce import retained_indices
    values=[r[channel] for r in rows];keep=retained_indices(frames,values,1e-6);coords=array('f')
    for index in keep:coords.extend((frames[index],values[index]))
    curve.keyframe_points.clear();curve.keyframe_points.add(len(keep));curve.keyframe_points.foreach_set('co',coords)
    for key in curve.keyframe_points:key.interpolation='LINEAR'
    curve.update()
   eye_count+=1
  if arm_action:arm_action['anymodel_eye_animation']=True
 if standalone:
  if not mixed and not eye_count:raise ValueError('No matching mouth or blink keys or assigned eye bones were found. Configure AnyModel Face.')
  scene=context.scene;scene.frame_start=motion['start'];scene.frame_end=motion['end'];scene.render.fps=30;scene.render.fps_base=1
  scene.avatar_action_scope='FACE' if eye_count else 'EVENTS';scene.avatar_action_name=Path(filepath).stem+' face';scene.avatar_action_id=uuid.uuid4().hex
  if arm_action:arm_action['avatar_action_id']=scene.avatar_action_id;arm_action.use_frame_range=True;arm_action.frame_start=motion['start'];arm_action.frame_end=motion['end']
  scene.frame_set(motion['start'])
 return dict(morphTracks=len(mapped),shapeChannels=len(mixed),eyeBones=eye_count,unmappedMorphs=missing,createdActions=[a.name for a in actions],start=motion['start'],end=motion['end'])

class ANYMODEL_OT_import_vmd_face(bpy.types.Operator,ImportHelper):
 bl_idname='anymodel.import_vmd_face';bl_label='Import MMD Face (.vmd)';bl_options={'REGISTER','UNDO'}
 filter_glob:StringProperty(default='*.vmd',options={'HIDDEN'})
 def execute(self,context):
  try:
   target=face._arm(context);result=import_face(context,target,self.filepath,standalone=True);self.report({'INFO'},str(result));return {'FINISHED'}
  except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}
def register():bpy.utils.register_class(ANYMODEL_OT_import_vmd_face)
def unregister():bpy.utils.unregister_class(ANYMODEL_OT_import_vmd_face)
