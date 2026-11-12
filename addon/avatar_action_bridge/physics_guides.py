"""Cached viewport lines for the quantities the bone solver actually uses."""
import bpy,math,time
from mathutils import Vector
from bpy.app.handlers import persistent

_handle=None;_shader=None;_cache=None;_expires=0;_revision=0
draw_stats={'draws':0,'vertices':0,'error':None}
CIRCLE=tuple((math.cos(i*math.tau/24),math.sin(i*math.tau/24)) for i in range(25))
COLORS={'fixed':(.15,.55,1,1),'radius':(.15,1,.3,.95),'limit':(.8,.3,1,.8),'collider':(1,.5,.08,.95)}

def invalidate(*args):
    global _expires,_revision
    _expires=0;_revision+=1

def _line(lines,a,b):lines.extend((tuple(a),tuple(b)))
def _ring(lines,centre,x,y,radius):
    for (a,b),(c,d) in zip(CIRCLE,CIRCLE[1:]):_line(lines,centre+(x*a+y*b)*radius,centre+(x*c+y*d)*radius)
def _sphere(lines,centre,radius):
    if radius<=0:return
    x,y,z=Vector((1,0,0)),Vector((0,1,0)),Vector((0,0,1))
    for a,b in ((x,y),(x,z),(y,z)):_ring(lines,centre,a,b,radius)

def geometry(context,arm):
    from . import bone_physics as bp,physics_preview as pp
    groups={name:[] for name in COLORS};details=[]
    chains=bp.selected_items(context,arm);colliders=bp.selected_items(context,arm,'colliders')
    if not arm.avatar_physics_guides_selected:chains=list(arm.avatar_physics_chains);colliders=list(arm.avatar_physics_colliders)
    elif not chains and arm.avatar_physics_chains:chains=[arm.avatar_physics_chains[min(arm.avatar_physics_chain_index,len(arm.avatar_physics_chains)-1)]]
    if not colliders and arm.avatar_physics_colliders:colliders=[arm.avatar_physics_colliders[min(arm.avatar_physics_collider_index,len(arm.avatar_physics_colliders)-1)]]
    display=pp._session.copy if pp.running(arm) else arm
    root=display.matrix_world;scale=max(root.to_scale())
    marker=bp.local_height(arm)*scale*.002
    for chain in chains:
        if not chain.enabled:continue
        bones=bp.chain_bones(arm,chain)
        if not bones:continue
        base=len(arm.data.bones[chain.root].parent_recursive)
        maximum=max(len(b.parent_recursive)-base for b in bones);first=True
        for b in bones:
            depth=len(b.parent_recursive)-base;pb=display.pose.bones.get(b.name)
            if not pb:continue
            endpoint=bp.physics_tip(b,{item.name for item in bones})
            pivot=root@pb.head;tip=root@(pb.matrix@endpoint)
            if depth<chain.anchor_levels:
                _line(groups['fixed'],pivot,tip);_sphere(groups['fixed'],pivot,marker);continue
            radius=chain.radius*scale*max(pb.matrix.to_scale())
            _line(groups['radius'],pivot,tip);_sphere(groups['radius'],tip,radius)
            if first and arm.avatar_physics_guides_angles:
                first=False;source=arm.pose.bones[b.name]
                target=root@(source.matrix@endpoint)
                rest=target-pivot;length=rest.length
                if length<=1e-8:continue
                rest.normalize();angle=chain.angle*bp.motion_at(chain,depth,maximum)
                x=rest.orthogonal().normalized();y=rest.cross(x).normalized()
                centre=pivot+rest*(length*math.cos(angle));r=length*math.sin(angle)
                _ring(groups['limit'],centre,x,y,r)
                for tangent in (x,y,-x,-y):_line(groups['limit'],pivot,centre+tangent*r)
                details.append(dict(chain=chain.name,bone=b.name,radiusWorld=radius,angle=angle))
    inverse=arm.matrix_world.inverted()
    for c in colliders:
        if not c.enabled or c.bone not in arm.pose.bones:continue
        matrix=arm.matrix_world@arm.pose.bones[c.bone].matrix
        a=c.start_object.matrix_world.translation if c.start_object else matrix@Vector(c.offset)
        b=(c.end_object.matrix_world.translation if c.end_object else matrix@Vector(c.tail)) if c.shape=='capsule' else a
        radius=max(c.start_object.matrix_world.to_scale()) if c.start_object else c.radius*max(matrix.to_scale())
        _sphere(groups['collider'],a,radius)
        if (b-a).length_squared>1e-12:
            _sphere(groups['collider'],b,radius);axis=(b-a).normalized();x=axis.orthogonal().normalized();y=axis.cross(x).normalized()
            for side in (x,y,-x,-y):_line(groups['collider'],a+side*radius,b+side*radius)
    return groups,details

def _draw():
    global _shader,_cache,_expires
    from .bone_physics import rig
    import gpu
    from gpu_extras.batch import batch_for_shader
    context=bpy.context
    if not context.space_data or not context.space_data.overlay.show_overlays:return
    try:arm=rig(context)
    except (ValueError,ReferenceError):return
    if not arm.avatar_physics_guides:return
    now=time.monotonic();key=(arm.as_pointer(),_revision)
    try:
        if _shader is None:_shader=gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
        if _cache is None or _cache[0]!=key or now>=_expires:
            groups,_=geometry(context,arm)
            _cache=(key,[(name,batch_for_shader(_shader,'LINES',{'pos':points})) for name,points in groups.items() if points]);_expires=now+.08
            draw_stats['vertices']=sum(len(points) for points in groups.values())
        depth=gpu.state.depth_test_get();blend=gpu.state.blend_get()
        try:
            gpu.state.blend_set('ALPHA');gpu.state.depth_test_set('NONE' if arm.avatar_physics_guides_xray else 'LESS_EQUAL')
            _shader.bind();_shader.uniform_float('viewportSize',gpu.state.viewport_get()[2:]);_shader.uniform_float('lineWidth',1.4)
            for name,batch in _cache[1]:_shader.uniform_float('color',COLORS[name]);batch.draw(_shader)
            draw_stats['draws']+=1;draw_stats['error']=None
        finally:gpu.state.depth_test_set(depth);gpu.state.blend_set(blend)
    except Exception as ex:
        if draw_stats['error']!=str(ex):print('AnyModel guides:',ex)
        draw_stats['error']=str(ex);invalidate()

@persistent
def _clear(*args):
    global _cache
    _cache=None;invalidate()

def register():
    global _handle
    if not bpy.app.background:_handle=bpy.types.SpaceView3D.draw_handler_add(_draw,(),'WINDOW','POST_VIEW')
    for handlers in (bpy.app.handlers.load_post,bpy.app.handlers.undo_post,bpy.app.handlers.redo_post):
        if _clear not in handlers:handlers.append(_clear)

def unregister():
    global _handle,_shader
    if _handle:
        bpy.types.SpaceView3D.draw_handler_remove(_handle,'WINDOW');_handle=None
    for handlers in (bpy.app.handlers.load_post,bpy.app.handlers.undo_post,bpy.app.handlers.redo_post):
        if _clear in handlers:handlers.remove(_clear)
    _shader=None;_clear()
