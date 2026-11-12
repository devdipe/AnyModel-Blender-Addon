"""Native AnyModel face setup. No CATS operators, imports or runtime dependency.

The 3-input viseme coefficient table is adapted from CATS (MIT),
Copyright (c) 2017 GiveMeAllYourCats; see LICENSE-CATS-MIT.txt.
https://github.com/absolute-quantum/cats-blender-plugin/blob/master/tools/viseme.py
Eye setup/preview, export descriptors, and UI are AnyModel implementations.
"""
import bpy, json, math, time, re
import numpy as np
from mathutils import Matrix,Vector,Quaternion
from bpy.props import StringProperty,FloatProperty,FloatVectorProperty,BoolProperty,PointerProperty,EnumProperty

# Exact neutral pp/sil prevent the tiny residual deformation in old CATS mixes.
VISEMES={'aa':(1,0,0),'ch':(0,0,1),'dd':(.3,0,.7),'e':(0,.3,.7),
 'ff':(.2,0,.4),'ih':(.5,0,.2),'kk':(.7,0,.4),'nn':(.2,0,.7),
 'oh':(.2,.8,0),'ou':(0,1,0),'pp':(0,0,0),'rr':(0,.3,.5),
 'sil':(0,0,0),'ss':(0,0,.8),'th':(.4,.15,0)}
MMD_VOWELS={'あ':'aa','い':'ih','う':'ou','え':'e','お':'oh'}
_PREVIEW=None

def _arm(context):
 from . import armature_for
 return armature_for(context)

def _coords(key):
 a=np.empty(len(key.data)*3,dtype=np.float32);key.data.foreach_get('co',a);return a.reshape(-1,3)

def _mesh(arm,eyelids=False):
 settings=arm.anymodel_face;obj=(settings.eyelid_mesh or settings.face_mesh) if eyelids else settings.face_mesh
 if not obj or obj.type!='MESH' or obj.find_armature()!=arm or not obj.data.shape_keys:raise ValueError('Choose a mesh with Shape Keys bound to this armature.')
 if not obj.data.shape_keys.use_relative:raise ValueError('Relative Shape Keys are required.')
 return obj

def _key(obj,name,required=True):
 key=obj.data.shape_keys.key_blocks.get(name) if obj and obj.data.shape_keys else None
 if required and (not key or key==obj.data.shape_keys.key_blocks[0]):raise ValueError('Choose a non-empty Shape Key: '+str(name))
 return key

def _delta(key):return _coords(key)-_coords(key.relative_key)
def _nonempty(key):return key and bool(np.any(np.abs(_delta(key))>1e-6))

def auto_detect(arm):
 from .avatar_export import mapping_for,normalized
 s=arm.anymodel_face;mapping=mapping_for(bpy.context.scene,arm);s.head=mapping.get('Head',s.head)
 aliases={'left':['LeftEye','EyeTrack_L','Eye_L','Eye.L','eye_left','J_Adj_L_FaceEye','左目'],'right':['RightEye','EyeTrack_R','Eye_R','Eye.R','eye_right','J_Adj_R_FaceEye','右目']}
 for side,names in aliases.items():
  if getattr(s,side) in arm.data.bones:continue
  for name in names:
   exact=arm.data.bones.get(name)
   if exact:setattr(s,side,exact.name);break
   norm=normalized(name)
   if norm:
    found=[b for b in arm.data.bones if normalized(b.name)==norm]
    if len(found)==1:setattr(s,side,found[0].name);break
 meshes=[o for o in bpy.context.scene.objects if o.type=='MESH' and o.find_armature()==arm and o.data.shape_keys]
 def score(o):
  names={k.name.lower() for k in o.data.shape_keys.key_blocks}
  return sum(n in names for n in ['vrc.v_aa','vrc.v_oh','vrc.v_ch','a mouth','o mouth','close eyes','blink'])+10*bool(re.search('head|face|顔',o.name,re.I))
 if not s.face_mesh and meshes:s.face_mesh=max(meshes,key=score)
 if not s.eyelid_mesh:s.eyelid_mesh=s.face_mesh
 for prop,aliases in {'mouth_a':['vrc.v_aa','A Mouth','あ','Ah','A'],'mouth_o':['vrc.v_oh','O Mouth','お','Oh','O'],
  'mouth_ch':['vrc.v_ch','CH','Ch','I Mouth','い'],'blink':['Close Eyes','blink','まばたき','EYE CLOSED','vrc.blink'],
  'blink_left':['Close Eyes L','blink_left','ウィンク','EYE CLOSED L'],'blink_right':['Close Eyes R','blink_right','ウィンク右','EYE CLOSED R']}.items():
  obj=s.face_mesh if prop.startswith('mouth') else s.eyelid_mesh
  if not obj:continue
  old=_key(obj,getattr(s,prop),False)
  if _nonempty(old):continue
  for name in aliases:
   key=_key(obj,name,False)
   if _nonempty(key):setattr(s,prop,key.name);break
 return dict(left=s.left,right=s.right,mesh=s.face_mesh.name if s.face_mesh else '')

def generate_visemes(arm):
 stop_preview();s=arm.anymodel_face;obj=_mesh(arm)
 if obj.mode=='EDIT':raise ValueError('Exit Edit Mode before creating Shape Keys.')
 keys=obj.data.shape_keys.key_blocks
 inputs=[_key(obj,getattr(s,p)) for p in ('mouth_a','mouth_o','mouth_ch')]
 for key in inputs:
  if not _nonempty(key):raise ValueError('The source key does not change the geometry: '+key.name)
 if len({k.name for k in inputs})<3:raise ValueError('Choose three different keys: A, O, and CH.')
 base=_coords(keys[0]);deltas=[_delta(k).copy() for k in inputs];generated=json.loads(arm.get('anymodel_face_generated','[]'));owned={(v['mesh'],v['key']) for v in generated};sources={k.name for k in inputs}
 output={'vrc.v_'+name:base+sum((delta*float(weight)*s.intensity for delta,weight in zip(deltas,mix)),np.zeros_like(base)) for name,mix in VISEMES.items()}
 if s.mmd_aliases:
  for jp,viseme in MMD_VOWELS.items():output[jp]=output['vrc.v_'+viseme].copy()
 created=[];updated=[];skipped=[]
 for name,coords in output.items():
  old=keys.get(name)
  if old and (name in sources or not (s.overwrite or (obj.name,name) in owned)):skipped.append(name);continue
  key=old or obj.shape_key_add(name=name,from_mix=False);key.relative_key=keys[0];key.data.foreach_set('co',coords.ravel());key.slider_min=0;key.slider_max=1
  (updated if old else created).append(name);owned.add((obj.name,name))
 arm['anymodel_face_generated']=json.dumps([dict(mesh=m,key=k) for m,k in sorted(owned)],ensure_ascii=False)
 if s.mmd_aliases:
  eye=s.eyelid_mesh or obj
  for name,source in [('まばたき',s.blink),('ウィンク',s.blink_left),('ウィンク右',s.blink_right)]:
   key=_key(eye,source,False)
   if not _nonempty(key) or name==source:continue
   old=eye.data.shape_keys.key_blocks.get(name)
   if old and not(s.overwrite or (eye.name,name) in owned):continue
   coords=_coords(eye.data.shape_keys.key_blocks[0])+_delta(key)
   dest=old or eye.shape_key_add(name=name,from_mix=False);dest.relative_key=eye.data.shape_keys.key_blocks[0];dest.data.foreach_set('co',coords.ravel());owned.add((eye.name,name))
  arm['anymodel_face_generated']=json.dumps([dict(mesh=m,key=k) for m,k in sorted(owned)],ensure_ascii=False)
 bpy.context.view_layer.update();return dict(created=created,updated=updated,skipped=skipped)

def capture_neutral(arm):
 s=arm.anymodel_face;was_preview=_PREVIEW is not None and _PREVIEW.arm==arm
 if s.head not in arm.pose.bones:raise ValueError('Assign the head bone.')
 head=arm.pose.bones[s.head];head_rest=arm.data.bones[s.head].matrix_local.to_quaternion();captured={}
 for side in ('left','right'):
  if getattr(s,side) not in arm.pose.bones:raise ValueError('Assign both eye bones.')
  bone=arm.pose.bones[getattr(s,side)];relative=head.matrix.to_quaternion().inverted()@bone.matrix.to_quaternion()
  neutral=bone.bone.matrix_local.to_quaternion().inverted()@head_rest@relative;neutral.normalize();captured[side]=tuple(neutral)
 stop_preview()
 for side,neutral in captured.items():setattr(s,side+'_neutral',neutral)
 s.test_yaw=s.test_pitch=0
 if was_preview:start_preview(arm)

def _head_frame(arm,head):
 from .avatar_export import mapping_for
 mapping=mapping_for(bpy.context.scene,arm)
 missing=[r for r in ('RightUpperArm','LeftUpperArm','Head','Hips') if r not in mapping]
 if missing:raise ValueError('Assign the humanoid bones first: '+', '.join(missing))
 right=(arm.data.bones[mapping['RightUpperArm']].head_local-arm.data.bones[mapping['LeftUpperArm']].head_local).normalized()
 up=(arm.data.bones[mapping['Head']].head_local-arm.data.bones[mapping['Hips']].head_local);up-=right*up.dot(right);up.normalize()
 return right,up

def preview_eye_rotation(arm,yaw,pitch):
 s=arm.anymodel_face
 if s.head not in arm.pose.bones or s.left not in arm.pose.bones or s.right not in arm.pose.bones:raise ValueError('Assign the head and both eye bones.')
 if s.left==s.right:raise ValueError('The left and right eyes must use different bones.')
 head=arm.pose.bones[s.head];frame=head.matrix.to_quaternion()@head.bone.matrix_local.to_quaternion().inverted();right,up=_head_frame(arm,head)
 # Positive yaw is avatar-right; positive pitch is up, independent of bone rolls.
 delta=Quaternion(frame@up,-yaw)@Quaternion(frame@right,pitch)
 for side in ('left','right'):
  pb=arm.pose.bones[getattr(s,side)];location=pb.matrix.translation.copy();scale=pb.matrix.to_scale();neutral=Quaternion(getattr(s,side+'_neutral'))
  rotation=delta@frame@pb.bone.matrix_local.to_quaternion()@neutral
  pb.matrix=Matrix.LocRotScale(location,rotation,scale)

class Preview:
 def __init__(self,arm):
  self.arm=arm;self.start=time.monotonic();self.pose_position=arm.data.pose_position;self.bones={};self.shapes={};self.drivers=[]
  s=arm.anymodel_face
  for side in ('left','right'):
   pb=arm.pose.bones.get(getattr(s,side))
   if pb:self.bones[pb.name]=(pb.matrix_basis.copy(),pb.rotation_mode)
  for obj in {s.face_mesh,s.eyelid_mesh}-{None}:
   if not obj.data.shape_keys:continue
   for key in obj.data.shape_keys.key_blocks:self.shapes[(obj.name,key.name)]=key.value
   ad=obj.data.shape_keys.animation_data
   if ad:
    for driver in ad.drivers:self.drivers.append((driver,driver.mute));driver.mute=True
  arm.data.pose_position='POSE'
 def apply(self):
  arm=self.arm;s=arm.anymodel_face;t=time.monotonic()-self.start
  yaw=math.radians(s.test_yaw);pitch=math.radians(s.test_pitch);blink=s.test_blink
  if s.test_animate:yaw=math.sin(t*1.2)*math.radians(s.max_yaw);pitch=math.sin(t*.83)*math.radians(s.max_up);phase=t%3.5;blink=max(blink,max(0.,1-abs(phase-.15)/.15))
  if s.left and s.right:preview_eye_rotation(arm,yaw,pitch)
  eye=s.eyelid_mesh or s.face_mesh
  combined=_key(eye,s.blink,False)
  for name in (s.blink,s.blink_left,s.blink_right):
   key=_key(eye,name,False)
   if key:key.value=blink if not combined or name==s.blink else 0
  obj=s.face_mesh
  if obj and obj.data.shape_keys:
   mouth_names={'vrc.v_'+n for n in VISEMES}|set(MMD_VOWELS)|{s.mouth_a,s.mouth_o,s.mouth_ch}
   for name in mouth_names:
    key=obj.data.shape_keys.key_blocks.get(name)
    if key:key.value=0
   if s.test_viseme!='NONE':
    name='vrc.v_'+s.test_viseme;key=obj.data.shape_keys.key_blocks.get(name)
    if key and _nonempty(key):key.value=s.test_mouth
    else:
     for source,value in zip((s.mouth_a,s.mouth_o,s.mouth_ch),VISEMES[s.test_viseme]):
      key=obj.data.shape_keys.key_blocks.get(source)
      if key:key.value=float(value)*s.test_mouth*s.intensity
  bpy.context.view_layer.update()
 def restore(self):
  for name,(basis,mode) in self.bones.items():
   pb=self.arm.pose.bones.get(name)
   if pb:pb.rotation_mode=mode;pb.matrix_basis=basis
  for (obj,name),value in self.shapes.items():
   mesh=bpy.data.objects.get(obj);key=_key(mesh,name,False)
   if key:key.value=value
  for driver,mute in self.drivers:
   try:driver.mute=mute
   except ReferenceError:pass
  self.arm.data.pose_position=self.pose_position;bpy.context.view_layer.update()

def _timer():
 if _PREVIEW is None:return None
 try:_PREVIEW.apply();return 1/30
 except (ReferenceError,RuntimeError,ValueError):stop_preview();return None

def start_preview(arm):
 global _PREVIEW
 stop_preview()
 if arm.mode=='EDIT':raise ValueError('Exit Edit Mode before testing the eyes.')
 _PREVIEW=Preview(arm)
 try:_PREVIEW.apply()
 except Exception:stop_preview();raise
 bpy.app.timers.register(_timer,first_interval=1/30)

def stop_preview(*_):
 global _PREVIEW
 old=_PREVIEW;_PREVIEW=None
 if bpy.app.timers.is_registered(_timer):bpy.app.timers.unregister(_timer)
 if old:
  try:old.restore()
  except ReferenceError:pass

def _update(self,context):
 if _PREVIEW and _PREVIEW.arm==self.id_data:
  try:_PREVIEW.apply()
  except (RuntimeError,ValueError):pass

def _mesh_poll(self,obj):return obj.type=='MESH'
class ANYMODEL_PG_face(bpy.types.PropertyGroup):
 face_mesh:PointerProperty(name='Face mesh',type=bpy.types.Object,poll=_mesh_poll)
 eyelid_mesh:PointerProperty(name='Eyelids mesh',type=bpy.types.Object,poll=_mesh_poll)
 head:StringProperty(name='Head')
 left:StringProperty(name='Left eye')
 right:StringProperty(name='Right eye')
 left_neutral:FloatVectorProperty(size=4,default=(1,0,0,0))
 right_neutral:FloatVectorProperty(size=4,default=(1,0,0,0))
 blink:StringProperty(name='Both eyes closed')
 blink_left:StringProperty(name='Left blink')
 blink_right:StringProperty(name='Right blink')
 mouth_a:StringProperty(name='Mouth A')
 mouth_o:StringProperty(name='Mouth O')
 mouth_ch:StringProperty(name='Mouth CH')
 intensity:FloatProperty(name='Viseme strength',default=1,min=0,max=2)
 overwrite:BoolProperty(name='Replace existing non-generated keys',default=False)
 mmd_aliases:BoolProperty(name='Create Japanese MMD keys too',default=True)
 max_yaw:FloatProperty(name='Left / right limit',default=12,min=0,max=45)
 max_up:FloatProperty(name='Up limit',default=8,min=0,max=35)
 max_down:FloatProperty(name='Down limit',default=10,min=0,max=35)
 auto_look:BoolProperty(name='Automatic eye look',default=True)
 auto_blink:BoolProperty(name='Automatic blinking',default=True)
 test_yaw:FloatProperty(name='Test left / right',default=0,min=-45,max=45,update=_update)
 test_pitch:FloatProperty(name='Test up / down',default=0,min=-35,max=35,update=_update)
 test_blink:FloatProperty(name='Test blink',default=0,min=0,max=1,update=_update)
 test_animate:BoolProperty(name='Live look / blink test',default=False,update=_update)
 test_viseme:EnumProperty(name='Test viseme',items=[('NONE','None','')]+[(n,n.upper(),'') for n in VISEMES],default='NONE',update=_update)
 test_mouth:FloatProperty(name='Mouth amount',default=1,min=0,max=1,update=_update)

class ANYMODEL_OT_face(bpy.types.Operator):
 bl_idname='anymodel.face';bl_label='AnyModel Face Tools';bl_options={'REGISTER','UNDO'}
 operation:EnumProperty(items=[('DETECT','Detect eyes / shapes',''),('GENERATE','Generate 15 visemes + MMD',''),('CAPTURE','Capture current eyes as forward',''),('START','Start face test',''),('STOP','Stop test / restore','')])
 def execute(self,context):
  try:
   arm=_arm(context)
   if self.operation=='DETECT':result=auto_detect(arm)
   elif self.operation=='GENERATE':result=generate_visemes(arm)
   elif self.operation=='CAPTURE':capture_neutral(arm);result='Forward pose saved'
   elif self.operation=='START':start_preview(arm);result='Face test started'
   else:stop_preview();result='Original pose and shape values restored'
   self.report({'INFO'},str(result));return {'FINISHED'}
  except Exception as error:self.report({'ERROR'},str(error));return {'CANCELLED'}

class ANYMODEL_PT_face(bpy.types.Panel):
 bl_label='AnyModel Face: Eyes / Mouth';bl_idname='ANYMODEL_PT_face';bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='AnyModel'
 def draw(self,context):
  layout=self.layout
  try:arm=_arm(context)
  except ValueError:layout.label(text='Select a humanoid armature or bound mesh.');return
  s=arm.anymodel_face
  layout.label(text='Native AnyModel tools — CATS is not required.')
  op=layout.operator('anymodel.face',text='Detect eyes and face shapes',icon='VIEWZOOM');op.operation='DETECT'
  box=layout.box();box.label(text='Eye Look')
  for prop in ('head','left','right'):box.prop_search(s,prop,arm.data,'bones')
  box.prop(s,'max_yaw');row=box.row(align=True);row.prop(s,'max_up');row.prop(s,'max_down')
  box.prop(s,'auto_look');box.prop(s,'auto_blink');box.operator('anymodel.face',text='Use current eye pose as forward').operation='CAPTURE'
  box.prop(s,'eyelid_mesh')
  eye=s.eyelid_mesh or s.face_mesh
  if eye and eye.data.shape_keys:
   for prop in ('blink','blink_left','blink_right'):box.prop_search(s,prop,eye.data.shape_keys,'key_blocks')
  box=layout.box();box.label(text='Mouth: 3 shapes → 15 visemes + MMD');box.prop(s,'face_mesh')
  if s.face_mesh and s.face_mesh.data.shape_keys:
   for prop in ('mouth_a','mouth_o','mouth_ch'):box.prop_search(s,prop,s.face_mesh.data.shape_keys,'key_blocks')
  box.prop(s,'intensity');box.prop(s,'mmd_aliases');box.prop(s,'overwrite');box.operator('anymodel.face',text='Generate mouth shapes').operation='GENERATE'
  box=layout.box();box.label(text='Live preview (restored on stop)');row=box.row(align=True)
  row.operator('anymodel.face',text='Start').operation='START';row.operator('anymodel.face',text='Stop / Restore').operation='STOP'
  box.prop(s,'test_animate');box.prop(s,'test_yaw');box.prop(s,'test_pitch');box.prop(s,'test_blink');box.prop(s,'test_viseme');box.prop(s,'test_mouth')
  box.operator('anymodel.import_vmd_face',text='Import MMD mouth / blink / eyes (.vmd)')

def export_face(arm,names):
 from .morphs import target_id
 s=arm.anymodel_face
 if not s.face_mesh and not s.left and not s.right:return None
 def shape(obj,key):
  if not _nonempty(_key(obj,key,False)):return None
  mesh=str(obj.get('avatar_mesh',obj.name));return dict(id=target_id(mesh,key),mesh=mesh,key=key)
 eyes=[]
 for side in ('left','right'):
  bone=getattr(s,side)
  if bone in names:eyes.append(dict(side=side,bone=names[bone],neutral=list(getattr(s,side+'_neutral'))))
 return dict(version=1,head=names.get(s.head,''),eyes=eyes,limits=dict(yaw=s.max_yaw,up=s.max_up,down=s.max_down),
  autoLook=s.auto_look,autoBlink=s.auto_blink,
  blink=shape(s.eyelid_mesh or s.face_mesh,s.blink),blinkLeft=shape(s.eyelid_mesh or s.face_mesh,s.blink_left),blinkRight=shape(s.eyelid_mesh or s.face_mesh,s.blink_right),
  visemes={name:ref for name in VISEMES if (ref:=shape(s.face_mesh,'vrc.v_'+name))})

CLASSES=(ANYMODEL_PG_face,ANYMODEL_OT_face,ANYMODEL_PT_face)
def register():
 for cls in CLASSES:bpy.utils.register_class(cls)
 bpy.types.Object.anymodel_face=PointerProperty(type=ANYMODEL_PG_face)
 if stop_preview not in bpy.app.handlers.load_pre:bpy.app.handlers.load_pre.append(stop_preview)
 from . import face_vmd
 face_vmd.register()
def unregister():
 stop_preview()
 from . import face_vmd
 face_vmd.unregister()
 if stop_preview in bpy.app.handlers.load_pre:bpy.app.handlers.load_pre.remove(stop_preview)
 del bpy.types.Object.anymodel_face
 for cls in reversed(CLASSES):bpy.utils.unregister_class(cls)
