import argparse
import json
from common import inspect

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("paths", nargs="+")
    a = p.parse_args()
    print(json.dumps([{ "path": path, **inspect(path)} for path in a.paths], ensure_ascii=False, indent=2))
