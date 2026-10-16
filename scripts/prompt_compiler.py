"""Compile one image request without re-sending stale workflow prose."""
import json
from common import require


def compile_prompt(config, index, refs, observations, args, policy):
    spec = config['segment_plan'][index]
    execution = args.get('execution_spec', spec.get('execution_spec'))
    baseline = dict(config['visual_baseline'])
    if execution is None:
        require(not (observations and baseline.get('continuity')),
                'Provide execution_spec with current scene/composition; legacy continuity may conflict with observed pixels')
        execution = {'scene': spec['generation_notes']}
    require(isinstance(execution, dict) and isinstance(execution.get('scene'), str) and execution['scene'].strip(),
            'execution_spec.scene is required')
    require(set(execution).issubset({'scene', 'composition', 'constraints'}), 'Unknown execution_spec fields')
    continuity = baseline.pop('continuity', None)
    connection = ("Decorative paths may fade into the actual paper/background in the safe connection bands. "
                  "Match the actual previous image palette and texture. Do not require exact road width. "
                  "Keep semantic connectors and important subjects intact.") if policy == 'natural' else (
                  "Preserve structural continuity at the incoming edge. Actual measured previous outgoing geometry "
                  "takes precedence. Do not hide meaningful connections with haze.")
    data = {'global_plan': config['poster_plan'], 'baseline': baseline,
            'current_segment': spec['id'], 'segment_count': len(config['segment_plan']),
            'text_verbatim': spec['text'], 'execution_spec': execution,
            'reference_roles': [{'image_index': i+1, 'role': r.get('role', 'previous_slice'),
                                 'source_box': r.get('source_box')} for i, r in enumerate(refs)],
            'previous_connection_state': observations,
            'connection_requirement': connection,
            'first_segment': index == 0, 'last_segment': index == len(config['segment_plan'])-1,
            'targeted_edit': args.get('repair_notes', '')}
    if not observations and policy != 'natural':
        data['planned_connection'] = continuity
    prompt = ("Generate ONLY the current slice of ONE continuous vertical poster. Follow the structured specification below. "
              "Render only text_verbatim, exactly; no extra text or page numbers. Preserve the visual baseline, "
              "readable typography and margins. Important text, faces, logos and subjects must stay outside the "
              "top/bottom connection bands. Only the first slice opens with the total title; only the last slice closes. "
              "The scene/composition have been resolved for this request; do not reconstruct previous planning notes.\n" +
              json.dumps(data, ensure_ascii=False))
    return prompt, {'compiler': '2.2', 'execution_spec': execution,
                    'superseded_fields': ['visual_baseline.continuity', 'segment.generation_notes'] if execution != {'scene': spec['generation_notes']} else []}
