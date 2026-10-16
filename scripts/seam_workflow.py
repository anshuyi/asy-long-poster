"""Audited candidate previews; never changes a candidate to accepted by rendering it."""
import copy
import time
import uuid
from PIL import Image
from common import require, safe_path, read_json, digest, canonical, atomic_json, inspect, evidence


def signature(plan, images):
    upper, lower = plan['segments']
    return canonical({'upper': {k: upper[k] for k in ('id', 'version', 'sha256')},
                      'lower': {k: lower[k] for k in ('id', 'version', 'sha256')},
                      'upper_cut': upper['crop_box'][3], 'lower_cut': lower['crop_box'][1],
                      'seam': plan['seams'][0],
                      'protected_boxes': {sid: v['protected_boxes'] for sid, v in images.items()}})


def preview(root, state, args):
    from project_state import active, check_chain, segment_config_hash
    from stitch_images import validate_layout, render_canvas
    started = time.monotonic()
    policy = state.get('seam_policy', 'legacy')
    require(policy in ('natural', 'structural'), 'Choose an explicit preview policy first')
    require(not any(a['status'] == 'pending' for s in state['segments'].values() for a in s['attempts']), 'Resolve pending generation first')
    ids = list(state['segments'])
    sid = args['segment_id']
    idx = ids.index(sid)
    require(idx > 0, 'First segment has no seam')
    check_chain(root, state, ids[idx-1])
    upper = active(state, ids[idx-1])
    lower = next((v for v in state['segments'][sid]['versions'] if v['version'] == args['version']), None)
    require(lower and lower['status'] in ('qc_pending', 'accepted', 'failed'), 'Missing candidate')
    checked = evidence(root, args['self_review_path'], lower['sha256'], ('content', 'visual', 'geometry', 'outgoing'))
    require(checked['reviewer'] != 'automated' or state['config']['provider_capabilities']['mode'] == 'synthetic_test', 'Preview requires actual visual self-review')
    report = checked['report']
    require(isinstance(report.get('protected_boxes'), list), 'Missing candidate protection boxes')
    images = {ids[idx-1]: copy.deepcopy(upper), sid: copy.deepcopy(lower)}
    images[sid]['protected_boxes'] = report['protected_boxes']
    for v in images.values():
        require(digest(safe_path(root, v['original_path'], True)) == v['original_sha256'], 'Original changed')
        require(digest(safe_path(root, v['path'], True)) == v['sha256'], 'Candidate changed')
        info = inspect(safe_path(root, v['path'], True), state['limits']['max_pixels'])
        require((info['width'], info['height']) == (v['width'], v['height']), 'Candidate dimensions changed')
    plan = read_json(safe_path(root, args['plan_path'], True))
    require(plan.get('schema_version') == '1.1', 'Candidate preview requires schema 1.1')
    width, height = validate_layout(root, plan, state, images, preview=True)
    sig = signature(plan, images)
    key = f'{ids[idx-1]}->{sid}'
    counter = state.setdefault('seam_plan_attempts', {}).setdefault(key, {'count': 0, 'signature': None})
    if counter['signature'] != sig:
        require(counter['count'] < state['limits']['seam_attempts'], 'Seam revision limit reached')
        counter.update(count=counter['count']+1, signature=sig)
    canvas, seams = render_canvas(root, plan, width, height)
    destination = safe_path(root, plan['output']['path'])
    with destination.open('xb') as stream:
        canvas.save(stream, format='PNG')
    seam = seams[0]
    local_path = f'previews/{destination.stem}-join.png'
    local = safe_path(root, local_path)
    with local.open('xb') as stream:
        canvas.crop((0, max(0, seam['y']-160), width, min(height, seam['y']+seam['overlap_px']+160))).save(stream, format='PNG')
    pid = uuid.uuid4().hex
    record = {'preview_id': pid, 'policy': policy, 'policy_revision': state.get('policy_revision', 0),
              'plan': plan, 'signature': sig, 'self_review': {k: checked[k] for k in ('path', 'sha256')},
              'images': images, 'segment_hashes': {s: segment_config_hash(state['config'], s) for s in images},
              'output': {'path': plan['output']['path'], **inspect(destination)},
              'join': {'path': local_path, **inspect(local)}, 'elapsed_seconds': round(time.monotonic()-started, 3)}
    path = f'records/preview-{pid}.json'
    atomic_json(safe_path(root, path), record, exclusive=True)
    state.setdefault('seam_previews', {})[pid] = {'path': path, 'sha256': digest(safe_path(root, path))}
    state.setdefault('timings', []).append({'kind': 'seam_preview', 'elapsed_seconds': record['elapsed_seconds']})
    return record


def verify_preview(root, state, pid, lower_boxes=None):
    from project_state import active, segment_config_hash
    binding = state.get('seam_previews', {}).get(pid)
    require(binding, 'Missing registered seam preview')
    path = safe_path(root, binding['path'], True)
    require(digest(path) == binding['sha256'], 'Preview record changed')
    record = read_json(path)
    require(record['policy'] == state.get('seam_policy') and record['policy_revision'] == state.get('policy_revision', 0), 'Preview uses old policy')
    require(digest(safe_path(root, record['self_review']['path'], True)) == record['self_review']['sha256'], 'Candidate review changed')
    for seam in record['plan']['seams']:
        for prefix in ('mask', 'haze_mask'):
            if seam.get(prefix + '_path'):
                require(digest(safe_path(root, seam[prefix + '_path'], True)) == seam[prefix + '_sha256'], 'Preview mask changed')
    for item in (record['output'], record['join']):
        require(digest(safe_path(root, item['path'], True)) == item['sha256'], 'Preview image changed')
    ids = list(record['images'])
    upper = active(state, ids[0])
    for sid, old in record['images'].items():
        current = next(v for v in state['segments'][sid]['versions'] if v['version'] == old['version'])
        require(digest(safe_path(root, current['path'], True)) == old['sha256'] == current['sha256'], 'Preview source changed')
        require(digest(safe_path(root, current['original_path'], True)) == current['original_sha256'], 'Original changed')
        require(record['segment_hashes'][sid] == segment_config_hash(state['config'], sid), 'Preview content/baseline stale')
    require(upper['version'] == record['images'][ids[0]]['version'] and upper['protected_boxes'] == record['images'][ids[0]]['protected_boxes'], 'Preview uses old upper image/protection')
    if lower_boxes is not None:
        require(lower_boxes == record['images'][ids[1]]['protected_boxes'], 'Preview uses different candidate protection')
    return record


def review_binding(root, state, report, sid, version, boxes):
    pid = report.get('preview_id')
    require(pid, 'Incoming review must reference an actual seam preview')
    record = verify_preview(root, state, pid, boxes)
    require(list(record['images'])[-1] == sid and record['images'][sid]['version'] == version, 'Preview belongs to another candidate')
    require(any(f.get('path') == record['join']['path'] and f.get('sha256') == record['join']['sha256'] for f in report['evidence_files']), 'Review must include seam preview pixels')
    return {'preview_id': pid, 'signature': record['signature'], 'policy_revision': record['policy_revision']}


def validate_final_seams(root, plan, state):
    from project_state import active
    require(plan.get('schema_version') == '1.1', 'Reviewed preview workflow requires plan schema 1.1')
    for i, seam in enumerate(plan['seams']):
        edge = state['connections'][f"{seam['upper']}->{seam['lower']}"]
        record = verify_preview(root, state, edge.get('preview_id'), active(state, seam['lower'])['protected_boxes'])
        images = {s: active(state, s) for s in (seam['upper'], seam['lower'])}
        pair = {'segments': plan['segments'][i:i+2], 'seams': [seam]}
        require(signature(pair, images) == record['signature'] == edge['signature'], 'Final seam differs from reviewed preview')
