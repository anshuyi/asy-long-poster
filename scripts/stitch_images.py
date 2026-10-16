"""Execute measured full-width vertical crops and per-seam blends."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import sys
from PIL import Image, ImageChops
import math
from statistics import median
from common import require, integer, read_json, digest, safe_path, box, intersects, inspect, ContractError


def validate_plan(root, plan, state):
    from project_state import active, check_chain
    check_chain(root, state)
    images = {sid: active(state, sid) for sid in state['segments']}
    result = validate_layout(root, plan, state, images)
    if state.get('seam_policy', 'legacy') != 'legacy':
        from seam_workflow import validate_final_seams
        validate_final_seams(root, plan, state)
    return result


def validate_layout(root, plan, state, images, preview=False):
    require(plan.get("schema_version") in ("1.0", "1.1") and plan.get("coordinate_space") == "normalized_pixels", "Unsupported plan schema or coordinate space")
    require(integer(plan.get("plan_version"), 1), "Invalid plan version")
    width = plan.get("target_width")
    require(width == state["config"]["width"], "Plan width mismatch")
    segments, seams = plan.get("segments", []), plan.get("seams", [])
    ids = list(images)
    require([s.get("id") for s in segments] == ids, "Segments must appear once in current order")
    require(len(seams) == len(ids) - 1, "Wrong number of seams")
    heights = []
    for s in segments:
        v = images[s['id']]
        require(all(s.get(k) == v[k] for k in ("version", "path", "sha256", "width", "height")), "Plan references a non-current image")
        require(s["width"] == width, "Image width mismatch")
        crop = box(s.get("crop_box"), width, s["height"])
        require(crop[0] == 0 and crop[2] == width, "Horizontal crop/shift is unsupported")
        heights.append(crop[3] - crop[1])
        for p in v["protected_boxes"]:
            box(p, width, s['height'])
            require(p[1] >= crop[1] and p[3] <= crop[3], "Crop destroys protected content")
    overlaps = []
    for i, seam in enumerate(seams):
        require((seam.get("upper"), seam.get("lower")) == (ids[i], ids[i + 1]), "Seam order mismatch")
        overlap = seam.get("overlap_px")
        require(integer(overlap) and overlap < min(heights[i], heights[i + 1]), "Invalid overlap")
        overlaps.append(overlap)
        mode = seam.get("blend")
        require(mode in ("none", "hard", "feather", "mask", "haze"), "Unknown blend mode")
        require(mode != 'haze' or (plan['schema_version'] == '1.1' and state.get('seam_policy') == 'natural'), 'Haze requires natural policy and schema 1.1')
        require((overlap == 0) == (mode == "none"), "Blend and overlap disagree")
        require(isinstance(seam.get("reason"), str) and seam["reason"].strip(), "Seam needs observed rationale")
        require(seam.get("connection_type") in ("edge_continuation", "shared_content", "replaceable_background"), "Unknown connection type")
        if overlap:
            require(seam["connection_type"] != "edge_continuation", "Edge continuation alone does not justify overlap")
            upper, lower = segments[i], segments[i + 1]
            regions = ([0, upper["crop_box"][3] - overlap, width, upper["crop_box"][3]],
                       [0, lower["crop_box"][1], width, lower["crop_box"][1] + overlap])
            for item, region in zip((upper, lower), regions):
                require(not any(intersects(p, region) for p in images[item['id']]["protected_boxes"]), "Overlap touches protected content")
        if mode in ("feather", "haze"):
            require(integer(seam.get("feather_px"), 2) and seam["feather_px"] <= overlap, "Invalid feather width")
        if mode == "mask" or (mode == 'haze' and seam.get('haze_mask_path') is not None):
            prefix = 'haze_mask' if mode == 'haze' else 'mask'
            path = safe_path(root, seam.get(prefix + "_path"), True)
            require(digest(path) == seam.get(prefix + "_sha256"), "Mask hash mismatch")
            with Image.open(path) as mask:
                require(mask.mode == "L" and mask.size == (width, overlap), "Mask must be L mode W x overlap")
        if mode == 'haze':
            require(seam.get('connection_type') == 'replaceable_background', 'Haze needs replaceable background')
            strength = seam.get('haze_strength')
            require(type(strength) in (int, float) and math.isfinite(strength) and 0 <= strength <= 1, 'Invalid haze strength')
            color = seam.get('background_rgb')
            require(isinstance(color, list) and len(color) == 3 and all(integer(c) and c <= 255 for c in color), 'Invalid haze color')
            require(seam.get('background_reviewed') is True, 'Background sampling needs visual review')
            samples = seam.get('background_samples', {})
            require(set(samples) == {'upper', 'lower'}, 'Need both background samples')
            medians = []
            for role, item, region in zip(('upper', 'lower'), (segments[i], segments[i+1]), regions):
                boxes = samples[role]
                require(isinstance(boxes, list) and boxes, 'Missing background sample boxes')
                colors = []
                with Image.open(safe_path(root, item['path'], True)) as source:
                    for sample in boxes:
                        box(sample, width, item['height'])
                        require(sample[1] >= region[1] and sample[3] <= region[3], 'Background sample outside blend band')
                        require(not any(intersects(sample, p) for p in images[item['id']]['protected_boxes']), 'Background sample touches content')
                        pixels = list(source.convert('RGB').crop(tuple(sample)).get_flattened_data())
                        channels = [sorted(p[k] for p in pixels) for k in range(3)]
                        require(all(ch[int((len(ch)-1)*.95)] - ch[int((len(ch)-1)*.05)] <= 24 for ch in channels), 'Background sample has excessive texture/foreground')
                        colors.append([median(ch) for ch in channels])
                medians.append([median(c[k] for c in colors) for k in range(3)])
            require(max(abs(medians[0][k]-medians[1][k]) for k in range(3)) <= 24, 'Backgrounds differ too much for automatic haze')
            require(all(abs(color[k]-(medians[0][k]+medians[1][k])/2) <= 8 for k in range(3)), 'Haze color does not match sampled background')
    for i in range(len(heights)):
        require((overlaps[i-1] if i else 0) + (overlaps[i] if i < len(overlaps) else 0) < heights[i], "Overlap bands intersect")
    height = sum(heights) - sum(overlaps)
    require(plan.get("expected_height") == height, "Expected output height is wrong")
    require(width * height <= state["limits"]["max_pixels"], "Output pixel limit exceeded")
    output = plan.get("output", {})
    require(output.get("format") == "PNG", "Initial implementation exports lossless PNG")
    destination = safe_path(root, output.get("path"))
    prefix = 'previews/' if preview else 'outputs/'
    require(str(output["path"]).replace("\\", "/").startswith(prefix) and destination.suffix.lower() == ".png", "Output must be a new PNG under " + prefix)
    require(not destination.exists(), "Output already exists; use a new version")
    require(shutil.disk_usage(Path(root)).free > width * height * 12 + 1024 * 1024, "Insufficient disk space")
    return width, height


def blend_region(root, upper, lower, seam):
    width, overlap = upper.size
    if seam['blend'] == 'mask':
        with Image.open(safe_path(root, seam['mask_path'], True)) as opened:
            mask = opened.copy()
    else:
        if seam['blend'] == 'hard':
            values = [0 if k < overlap // 2 else 255 for k in range(overlap)]
        else:
            feather = seam['feather_px']
            start = (overlap - feather) // 2
            values = [max(0, min(255, round(255 * (k - start) / (feather - 1)))) for k in range(overlap)]
        mask = Image.frombytes('L', (1, overlap), bytes(values)).resize((width, overlap), Image.Resampling.NEAREST)
    combined = Image.composite(lower, upper, mask)
    if seam['blend'] == 'haze':
        # Smooth zero endpoints preserve the exact pixels outside the transition.
        values = [round(255 * seam['haze_strength'] * math.sin(math.pi*k/(overlap-1))**2) for k in range(overlap)]
        veil = Image.frombytes('L', (1, overlap), bytes(values)).resize((width, overlap), Image.Resampling.NEAREST)
        if seam.get('haze_mask_path'):
            with Image.open(safe_path(root, seam['haze_mask_path'], True)) as opened:
                veil = ImageChops.multiply(veil, opened)
        combined = Image.composite(Image.new('RGB', upper.size, tuple(seam['background_rgb'])), combined, veil)
    return combined


def render_canvas(root, plan, width, height):
    canvas = Image.new("RGB", (width, height))
    y = 0
    seams = []
    for i, s in enumerate(plan["segments"]):
        with Image.open(safe_path(root, s["path"], True)) as opened:
            image = opened.convert("RGB").crop(tuple(s["crop_box"]))
        overlap = plan["seams"][i-1]["overlap_px"] if i else 0
        y -= overlap
        if overlap:
            seam = plan["seams"][i-1]
            upper = canvas.crop((0, y, width, y + overlap))
            lower = image.crop((0, 0, width, overlap))
            combined = blend_region(root, upper, lower, seam)
            canvas.paste(image, (0, y))
            canvas.paste(combined, (0, y))
        else:
            canvas.paste(image, (0, y))
        if i:
            seams.append({"index": i, "y": y, "overlap_px": overlap})
        y += image.height
    return canvas, seams


def render(root, plan, state):
    width, height = validate_plan(root, plan, state)
    canvas, seams = render_canvas(root, plan, width, height)
    output = safe_path(root, plan["output"]["path"])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        canvas.save(stream, format="PNG")
    tag = output.stem
    previews = []
    preview = canvas.copy()
    preview.thumbnail((600, 4000))
    p = safe_path(root, f"previews/{tag}-overview.png")
    require(not p.exists(), "Preview exists")
    preview.save(p)
    previews.append(str(p.relative_to(Path(root).resolve())).replace("\\", "/"))
    for seam in seams:
        p = safe_path(root, f"previews/{tag}-seam-{seam['index']}.png")
        require(not p.exists(), "Seam preview exists")
        canvas.crop((0, max(0, seam["y"] - 180), width, min(height, seam["y"] + max(180, seam["overlap_px"]) + 180))).save(p)
        previews.append(str(p.relative_to(Path(root).resolve())).replace("\\", "/"))
    return {"path": plan["output"]["path"], **inspect(output, state["limits"]["max_pixels"]), "previews": previews}


def stitch(root):
    from project_state import transaction
    with transaction(root) as state:
        binding = state.get("stitch_plan")
        require(binding, "No current approved plan")
        path = safe_path(root, binding["path"], True)
        require(digest(path) == binding["sha256"], "Stitch plan changed")
        output = render(root, read_json(path), state)
        output["plan_sha256"] = binding["sha256"]
        state["final_output"] = output
        state["phase"] = "reviewing"
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(stitch(Path(args.root).resolve()), ensure_ascii=False, indent=2))
    except (ContractError, OSError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
