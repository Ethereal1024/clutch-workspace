#!/usr/bin/env python3
"""Write this component's release manifest — the one file a host's client reads.

Usage: release-manifest.py <artifact> <declaration> <out.json> [--asset NAME]

The manifest says what a release IS and where its bytes are:

    {"schema": 1, "name": ..., "interface": ..., "version": ...,
     "declaration": {"asset": "component.json", "sha256": "<64 hex>"},
     "artifacts": {"any": {"asset": "clutch-<name>.tar.gz", "sha256": "<64 hex>"}}}

Assets are named RELATIVE to the manifest (the release that holds it), so this
script never needs to know its own URL: the same manifest works from a GitHub
release, a mirror, or a directory on disk. `any` is the artifact key a
platform-independent build uses — this component is pure Python (stdlib only),
so one tar serves every machine; a platform-specific build would add keys like
`linux-x86_64` beside it (the host's COMPONENTS.md §一 has the vocabulary).

Both digests are sha256 of the BYTES as published: the client verifies the
artifact it downloads against `artifacts` before handing anything to a host, and
verifies the declaration against `declaration`. A manifest whose digest does not
match is refused, never guessed at — so this script must run on exactly the files
that will be uploaded, unmoved and unedited.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SCHEMA = 1
REQUIRED = ("name", "interface", "version")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="write clutch-component.json for one release")
    parser.add_argument("artifact", type=Path, help="the artifact, as it will be published")
    parser.add_argument("declaration", type=Path, help="the component's own component.json")
    parser.add_argument("out", type=Path, help="where to write the manifest")
    parser.add_argument("--asset", default="", help="the artifact's published name (default: its own path's)")
    args = parser.parse_args()

    for path in (args.artifact, args.declaration):
        if not path.is_file():
            print(f"no such file: {path}", file=sys.stderr)
            return 1
    try:
        declared = json.loads(args.declaration.read_text(encoding="utf-8"))
    except ValueError as err:
        print(f"{args.declaration} is not JSON: {err}", file=sys.stderr)
        return 1
    for field in REQUIRED:
        value = declared.get(field) if isinstance(declared, dict) else None
        if not isinstance(value, str) or not value:
            print(f"the declaration names no {field}", file=sys.stderr)
            return 1

    manifest = {
        "schema": SCHEMA,
        "name": declared["name"],
        "interface": declared["interface"],
        "version": declared["version"],
        "declaration": {"asset": args.declaration.name, "sha256": digest(args.declaration)},
        "artifacts": {"any": {"asset": args.asset or args.artifact.name, "sha256": digest(args.artifact)}},
    }
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{args.out}: {manifest['name']} {manifest['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
