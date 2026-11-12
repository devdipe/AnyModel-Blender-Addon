"""Bounded linear-key reduction. Endpoints and sampled motion are retained."""
from array import array


def retained_indices(frames,values,tolerance):
    n=len(values)
    if n<=2:return list(range(n))
    if max(values)-min(values)<=tolerance:return [0,n-1]
    keep={0,n-1};pending=[(0,n-1)]
    while pending:
        left,right=pending.pop()
        if right-left<2:continue
        span=frames[right]-frames[left]
        if span<=0:
            keep.update(range(left,right+1));continue
        slope=(values[right]-values[left])/span
        maximum=tolerance;chosen=-1
        start,origin=values[left],frames[left]
        for i in range(left+1,right):
            error=abs(values[i]-(start+(frames[i]-origin)*slope))
            if error>maximum:maximum,chosen=error,i
        if chosen>=0:
            keep.add(chosen);pending.extend(((left,chosen),(chosen,right)))
    return sorted(keep)


def tolerance_for(path,world_scale):
    if path.endswith('.location'):return .00002/max(1e-6,world_scale)
    return 1e-5


def reduce_action(action,slot,world_scale):
    """Only linear baked curves, so edited Bezier curves are not resampled."""
    from . import fcurves
    before=after=0
    for curve in fcurves(action,slot):
        points=curve.keyframe_points;n=len(points);before+=n
        if n<=2 or any(not m.mute for m in curve.modifiers) or any(p.interpolation!='LINEAR' for p in points):after+=n;continue
        coords=array('f',[0])*(n*2);points.foreach_get('co',coords)
        frames,values=coords[0::2],coords[1::2]
        indices=retained_indices(frames,values,tolerance_for(curve.data_path,world_scale))
        if len(indices)<n:
            reduced=array('f')
            for i in indices:reduced.extend((frames[i],values[i]))
            points.clear();points.add(len(indices));points.foreach_set('co',reduced)
            for point in points:point.interpolation='LINEAR'
            curve.update()
        after+=len(points)
    return dict(before=before,after=after,removed=before-after)
