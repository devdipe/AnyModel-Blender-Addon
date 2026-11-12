"""Sparse avatar blendshapes and scalar AVANIM tracks, using stable mesh/key IDs."""
import bpy, hashlib, io, json, math, struct
import numpy as np
from contextlib import AbstractContextManager

MAX_TARGETS=256
MAX_ENTRIES=2_000_000
MAX_BYTES=64*1024*1024
TRACK_LIMIT=16*1024*1024

def target_id(mesh,key):return 'm_'+hashlib.sha1((mesh+'\0'+key).encode('utf-8')).hexdigest()[:12]

def descriptor(obj,key):
    mesh=str(obj.get('avatar_mesh',obj.name))
    return dict(id=target_id(mesh,key.name),name=key.name,mesh=mesh,key=key.name,
                min=max(-4.,float(key.slider_min)),max=min(4.,float(key.slider_max)),base=0. if key.mute else float(key.value))

def _coords(key):
    a=np.empty(len(key.data)*3,dtype=np.float32);key.data.foreach_get('co',a);return a.reshape(-1,3)

class AvatarMorphs(AbstractContextManager):
    """Temporary POINT attributes survive subdivision/decimation on frozen base topology.
    Original mesh datablocks, keys, values, drivers and user modifiers are restored.
    """
    def __init__(self,meshes):self.meshes=meshes;self.restore=[];self.targets=[];self.by_object={};self.parts={};self.entries=0;self.raw={};self.projected=[]
    def __enter__(self):
        try:
            for obj in self.meshes:
                original=obj.data;keys=original.shape_keys
                if not keys or len(keys.key_blocks)<2:continue
                if not keys.use_relative:raise ValueError('Convert absolute shape keys to relative before avatar export: '+obj.name)
                from . import fcurves
                ad=keys.animation_data
                animated={c.data_path for c in fcurves(ad.action,getattr(ad,'action_slot',None)) if not c.mute} if ad and ad.action else set()
                if ad:animated.update(c.data_path for c in ad.drivers if not c.mute)
                nla=ad and ad.use_nla and any(not t.mute and len(t.strips) for t in ad.nla_tracks)
                neutral={key.name:0. if nla or key.path_from_id('value') in animated else float(key.value) for key in keys.key_blocks}
                copy=None;rows=[]
                for key in list(keys.key_blocks)[1:]:
                    delta=_coords(key)-_coords(key.relative_key)
                    if not np.any(np.abs(delta)>1e-7):continue
                    if len(self.targets)>=MAX_TARGETS:raise ValueError('Maximum 256 non-empty avatar shape keys.')
                    d=descriptor(obj,key)
                    d['base']=0. if key.mute else neutral[key.name]
                    self.raw[d['id']]=delta.copy()
                    if not d['min']<=d['base']<=d['max']:raise ValueError('Shape-key value outside supported -4..4 range: '+key.name)
                    if copy is None:
                        copy=original.copy();obj.data=copy;self.restore.append((obj,original,copy))
                        copy.shape_keys.animation_data_clear()
                        for frozen in copy.shape_keys.key_blocks:frozen.value=neutral[frozen.name]
                    index=len(self.targets);self.targets.append(d);rows.append(index)
                    attr=copy.attributes.new('_am_p_'+d['id'],'FLOAT_VECTOR','POINT');attr.data.foreach_set('vector',delta.ravel())
                    # Vertex normal deltas are optional. Position deltas always export.
                    try:
                        dn=np.asarray(key.normals_vertex_get(),dtype=np.float32).reshape(-1,3)-np.asarray(key.relative_key.normals_vertex_get(),dtype=np.float32).reshape(-1,3)
                        attr=copy.attributes.new('_am_n_'+d['id'],'FLOAT_VECTOR','POINT');attr.data.foreach_set('vector',dn.ravel())
                    except (AttributeError,RuntimeError):pass
                self.by_object[obj.name]=rows
            bpy.context.view_layer.update();return self
        except BaseException:
            self.__exit__(None,None,None);raise
    def __exit__(self,*_):
        for obj,original,copy in reversed(self.restore):obj.data=original;bpy.data.meshes.remove(copy)
        self.restore.clear();bpy.context.view_layer.update()
    def evaluated(self,obj,mesh,transform,normal_transform):
        per_vertex={};linear=np.asarray(transform.to_3x3(),dtype=np.float32);normal=np.asarray(normal_transform,dtype=np.float32)
        for index in self.by_object.get(obj.name,[]):
            d=self.targets[index];attribute=mesh.attributes.get('_am_p_'+d['id'])
            if attribute is None:
                return self.project(obj,mesh,transform,normal_transform)
            a=np.empty(len(mesh.vertices)*3,dtype=np.float32);attribute.data.foreach_get('vector',a);pos=a.reshape(-1,3)@linear.T
            attribute=mesh.attributes.get('_am_n_'+d['id']);dn=np.zeros_like(pos)
            if attribute:
                a=np.empty(len(mesh.vertices)*3,dtype=np.float32);attribute.data.foreach_get('vector',a);dn=a.reshape(-1,3)@normal.T
            delta=np.concatenate((pos,dn),axis=1)
            for v in np.flatnonzero(np.any(np.abs(delta)>1e-6,axis=1)):per_vertex.setdefault(int(v),[]).append((index,tuple(float(x) for x in delta[v])))
        return per_vertex
    def project(self,obj,mesh,transform,normal_transform):
        # Some Blender modifiers discard generic attributes. Transfer on the
        # frozen evaluated surface, rather than re-decimating each expression.
        # Shared barycentric correspondences keep topology and vertex order fixed.
        from mathutils import Vector
        from mathutils.bvhtree import BVHTree
        source=obj.data;source.calc_loop_triangles()
        points=np.empty(len(source.vertices)*3,dtype=np.float32);source.vertices.foreach_get('co',points);points=points.reshape(-1,3)
        for index in self.by_object.get(obj.name,[]):
            d=self.targets[index];points=points+self.raw[d['id']]*d['base']
        tri=np.asarray([tuple(t.vertices) for t in source.loop_triangles],dtype=np.int32)
        bvh=BVHTree.FromPolygons([Vector(p) for p in points],tri.tolist(),all_triangles=True)
        target=np.empty(len(mesh.vertices)*3,dtype=np.float32);mesh.vertices.foreach_get('co',target);target=target.reshape(-1,3)
        nearest=[bvh.find_nearest(Vector(p)) for p in target]
        diagonal=float(np.linalg.norm(np.max(points,axis=0)-np.min(points,axis=0)))
        if any(n[0] is None or n[3]>max(.0001,diagonal*.025) for n in nearest):raise ValueError('A modifier changed the surface too far to preserve shape keys on '+obj.name+'. Apply it before authoring the keys.')
        triangles=tri[np.array([n[2] for n in nearest])];q=np.array([n[0] for n in nearest]);a=points[triangles[:,0]];v0=points[triangles[:,1]]-a;v1=points[triangles[:,2]]-a;v2=q-a
        d00=np.sum(v0*v0,axis=1);d01=np.sum(v0*v1,axis=1);d11=np.sum(v1*v1,axis=1);d20=np.sum(v2*v0,axis=1);d21=np.sum(v2*v1,axis=1);den=d00*d11-d01*d01;den=np.where(np.abs(den)<1e-20,1,den);v=(d11*d20-d01*d21)/den;w=(d00*d21-d01*d20)/den;weights=np.stack((1-v-w,v,w),axis=1)
        per_vertex={};linear=np.asarray(transform.to_3x3(),dtype=np.float32)
        for index in self.by_object.get(obj.name,[]):
            d=self.targets[index];delta=np.sum(self.raw[d['id']][triangles]*weights[:,:,None],axis=1)@linear.T
            for vertex in np.flatnonzero(np.any(np.abs(delta)>1e-6,axis=1)):per_vertex.setdefault(int(vertex),[]).append((index,tuple(float(x) for x in delta[vertex])+(0.,0.,0.)))
        self.projected.append(obj.name)
        return per_vertex

    def vertex(self,changes,part,vertex):
        for target,delta in changes:
            self.entries+=1
            if self.entries>MAX_ENTRIES:raise ValueError('Blendshapes exceed 2 million sparse vertices; reduce the morph or triangle budget.')
            self.parts.setdefault((target,part),[]).append((vertex,delta))
    def finish_part(self,part,chunk):
        for (target,key),entries in list(self.parts.items()):
            if key==part:
                del self.parts[(target,key)];self.parts[(target,chunk)]=entries
    def binary(self):
        out=io.BytesIO();out.write(b'AMOR1');out.write(struct.pack('<I',len(self.targets)))
        for index,d in enumerate(self.targets):
            for k in ('id','name','mesh','key'):
                text=d[k].encode('utf-8')
                if len(text)>1024:raise ValueError('Blendshape name too long')
                out.write(struct.pack('<I',len(text)));out.write(text)
            out.write(struct.pack('<3f',d['min'],d['max'],d['base']))
            parts=sorted((chunk,rows) for (target,chunk),rows in self.parts.items() if target==index)
            out.write(struct.pack('<I',len(parts)))
            for chunk,rows in parts:
                out.write(struct.pack('<II',chunk,len(rows)))
                for vertex,delta in rows:out.write(struct.pack('<I6f',vertex,*delta))
        data=out.getvalue()
        if len(data)>MAX_BYTES:raise ValueError('Blendshape data exceeds 64 MiB')
        return data

def animated_targets(arm):
    from . import fcurves
    result=[]
    for obj in bpy.context.scene.objects:
        if obj.type!='MESH' or not (obj.find_armature()==arm or obj.parent==arm) or not obj.data.shape_keys:continue
        keys=obj.data.shape_keys;ad=keys.animation_data
        if not ad:continue
        curves={c.data_path:c for c in fcurves(ad.action,getattr(ad,'action_slot',None))} if ad.action else {}
        # Pose-driven body correctors are optional; explicitly animated shape
        # curves remain fast and are always exported. Property-driven facial
        # controls are sampled when explicitly requested along with drivers.
        driven={c.data_path for c in ad.drivers if not c.mute} if getattr(bpy.context.scene,'avatar_action_morph_drivers',False) else set()
        nla=ad.use_nla and any(not t.mute and len(t.strips) for t in ad.nla_tracks)
        for key in list(keys.key_blocks)[1:]:
            path=key.path_from_id('value');curve=curves.get(path)
            if key.mute or (curve is not None and curve.mute):continue
            if not curve and path not in driven and not nla:continue
            if not np.any(np.abs(_coords(key)-_coords(key.relative_key))>1e-7):continue
            result.append((descriptor(obj,key),key,curve,path in driven or nla))
    if len(result)>MAX_TARGETS:raise ValueError('Maximum 256 animated shape keys')
    return result

class ActionMorphs:
    def __init__(self,arm,count):
        self.rows=animated_targets(arm);self.scene=any(row[3] for row in self.rows);self.data=bytearray()
        if len(self.rows)*count*4>TRACK_LIMIT:raise ValueError('Morph animation exceeds 16 MiB; reduce duration or sample rate.')
    def sample(self,frame):
        for descriptor,key,curve,complex_ in self.rows:
            value=float(key.value if complex_ or not curve else curve.evaluate(frame))
            if not math.isfinite(value) or abs(value)>4:raise ValueError('Invalid shape-key animation: '+key.name)
            self.data.extend(struct.pack('<f',value))
    def manifest(self):return dict(file='morph.bin',targets=[{k:d[k] for k in ('id','mesh','key')} for d,*_ in self.rows]) if self.rows else None

def import_tracks(context,filepath,manifest,arm,start,fps):
    from . import fcurves
    import zipfile
    info=manifest.get('morphs')
    if not info:return
    targets=info.get('targets',[]);count=int(manifest['frameCount']);rate=float(manifest['sampleRate'])
    if not 1<=len(targets)<=MAX_TARGETS or len(targets)*count*4>TRACK_LIMIT:raise ValueError('Morph animation size limit')
    with zipfile.ZipFile(filepath) as z:
        if z.getinfo('morph.bin').file_size>TRACK_LIMIT:raise ValueError('Morph animation size limit')
        data=z.read('morph.bin')
    if len(data)!=count*len(targets)*4:raise ValueError('Invalid morph.bin length')
    samples=np.frombuffer(data,dtype='<f4').reshape(count,len(targets))
    if not np.all(np.isfinite(samples)) or np.any(np.abs(samples)>4):raise ValueError('Invalid morph animation value')
    meshes={str(o.get('avatar_mesh',o.name)):o for o in context.scene.objects if o.type=='MESH' and (o.find_armature()==arm or o.parent==arm)}
    actions={}
    for index,t in enumerate(targets):
        obj=meshes.get(t['mesh']);keys=obj.data.shape_keys if obj else None
        if not keys or t['key'] not in keys.key_blocks:continue
        if keys.name not in actions:
            from .roundtrip import SETUP_KEY
            backup=json.loads(context.scene.get(SETUP_KEY,'{}'))
            ad=keys.animation_data_create()
            backup.setdefault('shapes',[]).append(dict(data=keys.name,useNla=ad.use_nla,drivers=[dict(path=d.data_path,index=d.array_index,mute=d.mute) for d in ad.drivers]))
            context.scene[SETUP_KEY]=json.dumps(backup)
            if ad.action:ad.action.use_fake_user=True
            action=bpy.data.actions.new(manifest['name']+' / '+obj.name+' face');action.use_fake_user=True;ad.action=action
            if hasattr(action,'slots'):ad.action_slot=action.slots.new(id_type='KEY',name=keys.name)
            ad.use_nla=False;actions[keys.name]=action
        key=keys.key_blocks[t['key']];path=key.path_from_id('value')
        for driver in keys.animation_data.drivers:
            if driver.data_path==path:driver.mute=True
        for frame in range(count):
            key.value=float(samples[frame,index]);key.keyframe_insert(data_path='value',frame=start+frame/rate*fps,group='AnyModel expressions')
    for key_name,action in actions.items():
        keys=bpy.data.shape_keys[key_name]
        for curve in fcurves(action,getattr(keys.animation_data,'action_slot',None)):
            for point in curve.keyframe_points:point.interpolation='LINEAR'
