#!/usr/bin/env python3
"""Update the consensus-specs submodule to the latest release tag."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CONSENSUS = ROOT / "consensus-specs"


def run_git(args: list[str]) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def latest_tag() -> str:
    tags = run_git(
        [
            "git",
            "-C",
            str(CONSENSUS),
            "tag",
            "--list",
            "v*",
            "--sort=-version:refname",
        ]
    ).splitlines()
    if not tags:
        tags = run_git(
            ["git", "-C", str(CONSENSUS), "tag", "--sort=-creatordate"]
        ).splitlines()
    if not tags:
        raise RuntimeError("No tags found in consensus-specs.")
    return tags[0]


def main() -> int:
    if not CONSENSUS.exists():
        print("consensus-specs submodule is missing.", file=sys.stderr)
        return 1
    try:
        run_git(["git", "-C", str(CONSENSUS), "fetch", "--tags", "--force"])
        tag = latest_tag()
        run_git(["git", "-C", str(CONSENSUS), "checkout", tag])
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"sync_consensus: {exc}", file=sys.stderr)
        return 1
    print(f"consensus-specs checked out at tag {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
