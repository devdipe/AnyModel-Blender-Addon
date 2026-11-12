"""Author secondary bone motion as data; never depends on Unity/VRChat binaries."""
import bpy
import json,zipfile,struct,os,shutil
from pathlib import Path
from bpy_extras.io_utils import ExportHelper
from bpy.props import (BoolProperty, StringProperty, FloatProperty, FloatVectorProperty,
                       EnumProperty, CollectionProperty, IntProperty, PointerProperty)
from mathutils import Matrix, Vector

CHAIN_FIELDS=('enabled','children','anchor_levels','base_motion','tip_motion','pull','spring',
              'stiffness','gravity','falloff','immobile','radius','angle','collision','colliders')
COLLIDER_FIELDS=('enabled','shape','radius','offset','tail')
_batch_busy=False


def selected_names(context, arm):
    if context.object!=arm or context.mode not in {'POSE','EDIT_ARMATURE'}:return set()
    bones=arm.data.edit_bones if context.mode=='EDIT_ARMATURE' else arm.data.bones
    return {b.name for b in bones if b.select}


def selected_items(context, arm, kind='chains'):
    collection=getattr(arm,'avatar_physics_'+kind)
    if arm.avatar_physics_selection=='LIST':return [c for c in collection if c.selected]
    names=selected_names(context,arm)
    if kind=='colliders':return [c for c in collection if c.bone in names]
    return [c for c in collection if c.root in names or c.children and c.root in arm.data.bones
            and any(c.root in {p.name for p in arm.data.bones[n].parent_recursive} for n in names if n in arm.data.bones)]


def _selected_changed(item,context):
    from . import physics_guides
    physics_guides.invalidate()
    if not _batch_busy:item.id_data.avatar_physics_selection='LIST'


def motion_at(c,depth,maximum):
    # A chain with a single movable link is its tip, not a second anchored base.
    if maximum<=c.anchor_levels:return c.tip_motion
    t=(depth-c.anchor_levels)/(maximum-c.anchor_levels)
    return c.base_motion+(c.tip_motion-c.base_motion)*t


def _changed(field, kind='chains', extra=None):
    def update(item, context):
        global _batch_busy
        if extra:extra(item,context)
        from . import physics_preview
        from . import physics_guides
        physics_guides.invalidate()
        physics_preview.dirty(item.id_data)
        if _batch_busy or not context or field not in (CHAIN_FIELDS if kind=='chains' else COLLIDER_FIELDS):return
        arm=item.id_data
        if not arm.avatar_physics_batch:return
        items=selected_items(context,arm,kind)
        try:
            _batch_busy=True
            for c in items:
                if c!=item:setattr(c,field,getattr(item,field))
        finally:_batch_busy=False
    return update


def chain_bones(arm, item):
    if item.root not in arm.data.bones:return []
    ignored=set(_names(item.ignore))
    return [b for b in arm.data.bones if (b.name==item.root or item.children and item.root in {p.name for p in b.parent_recursive})
            and not ignored.intersection(p.name for p in (b,*b.parent_recursive))]


def add_selected_chains(context, arm):
    global _batch_busy
    names=selected_names(context,arm)
    if not names:raise ValueError('Select bones in Pose Mode first')
    from .avatar_export import mapping_for
    protected=set(mapping_for(context.scene,arm).values())
    template=arm.avatar_physics_chains[min(arm.avatar_physics_chain_index,len(arm.avatar_physics_chains)-1)] if arm.avatar_physics_chains else None
    defaults={k:getattr(template,k) for k in CHAIN_FIELDS} if template else {}
    include=defaults.get('children',True)
    roots=[b for b in arm.data.bones if b.name in names and (not include or not names.intersection(p.name for p in b.parent_recursive))]
    existing={b.name for c in arm.avatar_physics_chains for b in chain_bones(arm,c)}
    count=skipped=0
    try:
        _batch_busy=True
        for bone in roots:
            subtree=(bone,*bone.children_recursive);covered={b.name for b in subtree} if include else {bone.name}
            if protected.intersection(b.name for b in subtree) or covered.intersection(existing):skipped+=1;continue
            item=arm.avatar_physics_chains.add();item.root=bone.name;item.name=bone.name[:80]
            for k,v in defaults.items():setattr(item,k,v)
            item.radius=defaults.get('radius',local_height(arm)*.004)
            depth=max(len(b.parent_recursive)-len(bone.parent_recursive) for b in subtree) if include else 0
            item.anchor_levels=min(defaults.get('anchor_levels',1),depth)
            item.selected=True
            arm.avatar_physics_chain_index=len(arm.avatar_physics_chains)-1;count+=1;existing.update(covered)
            if 'hair' in bone.name.lower() and not arm.avatar_physics_pin_enabled:
                mesh=next((o for o in context.scene.objects if o.type=='MESH' and 'hair' in o.name.lower() and o.find_armature()==arm),None)
                if mesh:arm.avatar_physics_pin_mesh=mesh;arm.avatar_physics_pin_enabled=True;arm.avatar_physics_pin_depth=local_height(arm)*.035;arm.avatar_physics_pin_feather=local_height(arm)*.02
    finally:_batch_busy=False
    return count,skipped


def rig(context):
    obj=context.active_object
    while obj:
        if obj.type=='ARMATURE':return obj
        obj=obj.parent
    from . import armature_for
    return armature_for(context)


def _radius_changed(item, context):
    arm=item.id_data
    for obj in (item.start_object,item.end_object):
        if obj:
            location,rotation,_=obj.matrix_world.decompose();radius=item.radius*arm.matrix_world.to_scale().x
            obj.matrix_world=Matrix.LocRotScale(location,rotation,Vector((radius,radius,radius)))


def _offset_changed(item, context):
    arm=item.id_data;bone=arm.pose.bones.get(item.bone) if arm.type=='ARMATURE' else None
    if bone:
        for obj,value in [(item.start_object,item.offset),(item.end_object,item.tail)]:
            if obj:obj.matrix_world.translation=arm.matrix_world @ bone.matrix @ Vector(value)


def selected_bone(context, arm):
    active = context.active_pose_bone or context.active_bone
    return active.name if active and active.name in arm.data.bones else ''


def local_height(arm):
    points=[arm.matrix_world @ point for bone in arm.data.bones for point in (bone.head_local,bone.tail_local)]
    return max(.001,(max(p.z for p in points)-min(p.z for p in points))/max(.000001,arm.matrix_world.to_scale().x))


def _world_centimetres(arm):return max(.000001,max(arm.matrix_world.to_scale())*100)
def _radius_cm_get(item):
    helper=getattr(item,'start_object',None)
    return max(helper.matrix_world.to_scale())*100 if helper else item.radius*_world_centimetres(item.id_data)
def _radius_cm_set(item,value):item.radius=value/_world_centimetres(item.id_data)


class AVATAR_PG_physics_chain(bpy.types.PropertyGroup):
    name: StringProperty(name='Name', default='Hair')
    selected:BoolProperty(name='Select chain',description='Mark this chain for batch editing; independent of Enable',default=False,update=_selected_changed)
    enabled: BoolProperty(name='Enabled', default=True)
    root: StringProperty(name='Root bone')
    children: BoolProperty(name='Include descendants', default=True)
    anchor_levels: IntProperty(name='Anchored levels',description='Keep these levels at the chain base exactly animated; 0 allows a single-bone chain',default=1,min=0,max=16)
    base_motion: FloatProperty(name='Motion at base',default=.65,min=0,max=1)
    tip_motion: FloatProperty(name='Motion at tip',default=1,min=0,max=1)
    ignore: StringProperty(name='Ignore subtrees', description='Bone names separated by semicolons')
    pull: FloatProperty(name='Pull', description='Return toward the animated pose', default=.25, min=0, max=1)
    spring: FloatProperty(name='Spring', description='Higher values keep oscillations longer', default=.2, min=0, max=1)
    stiffness: FloatProperty(name='Stiffness', description='Directly preserve the animated direction', default=.04, min=0, max=1)
    gravity: FloatProperty(name='Gravity', description='Downward force; negative values rise', default=0, min=-1, max=1)
    falloff: FloatProperty(name='Gravity falloff', description='Reduce gravity near the animated pose', default=.8, min=0, max=1)
    immobile: FloatProperty(name='Follow avatar motion', description='0: inertia from avatar motion; 1: move with avatar', default=.08, min=0, max=1)
    radius: FloatProperty(name='Bone radius', default=.007, min=0, max=1000, subtype='DISTANCE')
    radius_cm:FloatProperty(name='Tip radius (cm)',description='Actual collision sphere radius in Blender world centimetres, including the Armature object scale',get=_radius_cm_get,set=_radius_cm_set,min=0,max=100000,precision=3)
    angle: FloatProperty(name='Max angle', default=.69813170, min=0, max=3.14159265, subtype='ANGLE')
    collision: BoolProperty(name='Collisions', default=True)
    colliders: StringProperty(name='Collider names', description='Semicolon-separated names; blank selects all enabled colliders')


class AVATAR_PG_physics_collider(bpy.types.PropertyGroup):
    name: StringProperty(name='Name', default='Head')
    selected:BoolProperty(name='Select collider',description='Mark for batch editing',default=False,update=_selected_changed)
    enabled: BoolProperty(name='Enabled', default=True)
    bone: StringProperty(name='Attached bone')
    shape: EnumProperty(name='Shape', items=[('sphere','Sphere','One centre'),('capsule','Capsule','Two centres')])
    radius: FloatProperty(name='Radius', default=.08, min=.00001, max=1000, subtype='DISTANCE',update=_radius_changed)
    radius_cm:FloatProperty(name='Collider radius (cm)',description='Actual world radius in centimetres; changing this also resizes existing wire gizmos',get=_radius_cm_get,set=_radius_cm_set,min=.00001,max=100000,precision=3)
    offset: FloatVectorProperty(name='Centre', subtype='TRANSLATION', size=3,update=_offset_changed)
    tail: FloatVectorProperty(name='Capsule end', subtype='TRANSLATION', size=3, default=(0,.15,0),update=_offset_changed)
    start_object: PointerProperty(name='Start gizmo', type=bpy.types.Object)
    end_object: PointerProperty(name='End gizmo', type=bpy.types.Object)


class AVATAR_UL_physics(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.prop(item,'selected',text='')
        op=layout.operator('avatar_physics.row_select',text=item.name,emboss=False)
        op.kind='chains' if 'chain_' in active_propname else 'colliders';op.index=index
        layout.prop(item,'enabled',text='',icon='HIDE_OFF' if item.enabled else 'HIDE_ON',emboss=False)


def select_rows(arm,kind,index,ctrl=False,shift=False):
    global _batch_busy
    rows=getattr(arm,'avatar_physics_'+kind)
    prefix='avatar_physics_'+('chain' if kind=='chains' else 'collider')
    if not 0<=index<len(rows):raise ValueError('Select an existing row')
    anchor=min(getattr(arm,prefix+'_anchor'),len(rows)-1)
    try:
        _batch_busy=True
        if shift:
            if not ctrl:
                for item in rows:item.selected=False
            for i in range(min(anchor,index),max(anchor,index)+1):rows[i].selected=True
        elif ctrl:rows[index].selected=not rows[index].selected
        else:
            for i,item in enumerate(rows):item.selected=i==index
        if not shift:setattr(arm,prefix+'_anchor',index)
        setattr(arm,prefix+'_index',index);arm.avatar_physics_selection='LIST'
    finally:_batch_busy=False


class AVATAR_OT_physics_row_select(bpy.types.Operator):
    bl_idname='avatar_physics.row_select'
    bl_label='Select physics row (Ctrl: toggle, Shift: range)'
    kind:EnumProperty(items=[('chains','Chains',''),('colliders','Colliders','')])
    index:IntProperty(min=0)
    def invoke(self,context,event):
        select_rows(rig(context),self.kind,self.index,event.ctrl or event.oskey,event.shift)
        return {'FINISHED'}
    def execute(self,context):
        select_rows(rig(context),self.kind,self.index);return {'FINISHED'}


class AVATAR_OT_physics_selection(bpy.types.Operator):
    bl_idname='avatar_physics.selection'
    bl_label='Select physics rows'
    kind:EnumProperty(items=[('chains','Chains',''),('colliders','Colliders','')])
    operation:EnumProperty(items=[('ALL','All',''),('NONE','None',''),('INVERT','Invert',''),('POSE','From Pose selection','')])
    def execute(self,context):
        global _batch_busy
        arm=rig(context);rows=getattr(arm,'avatar_physics_'+self.kind)
        try:
            _batch_busy=True
            previous=arm.avatar_physics_selection;arm.avatar_physics_selection='POSE'
            pose=selected_items(context,arm,self.kind)
            arm.avatar_physics_selection='LIST'
            for item in rows:
                item.selected=self.operation=='ALL' or self.operation=='INVERT' and not item.selected or self.operation=='POSE' and any(item==p for p in pose)
            return {'FINISHED'}
        finally:_batch_busy=False


class AVATAR_OT_physics_response(bpy.types.Operator):
    bl_idname='avatar_physics.response'
    bl_label='Responsive motion preset'
    bl_description='Make the selected chains respond more visibly; retain roots, colliders, ignore lists and scalp protection'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        global _batch_busy
        arm=rig(context);targets=selected_items(context,arm)
        if not targets:self.report({'ERROR'},'Select chains in the list first');return {'CANCELLED'}
        try:
            _batch_busy=True
            for c in targets:
                c.base_motion=.65;c.tip_motion=1;c.pull=.25;c.spring=.2;c.stiffness=.04;c.angle=.69813170;c.immobile=.08
                bones=chain_bones(arm,c)
                maximum=max((len(b.parent_recursive)-len(arm.data.bones[c.root].parent_recursive) for b in bones),default=0)
                c.anchor_levels=min(1,maximum)
            self.report({'INFO'},f'Applied responsive motion to {len(targets)} chains');return {'FINISHED'}
        finally:_batch_busy=False


def selection_toolbar(layout,arm,kind):
    row=layout.row(align=True)
    for op,label in [('ALL','All'),('NONE','None'),('INVERT','Invert'),('POSE','From Pose')]:
        button=row.operator('avatar_physics.selection',text=label);button.operation=op;button.kind=kind


class AVATAR_OT_physics_chain(bpy.types.Operator):
    bl_idname='avatar_physics.chain'
    bl_label='Physics chain'
    bl_options={'REGISTER','UNDO'}
    operation: EnumProperty(items=[('ADD','Add',''),('REMOVE','Remove',''),('SELECT','Select root','')])
    def execute(self, context):
        try:
            arm=rig(context)
            if self.operation=='ADD':
                count,skipped=add_selected_chains(context,arm)
                self.report({'INFO'},f'Added {count} chains; skipped {skipped} overlapping/body roots')
            elif self.operation=='REMOVE':
                if arm.avatar_physics_chains:arm.avatar_physics_chains.remove(arm.avatar_physics_chain_index);arm.avatar_physics_chain_index=max(0,min(arm.avatar_physics_chain_index,len(arm.avatar_physics_chains)-1))
            else:
                item=arm.avatar_physics_chains[arm.avatar_physics_chain_index]
                if item.root not in arm.data.bones:raise ValueError('Choose a root bone first')
                context.view_layer.objects.active=arm;arm.select_set(True)
                if context.object.mode!='POSE':bpy.ops.object.mode_set(mode='POSE')
                for bone in arm.data.bones:bone.select=False
                arm.data.bones.active=arm.data.bones[item.root];arm.data.bones[item.root].select=True
            return {'FINISHED'}
        except (ValueError,IndexError) as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}


class AVATAR_OT_physics_batch(bpy.types.Operator):
    bl_idname='avatar_physics.batch'
    bl_label='Copy active settings to selected bones'
    bl_options={'REGISTER','UNDO'}
    kind:EnumProperty(items=[('chains','Chains',''),('colliders','Colliders','')])
    def execute(self,context):
        global _batch_busy
        try:
            arm=rig(context);collection=getattr(arm,'avatar_physics_'+self.kind)
            source=collection[getattr(arm,'avatar_physics_'+('chain' if self.kind=='chains' else 'collider')+'_index')]
            targets=selected_items(context,arm,self.kind)
            if not targets:raise ValueError('Mark target rows in the list or select bones in Pose mode')
            _batch_busy=True
            for target in targets:
                for key in CHAIN_FIELDS if self.kind=='chains' else COLLIDER_FIELDS:setattr(target,key,getattr(source,key))
            self.report({'INFO'},f'Applied settings to {len(targets)} selected {self.kind}')
            return {'FINISHED'}
        except (ValueError,IndexError) as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}
        finally:_batch_busy=False


def _helper(context, arm, item, end):
    bone=arm.pose.bones.get(item.bone)
    if not bone:raise ValueError('Choose an attached bone first')
    obj=item.end_object if end else item.start_object
    if not obj:
        obj=bpy.data.objects.new('AnyModel collider '+item.name+(' end' if end else ''),None);context.scene.collection.objects.link(obj)
        obj['anymodel_physics_helper']=True
        if end:item.end_object=obj
        else:item.start_object=obj
    obj.empty_display_type='SPHERE';obj.empty_display_size=1;obj.show_in_front=True;obj.hide_render=True
    obj.parent=arm;obj.parent_type='BONE';obj.parent_bone=item.bone
    context.view_layer.update()
    world=arm.matrix_world @ bone.matrix @ Matrix.Translation(item.tail if end else item.offset)
    location=world.translation.copy();radius=item.radius*arm.matrix_world.to_scale().x
    obj.matrix_world=Matrix.Translation(location) @ Matrix.Scale(radius,4)
    context.view_layer.update()
    return obj


class AVATAR_OT_physics_collider(bpy.types.Operator):
    bl_idname='avatar_physics.collider'
    bl_label='Physics collider'
    bl_options={'REGISTER','UNDO'}
    operation: EnumProperty(items=[('ADD','Add',''),('REMOVE','Remove',''),('GIZMOS','Create / reset gizmos',''),('SELECT','Select gizmo','')])
    def execute(self, context):
        global _batch_busy
        try:
            arm=rig(context)
            if self.operation=='ADD':
                names=selected_names(context,arm)
                if not names:raise ValueError('Select bones in Pose Mode first')
                existing={c.bone for c in arm.avatar_physics_colliders};count=0
                _batch_busy=True
                for bone in arm.data.bones:
                    if bone.name not in names or bone.name in existing:continue
                    item=arm.avatar_physics_colliders.add();item.bone=bone.name;item.name=bone.name[:80];arm.avatar_physics_collider_index=len(arm.avatar_physics_colliders)-1
                    item.radius=local_height(arm)*.05;item.tail=(0,local_height(arm)*.15,0);count+=1
                self.report({'INFO'},f'Added {count} colliders')
            elif self.operation=='REMOVE':
                if arm.avatar_physics_colliders:
                    item=arm.avatar_physics_colliders[arm.avatar_physics_collider_index]
                    for obj in (item.start_object,item.end_object):
                        if obj and obj.get('anymodel_physics_helper'):bpy.data.objects.remove(obj,do_unlink=True)
                    arm.avatar_physics_colliders.remove(arm.avatar_physics_collider_index);arm.avatar_physics_collider_index=max(0,min(arm.avatar_physics_collider_index,len(arm.avatar_physics_colliders)-1))
            elif self.operation=='GIZMOS':
                item=arm.avatar_physics_colliders[arm.avatar_physics_collider_index];_helper(context,arm,item,False)
                if item.shape=='capsule':_helper(context,arm,item,True)
            else:
                item=arm.avatar_physics_colliders[arm.avatar_physics_collider_index]
                if not item.start_object:raise ValueError('Create gizmos first')
                if context.object and context.object.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
                for obj in context.selected_objects:obj.select_set(False)
                item.start_object.select_set(True);context.view_layer.objects.active=item.start_object
            return {'FINISHED'}
        except (ValueError,IndexError) as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}
        finally:_batch_busy=False


class AVATAR_PT_physics(bpy.types.Panel):
    bl_label='AnyModel Bone Physics'
    bl_idname='AVATAR_PT_bone_physics'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='AnyModel'
    bl_order=-20
    def draw(self, context):
        layout=self.layout
        try:arm=rig(context)
        except ValueError as ex:layout.label(text=str(ex));return
        layout.label(text='Hair / ears / tail / clothing bones')
        from . import physics_preview
        active=physics_preview.running(arm)
        row=layout.row(align=True)
        row.operator('avatar_physics.preview',text='Stop live preview' if active else 'Start live preview',icon='PAUSE' if active else 'PLAY').operation='STOP' if active else 'START'
        if active:row.operator('avatar_physics.preview',text='',icon='FILE_REFRESH').operation='RESET'
        if active:
            layout.operator('avatar_physics.preview',text='Move avatar (Object Mode)',icon='OBJECT_ORIGIN').operation='MOVE'
            layout.label(text='G: move avatar / R: rotate. Physics follows live.',icon='INFO')
            layout.prop(arm,'avatar_physics_preview_restore')
        if physics_preview.error():layout.label(text=physics_preview.error(),icon='ERROR')
        layout.prop(arm,'avatar_physics_guides',text='Show physics guides')
        if arm.avatar_physics_guides:
            row=layout.row(align=True);row.prop(arm,'avatar_physics_guides_selected');row.prop(arm,'avatar_physics_guides_xray')
            layout.prop(arm,'avatar_physics_guides_angles')
            layout.label(text='Blue: fixed / Green: tip radius')
            layout.label(text='Purple: angle limit / Orange: collider')
        names=selected_names(context,arm);targets=selected_items(context,arm)
        layout.operator('avatar_physics.chain',text=f'Add selected bones ({len(names)})',icon='ADD').operation='ADD'
        layout.prop(arm,'avatar_physics_batch',text='Edit selected chains together')
        layout.prop(arm,'avatar_physics_selection',expand=True)
        if arm.avatar_physics_batch:layout.label(text=f'Changes also apply to {len(targets)} selected chains')
        layout.prop(arm,'avatar_physics_pin_enabled',text='Protect scalp / mesh base')
        if arm.avatar_physics_pin_enabled:
            box=layout.box();box.prop(arm,'avatar_physics_pin_mesh');box.prop(arm,'avatar_physics_pin_depth');box.prop(arm,'avatar_physics_pin_feather');box.label(text='Protected vertices keep their animated pose.')
        selection_toolbar(layout,arm,'chains')
        row=layout.row();row.template_list('AVATAR_UL_physics','chains',arm,'avatar_physics_chains',arm,'avatar_physics_chain_index',rows=5)
        col=row.column(align=True);col.operator('avatar_physics.chain',text='',icon='ADD').operation='ADD';col.operator('avatar_physics.chain',text='',icon='REMOVE').operation='REMOVE'
        if arm.avatar_physics_chains:
            item=arm.avatar_physics_chains[min(arm.avatar_physics_chain_index,len(arm.avatar_physics_chains)-1)]
            layout.operator('avatar_physics.response',text='Responsive motion for selected',icon='MOD_PHYSICS')
            box=layout.box();box.prop(item,'name');box.prop_search(item,'root',arm.data,'bones');box.operator('avatar_physics.chain',text='Select root in Pose Mode').operation='SELECT'
            box.operator('avatar_physics.batch',text='Copy active settings to selected').kind='chains'
            box.prop(item,'children');box.prop(item,'ignore');box.prop(item,'anchor_levels');box.prop(item,'base_motion');box.prop(item,'tip_motion')
            for key in ['pull','spring','stiffness','gravity','falloff','immobile','radius_cm','angle']:box.prop(item,key)
            box.label(text='Green sphere is at the moving bone tip.')
            box.prop(item,'collision')
            if item.collision:box.prop(item,'colliders')
        layout.label(text='Body colliders')
        layout.operator('avatar_physics.collider',text='Add colliders to selected bones',icon='ADD').operation='ADD'
        selection_toolbar(layout,arm,'colliders')
        row=layout.row();row.template_list('AVATAR_UL_physics','colliders',arm,'avatar_physics_colliders',arm,'avatar_physics_collider_index',rows=2)
        col=row.column(align=True);col.operator('avatar_physics.collider',text='',icon='ADD').operation='ADD';col.operator('avatar_physics.collider',text='',icon='REMOVE').operation='REMOVE'
        if arm.avatar_physics_colliders:
            item=arm.avatar_physics_colliders[min(arm.avatar_physics_collider_index,len(arm.avatar_physics_colliders)-1)]
            box=layout.box();box.prop(item,'name');box.prop_search(item,'bone',arm.data,'bones');box.prop(item,'shape');box.prop(item,'radius_cm');box.prop(item,'offset')
            box.operator('avatar_physics.batch',text='Copy collider settings to selected').kind='colliders'
            if item.shape=='capsule':box.prop(item,'tail')
            box.operator('avatar_physics.collider',text='Create / reset wire gizmos').operation='GIZMOS'
            if item.start_object:
                box.operator('avatar_physics.collider',text='Select start gizmo').operation='SELECT';box.prop(item,'start_object')
                if item.shape=='capsule':box.prop(item,'end_object')
                box.label(text='Gizmo position/scale override numeric fields.')
        layout.label(text='Endpoints follow the strand; leaves continue its direction.')
        layout.label(text='Do not choose mapped humanoid bones.')
        layout.label(text='Saved with .blend; included in Build Avatar.')
        layout.operator('export_scene.avatar_physics',text='Update physics in existing .mcavatar')


def _names(value):return [v.strip() for v in value.split(';') if v.strip()]


def physics_tip(bone, chain_names):
    """A strand follows child heads, which need not lie on an imported bone's Y axis.

    FBX often uses transverse display tails. Leaves continue the incoming chain
    tangent; isolated bones retain their explicitly authored tail.
    """
    children=[b for b in bone.children if b.name in chain_names and (b.head_local-bone.head_local).length_squared>1e-10]
    if children:
        delta=sum((b.head_local-bone.head_local for b in children),Vector())/len(children)
        if delta.length_squared<1e-10:delta=max(children,key=lambda b:(b.head_local-bone.head_local).length_squared).head_local-bone.head_local
    elif bone.parent and bone.parent.name in chain_names:
        delta=bone.head_local-bone.parent.head_local
        if delta.length_squared>1e-10:delta=delta.normalized()*bone.length
        else:delta=bone.tail_local-bone.head_local
    else:delta=bone.tail_local-bone.head_local
    return bone.matrix_local.inverted()@(bone.head_local+delta)


def export_physics(arm, bones, names, globals_, conversion, unit_scale, protected):
    """Convert Blender units, aliases and native roll into normalized NZM bone frames."""
    chains=[];colliders=[];collider_names={};used=set();factor=conversion.to_scale().x
    for c in arm.avatar_physics_colliders:
        if not c.enabled:continue
        if not c.name.strip() or len(c.name)>80 or c.name in collider_names:raise ValueError('Physics: unique collider names required')
        if c.bone not in globals_:raise ValueError('Physics: missing collider bone '+c.bone)
        bone=arm.data.bones[c.bone];inverse=globals_[c.bone].inverted()
        def centre(helper, local):
            world=helper.matrix_world.translation if helper else arm.matrix_world @ bone.matrix_local @ Vector(local)
            return list(inverse @ (conversion @ world))
        offset=centre(c.start_object,c.offset);tail=centre(c.end_object,c.tail) if c.shape=='capsule' else offset
        radius=max(c.start_object.matrix_world.to_scale())*factor if c.start_object else c.radius*unit_scale
        if not .001<=radius<=100:raise ValueError('Physics: collider radius outside 0.001..100 exported cm')
        collider_names[c.name]=len(colliders);colliders.append(dict(name=c.name,bone=names[c.bone],shape=c.shape,offset=offset,tail=tail,radius=radius))
    for c in arm.avatar_physics_chains:
        if not c.enabled:continue
        if c.root not in arm.data.bones:raise ValueError('Physics: missing root '+c.root)
        if not c.name.strip() or len(c.name)>80:raise ValueError('Physics: name must be 1..80 characters')
        ignored=set(_names(c.ignore))
        if any(n not in arm.data.bones for n in ignored):raise ValueError('Physics: ignored bone does not exist')
        selected=[]
        for bone in bones:
            ancestry={p.name for p in (bone,*bone.parent_recursive)}
            if (bone.name==c.root or c.children and c.root in ancestry) and not ignored.intersection(ancestry):selected.append(bone)
        if not selected:raise ValueError('Physics: empty chain '+c.name)
        depths={b.name:len(b.parent_recursive)-len(arm.data.bones[c.root].parent_recursive) for b in selected}
        depth_max=max(depths.values())
        selected=[b for b in selected if depths[b.name]>=c.anchor_levels]
        if not selected:raise ValueError('Physics: all bones anchored in '+c.name+'; reduce Anchored levels (0 for one bone)')
        for bone in selected:
            if bone.name in used:raise ValueError('Physics: overlapping chains at '+bone.name)
            descendants={b.name for b in bones if bone.name in {p.name for p in (b,*b.parent_recursive)}}
            if protected.intersection(descendants):raise ValueError('Physics: chain would move humanoid bone below '+bone.name)
            used.add(bone.name)
        links=_names(c.colliders)
        if any(n not in collider_names for n in links):raise ValueError('Physics: unknown collider name in '+c.name)
        indices=[collider_names[n] for n in links] if links else list(range(len(colliders)))
        if len(set(indices))!=len(indices):raise ValueError('Physics: duplicate collider names in '+c.name)
        if not c.collision:indices=[]
        links=[]
        for b in selected:
            endpoint=globals_[b.name].inverted() @ (conversion @ arm.matrix_world @ b.matrix_local @ physics_tip(b,set(depths)))
            if endpoint.length<.001:raise ValueError('Physics: zero-length bone '+b.name)
            links.append(dict(bone=names[b.name],tip=list(endpoint),motion=motion_at(c,depths[b.name],depth_max)))
        chains.append(dict(name=c.name,bones=links,pull=c.pull,spring=c.spring,stiffness=c.stiffness,gravity=c.gravity,gravityFalloff=c.falloff,immobile=c.immobile,radius=c.radius*unit_scale,maxAngle=c.angle*180/3.141592653589793,colliders=indices,anchorLevels=c.anchor_levels,baseMotion=c.base_motion,tipMotion=c.tip_motion))
        if chains[-1]['radius']>20:raise ValueError('Physics: bone radius exceeds 20 exported cm')
    affected={names[b.name] for b in bones if used.intersection(p.name for p in (b,*b.parent_recursive))}
    if any(c['bone'] in affected for c in colliders):raise ValueError('Physics: attach colliders to body bones outside simulated chains')
    if len(chains)>64 or len(colliders)>64 or len(used)>256 or sum(len(c['bones'])*len(c['colliders']) for c in chains)>4096:raise ValueError('Physics budget: 64 chains/colliders, 256 bones, 4096 pairs')
    pins=[]
    if arm.avatar_physics_pin_enabled:
        obj=arm.avatar_physics_pin_mesh
        if not obj or obj.type!='MESH' or (obj.find_armature()!=arm and obj.parent!=arm):raise ValueError('Physics: select the scalp mesh bound to this armature')
        top=max((conversion @ obj.matrix_world @ vertex.co).y for vertex in obj.data.vertices)
        end=top-arm.avatar_physics_pin_depth*unit_scale;start=end-arm.avatar_physics_pin_feather*unit_scale
        if not -1000<=start<end<=1000 or end-start<.001:raise ValueError('Physics: invalid scalp depth/feather')
        pins.append(dict(mesh=str(obj.get('avatar_mesh',obj.name)),start=start,end=end))
    return dict(version=1,endpointMode="chain",chains=chains,colliders=colliders,pins=pins)


def update_package(context, filepath):
    from . import physics_preview
    physics_preview.stop()
    from .avatar_export import SPACE_KEY,engine_matrix,mapping_for
    arm=rig(context);space=json.loads(arm.get(SPACE_KEY,'{}'))
    if not space.get('bones'):raise ValueError('Build the avatar once before updating only physics')
    target=Path(filepath);temporary=target.with_name(target.name+'.physics.tmp');old_pose=arm.data.pose_position
    try:
        arm.data.pose_position='REST';context.view_layer.update()
        conversion=Matrix(space['conversion']);names={name:info['name'] for name,info in space['bones'].items()}
        bones=[];globals_={}
        with zipfile.ZipFile(target) as source:
            if len(source.infolist())>256 or sum(e.file_size for e in source.infolist())>768*1024*1024:raise ValueError('Avatar size/entry limit')
            manifest=json.loads(source.read('avatar.json'))
            if manifest['id']!=space['profile']:raise ValueError('This package belongs to a different avatar')
            if source.getinfo('model.nzm').file_size>128*1024*1024:raise ValueError('Model exceeds 128 MiB')
            binary=source.read('model.nzm');offset=4
            if binary[:4] not in (b'NZM3',b'NZM4'):raise ValueError('Unsupported model format')
            count,=struct.unpack_from('<I',binary,offset);offset+=4
            if count!=len(arm.data.bones) or count>512:raise ValueError('Skeleton changed; rebuild the avatar')
            by_alias={alias:name for name,alias in names.items()}
            order=[]
            for i in range(count):
                length,=struct.unpack_from('<H',binary,offset);offset+=2;alias=binary[offset:offset+length].decode('utf-8');offset+=length
                parent,=struct.unpack_from('<i',binary,offset);offset+=4;values=struct.unpack_from('<24f',binary,offset);offset+=96
                name=by_alias.get(alias);bone=arm.data.bones.get(name) if name else None
                if not bone:raise ValueError('Bone names changed; rebuild the avatar')
                actual_parent=names.get(bone.parent.name,'') if bone.parent else ''
                if parent<-1 or parent>=i or actual_parent!=('' if parent<0 else order[parent]):raise ValueError('Bone hierarchy changed; rebuild the avatar')
                order.append(alias)
                current=engine_matrix(space,arm.matrix_world,bone.matrix_local)
                if any(abs(current[r][c]-values[12+r*4+c])>.003 for r in range(3) for c in range(4)):raise ValueError('Rest pose/object transform changed; rebuild the avatar')
                bones.append(bone);globals_[name]=current
            manifest['physics']=export_physics(arm,bones,names,globals_,conversion,space['unitScale'],set(mapping_for(context.scene,arm).values()))
            with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as out:
                for entry in source.infolist():out.writestr(entry,json.dumps(manifest,ensure_ascii=False).encode('utf-8') if entry.filename=='avatar.json' else source.read(entry))
        shutil.copy2(target,target.with_name(target.name+'.bak'));os.replace(temporary,target);arm['anymodel_package_path']=str(target)
        return manifest['physics']
    finally:
        arm.data.pose_position=old_pose;context.view_layer.update()
        if temporary.exists():temporary.unlink()


class AVATAR_OT_update_physics(bpy.types.Operator,ExportHelper):
    bl_idname='export_scene.avatar_physics'
    bl_label='Update AnyModel Physics'
    filename_ext='.mcavatar'
    filter_glob:StringProperty(default='*.mcavatar',options={'HIDDEN'})
    def invoke(self,context,event):
        try:self.filepath=rig(context).get('anymodel_package_path','')
        except ValueError:pass
        return super().invoke(context,event)
    def execute(self,context):
        try:
            p=update_package(context,self.filepath);self.report({'INFO'},f"Updated {len(p['chains'])} physics chains; mesh/textures preserved");return {'FINISHED'}
        except Exception as ex:self.report({'ERROR'},str(ex));return {'CANCELLED'}


CLASSES=(AVATAR_PG_physics_chain,AVATAR_PG_physics_collider,AVATAR_UL_physics,AVATAR_OT_physics_row_select,AVATAR_OT_physics_selection,AVATAR_OT_physics_response,AVATAR_OT_physics_chain,AVATAR_OT_physics_batch,AVATAR_OT_physics_collider,AVATAR_OT_update_physics,AVATAR_PT_physics)
def register():
    for key in CHAIN_FIELDS:AVATAR_PG_physics_chain.__annotations__[key].keywords['update']=_changed(key)
    for key in ('root','ignore'):AVATAR_PG_physics_chain.__annotations__[key].keywords['update']=_changed(key)
    for key in COLLIDER_FIELDS:
        extra={'radius':_radius_changed,'offset':_offset_changed,'tail':_offset_changed}.get(key)
        AVATAR_PG_physics_collider.__annotations__[key].keywords['update']=_changed(key,'colliders',extra)
    for cls in CLASSES:bpy.utils.register_class(cls)
    bpy.types.Object.avatar_physics_batch=BoolProperty(name='Edit selected together',default=True,description='Changed parameters also apply to selected chain/collider bones; root names and ignored subtrees stay individual')
    bpy.types.Object.avatar_physics_selection=EnumProperty(name='Batch selection',items=[('LIST','List rows','Use marked rows, even in Object Mode'),('POSE','Pose bones','Use selected bones in the viewport')],default='LIST')
    bpy.types.Object.avatar_physics_chain_anchor=IntProperty(default=0,min=0)
    bpy.types.Object.avatar_physics_collider_anchor=IntProperty(default=0,min=0)
    bpy.types.Object.avatar_physics_guides=BoolProperty(name='Show physics guides',default=True)
    bpy.types.Object.avatar_physics_guides_selected=BoolProperty(name='Selected only',default=True)
    bpy.types.Object.avatar_physics_guides_xray=BoolProperty(name='In front',default=True)
    bpy.types.Object.avatar_physics_guides_angles=BoolProperty(name='Show angle limits',default=True)
    bpy.types.Object.avatar_physics_preview_restore=BoolProperty(name='Return avatar on stop',default=True,description='Restore the armature object transform from preview start; pose edits and animation keys are retained')
    bpy.types.Object.avatar_physics_chains=CollectionProperty(type=AVATAR_PG_physics_chain)
    bpy.types.Object.avatar_physics_colliders=CollectionProperty(type=AVATAR_PG_physics_collider)
    bpy.types.Object.avatar_physics_chain_index=IntProperty(default=0,min=0)
    bpy.types.Object.avatar_physics_collider_index=IntProperty(default=0,min=0)
    bpy.types.Object.avatar_physics_pin_enabled=BoolProperty(name='Protect scalp',default=False)
    bpy.types.Object.avatar_physics_pin_mesh=PointerProperty(name='Scalp mesh',type=bpy.types.Object,poll=lambda self,obj:obj.type=='MESH')
    bpy.types.Object.avatar_physics_pin_depth=FloatProperty(name='Fully protected depth',default=.06,min=.00001,max=1000,subtype='DISTANCE')
    bpy.types.Object.avatar_physics_pin_feather=FloatProperty(name='Soft transition',default=.03,min=.00001,max=1000,subtype='DISTANCE')
    from . import physics_preview
    physics_preview.register()
    from . import physics_guides
    physics_guides.register()
def unregister():
    from . import physics_guides
    physics_guides.unregister()
    from . import physics_preview
    physics_preview.unregister()
    for prop in ['avatar_physics_guides','avatar_physics_guides_selected','avatar_physics_guides_xray','avatar_physics_guides_angles','avatar_physics_selection','avatar_physics_chain_anchor','avatar_physics_collider_anchor','avatar_physics_batch','avatar_physics_preview_restore','avatar_physics_chains','avatar_physics_colliders','avatar_physics_chain_index','avatar_physics_collider_index','avatar_physics_pin_enabled','avatar_physics_pin_mesh','avatar_physics_pin_depth','avatar_physics_pin_feather']:delattr(bpy.types.Object,prop)
    for cls in reversed(CLASSES):bpy.utils.unregister_class(cls)
