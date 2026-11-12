"""Build a portable, data-only Minecraft humanoid avatar from the current rig."""
import bpy
import io
import json
import math
import os
import re
import struct
import hashlib
from array import array
import tempfile
import zipfile
from pathlib import Path
from mathutils import Matrix, Vector, Quaternion
from bpy.props import StringProperty, BoolProperty, FloatProperty, IntProperty, CollectionProperty, EnumProperty
from bpy_extras.io_utils import ExportHelper

SPACE_KEY = 'minecraft_avatar_export_space'
REQUIRED = ('Hips', 'Spine', 'Head', 'LeftUpperArm', 'LeftLowerArm', 'LeftHand',
            'RightUpperArm', 'RightLowerArm', 'RightHand', 'LeftUpperLeg', 'LeftLowerLeg',
            'LeftFoot', 'RightUpperLeg', 'RightLowerLeg', 'RightFoot')
OPTIONAL = ('Chest', 'UpperChest', 'Neck', 'LeftShoulder', 'RightShoulder', 'LeftToes', 'RightToes')
STATES = ('idle', 'walk', 'walk_backward', 'run', 'crouch_idle', 'crouch_walk', 'jump_start',
          'jump_loop', 'jump_land', 'attack_empty', 'attack_item', 'swim', 'fly', 'elytra', 'sit', 'inventory')


def normalized(name):
    return re.sub('[^a-z0-9]', '', name.lower().split(':')[-1].replace('mixamorig', ''))


def aliases(role):
    result = [role]
    special = {'Hips': ['pelvis', 'spineBase', 'J_Bip_C_Hips'], 'Spine': ['spine01', 'J_Bip_C_Spine'],
               'Chest': ['spine1', 'spine02', 'J_Bip_C_Chest'], 'UpperChest': ['spine2', 'spine03', 'J_Bip_C_UpperChest'],
               'Neck': ['neck01', 'J_Bip_C_Neck'], 'Head': ['J_Bip_C_Head']}
    result += special.get(role, [])
    for side, short in [('Left', 'L'), ('Right', 'R')]:
        if not role.startswith(side):
            continue
        part = role[len(side):]
        roots = {'UpperArm': ['arm', 'upperarm'], 'LowerArm': ['forearm', 'lowerarm'], 'Hand': ['hand', 'wrist'],
                 'UpperLeg': ['upleg', 'thigh', 'upperleg'], 'LowerLeg': ['leg', 'calf', 'shin', 'lowerleg'],
                 'Foot': ['foot', 'ankle'], 'Toes': ['toebase', 'toe', 'toes'], 'Shoulder': ['shoulder', 'clavicle']}.get(part, [])
        for root in roots:
            result += [side+root, root+'.'+short, short+'_'+root, root+'_'+short, 'J_Bip_'+short+'_'+part]
        match = re.fullmatch(r'Hand(Thumb|Index|Middle|Ring|Little)([123])', part)
        if match:
            finger, segment = match.groups()
            for token in [finger, 'Pinky' if finger == 'Little' else finger]:
                result += [side+'Hand'+token+segment, token+'_'+segment.zfill(2)+'_'+short,
                           token+segment+'.'+short, 'J_Bip_'+short+'_'+token+segment]
    return {normalized(value) for value in result}


def all_roles():
    return [*REQUIRED, *OPTIONAL, *[side+'Hand'+finger+str(n) for side in ('Left', 'Right')
            for finger in ('Thumb', 'Index', 'Middle', 'Ring', 'Little') for n in (1, 2, 3)]]


def mapping_for(scene, arm):
    result = {}
    for role in all_roles():
        candidates = [b.name for b in arm.data.bones if str(b.get('avatar_bone', b.name)) == role]
        if not candidates:
            candidates = [b.name for b in arm.data.bones if normalized(b.name) in aliases(role)]
        if len(candidates) == 1:
            result[role] = candidates[0]
    for item in scene.avatar_humanoid_map:
        if item.bone and item.bone in arm.data.bones:
            result[item.role] = item.bone
    return result


def ordered_bones(arm):
    result, seen = [], set()
    def visit(bone):
        if bone.name in seen:
            return
        if bone.parent:
            visit(bone.parent)
        seen.add(bone.name);result.append(bone)
    for bone in arm.data.bones:
        visit(bone)
    return result


def affine(matrix):
    return [float(matrix[r][c]) for r in range(3) for c in range(4)]


def matrix12(values):
    return Matrix((values[0:4], values[4:8], values[8:12], (0, 0, 0, 1)))


def space_for(arm):
    raw = arm.get(SPACE_KEY)
    return json.loads(raw) if raw else None


def engine_matrix(space, object_world, bone_matrix):
    transform = Matrix(space['conversion']) @ object_world @ bone_matrix
    for r in range(3):
        for c in range(3):
            transform[r][c] /= space['unitScale']
    return transform


def local_rests(arm, bones, space):
    if space:
        table = space['bones']
        return [matrix12(table[b.name]['rest']) for b in bones]
    return [b.parent.matrix_local.inverted() @ b.matrix_local if b.parent else b.matrix_local.copy() for b in bones]


def _image_node(material):
    if not material or not material.use_nodes:
        return None, None, None
    nodes = material.node_tree.nodes
    bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if not bsdf:
        images=[n for n in nodes if n.type=='TEX_IMAGE' and n.image]
        def priority(node):
            text=(node.name+' '+node.label+' '+' '.join(link.to_socket.name+' '+link.to_node.name for output in node.outputs for link in output.links)).lower()
            positive=sum(token in text for token in ('basecolor','base_color','diffuse','maintex','albedo','lit color'))*20
            negative=sum(token in text for token in ('normal','roughness','metallic','occlusion','shade','emission','rim','matcap','bump'))*30
            return positive-negative
        if not images:return None,None,None
        images.sort(key=priority,reverse=True)
        if len(images)>1 and priority(images[0])==priority(images[1]):
            raise ValueError('Ambiguous Base Color texture: '+material.name+'. Bake/convert its color to a Principled image texture.')
        image=images[0];vector=image.inputs.get('Vector')
        uv=next((link.from_node.uv_map for link in vector.links if link.from_node.type=='UVMAP'),None) if vector else None
        return image,uv,None
    pending = [link.from_node for link in bsdf.inputs['Base Color'].links]
    seen = set()
    while pending:
        node = pending.pop(0)
        if node.as_pointer() in seen:
            continue
        seen.add(node.as_pointer())
        if node.type == 'TEX_IMAGE' and node.image:
            vector = node.inputs.get('Vector')
            uv = next((link.from_node.uv_map for link in vector.links if link.from_node.type == 'UVMAP'), None) if vector else None
            return node, uv, bsdf
        pending.extend(link.from_node for socket in node.inputs for link in socket.links)
    return None, None, bsdf


def _copy_image_pixels(source, directory):
    """Image.copy() does not duplicate loaded image buffers. Copy actual RGBA data."""
    recovered=None;image=None
    try:
        count=len(source.pixels)
        width,height=source.size[:]
        if width<1 or height<1 or count==0:
            # Recover an unreadable/lazy datablock from packed bytes or its real
            # source file, without reloading or changing the original datablock.
            packed=getattr(source,'packed_file',None)
            path=None
            if packed:
                data=bytes(packed.data)
                suffix='.png' if data.startswith(b'\x89PNG\r\n\x1a\n') else '.jpg' if data.startswith(b'\xff\xd8') else Path(source.filepath).suffix or '.img'
                path=directory/('_recover_'+str(source.as_pointer())+suffix)
                path.write_bytes(data)
            elif source.filepath:
                candidate=Path(bpy.path.abspath(source.filepath,library=source.library))
                if candidate.is_file():path=candidate
            if path is None:raise ValueError('Image buffer and source file unavailable')
            recovered=bpy.data.images.load(str(path),check_existing=False)
            source_pixels=recovered
        else:
            source_pixels=source
        count=len(source_pixels.pixels);width,height=source_pixels.size[:]
        if width<1 or height<1 or count==0 or count%(width*height):
            raise ValueError('No readable image pixels')
        channels=count//(width*height)
        if channels not in (1,2,3,4):raise ValueError('Unsupported image channel count')
        pixels=array('f',[0.0])*count
        source_pixels.pixels.foreach_get(pixels)
        if channels!=4:
            rgba=array('f',[1.0])*(width*height*4)
            if channels in (1,2):
                gray=pixels[0::channels]
                rgba[0::4]=gray;rgba[1::4]=gray;rgba[2::4]=gray
                if channels==2:rgba[3::4]=pixels[1::2]
            else:
                for channel in range(3):rgba[channel::4]=pixels[channel::3]
            pixels=rgba
        image=bpy.data.images.new('_AnyModelTexturePixels',width=width,height=height,alpha=True,float_buffer=source_pixels.is_float)
        image.colorspace_settings.name=source_pixels.colorspace_settings.name
        image.alpha_mode=source_pixels.alpha_mode
        image.pixels.foreach_set(pixels);image.update()
        return image
    except Exception:
        if image is not None:bpy.data.images.remove(image)
        raise
    finally:
        if recovered is not None:bpy.data.images.remove(recovered)


def _opacity(material, bsdf):
    """Texture alpha is data unless the material actually uses it for opacity."""
    if bsdf is None:
        # Preserve existing alpha for non-Principled/toon node groups.
        return ('texture',)
    def input_value(socket, seen):
        if not socket.is_linked:return ('constant', float(socket.default_value))
        link=socket.links[0];node=link.from_node
        if node.as_pointer() in seen:raise ValueError('Cyclic material Alpha: '+material.name)
        seen=seen|{node.as_pointer()}
        if node.type=='REROUTE':return input_value(node.inputs[0],seen)
        if node.type=='TEX_IMAGE' and link.from_socket.name=='Alpha':
            return ('image',node.image,1.) if node.image else ('constant',1.)
        if node.type=='MATH' and node.operation=='MULTIPLY':
            a,b=(input_value(s,seen) for s in list(node.inputs)[:2])
            if a[0]=='constant' and b[0]=='constant':return ('constant',a[1]*b[1])
            if a[0]=='constant':a,b=b,a
            if a[0]=='image' and b[0]=='constant':return ('image',a[1],a[2]*b[1])
        raise ValueError('Unsupported linked Alpha in '+material.name+'. Bake opacity to an image Alpha output.')
    return input_value(bsdf.inputs['Alpha'],set())


def _opacity_key(policy):
    return (policy[0],policy[1].as_pointer(),policy[2]) if policy[0]=='image' else policy


def _apply_opacity(image, policy, directory, skipped):
    if policy[0]=='texture':return
    count=image.size[0]*image.size[1];pixels=array('f',[0.])*count*4
    image.pixels.foreach_get(pixels)
    if policy[0]=='constant':pixels[3::4]=array('f',[max(0.,min(1.,policy[1]))])*count
    else:
        opacity=None
        try:
            opacity=_copy_image_pixels(policy[1],directory)
            if tuple(opacity.size)!=tuple(image.size):opacity.scale(*image.size)
            values=array('f',[0.])*count*4;opacity.pixels.foreach_get(values)
            pixels[3::4]=array('f',(max(0.,min(1.,v*policy[2])) for v in values[3::4]))
        except (RuntimeError,ValueError,OSError,ReferenceError):
            skipped.append(policy[1].name);pixels[3::4]=array('f',[1.])*count
        finally:
            if opacity is not None:bpy.data.images.remove(opacity)
    image.alpha_mode='STRAIGHT';image.pixels.foreach_set(pixels);image.update()


def _texture(material, cache, directory, max_side, skipped):
    key = material.as_pointer() if material else 0
    if key in cache:return cache[key]
    node, uv, bsdf = _image_node(material)
    policy=_opacity(material,bsdf)
    image_key=('image',node.image.as_pointer(),_opacity_key(policy)) if node else None
    filename = 'material_'+str(len(cache))+'.png'
    if image_key in cache:
        filename=cache[image_key][0]
        cache[key]=(filename,uv,True);return cache[key]
    image=None;textured=False
    if node:
        missing_key=('missing',node.image.as_pointer())
        if missing_key not in cache:
            try:
                if node.image.source=='TILED':raise ValueError('UDIM requires a baked image')
                image=_copy_image_pixels(node.image,directory);textured=True
            except (RuntimeError,ValueError,OSError,ReferenceError):
                cache[missing_key]=True;skipped.append(node.image.name)
    if image is None:
        # Missing image data is optional: keep geometry and export a plain color.
        # An unreadable texture must not abort the entire avatar export.
        if bsdf and not bsdf.inputs['Base Color'].is_linked:
            color=list(bsdf.inputs['Base Color'].default_value)
        else:
            color=list(material.diffuse_color) if material else [1,1,1,1]
        if bsdf and not bsdf.inputs['Alpha'].is_linked:color[3]=float(bsdf.inputs['Alpha'].default_value)
        image=bpy.data.images.new('_AvatarSolid',width=2,height=2,alpha=True)
        image.pixels=color*4
    try:
        _apply_opacity(image,policy,directory,skipped)
        width,height=image.size[:]
        if max(width,height)>max_side:
            factor=max_side/max(width,height)
            image.scale(max(1,math.floor(width*factor)),max(1,math.floor(height*factor)))
        image.filepath_raw=str(directory/filename);image.file_format='PNG';image.save()
    finally:
        bpy.data.images.remove(image)
    if textured:cache[image_key]=(filename,None,True)
    cache[key]=(filename,uv,textured)
    return cache[key]


def _fit_textures(directory, filenames):
    """Deduplicate exported copies, then fit the loader's decoded pixel budget."""
    remap={};unique={};sizes={}
    for filename in sorted(set(filenames)):
        data=(directory/filename).read_bytes()
        digest=hashlib.sha256(data).digest()
        canonical=unique.setdefault(digest,filename);remap[filename]=canonical
        if canonical==filename:
            sizes[filename]=struct.unpack_from('>II',data,16)
    if len(sizes)>128:
        raise ValueError('More than 128 unique texture images after deduplication. Combine materials/images into atlases before export.')
    before=sum(w*h for w,h in sizes.values());budget=64*1024*1024
    resized=0
    if before>budget:
        # Preserve tiny solid colors and icons. Scale the larger images together
        # so no single material is needlessly sacrificed to the entire budget.
        fixed=sum(w*h for w,h in sizes.values() if w*h<=128*128)
        scalable=before-fixed
        factor=math.sqrt((budget-fixed)/scalable)
        def planned_pixels(value):
            return fixed+sum(max(1,math.floor(w*value))*max(1,math.floor(h*value))
                             for w,h in sizes.values() if w*h>128*128)
        # Thin images can hit the one-pixel minimum on their short side. Account
        # for the actual rounded dimensions before resizing any copies.
        if planned_pixels(factor)>budget:
            low,high=0.,factor
            for _ in range(40):
                middle=(low+high)*.5
                if planned_pixels(middle)<=budget:low=middle
                else:high=middle
            factor=low
        for filename,(width,height) in list(sizes.items()):
            if width*height<=128*128:
                continue
            w,h=max(1,math.floor(width*factor)),max(1,math.floor(height*factor))
            if (w,h)==(width,height):
                continue
            image=bpy.data.images.load(str(directory/filename),check_existing=False)
            try:
                image.scale(w,h);image.file_format='PNG';image.filepath_raw=str(directory/filename);image.save()
            finally:
                bpy.data.images.remove(image)
            sizes[filename]=(w,h);resized+=1
    after=sum(w*h for w,h in sizes.values())
    if after>budget:
        raise ValueError('Unable to fit exported textures into the avatar pixel budget.')
    return remap,dict(uniqueImages=len(sizes),duplicates=len(remap)-len(sizes),
                      resizedImages=resized,pixelsBefore=before,pixelsAfter=after)


def _write_string(stream, value):
    raw = value.encode('utf-8')
    if len(raw) > 4096:
        raise ValueError('Name is too long: '+value[:40])
    stream.write(struct.pack('<H', len(raw)));stream.write(raw)


def _base_animations(bones, space, package):
    """Retarget bundled global rotation deltas to this rig, preserving its lengths/axes."""
    target = space['bones']
    engine_names = {b.name: target[b.name]['name'] for b in bones}
    target_by_role = {engine_names[b.name]: b.name for b in bones}
    global_rest = {b.name: matrix12(target[b.name]['global']) for b in bones}
    local_rest = local_rests(None, bones, space)
    manifest_bones = [dict(name=engine_names[b.name], parent=engine_names[b.parent.name] if b.parent else '', rest=affine(local_rest[i])) for i, b in enumerate(bones)]
    templates = Path(__file__).parent/'base_templates'
    for state in STATES:
        template = templates/(state+'.avanim')
        if not template.exists():
            raise ValueError('Base animation templates missing. Reinstall the complete add-on ZIP.')
        with zipfile.ZipFile(template) as archive:
            source = json.loads(archive.read('manifest.json'));data = archive.read('pose.bin')
        source_rest, source_rot, index = {}, {}, {}
        for i, b in enumerate(source['bones']):
            index[b['name']] = i
        def source_global(name):
            if name in source_rest:
                return source_rest[name]
            b = source['bones'][index[name]];rest = matrix12(b['rest'])
            source_rest[name] = source_global(b['parent']) @ rest if b['parent'] else rest
            return source_rest[name]
        for name in index:
            source_rot[name] = source_global(name).to_quaternion()
        # Calibrate limb directions, so an A-pose rest rig does not keep its
        # shoulder bias when following a T-pose template (and vice versa).
        children={'LeftUpperArm':'LeftLowerArm','RightUpperArm':'RightLowerArm',
                  'LeftLowerArm':'LeftHand','RightLowerArm':'RightHand',
                  'LeftUpperLeg':'LeftLowerLeg','RightUpperLeg':'RightLowerLeg',
                  'LeftLowerLeg':'LeftFoot','RightLowerLeg':'RightFoot',
                  'LeftFoot':'LeftToes','RightFoot':'RightToes',
                  'LeftHand':'LeftHandMiddle1','RightHand':'RightHandMiddle1'}
        alignments={}
        for role,child in children.items():
            if role in index and child in index and role in target_by_role and child in target_by_role:
                a=global_rest[target_by_role[child]].translation-global_rest[target_by_role[role]].translation
                b=source_rest[child].translation-source_rest[role].translation
                if a.length>.00001 and b.length>.00001:alignments[role]=a.normalized().rotation_difference(b.normalized())
        ratio = global_rest[target_by_role['Hips']].translation.y/max(.1, source_rest['Hips'].translation.y)
        result = bytearray();previous = {}
        for frame in range(source['frameCount']):
            posed_source = {}
            def source_pose(name):
                if name in posed_source:
                    return posed_source[name]
                i = index[name];b = source['bones'][i]
                v = struct.unpack_from('<10f', data, (frame*len(index)+i)*40)
                d = Matrix.LocRotScale(Vector(v[:3]), Quaternion((v[6], *v[3:6])), Vector(v[7:]))
                local = matrix12(b['rest']) @ d
                posed_source[name] = source_pose(b['parent']) @ local if b['parent'] else local
                return posed_source[name]
            globals_ = {}
            for i, b in enumerate(bones):
                role = engine_names[b.name];parent = globals_.get(b.parent.name) if b.parent else None
                local = local_rest[i].copy()
                if role in index:
                    source_delta = source_pose(role).to_quaternion() @ source_rot[role].inverted()
                    rotation = source_delta @ alignments.get(role,Quaternion()) @ global_rest[b.name].to_quaternion()
                    local_rotation = parent.to_quaternion().inverted() @ rotation if parent else rotation
                    local = Matrix.LocRotScale(local.translation, local_rotation, Vector((1, 1, 1)))
                    if role == 'Hips':
                        offset = (source_pose(role).translation-source_rest[role].translation)*ratio
                        if parent:
                            offset = parent.to_3x3().inverted() @ offset
                        local.translation += offset
                globals_[b.name] = parent @ local if parent else local
                d = local_rest[i].inverted() @ local
                t, q, s = d.decompose();q.normalize()
                if b.name in previous and q.dot(previous[b.name]) < 0:
                    q.negate()
                previous[b.name] = q.copy()
                result.extend(struct.pack('<10f', *t, q.x, q.y, q.z, q.w, *s))
        manifest = {k: source[k] for k in ('version', 'mode', 'loop', 'duration', 'sampleRate', 'frameCount')}
        manifest.update(id='base_'+state, name=state.replace('_', ' ').title(), profile=space['profile'], scope='full',
                        bones=manifest_bones, visibility={}, audio=None, baseGameplay=True, origin='floor',
                        source=dict(application='Avatar Export', template=state))
        for key in ('authoredLocomotion','gaitClearance','authoredAttack','useOwner'):
            if key in source:manifest[key]=source[key]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', json.dumps(manifest).encode('utf-8'));archive.writestr('pose.bin', result)
        package.writestr('base_animations/'+state+'.avanim', buffer.getvalue())


def export_avatar(context, filepath):
    from . import armature_for
    from .face_tools import stop_preview
    stop_preview()
    from .morphs import AvatarMorphs
    active=context.view_layer.objects.active;selected=list(context.selected_objects);mode=context.mode;edit=context.edit_object or next((o for o in context.scene.objects if o.mode=='EDIT'),None)
    if edit:mode='EDIT'
    if mode!='OBJECT':
        if edit:context.view_layer.objects.active=edit
        bpy.ops.object.mode_set(mode='OBJECT')
        context.view_layer.objects.active=active
    try:
        arm=armature_for(context)
        meshes=[o for o in context.scene.objects if o.type=='MESH' and (o.find_armature()==arm or o.parent==arm)]
        with AvatarMorphs(meshes) as morphs:
            result=_export_avatar(context,filepath,morphs)
            result.update(morphTargets=len(morphs.targets),sparseMorphVertices=morphs.entries,morphProjection=morphs.projected)
            return result
    finally:
        for obj in context.selected_objects:obj.select_set(False)
        for obj in selected:obj.select_set(True)
        context.view_layer.objects.active=edit or active
        if mode!='OBJECT':bpy.ops.object.mode_set(mode='EDIT' if mode.startswith('EDIT') else mode)
        context.view_layer.objects.active=active

def _export_avatar(context, filepath, morphs):
    from . import physics_preview
    physics_preview.stop()
    from . import armature_for
    scene, arm = context.scene, armature_for(context)
    if arm.library or arm.data.library:raise ValueError('Make the linked armature local before exporting an editable avatar.')
    mapping = mapping_for(scene, arm)
    missing = [role for role in REQUIRED if role not in mapping]
    if missing:
        raise ValueError('Map humanoid bones first: '+', '.join(missing))
    if len(set(mapping.values())) != len(mapping):
        raise ValueError('Each humanoid role must use a different bone.')
    def ancestor(child,parent):
        bone=arm.data.bones[mapping[child]]
        while bone:
            if bone.name==mapping[parent]:return True
            bone=bone.parent
        return False
    chains=[('Spine','Hips'),('Head','Spine')]
    for side in ('Left','Right'):
        chains += [(side+'UpperArm','Spine'),(side+'LowerArm',side+'UpperArm'),(side+'Hand',side+'LowerArm'),
                   (side+'UpperLeg','Hips'),(side+'LowerLeg',side+'UpperLeg'),(side+'Foot',side+'LowerLeg')]
    for child,parent in chains:
        if not ancestor(child,parent):raise ValueError('Humanoid hierarchy mismatch: '+child+' must descend from '+parent)
    bones = ordered_bones(arm)
    if len(bones) > 512:
        raise ValueError('Maximum 512 bones. Export a cleaned rig without unused control bones.')
    scales = arm.matrix_world.to_scale()
    if min(scales) <= 0 or max(scales)-min(scales) > max(scales)*.001 or arm.matrix_world.determinant() <= 0:
        raise ValueError('Apply non-uniform/mirrored armature object scale before exporting.')
    meshes = [o for o in scene.objects if o.type == 'MESH' and (o.find_armature() == arm or o.parent == arm)]
    if len({str(o.get('avatar_mesh',o.name)) for o in meshes})!=len(meshes):raise ValueError('Duplicate avatar_mesh aliases.')
    if not meshes:
        raise ValueError('No meshes bound/parented to the selected armature.')
    slug = re.sub('[^a-z0-9_-]', '_', scene.avatar_model_id.lower()).strip('_')[:48]
    if not slug:
        raise ValueError('Use a Latin Avatar ID, for example my_avatar.')
    profile = 'custom_'+slug
    if len(scene.avatar_model_name.strip())>80:raise ValueError('Avatar name must be at most 80 characters.')
    original_pose = arm.data.pose_position;temporary_modifiers = []
    target = Path(filepath).with_suffix('.mcavatar');target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.mcavatar.tmp')
    try:
        arm.data.pose_position = 'REST';context.view_layer.update()
        # World-space ground and right shoulder axis define one normalized export space.
        bounds_meshes=[obj for obj in meshes if not obj.hide_render and obj.visible_get(view_layer=context.view_layer)] or meshes
        points = [obj.matrix_world @ Vector(corner) for obj in bounds_meshes for corner in obj.bound_box]
        low, high = min(p.z for p in points), max(p.z for p in points)
        if high-low < .00001:
            raise ValueError('Model must be upright on Blender Z (use object rotation to orient it).')
        right = arm.matrix_world.to_3x3() @ (arm.data.bones[mapping['RightUpperArm']].head_local-arm.data.bones[mapping['LeftUpperArm']].head_local)
        right.z = 0
        if right.length < .00001:
            raise ValueError('Cannot infer facing: left/right upper arms overlap. Check bone mapping.')
        right.normalize();up = Vector((0, 0, 1));back = right.cross(up)
        basis = Matrix((right, up, back)).to_4x4()
        hips_world = arm.matrix_world @ arm.data.bones[mapping['Hips']].head_local
        origin = Vector((hips_world.x, hips_world.y, low))
        factor = scene.avatar_model_height*100/(high-low)
        conversion = Matrix.Scale(factor, 4) @ basis @ Matrix.Translation(-origin)
        unit_scale = factor*scales.x
        reverse_map = {name: role for role, name in mapping.items()}
        names = {b.name: reverse_map.get(b.name, str(b.get('avatar_bone',b.name))) for b in bones}
        if len(set(names.values())) != len(bones):
            raise ValueError('Bone aliases collide with another bone name. Rename the unmapped bone.')
        space = dict(version=1, profile=profile, conversion=[list(row) for row in conversion], unitScale=unit_scale, bones={})
        globals_ = {b.name: engine_matrix(space, arm.matrix_world, b.matrix_local) for b in bones}
        for b in bones:
            local = globals_[b.parent.name].inverted() @ globals_[b.name] if b.parent else globals_[b.name]
            space['bones'][b.name] = dict(name=names[b.name], rest=affine(local), global_=affine(globals_[b.name]))
            space['bones'][b.name]['global'] = space['bones'][b.name].pop('global_')
        ids = {b.name: i for i, b in enumerate(bones)}
        estimate = 0
        for obj in meshes:
            ev = obj.evaluated_get(context.evaluated_depsgraph_get());mesh = ev.to_mesh()
            try:mesh.calc_loop_triangles();estimate += len(mesh.loop_triangles)
            finally:ev.to_mesh_clear()
        ratio = min(1, scene.avatar_model_triangles/max(1, estimate))
        if ratio < .999:
            for obj in meshes:
                modifier = obj.modifiers.new('_AvatarExportBudget', 'DECIMATE');modifier.ratio = ratio
                temporary_modifiers.append((obj, modifier))
            context.view_layer.update()
        with tempfile.TemporaryDirectory(prefix='avatar_model_') as directory:
            directory = Path(directory);texture_cache = {};chunks = [];visibility = {};total = 0;deforming = 0;skipped_textures=[]
            for obj in meshes:
                mesh_name=str(obj.get('avatar_mesh',obj.name))
                visibility[mesh_name] = not obj.hide_render and obj.visible_get(view_layer=context.view_layer)
                ev = obj.evaluated_get(context.evaluated_depsgraph_get());mesh = ev.to_mesh(preserve_all_data_layers=True, depsgraph=context.evaluated_depsgraph_get())
                try:
                    mesh.calc_loop_triangles();transform = conversion @ obj.matrix_world
                    normal_transform = transform.to_3x3().inverted().transposed()
                    morph_vertices=morphs.evaluated(obj,mesh,transform,normal_transform)
                    groups = {g.index: ids.get(g.name) for g in obj.vertex_groups}
                    parts = {}
                    for tri in mesh.loop_triangles:
                        mat = obj.material_slots[tri.material_index].material if tri.material_index < len(obj.material_slots) else None
                        filename, uv_name, textured = _texture(mat, texture_cache, directory,int(scene.avatar_model_texture_size),skipped_textures)
                        uv_layer = mesh.uv_layers.get(uv_name) if uv_name else mesh.uv_layers.active
                        if textured and uv_layer is None:
                            raise ValueError('Image-textured mesh has no UV map: '+obj.name)
                        output = parts.setdefault(filename, bytearray())
                        for loop_index in tri.loops:
                            loop = mesh.loops[loop_index];v = mesh.vertices[loop.vertex_index]
                            p = transform @ v.co
                            normal = normal_transform @ mesh.corner_normals[loop_index].vector;normal.normalize()
                            uv = uv_layer.data[loop_index].uv if uv_layer else Vector((0, 0))
                            weights = sorted([(groups[g.group], g.weight) for g in v.groups if groups.get(g.group) is not None and g.weight > .00001], key=lambda value: -value[1])[:4]
                            if not weights:
                                fallback = ids.get(obj.parent_bone, ids[mapping['Hips']]) if obj.parent_type == 'BONE' else ids[mapping['Hips']]
                                weights = [(fallback, 1)]
                            if any(index!=ids[mapping['Hips']] and weight>.001 for index,weight in weights):deforming+=1
                            amount = sum(w for _, w in weights);bone_ids = [i for i, _ in weights];quantized = [round(w/amount*255) for _, w in weights]
                            quantized[0] += 255-sum(quantized)
                            bone_ids += [bone_ids[0]]*(4-len(bone_ids));quantized += [0]*(4-len(quantized))
                            morphs.vertex(morph_vertices.get(loop.vertex_index,()),(obj.name,filename),len(output)//44)
                            output.extend(struct.pack('<8f4H4B', *p, *normal, float(uv.x), 1-float(uv.y), *bone_ids, *quantized));total += 1
                    for filename, data in parts.items():
                        morphs.finish_part((obj.name,filename),len(chunks))
                        chunks.append((mesh_name, filename, data))
                finally:ev.to_mesh_clear()
            if deforming==0:raise ValueError('Meshes have no limb/head skin weights or bone attachments. Rig and weight the model before exporting.')
            if not chunks or total > 600000:
                raise ValueError('Empty avatar or more than 200,000 triangles after reduction. Lower the triangle budget.')
            texture_remap,texture_stats=_fit_textures(directory,[filename for _,filename,_ in chunks])
            chunks=[(name,texture_remap[filename],data) for name,filename,data in chunks]
            binary = io.BytesIO();binary.write(b'NZM4');binary.write(struct.pack('<I', len(bones)))
            for b in bones:
                _write_string(binary, names[b.name]);binary.write(struct.pack('<i', ids[b.parent.name] if b.parent else -1))
                info = space['bones'][b.name];binary.write(struct.pack('<24f', *info['rest'], *info['global']))
            textures = sorted({filename for _, filename, _ in chunks});binary.write(struct.pack('<I', len(textures)))
            for name in textures:_write_string(binary, name)
            binary.write(struct.pack('<I', len(chunks)))
            for mesh_name, filename, data in chunks:
                _write_string(binary, mesh_name);_write_string(binary, '')
                binary.write(struct.pack('<II', textures.index(filename), len(data)//44));binary.write(data)
            manifest = dict(version=1, id=profile, name=scene.avatar_model_name.strip() or slug,
                            model='model.nzm', visibility=visibility, height=scene.avatar_model_height,
                            source=dict(application='Blender', version=bpy.app.version_string),
                            textureOptimization=texture_stats,missingTextures=skipped_textures,
                            humanoid=mapping)
            from .bone_physics import export_physics
            morph_binary=morphs.binary() if morphs.targets else None
            if morph_binary:manifest['morphs']='morphs.bin'
            from .face_tools import export_face
            face=export_face(arm,names)
            if face:manifest['face']=face
            manifest['physics']=export_physics(arm,bones,names,globals_,conversion,unit_scale,set(mapping.values()))
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as package:
                package.writestr('avatar.json', json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
                package.writestr('model.nzm', binary.getvalue())
                if morph_binary:package.writestr('morphs.bin',morph_binary)
                for filename in textures:package.write(directory/filename, 'textures/'+filename)
                _base_animations(bones, space, package)
                package.writestr('README.txt', 'Place this .mcavatar in config/AnyModel/custom/<your avatar folder>/, then G > Avatars. Base AVANIM files are copied once beside the package and can be replaced live.\n')
            os.replace(temporary, target)
        # Only metadata is attached to the rig. Native bone names, weights and Actions stay editable.
        arm[SPACE_KEY] = json.dumps(space)
        arm['anymodel_package_path']=str(target)
        for b in bones:b['avatar_bone'] = names[b.name]
        scene.avatar_action_profile = profile
        return dict(path=str(target), bones=len(bones), triangles=total//3, textures=len(textures),textureOptimization=texture_stats,skippedTextures=skipped_textures)
    finally:
        for obj, modifier in temporary_modifiers:obj.modifiers.remove(modifier)
        arm.data.pose_position = original_pose;context.view_layer.update()
        if temporary.exists():temporary.unlink()


class AVATAR_PG_bone_map(bpy.types.PropertyGroup):
    role: StringProperty()
    bone: StringProperty()


class AVATAR_OT_auto_map(bpy.types.Operator):
    bl_idname = 'avatar_model.auto_map'
    bl_label = 'Detect Humanoid Bones'
    def execute(self, context):
        from . import armature_for
        try:
            arm = armature_for(context);scene = context.scene
            scene.avatar_humanoid_map.clear();mapping = mapping_for(scene, arm)
            for role in all_roles():
                item = scene.avatar_humanoid_map.add();item.role = role;item.bone = mapping.get(role, '')
            self.report({'INFO'}, 'Mapped '+str(sum(role in mapping for role in REQUIRED))+'/'+str(len(REQUIRED))+' required bones')
            return {'FINISHED'}
        except Exception as ex:self.report({'ERROR'}, str(ex));return {'CANCELLED'}


class AVATAR_OT_export_model(bpy.types.Operator, ExportHelper):
    bl_idname = 'export_scene.minecraft_avatar'
    bl_label = 'Build AnyModel Avatar'
    filename_ext = '.mcavatar'
    filter_glob: StringProperty(default='*.mcavatar', options={'HIDDEN'})
    def execute(self, context):
        try:
            result = export_avatar(context, self.filepath)
            stats=result['textureOptimization']
            if result['skippedTextures']:self.report({'WARNING'},'Skipped unavailable textures: '+', '.join(result['skippedTextures'][:8]))
            self.report({'INFO'}, f"Avatar ready: {result['triangles']} triangles, {result['bones']} bones, {result['textures']} textures ({stats['pixelsAfter']/1000000:.1f} MP)")
            return {'FINISHED'}
        except Exception as ex:self.report({'ERROR'}, str(ex));return {'CANCELLED'}


class AVATAR_PT_model_export(bpy.types.Panel):
    bl_label = 'AnyModel Avatar'
    bl_idname = 'AVATAR_PT_model_export'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AnyModel'
    def draw(self, context):
        from . import armature_for
        layout, scene = self.layout, context.scene
        layout.prop(scene, 'avatar_model_name');layout.prop(scene, 'avatar_model_id')
        layout.prop(scene, 'avatar_model_height');layout.prop(scene, 'avatar_model_triangles')
        layout.prop(scene, 'avatar_model_texture_size')
        layout.label(text='Texture memory budget fits automatically.')
        layout.label(text='Relative Shape Keys export automatically.')
        layout.operator('avatar_model.auto_map', icon='ARMATURE_DATA')
        layout.prop(scene, 'avatar_model_show_map')
        if scene.avatar_model_show_map:
            try:
                arm = armature_for(context)
                layout.prop(scene,'avatar_model_map_fingers')
                for item in scene.avatar_humanoid_map:
                    if item.role not in (*REQUIRED,*OPTIONAL) and not scene.avatar_model_map_fingers:continue
                    layout.prop_search(item, 'bone', arm.data, 'bones', text=item.role+(' *' if item.role in REQUIRED else ''))
                missing = [role for role in REQUIRED if role not in mapping_for(scene, arm)]
                if missing:layout.label(text='Missing: '+', '.join(missing[:3]), icon='ERROR')
            except ValueError as ex:layout.label(text=str(ex))
        layout.operator('export_scene.minecraft_avatar', icon='EXPORT')
        layout.label(text='Rigged humanoid + UV image textures.')
        layout.label(text='Drop package in config/AnyModel/custom/<folder>/')


CLASSES = (AVATAR_PG_bone_map, AVATAR_OT_auto_map, AVATAR_OT_export_model, AVATAR_PT_model_export)


def menu(self, context):self.layout.operator(AVATAR_OT_export_model.bl_idname, text='AnyModel Avatar (.mcavatar)')


def register():
    for cls in CLASSES:bpy.utils.register_class(cls)
    bpy.types.Scene.avatar_humanoid_map = CollectionProperty(type=AVATAR_PG_bone_map)
    bpy.types.Scene.avatar_model_name = StringProperty(name='Avatar name', default='My avatar')
    bpy.types.Scene.avatar_model_id = StringProperty(name='Avatar ID', default='my_avatar')
    bpy.types.Scene.avatar_model_height = FloatProperty(name='Default height (blocks)', min=.2, max=3, default=1.8)
    bpy.types.Scene.avatar_model_triangles = IntProperty(name='Triangle budget', min=1000, max=180000, default=100000)
    bpy.types.Scene.avatar_model_texture_size = EnumProperty(name='Max texture side',items=[
        ('1024','1024','Smaller export'),('2048','2048','Balanced texture size'),
        ('4096','4096','Higher detail; total memory budget still fits automatically'),
        ('8192','8192','Retain larger images when the total budget permits'),
    ],default='4096')
    bpy.types.Scene.avatar_model_map_fingers = BoolProperty(name='Show finger mapping',default=False)
    bpy.types.Scene.avatar_model_show_map = BoolProperty(name='Edit humanoid mapping', default=False)
    bpy.types.TOPBAR_MT_file_export.append(menu)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu)
    for name in ('avatar_humanoid_map','avatar_model_name','avatar_model_id','avatar_model_height','avatar_model_triangles','avatar_model_texture_size','avatar_model_show_map','avatar_model_map_fingers'):delattr(bpy.types.Scene, name)
    for cls in reversed(CLASSES):bpy.utils.unregister_class(cls)
