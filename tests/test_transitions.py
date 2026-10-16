"""Regression tests for preview-bound natural transitions and legacy compatibility."""
import copy
import unittest
from pathlib import Path
from PIL import Image, ImageChops
import test_workflow as old
from common import ContractError, atomic_json, digest, read_json
from project_state import execute, load, active, check_chain
from stitch_images import stitch, blend_region, validate_layout
from prompt_compiler import compile_prompt


class TransitionTests(unittest.TestCase):
    setUp = old.WorkflowTests.setUp
    tearDown = old.WorkflowTests.tearDown
    init = old.WorkflowTests.init
    candidate = old.WorkflowTests.candidate
    review = old.WorkflowTests.review
    plan = old.WorkflowTests.plan
    bind = old.WorkflowTests.bind

    def prepare(self, policy='natural'):
        cfg=old.config(2);cfg['seam_policy']=policy
        self.init(custom=cfg)
        self.review('s01',self.candidate('s01'))
        self.v=self.candidate('s02')
        self.report={'subject_sha256':self.v['sha256'],'reviewer':'automated','notes':'Synthetic mechanics only',
                     'checks':{k:'pass' for k in ('content','visual','geometry','outgoing','incoming')},
                     'protected_boxes':[[10,35,90,160]],'evidence_files':[{'path':self.v['path'],'sha256':self.v['sha256']}]}
        self.report['previous_sha256']=active(load(self.root),'s01')['sha256']
        atomic_json(self.root/'records/self.json',self.report)

    def preview(self, mode='feather', strength=.6, name='one'):
        u=active(load(self.root),'s01');v=self.v
        segments=[{'id':sid,**{k:item[k] for k in ('version','path','sha256','width','height')},'crop_box':[0,0,100,200]} for sid,item in [('s01',u),('s02',v)]]
        seam={'upper':'s01','lower':'s02','blend':mode,'overlap_px':20,'feather_px':20,'connection_type':'replaceable_background','reason':'test'}
        if mode=='haze':
            seam.update(haze_strength=strength,background_rgb=[255,255,255],background_reviewed=True,
                        background_samples={'upper':[[0,180,8,200]],'lower':[[0,0,8,20]]})
        p={'schema_version':'1.1','plan_version':1,'coordinate_space':'normalized_pixels','target_width':100,'segments':segments,'seams':[seam],
           'expected_height':380,'output':{'format':'PNG','path':f'previews/{name}.png'}}
        atomic_json(self.root/f'records/{name}.json',p)
        return execute(self.root,'preview',{'segment_id':'s02','version':v['version'],'self_review_path':'records/self.json','plan_path':f'records/{name}.json'})

    def accept_preview(self, pr):
        report=copy.deepcopy(self.report);report['preview_id']=pr['preview_id']
        report['evidence_files'].append({'path':pr['join']['path'],'sha256':pr['join']['sha256']})
        atomic_json(self.root/'records/accept.json',report)
        execute(self.root,'review',{'segment_id':'s02','version':self.v['version'],'review_path':'records/accept.json'})

    def test_preview_is_not_acceptance_and_final_pixels_match(self):
        self.prepare();pr=self.preview('haze')
        self.assertIsNone(load(self.root)['segments']['s02']['active_version'])
        with self.assertRaises(ContractError):check_chain(self.root,load(self.root))
        self.accept_preview(pr)
        plan=copy.deepcopy(pr['plan']);plan['output']['path']='outputs/final.png';self.bind(plan)
        out=stitch(self.root)
        with Image.open(self.root/out['path']) as a,Image.open(self.root/pr['output']['path']) as b:
            self.assertIsNone(ImageChops.difference(a,b).getbbox())
        self.assertEqual(load(self.root)['calls'],2)

    def test_natural_review_cannot_skip_preview(self):
        self.prepare()
        with self.assertRaises(ContractError):self.review('s02',self.v)

    def test_changed_plan_and_preview_are_rejected(self):
        self.prepare();pr=self.preview();self.accept_preview(pr)
        p=copy.deepcopy(pr['plan']);p['output']['path']='outputs/final.png';p['seams'][0]['feather_px']=18
        with self.assertRaises(ContractError):self.bind(p)
        with (self.root/pr['join']['path']).open('ab') as f:f.write(b'changed')
        with self.assertRaises(ContractError):check_chain(self.root,load(self.root))

    def test_protection_cannot_change_after_preview(self):
        self.prepare();pr=self.preview();self.report['protected_boxes']=[[0,0,100,200]]
        with self.assertRaises(ContractError):self.accept_preview(pr)

    def test_structural_policy_rejects_haze(self):
        self.prepare('structural')
        with self.assertRaises(ContractError):self.preview('haze')

    def test_haze_bad_strength_color_and_foreground_rejected(self):
        self.prepare()
        for i,value in enumerate((float('nan'),-1,2)):
            with self.assertRaises(ContractError):self.preview('haze',value,str(i))
        # Sample RGB near black cannot be passed off as white background.
        with Image.open(self.root/self.v['path']) as image:
            image=image.copy();image.paste((0,0,0),(0,0,8,20));image.save(self.root/self.v['path'])
        with self.assertRaises(ContractError):self.preview('haze',name='changed-source')

    def test_haze_endpoints_and_no_change_outside_band(self):
        up=Image.new('RGB',(3,20),(20,40,60));lo=Image.new('RGB',(3,20),(60,80,100))
        seam={'blend':'haze','feather_px':20,'haze_strength':1,'background_rgb':[240,240,240]}
        result=blend_region(self.root,up,lo,seam)
        self.assertEqual(result.getpixel((1,0)),up.getpixel((1,0)))
        self.assertEqual(result.getpixel((1,19)),lo.getpixel((1,19)))
        self.prepare();pr=self.preview('haze');self.accept_preview(pr)
        p=copy.deepcopy(pr['plan']);p['output']['path']='outputs/final.png';self.bind(p);out=stitch(self.root)
        with Image.open(self.root/out['path']) as image,Image.open(self.root/self.v['path']) as lower:
            self.assertEqual(image.crop((0,200,100,380)).tobytes(),lower.crop((0,20,100,200)).tobytes())

    def test_preview_budget_persists_across_policy_change(self):
        self.prepare()
        for i in range(3):self.preview('haze',i/3,str(i))
        with self.assertRaises(ContractError):self.preview('haze',1,'four')
        execute(self.root,'policy',{'policy':'structural','reason':'test change'})
        self.assertEqual(load(self.root)['seam_plan_attempts']['s01->s02']['count'],3)
        self.assertEqual(load(self.root)['calls'],2)

    def test_reconsider_preserves_history_and_rejects_content_failure(self):
        self.prepare('legacy')
        self.review('s02',self.v,{'checks':{'content':'pass','incoming':'fail'}},accept=False)
        execute(self.root,'policy',{'policy':'natural','reason':'approved new policy'})
        execute(self.root,'reconsider',{'segment_id':'s02','version':1,'reason':'new preview'})
        item=load(self.root)['segments']['s02']['versions'][0]
        self.assertEqual(len(item['rejections']),1);self.assertEqual(load(self.root)['calls'],2)
        self.review('s02',self.v,{'checks':{'content':'fail','incoming':'fail'}},accept=False)
        execute(self.root,'policy',{'policy':'structural','reason':'test'})
        with self.assertRaises(ContractError):execute(self.root,'reconsider',{'segment_id':'s02','version':1,'reason':'must refuse'})

    def test_compiler_removes_stale_numbers_and_keeps_text(self):
        cfg=old.config(2);cfg['visual_baseline']['continuity']='road 24px'
        cfg['segment_plan'][1]['generation_notes']='road 24px; legacy scene'
        observed={'connection_observations':{'outgoing':{'road_width':201}}}
        with self.assertRaises(ContractError):compile_prompt(cfg,1,[],observed,{},'natural')
        prompt,meta=compile_prompt(cfg,1,[],observed,{'execution_spec':{'scene':'pencil drawing a plane'}},'natural')
        self.assertNotIn('24px',prompt);self.assertIn('201',prompt);self.assertIn('text_verbatim',prompt)
        self.assertEqual(meta['execution_spec']['scene'],'pencil drawing a plane')

    def test_old_state_without_policy_remains_legacy(self):
        self.prepare('legacy');state=load(self.root);state.pop('seam_policy');state.pop('policy_revision')
        atomic_json(self.root/'project_state.json',state)
        self.review('s02',self.v)
        self.assertTrue(check_chain(self.root,load(self.root)))

    def test_background_sampling_rejects_noise_and_color_mismatch(self):
        self.prepare();pr=self.preview('haze')
        plan=copy.deepcopy(pr['plan']);plan['output']['path']='previews/not-written.png'
        images=copy.deepcopy(pr['images'])
        plan['seams'][0]['background_rgb']=[220,220,220]
        with self.assertRaises(ContractError):validate_layout(self.root,plan,load(self.root),images,preview=True)
        plan['seams'][0]['background_rgb']=[255,255,255]
        with Image.open(self.root/self.v['path']) as opened:
            noisy=opened.copy()
        for y in range(20):
            for x in range(8):noisy.putpixel((x,y),(0,0,0) if (x+y)%2 else (255,255,255))
        noisy.save(self.root/self.v['path'])
        images['s02']['sha256']=digest(self.root/self.v['path']);plan['segments'][1]['sha256']=images['s02']['sha256']
        with self.assertRaises(ContractError):validate_layout(self.root,plan,load(self.root),images,preview=True)

    def test_mask_mutation_invalidates_reviewed_natural_seam(self):
        self.prepare()
        # Register a real mask in the preview recipe before the preview is produced.
        pr=self.preview('haze',name='base')
        plan=copy.deepcopy(pr['plan']);plan['output']['path']='previews/masked.png'
        Image.new('L',(100,20),0).save(self.root/'inputs/veil.png')
        plan['seams'][0].update(haze_mask_path='inputs/veil.png',haze_mask_sha256=digest(self.root/'inputs/veil.png'))
        atomic_json(self.root/'records/masked.json',plan)
        pr=execute(self.root,'preview',{'segment_id':'s02','version':1,'self_review_path':'records/self.json','plan_path':'records/masked.json'})
        self.accept_preview(pr)
        Image.new('L',(100,20),255).save(self.root/'inputs/veil.png')
        with self.assertRaises(ContractError):check_chain(self.root,load(self.root))


if __name__=='__main__':unittest.main()
