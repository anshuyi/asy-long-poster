"""Synthetic fixtures test mechanics only; they are not real image generation."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from common import ContractError, atomic_json, digest, safe_path, normalize
from project_state import execute, load, active, check_chain
from stitch_images import stitch, validate_plan


def config(n=3):
    return {"width": 100, "seam_policy": "legacy", "user_goal": {"purpose": "test"}, "poster_plan": {"theme": "test"},
            "visual_baseline": {"background": "white"}, "provider_capabilities": {"mode": "synthetic_test"},
            "segment_plan": [{"id": f"s{i:02d}", "text": str(i), "generation_notes": "synthetic test only"} for i in range(1, n+1)]}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def init(self, n=3, custom=None):
        return execute(self.root, "init", custom or config(n))

    def candidate(self, sid, color="white"):
        r = execute(self.root, "begin", {"segment_id": sid})
        path = self.root / "inputs" / (r["attempt_id"] + ".png")
        Image.new("RGB", (100, 200), color).save(path)
        return execute(self.root, "register", {"segment_id": sid, "attempt_id": r["attempt_id"], "source_path": str(path)})

    def review(self, sid, v, changes=None, accept=True):
        report = {"subject_sha256": v["sha256"], "reviewer": "automated", "notes": "Synthetic fixture; tests mechanics only",
                  "checks": {k: "pass" for k in ("content", "visual", "geometry", "outgoing", "incoming")},
                  "protected_boxes": [[10, 35, 90, 160]], "evidence_files": [{"path": v["path"], "sha256": v["sha256"]}]}
        idx = list(load(self.root)["segments"]).index(sid)
        if idx:
            report["previous_sha256"] = active(load(self.root), f"s{idx:02d}")["sha256"]
        if changes:
            report.update(changes)
        path = f"records/qc-{sid}-v{v['version']}-{len(list((self.root/'records').glob('qc*')))}.json"
        atomic_json(self.root / path, report, exclusive=True)
        execute(self.root, "review", {"segment_id": sid, "version": v["version"], "review_path": path, "accept": accept})
        return path

    def ready(self, n=3):
        self.init(n)
        for i in range(1, n+1):
            sid = f"s{i:02d}"
            self.review(sid, self.candidate(sid))

    def plan(self, mode="none", overlap=0, output="outputs/final-v1.png"):
        state = load(self.root)
        segments = []
        ids = list(state["segments"])
        for sid in ids:
            v = active(state, sid)
            segments.append({"id": sid, **{k: v[k] for k in ("version", "path", "sha256", "width", "height")}, "crop_box": [0, 0, 100, 200]})
        seams = [{"upper": ids[i], "lower": ids[i+1], "overlap_px": overlap, "blend": mode,
                  "feather_px": overlap, "connection_type": "replaceable_background", "reason": "synthetic fixture"} for i in range(len(ids)-1)]
        return {"schema_version": "1.0", "plan_version": 1, "coordinate_space": "normalized_pixels", "target_width": 100,
                "segments": segments, "seams": seams, "output": {"path": output, "format": "PNG"}, "expected_height": len(ids)*200 - (len(ids)-1)*overlap}

    def bind(self, plan, name="records/plan.json"):
        atomic_json(self.root/name, plan, exclusive=True)
        execute(self.root, "plan", {"plan_path": name})

    def test_first_must_pass_before_next(self):
        self.init()
        self.candidate("s01")
        with self.assertRaises(ContractError):
            execute(self.root, "begin", {"segment_id": "s02"})

    def test_actual_connection_observation_is_carried_forward(self):
        self.init(2)
        self.review('s01',self.candidate('s01'),{'connection_observations':{'bottom_width':26,'center':52}})
        r=execute(self.root,'begin',{'segment_id':'s02'})
        self.assertEqual(r['previous_connection_state']['connection_observations']['bottom_width'],26)
        self.assertIn('bottom_width',r['prompt'])

    def test_repair_carries_both_real_reference_files(self):
        self.init(2)
        self.review('s01',self.candidate('s01'))
        v=self.candidate('s02')
        r=execute(self.root,'begin',{'segment_id':'s02','repair_version':v['version'],'repair_notes':'only repair border'})
        self.assertEqual(len(r['references']),2)
        self.assertEqual(r['references'][1]['sha256'],v['sha256'])
        self.assertEqual(r['references'][1]['role'],'edit_target')

    def test_connection_crop_is_real_and_traceable(self):
        self.init(2)
        self.review('s01',self.candidate('s01'))
        r=execute(self.root,'begin',{'segment_id':'s02','connection_crop_box':[0,170,100,200]})
        crop=r['references'][1]
        self.assertEqual(crop['source_sha256'],r['references'][0]['sha256'])
        self.assertEqual(crop['source_box'],[0,170,100,200])
        with Image.open(self.root/crop['path']) as im:self.assertEqual(im.size,(100,30))

    def test_register_recovers_after_state_commit_interruption(self):
        self.init(1)
        r=execute(self.root,'begin',{'segment_id':'s01'})
        source=self.root/'inputs/source.png';Image.new('RGB',(100,200),'white').save(source)
        args={'segment_id':'s01','attempt_id':r['attempt_id'],'source_path':str(source)}
        def fail_commit(path,data,**kwargs):
            if Path(path).name=='project_state.json':raise OSError('injected interruption')
            return atomic_json(path,data,**kwargs)
        with patch('project_state.atomic_json',side_effect=fail_commit):
            with self.assertRaises(OSError):execute(self.root,'register',args)
        self.assertEqual(load(self.root)['segments']['s01']['attempts'][0]['status'],'pending')
        v=execute(self.root,'register',args)
        self.assertEqual(v['version'],1)
        self.assertEqual(load(self.root)['calls'],1)

    def test_single_segment_real_output_dimensions(self):
        self.ready(1)
        p = self.plan()
        self.assertEqual(p["seams"], [])
        self.bind(p)
        out = stitch(self.root)
        self.assertEqual((out["width"], out["height"]), (100, 200))

    def test_two_segment_feather_pixels_and_height(self):
        self.ready(2)
        self.bind(self.plan("feather", 20))
        out = stitch(self.root)
        self.assertEqual(out["height"], 380)
        with Image.open(self.root/out["path"]) as im:
            self.assertEqual(im.getpixel((50, 190)), (255, 255, 255))

    def test_safe_measured_crops_are_executed(self):
        self.ready(2)
        p=self.plan('feather',20)
        p['segments'][0]['crop_box']=[0,0,100,190]
        p['segments'][1]['crop_box']=[0,10,100,200]
        p['expected_height']=360
        self.bind(p)
        self.assertEqual(stitch(self.root)['height'],360)

    def test_mask_and_hard_blend(self):
        self.ready(2)
        p = self.plan("mask", 20)
        Image.new("L", (100,20),128).save(self.root/'inputs/mask.png')
        p["seams"][0].update(mask_path="inputs/mask.png", mask_sha256=digest(self.root/'inputs/mask.png'))
        self.bind(p)
        self.assertEqual(stitch(self.root)["height"],380)
        p = self.plan("hard",20,"outputs/hard.png")
        self.bind(p,'records/hard.json')
        self.assertEqual(stitch(self.root)["height"],380)

    def test_feather_weights_on_different_colors(self):
        self.init(2)
        self.review('s01',self.candidate('s01','red'))
        self.review('s02',self.candidate('s02','blue'))
        self.bind(self.plan('feather',20))
        out=stitch(self.root)
        with Image.open(self.root/out['path']) as im:
            self.assertEqual(im.getpixel((50,180)),(255,0,0))
            self.assertEqual(im.getpixel((50,199)),(0,0,255))
            px=im.getpixel((50,190))
            self.assertEqual(px[0]+px[2],255)
            self.assertGreater(px[0],100)
            self.assertGreater(px[2],100)

    def test_pending_replacement_blocks_export(self):
        self.ready(1)
        p=self.plan()
        execute(self.root,'begin',{'segment_id':'s01'})
        with self.assertRaises(ContractError):self.bind(p)

    def test_middle_replacement_stales_only_adjacent_dependency(self):
        self.ready(3)
        old3 = copy.deepcopy(active(load(self.root), "s03"))
        self.bind(self.plan())
        self.review("s02", self.candidate("s02", "ivory"))
        state = load(self.root)
        self.assertIsNone(state["stitch_plan"])
        self.assertTrue(state["segments"]["s03"]["needs_recheck"])
        self.assertEqual(old3, active(state,"s03"))
        with self.assertRaises(ContractError):
            check_chain(self.root,state)
        report={"subject_sha256":old3["sha256"],"previous_sha256":active(state,"s02")["sha256"],"reviewer":"automated","notes":"injected replacement fixture","checks":{"incoming":"pass"},"evidence_files":[{"path":old3["path"],"sha256":old3["sha256"]}]}
        atomic_json(self.root/'records/recheck.json',report)
        execute(self.root,'recheck',{'segment_id':'s03','review_path':'records/recheck.json'})
        self.assertTrue(check_chain(self.root,load(self.root)))
        self.assertEqual(load(self.root)["calls"],4)

    def test_rejected_candidate_preserves_active(self):
        self.ready(1)
        old = active(load(self.root), "s01")
        v = self.candidate("s01", "red")
        self.review("s01", v, {"notes": "Injected wrong text"}, accept=False)
        self.assertEqual(active(load(self.root),"s01"),old)

    def test_unknown_request_not_repeated(self):
        self.init()
        execute(self.root,"begin",{"segment_id":"s01"})
        with self.assertRaises(ContractError):
            execute(self.root,"begin",{"segment_id":"s01"})
        self.assertEqual(load(self.root)["calls"],1)

    def test_limit_persists_after_failures(self):
        self.init(1)
        for _ in range(3):
            r=execute(self.root,"begin",{"segment_id":"s01"})
            execute(self.root,"request_failed",{"segment_id":"s01","attempt_id":r["attempt_id"],"definite":True,"reason":"test failure"})
        with self.assertRaises(ContractError):
            execute(self.root,"begin",{"segment_id":"s01"})
        self.assertEqual(execute(self.root,"status",{})["calls"],3)

    def test_money_limit_requires_upper_bound(self):
        c=config(1);c['limits']={'money_budget':1}
        self.init(custom=c)
        with self.assertRaises(ContractError):
            execute(self.root,'begin',{'segment_id':'s01'})
        with self.assertRaises(ContractError):
            execute(self.root,'begin',{'segment_id':'s01','reserved_cost':2})

    def test_unavailable_provider_does_not_generate(self):
        c=config(1);c['provider_capabilities']['mode']='text_only'
        self.init(custom=c)
        with self.assertRaises(ContractError):
            execute(self.root,'begin',{'segment_id':'s01'})

    def test_replan_keeps_call_budget_and_invalidates_content(self):
        self.ready(2)
        execute(self.root,'replan',{'segment_updates':{'s02':{'text':'18'}},'reason':'user changed number'})
        self.assertEqual(load(self.root)['calls'],2)
        self.assertEqual(load(self.root)['limits']['total_calls'],6)
        with self.assertRaises(ContractError):
            check_chain(self.root,load(self.root))
        self.review('s02',self.candidate('s02'))
        self.assertTrue(check_chain(self.root,load(self.root)))

    def test_append_does_not_expand_budget(self):
        self.ready(1)
        execute(self.root,'replan',{'append_segments':[{'id':'s02','text':'next','generation_notes':'next'}],'reason':'add segment'})
        self.assertEqual(load(self.root)['limits']['total_calls'],3)
        self.assertTrue(load(self.root)['segments']['s01']['needs_recheck'])

    def test_geometry_failures_are_rejected(self):
        self.ready(2)
        p=self.plan()
        variants=[]
        q=copy.deepcopy(p);q['segments'][0]['crop_box']=[0,0,100,201];variants.append(q)
        q=copy.deepcopy(p);q['segments'][0]['crop_box']=[1,0,100,200];variants.append(q)
        q=copy.deepcopy(p);q['segments'][0]['crop_box']=[0,50,100,200];variants.append(q)
        q=copy.deepcopy(p);q['segments'][0]['sha256']='fake';variants.append(q)
        q=copy.deepcopy(p);q['segments'].reverse();variants.append(q)
        q=copy.deepcopy(p);q['seams'][0]['blend']='unknown';variants.append(q)
        q=copy.deepcopy(p);q['expected_height']=401;variants.append(q)
        q=copy.deepcopy(p);q['output']['path']='../evil.png';variants.append(q)
        q=copy.deepcopy(p);q['output']['path']='normalized/s01-v1.png';variants.append(q)
        q=self.plan('feather',50);variants.append(q)
        q=self.plan('mask',20);variants.append(q)
        for q in variants:
            with self.subTest(plan=q),self.assertRaises((ContractError,KeyError)):
                validate_plan(self.root,q,load(self.root))

    def test_changed_image_and_review_rejected(self):
        self.ready(1)
        v=active(load(self.root),'s01')
        with (self.root/v['path']).open('ab') as f:f.write(b'changed')
        with self.assertRaises(ContractError):check_chain(self.root,load(self.root))

    def test_mutated_plan_after_binding_is_rejected(self):
        self.ready(1);p=self.plan();self.bind(p)
        p['expected_height']=201;atomic_json(self.root/'records/plan.json',p)
        with self.assertRaises(ContractError):stitch(self.root)

    def test_final_requires_all_semantic_checks(self):
        self.ready(1);self.bind(self.plan());out=stitch(self.root)
        r={'subject_sha256':out['sha256'],'plan_sha256':out['plan_sha256'],'reviewer':'automated','notes':'synthetic fixture','checks':{'content':'pass','visual':'pass','seams':'pass','goal':'pending'},'evidence_files':[{'path':out['path'],'sha256':out['sha256']}]}
        atomic_json(self.root/'records/final.json',r)
        with self.assertRaises(ContractError):execute(self.root,'complete',{'review_path':'records/final.json'})
        r['checks']['goal']='pass';atomic_json(self.root/'records/final.json',r)
        execute(self.root,'complete',{'review_path':'records/final.json'})
        self.assertEqual(load(self.root)['phase'],'completed')

    def test_wrong_text_check_cannot_accept(self):
        self.init(1);v=self.candidate('s01')
        with self.assertRaises(ContractError):self.review('s01',v,{'checks':{'content':'fail'}})
        self.assertIsNone(load(self.root)['segments']['s01']['active_version'])

    def test_idempotent_pixels_with_new_output_path(self):
        self.ready(2);self.bind(self.plan('feather',20));a=stitch(self.root)
        self.bind(self.plan('feather',20,'outputs/repeat.png'),'records/repeat.json');b=stitch(self.root)
        with Image.open(self.root/a['path']) as x,Image.open(self.root/b['path']) as y:self.assertEqual(x.tobytes(),y.tobytes())
        self.assertEqual(load(self.root)['calls'],2)

    def test_revision_and_writer_lock(self):
        self.init(1)
        with self.assertRaises(ContractError):execute(self.root,'block',{'reason':'x'},expected_revision=3)
        (self.root/'.state.lock').write_text('test owner')
        with self.assertRaises(ContractError):execute(self.root,'block',{'reason':'x'})
        self.assertTrue((self.root/'.state.lock').exists())

    def test_path_and_pixel_limits(self):
        self.ready(1)
        for path in ('../escape','C:/escape','/escape'):
            with self.assertRaises(ContractError):safe_path(self.root,path)
        s=load(self.root);s['limits']['max_pixels']=1
        with self.assertRaises(ContractError):validate_plan(self.root,self.plan(),s)

    def test_disk_limit(self):
        self.ready(1)
        with patch('stitch_images.shutil.disk_usage',return_value=type('Disk',(),{'free':0})()):
            with self.assertRaises(ContractError):validate_plan(self.root,self.plan(),load(self.root))

    def test_normalize_alpha_and_no_overwrite(self):
        p=self.root/'in.png';q=self.root/'out.png'
        Image.new('RGBA',(20,30),(0,0,0,0)).save(p)
        normalize(p,q,40,[240,230,220])
        with Image.open(q) as im:self.assertEqual((im.size,im.getpixel((0,0))),((40,60),(240,230,220)))
        with self.assertRaises(ContractError):normalize(p,q,40)


if __name__ == '__main__':
    unittest.main()
