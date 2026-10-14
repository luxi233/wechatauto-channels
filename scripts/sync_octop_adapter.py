#!/usr/bin/env python3
"""Sync the Octop channel adapter into this repo's vendored copy.

Canonical source: ../octop-gateway/src/octop_gateway/channels/wechatauto
Vendored copy:    octop/octop_gateway/channels/wechatauto   (what
``manager_ops._octop_adapter_src`` picks up in a release-zip install)

Usage:
    python scripts/sync_octop_adapter.py          # copy fresh source in
    python scripts/sync_octop_adapter.py --check  # exit 1 on drift (CI gate)
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENDORED = REPO / "octop" / "octop_gateway" / "channels" / "wechatauto"


def _source(repo_arg: str | None) -> Path | None:
    candidates = []
    if repo_arg:
        candidates.append(Path(repo_arg))
    candidates.append(REPO.parent / "octop-gateway")
    for base in candidates:
        src = base / "src" / "octop_gateway" / "channels" / "wechatauto"
        if (src / "channel.py").exists():
            return src
    return None


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts)


def _drift(src: Path, dst: Path) -> list[str]:
    names_src = {p.relative_to(src) for p in _py_files(src)}
    names_dst = {p.relative_to(dst) for p in _py_files(dst)} \
        if dst.exists() else set()
    out = [f"missing in vendored: {p}" for p in sorted(names_src - names_dst)]
    out += [f"stale in vendored: {p}" for p in sorted(names_dst - names_src)]
    for rel in sorted(names_src & names_dst):
        if not filecmp.cmp(src / rel, dst / rel, shallow=False):
            out.append(f"content differs: {rel}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="report drift instead of copying")
    ap.add_argument("--src", default="", help="octop-gateway checkout path")
    args = ap.parse_args()

    src = _source(args.src or None)
    if src is None:
        print("octop-gateway checkout not found "
              "(expected sibling dir or --src)", file=sys.stderr)
        return 2

    drift = _drift(src, VENDORED)
    if args.check:
        if drift:
            print("vendored adapter is stale:")
            for line in drift:
                print(f"  {line}")
            return 1
        print("vendored adapter in sync")
        return 0

    if VENDORED.exists():
        shutil.rmtree(VENDORED)
    VENDORED.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, VENDORED,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if drift:
        print("synced; previous drift was:")
        for line in drift:
            print(f"  {line}")
    else:
        print(f"already in sync: {src} -> {VENDORED}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
