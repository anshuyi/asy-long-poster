import argparse
import json
from common import require, safe_path, digest, inspect, read_json


def validate_final(root, state):
    from project_state import check_chain
    check_chain(root, state)
    binding, output = state.get("stitch_plan"), state.get("final_output")
    require(binding and output, "No current final output")
    require(digest(safe_path(root, binding["path"], True)) == binding["sha256"] == output["plan_sha256"], "Plan/output mismatch")
    plan = read_json(safe_path(root, binding["path"], True))
    if state.get('seam_policy', 'legacy') != 'legacy':
        from seam_workflow import validate_final_seams
        validate_final_seams(root, plan, state)
    info = inspect(safe_path(root, output["path"], True), state["limits"]["max_pixels"])
    require(info["sha256"] == output["sha256"], "Final image changed")
    require(info["width"] == plan["target_width"] and info["height"] == plan["expected_height"], "Final dimensions mismatch")
    return output


if __name__ == "__main__":
    from project_state import load
    p = argparse.ArgumentParser()
    p.add_argument("--root", required=True)
    a = p.parse_args()
    print(json.dumps(validate_final(a.root, load(a.root)), ensure_ascii=False, indent=2))
