"""Transient viewport preview of AnyModel's 60 Hz secondary-bone solver.

Original pose channels, meshes, weights and animation data are never edited.
The copied armature/affected meshes are removed before save, undo and export.
"""
import bpy
import json,math,time
from mathutils import Matrix,Vector,Quaternion
from bpy.props import EnumProperty
from bpy.app.handlers import persistent

STEP=1/60
_session=None
_error=''

def running(arm=None):
    try:return _session is not None and (arm is None or _session.arm==arm)
    except ReferenceError:return False

def error():return _error

def dirty(arm):
    if running(arm):_session.changed=time.monotonic()

def _signature(arm):
    from .bone_physics import CHAIN_FIELDS,COLLIDER_FIELDS
    return (tuple(tuple(str(getattr(c,k)) for k in ('root','ignore')+CHAIN_FIELDS) for c in arm.avatar_physics_chains),
            tuple(tuple(str(getattr(c,k)) for k in ('name','bone')+COLLIDER_FIELDS) for c in arm.avatar_physics_colliders),
            arm.avatar_physics_pin_enabled,arm.avatar_physics_pin_mesh.name if arm.avatar_physics_pin_mesh else '',
            arm.avatar_physics_pin_depth,arm.avatar_physics_pin_feather)

def constrain(point,pivot,rest,length,angle):
    direction=point-pivot
    if direction.length_squared<1e-12:direction=rest.copy()
    else:direction.normalize()
    dot=max(-1,min(1,direction.dot(rest)))
    if math.acos(dot)>angle:
        tangent=direction-rest*dot
        if tangent.length_squared<1e-10:
            tangent=Vector((1,0,0)) if abs(rest.x)<.8 else Vector((0,1,0))
            tangent-=rest*tangent.dot(rest)
        tangent.normalize();direction=rest*math.cos(angle)+tangent*math.sin(angle)
    return pivot+direction*length

def collide(point,a,b,radius,fallback):
    axis=b-a;length=axis.length_squared
    t=max(0,min(1,(point-a).dot(axis)/length)) if length>1e-12 else 0
    centre=a+axis*t;delta=point-centre
    if delta.length_squared>=radius*radius:return point
    if delta.length_squared<1e-12:
        delta=fallback.copy()
        if length>1e-12:
            delta-=axis*(delta.dot(axis)/length)
            if delta.length_squared<1e-12:
                delta=Vector((1,0,0)) if abs(axis.x)<abs(axis.y) else Vector((0,1,0))
                delta-=axis*(delta.dot(axis)/length)
        if delta.length_squared<1e-12:delta=Vector((1,0,0))
    return centre+delta.normalized()*radius

class Solver:
    """Same integration, cone/length constraints and collision order as BonePhysics.java."""
    def __init__(self,arm,context):
        from .bone_physics import chain_bones,local_height,_names,motion_at,physics_tip
        from .avatar_export import mapping_for,SPACE_KEY
        self.arm=arm;self.bones=sorted(arm.data.bones,key=lambda b:len(b.parent_recursive))
        self.ids={b.name:i for i,b in enumerate(self.bones)};self.links=[];self.colliders=[]
        self.unit=float(json.loads(arm.get(SPACE_KEY,'{}')).get('unitScale',context.scene.avatar_model_height*100/local_height(arm)))
        if not math.isfinite(self.unit) or self.unit<=0:raise ValueError('Invalid avatar export scale; rebuild the avatar')
        protected=set(mapping_for(context.scene,arm).values());used=set();collider_ids={}
        for c in arm.avatar_physics_colliders:
            if not c.enabled:continue
            if c.bone not in self.ids:raise ValueError('Missing collider bone: '+c.bone)
            if c.name in collider_ids:raise ValueError('Duplicate collider name: '+c.name)
            collider_ids[c.name]=len(self.colliders);self.colliders.append(c)
        for c in arm.avatar_physics_chains:
            if not c.enabled:continue
            selected=chain_bones(arm,c)
            if not selected:raise ValueError('Missing/empty chain: '+c.name)
            depth=len(arm.data.bones[c.root].parent_recursive)
            maxdepth=max(len(b.parent_recursive)-depth for b in selected)
            dynamic=[b for b in selected if len(b.parent_recursive)-depth>=c.anchor_levels]
            if not dynamic:raise ValueError('All bones anchored: '+c.name)
            names=_names(c.colliders)
            if any(n not in collider_ids for n in names):raise ValueError('Unknown collider in chain '+c.name)
            indices=[collider_ids[n] for n in names] if names else list(range(len(self.colliders)))
            if not c.collision:indices=[]
            for bone in dynamic:
                subtree=(bone,*bone.children_recursive)
                if bone.name in used:raise ValueError('Overlapping chains: '+bone.name)
                if protected.intersection(b.name for b in subtree):raise ValueError('Humanoid bones cannot be simulated: '+bone.name)
                used.add(bone.name)
                motion=motion_at(c,len(bone.parent_recursive)-depth,maxdepth)
                self.links.append(dict(bone=self.ids[bone.name],tip=physics_tip(bone,{b.name for b in selected}),c=c,
                    motion=motion,colliders=indices,desc=[self.ids[b.name] for b in subtree],
                    damping=math.exp(-(32-30*c.spring*motion)*STEP),force=(1-(1-c.pull)*motion)*625*STEP*STEP,
                    stiffness=1-(1-c.stiffness)*motion,gravity=c.gravity*motion*STEP*STEP,
                    falloff=c.falloff,immobile=c.immobile,radius=c.radius,angle=c.angle*motion))
        if not self.links:raise ValueError('Add at least one enabled physics chain')
        affected={i for link in self.links for i in link['desc']}
        if any(self.ids[c.bone] in affected for c in self.colliders):raise ValueError('Attach colliders outside simulated chains')
        if len(self.links)>256 or len(self.colliders)>64 or sum(c.enabled for c in arm.avatar_physics_chains)>64 or sum(len(l['colliders']) for l in self.links)>4096:raise ValueError('Physics budget exceeded')
        self.links.sort(key=lambda l:l['bone']);self.affected={self.bones[i].name for i in affected}
        self.current=[];self.previous=[];self.rendered=[Quaternion() for _ in self.links];self.render_blend=1;self.last_root=None;self.last_time=None;self.accumulator=0;self.steps=0;self.resets=0

    def apply(self,animated,root,now):
        dt=0 if self.last_time is None else now-self.last_time
        # Blender throttles inactive windows and modal operations can delay timers.
        # Keep bounded catch-up through these short gaps instead of resetting every redraw.
        reset=self.last_root is None or dt<0 or dt>1.5
        if self.last_root is not None:
            reset=reset or (root.translation-self.last_root.translation).length*self.unit>800
            reset=reset or any(abs(a-b)>max(1e-6,abs(b)*.02) for a,b in zip(root.to_scale(),self.last_root.to_scale()))
        if reset:
            self.current=[animated[l['bone']]@l['tip'] for l in self.links]
            self.previous=[v.copy() for v in self.current];self.rendered=[Quaternion() for _ in self.links];self.accumulator=0;self.resets+=1
        else:
            transport=root.inverted()@self.last_root
            for i,l in enumerate(self.links):
                inertia=1-l['immobile']
                self.current[i]=self.current[i].lerp(transport@self.current[i],inertia)
                self.previous[i]=self.previous[i].lerp(transport@self.previous[i],inertia)
            self.accumulator+=min(.1,dt)
        self.render_blend=1 if reset else -math.expm1(-max(0,dt)/.04)
        gravity=(root.to_3x3().inverted()@Vector((0,0,-1))).normalized()*(980/self.unit)
        inverse=root.inverted();self.frame_colliders=[];root_scale=max(root.to_scale())
        for c in self.colliders:
            matrix=animated[self.ids[c.bone]]
            a=inverse@c.start_object.matrix_world.translation if c.start_object else matrix@Vector(c.offset)
            b=(inverse@c.end_object.matrix_world.translation if c.end_object else matrix@Vector(c.tail)) if c.shape=='capsule' else a
            radius=max(c.start_object.matrix_world.to_scale())/root_scale if c.start_object else c.radius*max(matrix.to_scale())
            self.frame_colliders.append((a,b,radius))
        count=0
        while self.accumulator>=STEP and count<6:
            self._pose(list(animated),root,gravity,True,1)
            self.accumulator-=STEP;self.steps+=1;count+=1
        self.accumulator=max(0,min(STEP,self.accumulator))
        result=self._pose(list(animated),root,gravity,False,self.accumulator/STEP)
        self.last_root=root.copy();self.last_time=now
        return result

    def _pose(self,matrices,root,gravity,integrate,alpha):
        for i,l in enumerate(self.links):
            matrix=matrices[l['bone']];pivot=matrix.translation.copy();target=matrix@l['tip']
            rest=target-pivot;length=rest.length
            if length<1e-8:continue
            rest/=length
            if integrate:
                result=self.current[i]+(self.current[i]-self.previous[i])*l['damping']
                direction=self.current[i]-pivot;alignment=direction.normalized().dot(rest) if direction.length_squared>1e-12 else 1
                falloff=1-l['falloff']*max(0,alignment)
                result+=(target-self.current[i])*l['force']+gravity*(l['gravity']*falloff)
                result=result.lerp(target,l['stiffness'])
            else:result=self.previous[i].lerp(self.current[i],alpha)
            result=constrain(result,pivot,rest,length,l['angle'])
            radius=l['radius']*max(matrix.to_scale())
            for _ in range(4 if l['colliders'] else 0):
                for index in l['colliders']:
                    a,b,r=self.frame_colliders[index];result=collide(result,a,b,r+radius,rest)
                result=constrain(result,pivot,rest,length,l['angle'])
            if not all(math.isfinite(v) for v in result):result=target
            if integrate:self.previous[i]=self.current[i].copy();self.current[i]=result.copy()
            turn=rest.rotation_difference((result-pivot).normalized())
            if not integrate:
                basis=matrix.to_quaternion();inverse=basis.conjugated()
                local=inverse@turn@basis
                self.rendered[i]=self.rendered[i].slerp(local,self.render_blend).normalized()
                turn=basis@self.rendered[i]@inverse
                result=constrain(pivot+(turn@rest)*length,pivot,rest,length,l['angle'])
                for _ in range(4 if l['colliders'] else 0):
                    for index in l['colliders']:
                        a,b,r=self.frame_colliders[index];result=collide(result,a,b,r+radius,rest)
                    result=constrain(result,pivot,rest,length,l['angle'])
                turn=rest.rotation_difference((result-pivot).normalized())
                self.rendered[i]=(inverse@turn@basis).normalized()
            turn=turn.to_matrix().to_4x4()
            transform=Matrix.Translation(pivot)@turn@Matrix.Translation(-pivot)
            for index in l['desc']:matrices[index]=transform@matrices[index]
        return matrices

class Preview:
    def __init__(self,context,arm):
        self.arm=arm;self.context=context;self.layer=context.view_layer;self.solver=Solver(arm,context)
        self.start_basis=arm.matrix_basis.copy()
        self.signature=_signature(arm);self.changed=0;self.next_signature_check=0;self.created=[];self.meshes=[];self.data=[]
        try:
            self.collection=bpy.data.collections.new('__AnyModel live physics (temporary)');context.scene.collection.children.link(self.collection)
            self.copy=arm.copy();self.copy.data=arm.data.copy();self.data.append(self.copy.data)
            self.copy.data.use_fake_user=False
            self.copy.data.pose_position='POSE'
            self.copy.name='__AnyModel physics rig';self.copy.parent=None;self.copy.animation_data_clear()
            self.collection.objects.link(self.copy);self.created.append(self.copy)
            self.copy['anymodel_preview']=True;self.copy.hide_render=True;self.copy.hide_select=True
            self.copy.data['anymodel_preview']=True
            for c in list(self.copy.constraints):self.copy.constraints.remove(c)
            for pb in self.copy.pose.bones:
                for c in list(pb.constraints):pb.constraints.remove(c)
            self.pose_targets=[(b,self.copy.pose.bones[b.name],self.solver.ids[b.parent.name] if b.parent else -1,b.matrix_local.copy(),b.parent.matrix_local.copy() if b.parent else None) for b in self.solver.bones]
            self.last_matrices=[None]*len(self.pose_targets)
            self.copy.hide_set(True,view_layer=self.layer)
            for source in list(context.scene.objects):
                if source.type!='MESH' or source.hide_get(view_layer=self.layer) or source.hide_viewport:continue
                bound=any(m.type=='ARMATURE' and m.object==arm for m in source.modifiers)
                if not bound or not self.solver.affected.intersection(g.name for g in source.vertex_groups):continue
                obj=source.copy();obj.parent=None;obj.animation_data_clear();obj.name='__AnyModel preview '+source.name
                self.collection.objects.link(obj);self.created.append(obj);obj['anymodel_preview']=True
                obj['anymodel_preview_source']=source;obj['anymodel_preview_source_hidden']=source.hide_get(view_layer=self.layer)
                obj['anymodel_preview_layer']=self.layer.name
                obj.hide_render=True;obj.hide_select=True
                for c in list(obj.constraints):obj.constraints.remove(c)
                for mod in obj.modifiers:
                    if mod.type=='ARMATURE' and mod.object==arm:mod.object=self.copy
                if arm.avatar_physics_pin_enabled and source==arm.avatar_physics_pin_mesh:self._scalp(source,obj)
                self.meshes.append((source,obj,source.hide_get(view_layer=self.layer)))
                source.hide_set(True,view_layer=self.layer)
            if not self.meshes:raise ValueError('No visible weighted mesh uses these physics bones')
            self.tick(time.monotonic())
        except Exception:self.close();raise

    def _scalp(self,source,obj):
        # Parallel armature deformation with one overall blend group; never edit source weights.
        obj.data=source.data.copy();obj.data.use_fake_user=False;self.data.append(obj.data);obj.data['anymodel_preview']=True
        group=obj.vertex_groups.new(name='__AnyModel preview motion')
        from .avatar_export import space_for,matrix12
        arm=self.arm;space=space_for(arm)
        if space and space.get('bones'):
            # Exported models may use native Y-up bones, arbitrary object roll or metre units.
            name=next(n for n in space['bones'] if n in arm.data.bones)
            model=matrix12(space['bones'][name]['global'])
            for row in range(3):
                for col in range(3):model[row][col]*=space['unitScale']
            projection=model@arm.data.bones[name].matrix_local.inverted()@arm.matrix_world.inverted()@source.matrix_world
            heights=[(projection@v.co).y for v in source.data.vertices];unit=space['unitScale']
        else:
            scale=max(arm.matrix_world.to_scale())
            heights=[(source.matrix_world@v.co).z/scale for v in source.data.vertices];unit=1
        top=max(heights);end=top-arm.avatar_physics_pin_depth*unit;start=end-arm.avatar_physics_pin_feather*unit
        if end-start<=1e-8:raise ValueError('Scalp feather must be positive')
        self.protected_indices=[]
        for v,z in zip(obj.data.vertices,heights):
            t=max(0,min(1,(z-start)/(end-start)));weight=1-t*t*(3-2*t)
            group.add([v.index],weight,'REPLACE')
            if weight==0:self.protected_indices.append(v.index)
        mods=[m for m in obj.modifiers if m.type=='ARMATURE' and m.object==self.copy]
        if len(mods)!=1:raise ValueError('Scalp preview needs one Armature modifier on its mesh')
        dynamic=mods[0];index=list(obj.modifiers).index(dynamic)
        base=obj.modifiers.new('__AnyModel animated scalp','ARMATURE');base.object=arm
        base.use_deform_preserve_volume=dynamic.use_deform_preserve_volume
        base.use_vertex_groups=dynamic.use_vertex_groups;base.use_bone_envelopes=dynamic.use_bone_envelopes
        obj.modifiers.move(len(obj.modifiers)-1,index)
        dynamic.use_multi_modifier=True;dynamic.vertex_group=group.name;dynamic.invert_vertex_group=False

    def tick(self,now):
        context=bpy.context;depsgraph=context.evaluated_depsgraph_get();evaluated=self.arm.evaluated_get(depsgraph)
        pose=evaluated.pose.bones;animated=[pose[b.name].matrix.copy() for b in self.solver.bones]
        root=evaluated.matrix_world.copy();result=self.solver.apply(animated,root,now)
        if self.copy.matrix_world!=root:self.copy.matrix_world=root
        changes=[last is None or last!=matrix for last,matrix in zip(self.last_matrices,result)]
        for i,((b,target,parent,rest,parent_rest),matrix) in enumerate(zip(self.pose_targets,result)):
            if not changes[i] and (parent<0 or not changes[parent]):continue
            args=dict(matrix=matrix,matrix_local=rest,invert=True)
            if parent>=0:args.update(parent_matrix=result[parent],parent_matrix_local=parent_rest)
            target.matrix_basis=b.convert_local_to_pose(**args)
        self.last_matrices=result
        for source,obj,_ in self.meshes:
            if obj.matrix_world!=source.matrix_world:obj.matrix_world=source.matrix_world.copy()
        for area in context.screen.areas if context.screen else []:
            if area.type=='VIEW_3D':area.tag_redraw()

    def reconfigure(self,context,signature):
        solver=Solver(self.arm,context);old=self.solver
        if solver.affected!=old.affected or signature[2:]!=self.signature[2:] or [(l['bone'],tuple(l['tip'])) for l in solver.links]!=[(l['bone'],tuple(l['tip'])) for l in old.links]:return False
        for key in ('current','previous','rendered','last_root','last_time','accumulator','steps','resets'):setattr(solver,key,getattr(old,key))
        self.solver=solver;self.signature=signature;self.changed=0
        return True

    def close(self):
        try:
            if self.arm.avatar_physics_preview_restore:self.arm.matrix_basis=self.start_basis
        except (ReferenceError,RuntimeError):pass
        for source,_,hidden in self.meshes:
            try:source.hide_set(hidden,view_layer=self.layer)
            except (ReferenceError,RuntimeError):pass
        for obj in reversed(self.created):
            try:bpy.data.objects.remove(obj,do_unlink=True)
            except (ReferenceError,RuntimeError):pass
        for data in self.data:
            try:
                if data.users==0:
                    (bpy.data.armatures if isinstance(data,bpy.types.Armature) else bpy.data.meshes).remove(data)
            except ReferenceError:pass
        if hasattr(self,'collection'):
            try:bpy.data.collections.remove(self.collection)
            except ReferenceError:pass

def start(context,arm,register_timer=True):
    global _session,_error
    stop();_error=''
    try:_session=Preview(context,arm)
    except Exception as ex:_error=str(ex);raise
    if register_timer and not bpy.app.timers.is_registered(_timer):bpy.app.timers.register(_timer,first_interval=STEP)
    return _session

def stop(unregister_timer=True):
    global _session
    old=_session;_session=None
    if unregister_timer and bpy.app.timers.is_registered(_timer):bpy.app.timers.unregister(_timer)
    if old:old.close()

def _timer():
    global _error,_session
    if _session is None:return None
    try:
        arm=_session.arm
        if arm.name not in bpy.context.scene.objects:stop(False);return None
        now=time.monotonic()
        if now>=_session.next_signature_check or _session.changed and now-_session.changed>.15:
            _session.next_signature_check=now+.25;signature=_signature(arm)
            if signature!=_session.signature:
                if not _session.changed:_session.changed=now
                if now-_session.changed>.15 and not _session.reconfigure(bpy.context,signature):
                    start_basis=_session.start_basis.copy();current_basis=arm.matrix_basis.copy()
                    stop(False);arm.matrix_basis=current_basis;_session=Preview(bpy.context,arm);_session.start_basis=start_basis
            else:_session.changed=0
        _session.tick(now)
    except Exception as ex:
        _error=str(ex);print('AnyModel preview stopped:',_error);stop(False);return None
    return STEP

@persistent
def _before_change(*args):
    stop();_cleanup_orphans()

@persistent
def _cleanup_orphans(*args):
    # Startup registration runs under Blender's RestrictBlend context. Scene
    # data is unavailable there; load_post/the deferred timer owns cleanup.
    if not hasattr(bpy.data,'objects'):return None
    for obj in list(bpy.data.objects):
        if not obj.get('anymodel_preview'):continue
        source=obj.get('anymodel_preview_source');layer=obj.get('anymodel_preview_layer','')
        if source:
            for scene in bpy.data.scenes:
                if layer in scene.view_layers and source.name in scene.objects:
                    try:source.hide_set(bool(obj.get('anymodel_preview_source_hidden',False)),view_layer=scene.view_layers[layer])
                    except RuntimeError:pass
        data=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
        if data and data.users==0 and data.get('anymodel_preview'):
            (bpy.data.armatures if isinstance(data,bpy.types.Armature) else bpy.data.meshes).remove(data)
    for c in list(bpy.data.collections):
        if c.name.startswith('__AnyModel live physics (temporary)') and not c.objects:bpy.data.collections.remove(c)

class AVATAR_OT_physics_preview(bpy.types.Operator):
    bl_idname='avatar_physics.preview'
    bl_label='AnyModel live physics preview'
    operation:EnumProperty(items=[('START','Start',''),('STOP','Stop',''),('RESET','Reset',''),('MOVE','Select avatar to move','')])
    def execute(self,context):
        global _error
        try:
            if self.operation=='STOP':stop();_error=''
            elif self.operation=='RESET':
                if _session:_session.solver.last_root=None
            elif self.operation=='MOVE':
                from .bone_physics import rig
                arm=rig(context)
                if context.object and context.object.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
                for obj in context.selected_objects:obj.select_set(False)
                arm.select_set(True);context.view_layer.objects.active=arm
            else:
                from .bone_physics import rig
                start(context,rig(context))
            return {'FINISHED'}
        except Exception as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}

def register():
    bpy.utils.register_class(AVATAR_OT_physics_preview)
    for handlers in (bpy.app.handlers.save_pre,bpy.app.handlers.load_pre,bpy.app.handlers.undo_pre,bpy.app.handlers.redo_pre):
        if _before_change not in handlers:handlers.append(_before_change)
    for handlers in (bpy.app.handlers.undo_post,bpy.app.handlers.redo_post,bpy.app.handlers.load_post):
        if _cleanup_orphans not in handlers:handlers.append(_cleanup_orphans)
    if not bpy.app.timers.is_registered(_cleanup_orphans):
        bpy.app.timers.register(_cleanup_orphans,first_interval=.1)

def unregister():
    stop()
    if bpy.app.timers.is_registered(_cleanup_orphans):bpy.app.timers.unregister(_cleanup_orphans)
    for handlers in (bpy.app.handlers.save_pre,bpy.app.handlers.load_pre,bpy.app.handlers.undo_pre,bpy.app.handlers.redo_pre):
        if _before_change in handlers:handlers.remove(_before_change)
    for handlers in (bpy.app.handlers.undo_post,bpy.app.handlers.redo_post,bpy.app.handlers.load_post):
        if _cleanup_orphans in handlers:handlers.remove(_cleanup_orphans)
    _cleanup_orphans()
    bpy.utils.unregister_class(AVATAR_OT_physics_preview)
