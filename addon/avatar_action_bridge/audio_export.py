"""Mix sequencer audio without evaluating the avatar and video geometry."""
import bpy
import time
from contextlib import contextmanager


def strips_for(scene):
    editor=scene.sequence_editor
    return list(getattr(editor,'strips_all',getattr(editor,'sequences_all',[]))) if editor else []


def isolation_reason(scene):
    if any(o.type=='SPEAKER' and o.data.sound and not o.data.muted for o in scene.objects):
        return 'Spatial speakers require original scene'
    if any(s.type=='SCENE' for s in strips_for(scene)):
        return 'Nested scene strips require original scene'
    data=scene.animation_data
    if data and any(not d.mute for d in data.drivers):return 'Scene audio drivers require original scene'
    if bpy.app.handlers.frame_change_pre or bpy.app.handlers.frame_change_post:
        return 'Frame handlers require original scene'
    if scene.rigidbody_world:return 'Rigid body scene requires original scene'
    return None


@contextmanager
def audio_scene(scene,start,end):
    reason=isolation_reason(scene)
    if reason:
        yield scene,reason
        return
    collections_before=set(bpy.data.collections);actions_before=set(bpy.data.actions)
    copy=None
    try:
        copy=scene.copy();copy.name='__AnyModel audio export (temporary)'
        if copy.collection==scene.collection:raise RuntimeError('Audio scene collection was not copied.')
        original_strips={s.as_pointer() for s in strips_for(scene)}
        if any(s.as_pointer() in original_strips for s in strips_for(copy)):
            raise RuntimeError('Audio sequencer strips were not copied.')
        for child in list(copy.collection.children):copy.collection.children.unlink(child)
        for obj in list(copy.collection.objects):copy.collection.objects.unlink(obj)
        copy.camera=None;copy.use_nodes=False
        # Keep META hierarchy, channel mutes, strip offsets, speed/pitch and
        # scene F-curves exactly as copied. Video/effects cannot emit audio.
        def prune(collection):
            for strip in list(collection):
                if strip.type=='META':prune(getattr(strip,'strips',getattr(strip,'sequences',[])))
                elif strip.type!='SOUND':collection.remove(strip)
        if copy.sequence_editor:
            prune(getattr(copy.sequence_editor,'strips',None) or copy.sequence_editor.sequences)
        copy.frame_start,copy.frame_end=int(start),int(end)
        assert not len(copy.objects),'Audio scene must have no avatar objects'
        yield copy,'Isolated sequencer audio'
    finally:
        if copy is not None:bpy.data.scenes.remove(copy)
        for collection in list(bpy.data.collections):
            if collection not in collections_before and collection.users==0:bpy.data.collections.remove(collection)
        for action in list(bpy.data.actions):
            if action not in actions_before and action.users<=int(action.use_fake_user):bpy.data.actions.remove(action)


def mixdown(context,scene,filepath,start,end):
    """Scene-copy isolation preserves packed sound and all strip automation."""
    started=time.perf_counter()
    old_range=(scene.frame_start,scene.frame_end)
    try:
        with audio_scene(scene,start,end) as (mix_scene,method):
            if mix_scene==scene:mix_scene.frame_start,mix_scene.frame_end=int(start),int(end)
            print('AnyModel audio: '+method,flush=True)
            with context.temp_override(scene=mix_scene,view_layer=mix_scene.view_layers[0]):
                result=bpy.ops.sound.mixdown(filepath=str(filepath),check_existing=False,
                    container='WAV',codec='PCM',format='S16',channels='STEREO',mixrate=44100)
            return result,dict(method=method,seconds=time.perf_counter()-started)
    finally:
        scene.frame_start,scene.frame_end=old_range
