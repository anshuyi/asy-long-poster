"""Shared deterministic helpers; no network, credentials, or shell execution."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import tempfile
from PIL import Image, ImageOps, ImageCms


class ContractError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ContractError(message)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def read_json(path):
    with open(path, encoding="utf-8-sig") as stream:
        return json.load(stream)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def safe_path(root, relative, must_exist=False):
    root = Path(root).resolve()
    require(isinstance(relative, str) and relative, "Path must be nonempty")
    raw = Path(relative)
    require(not raw.anchor and not raw.drive and ":" not in relative and ".." not in raw.parts, "Only project-relative paths allowed")
    candidate = root / raw
    node = candidate
    while node != root:
        require(not node.is_symlink() and not (hasattr(node, "is_junction") and node.is_junction()), "Linked paths are not allowed")
        require(node.parent != node, "Path ancestor does not reach project root")
        node = node.parent
    resolved = candidate.resolve()
    require(resolved.is_relative_to(root) and resolved != root, "Path escapes project")
    if must_exist:
        require(resolved.is_file(), f"Missing file: {relative}")
    return resolved


def atomic_json(path, data, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        return
    fd, temp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def box(value, width, height):
    require(isinstance(value, list) and len(value) == 4 and all(integer(v) for v in value), "Invalid rectangle")
    x0, y0, x1, y1 = value
    require(0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height, "Rectangle out of bounds")
    return value


def intersects(a, b):
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def inspect(path, max_pixels=40_000_000):
    with Image.open(path) as im:
        require(im.width * im.height <= max_pixels, "Image pixel limit exceeded")
        im.load()
        return {"width": im.width, "height": im.height, "mode": im.mode, "format": im.format, "sha256": digest(path)}


def normalize(source, destination, width, background=(255, 255, 255), max_pixels=40_000_000):
    require(integer(width, 1), "Invalid target width")
    destination = Path(destination)
    require(not destination.exists(), "Output already exists")
    inspect(source, max_pixels)
    with Image.open(source) as opened:
        im = ImageOps.exif_transpose(opened)
        original_size = im.size
        if im.mode == "P" and "transparency" in im.info:
            im = im.convert("RGBA")
        alpha = im.getchannel("A") if "A" in im.getbands() else None
        rgb = im.convert("RGB")
        if im.info.get("icc_profile"):
            import io
            rgb = ImageCms.profileToProfile(rgb, ImageCms.ImageCmsProfile(io.BytesIO(im.info["icc_profile"])), ImageCms.createProfile("sRGB"), outputMode="RGB")
        if alpha is not None:
            bg = Image.new("RGB", im.size, tuple(background))
            bg.paste(rgb, mask=alpha)
            rgb = bg
        height = max(1, round(rgb.height * width / rgb.width))
        require(width * height <= max_pixels, "Normalized pixel limit exceeded")
        rgb = rgb.resize((width, height), Image.Resampling.LANCZOS)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            rgb.save(stream, format="PNG")
    return {**inspect(destination, max_pixels), "oriented_source_size": list(original_size), "scale": width / original_size[0], "background": list(background)}


def evidence(root, relative, expected_hash, required_checks):
    path = safe_path(root, relative, True)
    report = read_json(path)
    require(report.get("subject_sha256") == expected_hash, "Review does not match subject hash")
    require(report.get("reviewer") in ("model", "human", "automated"), "Missing reviewer type")
    require(isinstance(report.get("notes"), str) and report["notes"].strip(), "Review needs observations")
    checks = report.get("checks", {})
    require(all(checks.get(k) == "pass" for k in required_checks), "Required checks failed or remain unreviewed")
    files = report.get("evidence_files", [])
    require(isinstance(files, list) and files, "Review needs evidence files")
    for item in files:
        p = safe_path(root, item["path"], True)
        require(digest(p) == item["sha256"], "Review evidence changed")
    return {"path": relative, "sha256": digest(path), "reviewer": report["reviewer"], "report": report}
