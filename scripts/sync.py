#!/usr/bin/env python3
"""Sync annotated specs with the upstream consensus-specs repository."""

# NOTE: Legacy script kept for backwards compatibility while the CLI workflow matures.

from __future__ import annotations

import subprocess
import sys
from collections import defaultdict
from pathlib import Path
import re
from typing import Dict, Iterator, List

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
CONSENSUS_ROOT = SCRIPT_ROOT / "consensus-specs"
HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
NOTES_MARK = "<!-- NOTES-BEGIN -->"
IGNORED_TOP_LEVEL = {".git", "consensus-specs", "phase1"}
IGNORED_PATHS = {Path("README.md")}


class SyncError(RuntimeError):
    """Raised when the sync process cannot continue."""


def slugify(title: str) -> str:
    slug = title.strip().lower()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    return slug.strip('-')


def ensure_consensus_repo() -> None:
    if not CONSENSUS_ROOT.exists():
        raise SyncError("consensus-specs submodule missing; run `git submodule update --init --recursive`.")


def consensus_commit() -> str:
    ensure_consensus_repo()
    return run_git(["git", "-C", str(CONSENSUS_ROOT), "rev-parse", "HEAD"]).strip()


def run_git(cmd: List[str]) -> str:
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return proc.stdout


def load_upstream_text(rel_path: Path, commit: str) -> str:
    rel_unix = rel_path.as_posix()
    spec_unix = (Path("specs") / rel_path).as_posix()
    try:
        return run_git(
            ["git", "-C", str(CONSENSUS_ROOT), "show", f"{commit}:{spec_unix}"]
        )
    except subprocess.CalledProcessError as exc:
        raise SyncError(
            f"Unable to locate consensus-specs/specs/{rel_unix} at commit {commit}: {exc}"
        ) from exc


def iter_markdown_files(targets: List[str]) -> Iterator[Path]:
    if targets:
        resolved: List[Path] = []
        for raw in targets:
            path = Path(raw)
            if not path.is_absolute():
                path = Path.cwd() / path
            path = path.resolve()
            if path.is_dir():
                resolved.extend(sorted(path.rglob("*.md")))
            else:
                resolved.append(path)
    else:
        resolved = sorted(Path.cwd().rglob("*.md"))

    seen = set()
    for path in resolved:
        try:
            rel = path.resolve().relative_to(SCRIPT_ROOT)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in IGNORED_TOP_LEVEL:
            continue
        if rel in IGNORED_PATHS:
            continue
        abs_path = SCRIPT_ROOT / rel
        if abs_path.is_dir():
            continue
        if abs_path in seen:
            continue
        seen.add(abs_path)
        yield abs_path


def build_section_lookup(text: str) -> Dict[str, List[str]]:
    table: Dict[str, List[str]] = defaultdict(list)
    for slug, body in extract_sections(text):
        table[slug].append(body)
    return table


def extract_sections(text: str) -> Iterator[tuple[str, str]]:
    matches = list(HEADING_RE.finditer(text))
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        yield slugify(match.group(2)), text[start:end]


def merge_sections(annotated_body: str, upstream_body: str) -> tuple[str, bool]:
    parts = annotated_body.split(NOTES_MARK, 1)
    notes_suffix = ""
    if len(parts) == 2:
        notes_suffix = NOTES_MARK + parts[1]

    upstream = upstream_body
    if upstream and not upstream.endswith("\n"):
        upstream += "\n"

    if notes_suffix and not upstream.endswith("\n"):
        upstream += "\n"

    new_body = upstream + notes_suffix
    return new_body, new_body != annotated_body


def update_body(body: str, upstream_lookup: Dict[str, List[str]]) -> tuple[str, bool]:
    pieces: List[str] = []
    cursor = 0
    changed = False
    matches = list(HEADING_RE.finditer(body))
    if not matches:
        return body, changed

    for idx, match in enumerate(matches):
        start = match.start()
        pieces.append(body[cursor:start])
        header = match.group(0)
        pieces.append(header)
        cursor = match.end()
        section_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        annotated_section = body[cursor:section_end]
        slug = slugify(match.group(2))
        upstream_variants = upstream_lookup.get(slug)
        if upstream_variants:
            upstream_section = upstream_variants.pop(0)
            merged, section_changed = merge_sections(annotated_section, upstream_section)
            pieces.append(merged)
            changed = changed or section_changed
        else:
            pieces.append(annotated_section)
        cursor = section_end

    pieces.append(body[cursor:])
    return "".join(pieces), changed


def sync_file(path: Path, commit: str) -> bool:
    text = path.read_text(encoding="utf-8")
    rel_path = path.resolve().relative_to(SCRIPT_ROOT)
    ensure_consensus_repo()
    upstream_text = load_upstream_text(rel_path, commit)
    upstream_lookup = build_section_lookup(upstream_text)

    updated_text, section_changed = update_body(text, upstream_lookup)
    if section_changed and updated_text != text:
        path.write_text(updated_text, encoding="utf-8")
    elif updated_text != text:
        path.write_text(updated_text, encoding="utf-8")
        section_changed = True

    status = "updated" if section_changed else "already current"
    print(f"{rel_path}: {status}")
    return section_changed


def main(argv: List[str]) -> int:
    try:
        commit = consensus_commit()
    except (SyncError, subprocess.CalledProcessError) as exc:
        print(f"sync: {exc}", file=sys.stderr)
        return 1

    targets = list(iter_markdown_files(argv))
    if not targets:
        print("No markdown files found.", file=sys.stderr)
        return 0

    exit_code = 0
    for path in targets:
        try:
            changed = sync_file(path, commit)
        except SyncError as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            exit_code = 1
        except subprocess.CalledProcessError as exc:
            print(f"{path}: git error: {exc}", file=sys.stderr)
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
