import argparse
import json
from common import normalize, safe_path

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--width", required=True, type=int)
    a = p.parse_args()
    print(json.dumps(normalize(a.source, safe_path(a.root, a.output), a.width), ensure_ascii=False, indent=2))
