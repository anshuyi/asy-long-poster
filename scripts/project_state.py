"""Versioned local workflow. The host agent performs actual image tool calls."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
import time
from datetime import datetime, timezone
from common import (ContractError, require, integer, read_json, digest, canonical, safe_path,
                    atomic_json, inspect, normalize, evidence, box)

ENGINE = Path(__file__).resolve().parents[1]


def new_project(root, config):
    root = Path(root)
    require(not root.is_symlink(), "Linked project root")
    root = root.resolve()
    require(not root.is_relative_to(ENGINE), "Runtime data must be outside Skill")
    require(not (root / "project_state.json").exists(), "Project already exists")
    plans = config.get("segment_plan", [])
    require(plans and len(plans) <= 100, "Need 1..100 segments")
    width = config.get("width")
    require(integer(width, 1) and width <= 10000, "Invalid width")
    require(config.get("user_goal") and config.get("poster_plan") and config.get("visual_baseline"), "Goal, poster plan and baseline required")
    ids = [p.get("id") for p in plans]
    require(ids == [f"s{i:02d}" for i in range(1, len(plans) + 1)], "IDs must be sequential s01..sNN")
    for p in plans:
        require(isinstance(p.get("text"), str) and p.get("generation_notes"), "Every segment needs text and generation notes")
    cap = config.get("provider_capabilities", {})
    require(config.get('seam_policy', 'natural') in ('natural', 'structural', 'legacy'), 'Unknown seam policy')
    require(cap.get("mode") in ("reference", "edit", "user_supplied", "text_only", "unavailable", "synthetic_test"), "Unknown provider mode")
    limits = {"per_segment": 3, "total_calls": len(plans) * 3, "seam_attempts": 3, "max_pixels": 40_000_000, **config.get("limits", {})}
    require(all(integer(limits[k], 1) for k in ("per_segment", "total_calls", "seam_attempts", "max_pixels")), "Invalid limits")
    if "money_budget" in limits:
        require(type(limits["money_budget"]) in (int, float) and limits["money_budget"] >= 0, "Invalid money budget")
    root.mkdir(parents=True, exist_ok=True)
    for name in ("inputs", "originals", "normalized", "previews", "outputs", "records", "snapshots"):
        safe_path(root, name).mkdir(exist_ok=True)
    state = {"schema_version": "1.0", "project_id": config.get("project_id", root.name), "revision": 0,
             "phase": "planned", "config": config, "config_hash": canonical(config), "limits": limits,
             "calls": 0, "reserved_cost": 0, "segments": {}, "connections": {}, "stitch_plan": None,
             "final_output": None, "final_review": None, "events": [], "blocked_reason": None}
    state.update(seam_policy=config.get('seam_policy', 'natural'), policy_revision=0,
                 created_at=datetime.now(timezone.utc).isoformat())
    for sid in ids:
        state["segments"][sid] = {"active_version": None, "versions": [], "attempts": [], "needs_recheck": False}
    atomic_json(root / "project_state.json", state, exclusive=True)
    return state


def load(root):
    state = read_json(safe_path(root, "project_state.json", True))
    require(state.get("schema_version") == "1.0", "Unsupported state schema")
    require(integer(state.get("revision")), "Invalid revision")
    require(canonical(state["config"]) == state["config_hash"], "Configuration changed outside versioned workflow")
    require(list(state["segments"]) == [p["id"] for p in state["config"]["segment_plan"]], "Segment order mismatch")
    require(state["calls"] == sum(len(s["attempts"]) for s in state["segments"].values()), "Call ledger mismatch")
    return state


@contextmanager
def transaction(root, expected_revision=None):
    root = Path(root).resolve()
    lock = safe_path(root, ".state.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ContractError("Project has a writer lock; inspect owner before recovery")
    try:
        os.write(fd, str(os.getpid()).encode())
        state = load(root)
        old = copy.deepcopy(state)
        if expected_revision is not None:
            require(state["revision"] == expected_revision, "Revision conflict")
        yield state
        require(load(root)["revision"] == old["revision"], "Concurrent update")
        state["revision"] += 1
        snapshot_path = safe_path(root, f"snapshots/state-{old['revision']:06d}.json")
        if snapshot_path.exists():
            require(read_json(snapshot_path) == old, "Snapshot conflict; inspect recovery state")
        else:
            atomic_json(snapshot_path, old, exclusive=True)
        atomic_json(safe_path(root, "project_state.json"), state)
    finally:
        os.close(fd)
        lock.unlink()


def active(state, sid):
    segment = state["segments"][sid]
    number = segment["active_version"]
    require(number is not None, f"No accepted version: {sid}")
    return next(v for v in segment["versions"] if v["version"] == number)


def verify_version(root, version):
    for path_key, hash_key in (("path", "sha256"), ("original_path", "original_sha256")):
        require(digest(safe_path(root, version[path_key], True)) == version[hash_key], "Image hash mismatch")
    info = inspect(safe_path(root, version["path"], True))
    require((info["width"], info["height"]) == (version["width"], version["height"]), "Image dimensions changed")
    review = version.get("review")
    require(review and digest(safe_path(root, review["path"], True)) == review["sha256"], "Missing or changed review")


def edge_key(upper, lower):
    return f"{upper}->{lower}"


def segment_config_hash(config, sid):
    plans = config["segment_plan"]
    index = [p["id"] for p in plans].index(sid)
    return canonical({"plan": plans[index], "baseline": config["visual_baseline"],
                      "poster_plan": config["poster_plan"], "goal": config["user_goal"],
                      "first": index == 0, "last": index == len(plans) - 1})


def check_chain(root, state, through=None):
    ids = list(state["segments"])
    if through is None:
        require(not any(a["status"] == "pending" for s in state["segments"].values() for a in s["attempts"]), "Unresolved generation request blocks finalization")
    if through is not None:
        ids = ids[:ids.index(through) + 1]
    for i, sid in enumerate(ids):
        segment = state["segments"][sid]
        require(not segment["needs_recheck"], f"Stale segment: {sid}")
        v = active(state, sid)
        verify_version(root, v)
        require(v.get("reviewed_config_hash") == segment_config_hash(state["config"], sid), "Image review belongs to an old content/baseline version")
        if state.get('seam_policy', 'legacy') != 'legacy':
            require(v.get('reviewed_policy_revision') == state.get('policy_revision', 0), 'Review uses old seam policy')
        if i:
            prev = active(state, ids[i - 1])
            edge = state["connections"].get(edge_key(ids[i - 1], sid))
            require(edge and edge["upper_hash"] == prev["sha256"] and edge["lower_hash"] == v["sha256"], "Stale or missing connection")
            require(digest(safe_path(root, edge["review_path"], True)) == edge["review_sha256"], "Connection evidence changed")
            if state.get('seam_policy', 'legacy') != 'legacy':
                from seam_workflow import verify_preview
                record = verify_preview(root, state, edge.get('preview_id'), v['protected_boxes'])
                require(record['images'][sid]['version'] == v['version'], 'Connection uses old candidate')
    return True


def invalidate(state):
    state["stitch_plan"] = None
    state["final_output"] = None
    state["final_review"] = None
    state["phase"] = "generating"


def execute(root, operation, args, expected_revision=None):
    started = time.monotonic()
    root = Path(root).resolve()
    if operation == "init":
        return new_project(root, args)
    if operation == "status":
        state = load(root)
        errors = []
        try:
            check_chain(root, state)
            if state["phase"] == "completed":
                from validate_output import validate_final
                output = validate_final(root, state)
                final = state.get("final_review")
                require(final and digest(safe_path(root, final["path"], True)) == final["sha256"], "Final review changed")
        except (ContractError, OSError) as exc:
            errors.append(str(exc))
        pending = [a["id"] for s in state["segments"].values() for a in s["attempts"] if a["status"] == "pending"]
        return {"revision": state["revision"], "phase": state["phase"], "calls": state["calls"], "limits": state["limits"],
                "pending_requests": pending, "chain_ready": not errors, "issues": errors, "blocked_reason": state["blocked_reason"]}
    result = {}
    with transaction(root, expected_revision) as state:
        ids = list(state["segments"])
        sid = args.get("segment_id")
        if sid is not None:
            require(sid in ids, "Unknown segment")
            seg = state["segments"][sid]
            idx = ids.index(sid)
        if operation == 'policy':
            require(args.get('policy') in ('natural', 'structural') and args.get('reason'), 'Policy and reason required')
            require(not any(a['status'] == 'pending' for s in state['segments'].values() for a in s['attempts']), 'Resolve pending generation before changing policy')
            require(args['policy'] != state.get('seam_policy', 'legacy'), 'Policy is unchanged')
            state.setdefault('policy_history', []).append({'policy': state.get('seam_policy', 'legacy'), 'reason': args['reason'], 'revision': state.get('policy_revision', 0)})
            state['seam_policy'] = args['policy']
            state['policy_revision'] = state.get('policy_revision', 0)+1
            state['connections'] = {}
            for item in state['segments'].values():
                if item['active_version'] is not None:
                    item['needs_recheck'] = True
            invalidate(state)
        elif operation == 'preview':
            from seam_workflow import preview
            result = preview(root, state, args)
        elif operation == 'reconsider':
            v = next((v for v in seg['versions'] if v['version'] == args.get('version')), None)
            require(v and v['status'] == 'failed' and args.get('reason'), 'Need a rejected candidate and reason')
            rejection = v['rejections'][-1]
            old = read_json(safe_path(root, rejection['path'], True))
            require(digest(safe_path(root, rejection['path'])) == rejection['sha256'], 'Rejection record changed')
            require(old.get('checks', {}).get('content') == 'pass' and old.get('checks', {}).get('incoming') == 'fail', 'Only connection-only failures can be reconsidered')
            require(not any(old.get('checks', {}).get(k) == 'fail' for k in ('visual', 'geometry', 'outgoing')), 'Candidate has other failures')
            require(state.get('policy_revision', 0) > rejection.get('policy_revision', 0), 'Reconsideration needs an explicit newer policy')
            v['status'] = 'qc_pending'
            v.setdefault('reconsiderations', []).append({'reason': args['reason'], 'policy_revision': state['policy_revision']})
        elif operation == "begin":
            cap = state["config"]["provider_capabilities"]
            require(cap["mode"] not in ("text_only", "unavailable"), "Provider cannot accept actual reference images")
            require(not any(a["status"] == "pending" for s in state["segments"].values() for a in s["attempts"]), "Unresolved request: recover or record a definite failure before retry")
            require(len(seg["attempts"]) < state["limits"]["per_segment"] and state["calls"] < state["limits"]["total_calls"], "Call limit reached")
            refs = []
            previous = None
            previous_observations = None
            if idx:
                check_chain(root, state, ids[idx - 1])
                prev = active(state, ids[idx - 1])
                previous = {"id": ids[idx - 1], "version": prev["version"], "sha256": prev["sha256"]}
                refs = [{"path": prev["path"], "sha256": prev["sha256"]}]
                previous_review = read_json(safe_path(root, prev["review"]["path"], True))
                previous_observations = {"notes": previous_review["notes"], "connection_observations": previous_review.get("connection_observations", {}), "protected_boxes": prev["protected_boxes"]}
                if args.get("connection_crop_box") is not None:
                    from PIL import Image
                    region = box(args["connection_crop_box"], prev["width"], prev["height"])
                    crop_path = f"previews/reference-crop-{uuid.uuid4().hex}.png"
                    with Image.open(safe_path(root, prev["path"], True)) as opened:
                        with safe_path(root, crop_path).open("xb") as output:
                            opened.crop(tuple(region)).save(output, format="PNG")
                    refs.append({"path": crop_path, "sha256": digest(safe_path(root, crop_path)), "role": "connection_crop", "source_sha256": prev["sha256"], "source_box": region})
            if args.get("repair_version") is not None:
                repair = next((v for v in seg["versions"] if v["version"] == args["repair_version"]), None)
                require(repair and digest(safe_path(root, repair["path"], True)) == repair["sha256"], "Repair target missing or changed")
                refs.append({"path": repair["path"], "sha256": repair["sha256"], "role": "edit_target"})
            estimate = args.get("reserved_cost")
            if "money_budget" in state["limits"]:
                require(type(estimate) in (int, float) and estimate >= 0, "Money budget requires known per-call upper cost")
                require(state["reserved_cost"] + estimate <= state["limits"]["money_budget"], "Money budget exceeded")
                state["reserved_cost"] += estimate
            aid = uuid.uuid4().hex
            from prompt_compiler import compile_prompt
            prompt, compilation = compile_prompt(state['config'], idx, refs, previous_observations, args, state.get('seam_policy', 'legacy'))
            request = {"attempt_id": aid, "segment_id": sid, "provider": cap, "references": refs, "previous": previous, "previous_connection_state": previous_observations, "config_hash": state["config_hash"], "prompt": prompt, 'compilation': compilation}
            request_path = f"records/request-{aid}.json"
            atomic_json(safe_path(root, request_path), request, exclusive=True)
            seg["attempts"].append({"id": aid, "status": "pending", "request_path": request_path, "request_sha256": digest(safe_path(root, request_path)), "previous": previous, 'started_at': time.time()})
            state["calls"] += 1
            state["phase"] = "generating"
            state["blocked_reason"] = None
            result = request
        elif operation in ("register", "request_failed"):
            attempt = next((a for a in seg["attempts"] if a["id"] == args.get("attempt_id")), None)
            require(attempt and attempt["status"] == "pending", "No matching pending request")
            require(digest(safe_path(root, attempt["request_path"], True)) == attempt["request_sha256"], "Request record changed")
            if operation == "request_failed":
                require(args.get("definite") is True and args.get("reason"), "Unknown result cannot be treated as definite failure")
                attempt.update(status="failed", reason=args["reason"])
                state["blocked_reason"] = args["reason"]
                state["phase"] = "blocked"
            else:
                source = Path(args["source_path"]).resolve()
                require(source.is_file(), "Actual tool output missing")
                if idx and state["config"]["provider_capabilities"]["mode"] not in ("user_supplied", "synthetic_test"):
                    request = read_json(safe_path(root, attempt["request_path"], True))
                    require(args.get("submitted_reference_hashes") == [r["sha256"] for r in request["references"]], "Actual submitted references must match request")
                number = len(seg["versions"]) + 1
                op = f"originals/{sid}-v{number}{source.suffix.lower()}"
                np = f"normalized/{sid}-v{number}.png"
                original_path = safe_path(root, op)
                inspect(source, state["limits"]["max_pixels"])
                if original_path.exists():
                    require(digest(original_path) == digest(source), "Existing pending original differs")
                else:
                    with source.open("rb") as src, original_path.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
                normalized_path = safe_path(root, np)
                receipt_path = safe_path(root, f"records/normalization-{attempt['id']}.json")
                if normalized_path.exists():
                    require(receipt_path.exists(), "Normalized artifact has no recovery receipt; inspect without overwriting")
                    receipt = read_json(receipt_path)
                    require(receipt.get("source_sha256") == digest(source) and receipt.get("path") == np and receipt["metadata"]["sha256"] == digest(normalized_path), "Normalization recovery receipt mismatch")
                    meta = receipt["metadata"]
                else:
                    meta = normalize(source, normalized_path, state["config"]["width"], state["config"].get("background", [255, 255, 255]), state["limits"]["max_pixels"])
                    atomic_json(receipt_path, {"source_sha256": digest(source), "path": np, "metadata": meta}, exclusive=True)
                version = {"version": number, "status": "qc_pending", "path": np, **meta, "original_path": op, "original_sha256": digest(original_path), "attempt_id": attempt["id"], "generation_previous": attempt["previous"], "config_hash": state["config_hash"], "tool_call_id": args.get("tool_call_id"), "submitted_reference_hashes": args.get("submitted_reference_hashes", []), "review": None, "protected_boxes": []}
                seg["versions"].append(version)
                attempt.update(status="returned", version=number)
                if 'started_at' in attempt:
                    attempt['elapsed_to_register_seconds'] = round(time.time()-attempt['started_at'], 3)
                result = version
        elif operation == "review":
            v = next((v for v in seg["versions"] if v["version"] == args.get("version")), None)
            require(v and (v["status"] == "qc_pending" or (v["status"] == "accepted" and seg["needs_recheck"])), "Candidate is not pending review")
            require(digest(safe_path(root, v["path"], True)) == v["sha256"], "Candidate changed")
            report_path = args["review_path"]
            report = read_json(safe_path(root, report_path, True))
            if args.get("accept") is False:
                require(report.get("subject_sha256") == v["sha256"] and report.get("notes"), "Invalid rejection report")
                if v["status"] == "qc_pending":
                    v["status"] = "failed"
                v.setdefault("rejections", []).append({"path": report_path, "sha256": digest(safe_path(root, report_path)), "reason": report["notes"], "config_hash": state["config_hash"], 'policy_revision': state.get('policy_revision', 0)})
            else:
                checked = evidence(root, report_path, v["sha256"], ("content", "visual", "geometry", "outgoing"))
                require(checked["reviewer"] != "automated" or state["config"]["provider_capabilities"]["mode"] == "synthetic_test", "Content and visual review requires model or human")
                boxes = report.get("protected_boxes")
                require(isinstance(boxes, list), "Protected boxes must be explicitly reviewed")
                for b in boxes:
                    box(b, v["width"], v["height"])
                if idx:
                    check_chain(root, state, ids[idx - 1])
                    prev = active(state, ids[idx - 1])
                    require(report.get("previous_sha256") == prev["sha256"] and report.get("checks", {}).get("incoming") == "pass", "Incoming connection not verified")
                    state["connections"][edge_key(ids[idx - 1], sid)] = {"upper_hash": prev["sha256"], "lower_hash": v["sha256"], "review_path": report_path, "review_sha256": checked["sha256"]}
                    if state.get('seam_policy', 'legacy') != 'legacy':
                        from seam_workflow import review_binding
                        state['connections'][edge_key(ids[idx-1], sid)].update(review_binding(root, state, report, sid, v['version'], boxes))
                if v.get("review"):
                    v.setdefault("review_history", []).append(v["review"])
                v.update(status="accepted", review={k: checked[k] for k in ("path", "sha256", "reviewer")}, protected_boxes=boxes,
                         reviewed_config_hash=segment_config_hash(state["config"], sid), reviewed_policy_revision=state.get('policy_revision', 0))
                seg["active_version"] = v["version"]
                seg["needs_recheck"] = False
                if idx + 1 < len(ids) and state["segments"][ids[idx + 1]]["active_version"] is not None:
                    state["segments"][ids[idx + 1]]["needs_recheck"] = True
                    state["connections"].pop(edge_key(sid, ids[idx + 1]), None)
                invalidate(state)
        elif operation == "recheck":
            require(idx > 0, "First segment has no incoming edge")
            check_chain(root, state, ids[idx - 1])
            v, prev = active(state, sid), active(state, ids[idx - 1])
            verify_version(root, v)
            checked = evidence(root, args["review_path"], v["sha256"], ("incoming",))
            require(checked["reviewer"] != "automated" or state["config"]["provider_capabilities"]["mode"] == "synthetic_test", "Connection requires a visual reviewer")
            require(v.get("reviewed_config_hash") == segment_config_hash(state["config"], sid), "Content changed: full review required")
            require(checked["report"].get("previous_sha256") == prev["sha256"], "Recheck uses old preceding image")
            state["connections"][edge_key(ids[idx - 1], sid)] = {"upper_hash": prev["sha256"], "lower_hash": v["sha256"], "review_path": args["review_path"], "review_sha256": checked["sha256"]}
            if state.get('seam_policy', 'legacy') != 'legacy':
                require(v.get('reviewed_policy_revision') == state.get('policy_revision', 0), 'Policy changed: full review required')
                from seam_workflow import review_binding
                state['connections'][edge_key(ids[idx-1], sid)].update(review_binding(root, state, checked['report'], sid, v['version'], v['protected_boxes']))
            seg["needs_recheck"] = False
            invalidate(state)
        elif operation == "replan":
            require(not any(a["status"] == "pending" for s in state["segments"].values() for a in s["attempts"]), "Resolve pending request before changing plan")
            before = copy.deepcopy(state["config"])
            config = copy.deepcopy(before)
            allowed = {"segment_updates", "append_segments", "visual_baseline", "poster_plan", "user_goal", "reason"}
            require(set(args).issubset(allowed) and args.get("reason"), "Replan needs reason and supported fields")
            for key in ("visual_baseline", "poster_plan", "user_goal"):
                if key in args:
                    require(isinstance(args[key], dict) and args[key], "Plan values must be nonempty objects")
                    config[key] = args[key]
            for update_id, update in args.get("segment_updates", {}).items():
                require(update_id in ids and set(update).issubset({"text", "generation_notes"}), "Unsupported segment update")
                item = config["segment_plan"][ids.index(update_id)]
                item.update(update)
                require(isinstance(item["text"], str) and isinstance(item["generation_notes"], str) and item["generation_notes"], "Invalid segment text/notes")
            for item in args.get("append_segments", []):
                require(item.get("id") == f"s{len(config['segment_plan']) + 1:02d}" and isinstance(item.get("text"), str) and item.get("generation_notes"), "Invalid appended segment")
                config["segment_plan"].append(item)
                state["segments"][item["id"]] = {"active_version": None, "versions": [], "attempts": [], "needs_recheck": False}
            require(len(config["segment_plan"]) <= 100, "Too many segments")
            for existing in ids:
                if segment_config_hash(before, existing) != segment_config_hash(config, existing):
                    state["segments"][existing]["needs_recheck"] = True
            state.setdefault("config_history", []).append({"config": before, "hash": state["config_hash"], "reason": args["reason"]})
            state["config"] = config
            state["config_hash"] = canonical(config)
            invalidate(state)
        elif operation == "block":
            require(args.get("reason"), "Reason required")
            state["phase"] = "blocked"
            state["blocked_reason"] = args["reason"]
        elif operation == "plan":
            from stitch_images import validate_plan
            check_chain(root, state)
            plan = read_json(safe_path(root, args["plan_path"], True))
            validate_plan(root, plan, state)
            counter = state.setdefault("seam_plan_attempts", {})
            for seam in plan["seams"]:
                key = edge_key(seam["upper"], seam["lower"])
                signature = canonical(seam)
                old = counter.get(key, {"count": 0, "signature": None})
                if state.get('seam_policy', 'legacy') == 'legacy' and old["signature"] != signature:
                    require(old["count"] < state["limits"]["seam_attempts"], "Seam revision limit reached")
                    counter[key] = {"count": old["count"] + 1, "signature": signature}
            state["stitch_plan"] = {"path": args["plan_path"], "sha256": digest(safe_path(root, args["plan_path"])), "signature": canonical(plan)}
            state["final_output"] = None
            state["final_review"] = None
            state["phase"] = "stitching"
        elif operation == "complete":
            from validate_output import validate_final
            output = validate_final(root, state)
            checked = evidence(root, args["review_path"], output["sha256"], ("content", "visual", "seams", "goal"))
            require(checked["report"].get("plan_sha256") == state["stitch_plan"]["sha256"], "Final review uses different plan")
            require(checked["reviewer"] != "automated" or state["config"]["provider_capabilities"]["mode"] == "synthetic_test", "Final visual review must be model or human")
            state["final_review"] = {k: checked[k] for k in ("path", "sha256", "reviewer")}
            state["phase"] = "completed"
        else:
            raise ContractError(f"Unknown operation: {operation}")
        state["events"].append({"operation": operation, "segment_id": sid, "revision": state["revision"] + 1})
        state.setdefault('timings', []).append({'kind': operation, 'elapsed_seconds': round(time.monotonic()-started, 3)})
    return result or {"ok": True, "operation": operation}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("operation", choices=("init", "status", "begin", "register", "request_failed", "review", "recheck", "replan", "block", "plan", "complete", "policy", "preview", "reconsider"))
    parser.add_argument("--request", help="JSON arguments file; source_path may refer to a user-authorized external input")
    parser.add_argument("--revision", type=int)
    args = parser.parse_args()
    try:
        result = execute(args.root, args.operation, read_json(args.request) if args.request else {}, args.revision)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ContractError, OSError, KeyError, StopIteration, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
