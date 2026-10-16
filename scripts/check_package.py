"""Check immutable source prompts, local references and Python syntax."""
import ast
import json
import re
from pathlib import Path
from common import digest, require

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / "prompt-manifest.json").read_text(encoding="utf-8"))
for item in manifest["prompts"]:
    require(digest(root / item["path"]) == item["sha256"], f"Original prompt changed: {item['path']}")
body = (root / "SKILL.md").read_text(encoding="utf-8")
for path in (root / "references").glob("*.md"):
    require(f"`references/{path.name}`" in body, f"Unlinked reference: {path.name}")
for path in root.rglob("*.md"):
    require(path.read_text(encoding="utf-8").count("```") % 2 == 0, f"Unclosed fence: {path}")
for path in (root / "templates").glob("*.json"):
    json.loads(path.read_text(encoding="utf-8"))
for path in (root / "scripts").glob("*.py"):
    ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
print("PASS: 5 original prompts, reference links, fences, JSON templates and Python syntax.")
