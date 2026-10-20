#!/usr/bin/env python3
"""Check the repository's Markdown: relative links, anchors and Mermaid blocks.

For every ``*.md`` file in the repository (outside ``.git``, ``node_modules``
and virtual environments) this script checks that:

- every relative link points to a file or directory that exists;
- every ``#anchor``, in the same file or another Markdown file, matches a
  heading, using GitHub's rules for turning headings into anchors;
- every fenced ``mermaid`` block starts with a known diagram type and has
  balanced brackets and quotes on each line (a basic sanity check, not a full
  Mermaid parser);
- heading levels do not skip (for example ``##`` followed by ``####``).

Links inside fenced code blocks and inline code are ignored, as are absolute
URLs (``http:``, ``https:``, ``mailto:``).

Standard library only. Usage::

    python scripts/check_docs.py            # check everything
    python scripts/check_docs.py docs/      # check one directory or file

Exit status 0 when everything passes, 1 otherwise.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", ".tox", "htmlcov", "dist", "build", ".pytest_cache"}

LINK_RE = re.compile(r"(?<!\!)\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<target>[^)\s]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)(.*)$")
INLINE_CODE_RE = re.compile(r"`[^`]*`")
HTML_ANCHOR_RE = re.compile(r"<a\s+(?:name|id)=\"([^\"]+)\"")

MERMAID_TYPES = (
    "flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram",
    "stateDiagram-v2", "erDiagram", "gantt", "pie", "journey", "gitGraph",
    "mindmap", "timeline", "quadrantChart", "requirementDiagram", "C4Context",
)


def iter_markdown(paths):
    for base in paths:
        base = Path(base)
        if base.is_file():
            yield base.resolve()
            continue
        for path in sorted(base.rglob("*.md")):
            if any(part in SKIP_DIRS for part in path.relative_to(base).parts):
                continue
            yield path.resolve()


def github_anchor(text: str) -> str:
    """Approximate GitHub's heading-to-anchor conversion."""
    text = re.sub(r"<[^>]+>", "", text)          # drop inline HTML
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # keep link text
    text = text.replace("`", "")
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)          # keep letters, digits, _, -, space
    return text.replace(" ", "-")


def parse(path: Path):
    """Return (anchors, links, mermaid_blocks, headings) for a Markdown file."""
    anchors = set()
    counts = {}
    links = []
    mermaid = []
    headings = []
    in_fence = False
    fence_lang = ""
    block = []
    block_start = 0
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = FENCE_RE.match(line)
        if m:
            if not in_fence:
                in_fence = True
                fence_lang = m.group(2).strip().lower()
                block = []
                block_start = lineno
            else:
                if fence_lang == "mermaid":
                    mermaid.append((block_start, block))
                in_fence = False
                fence_lang = ""
            continue
        if in_fence:
            block.append(line)
            continue
        h = HEADING_RE.match(line)
        if h:
            level = len(h.group(1))
            title = h.group(2)
            headings.append((lineno, level))
            base = github_anchor(title)
            n = counts.get(base, 0)
            anchors.add(base if n == 0 else f"{base}-{n}")
            counts[base] = n + 1
        for a in HTML_ANCHOR_RE.findall(line):
            anchors.add(a)
        stripped = INLINE_CODE_RE.sub("", line)
        for lm in LINK_RE.finditer(stripped):
            links.append((lineno, lm.group("target")))
        for im in IMAGE_RE.finditer(stripped):
            links.append((lineno, im.group("target")))
    return anchors, links, mermaid, headings


def check_mermaid(path, start, lines, errors):
    body = [l for l in lines if l.strip() and not l.strip().startswith("%%")]
    if not body:
        errors.append(f"{path}:{start}: empty mermaid block")
        return
    first = body[0].strip().split()[0]
    if first not in MERMAID_TYPES:
        errors.append(f"{path}:{start}: mermaid block starts with '{first}', not a known diagram type")
    pairs = {"[": "]", "(": ")", "{": "}"}
    for offset, line in enumerate(lines, 1):
        if line.count('"') % 2:
            errors.append(f"{path}:{start + offset}: unbalanced double quote in mermaid line")
            continue
        # Ignore bracket characters inside quoted labels.
        unquoted = re.sub(r'"[^"]*"', '""', line)
        stack = []
        for ch in unquoted:
            if ch in pairs:
                stack.append(pairs[ch])
            elif ch in pairs.values():
                if not stack or stack.pop() != ch:
                    errors.append(f"{path}:{start + offset}: unbalanced '{ch}' in mermaid line")
                    break
        else:
            if stack:
                errors.append(f"{path}:{start + offset}: unclosed bracket in mermaid line")


def main(argv):
    targets = argv[1:] or [ROOT]
    files = list(iter_markdown(targets))
    cache = {}

    def info(p: Path):
        if p not in cache:
            cache[p] = parse(p)
        return cache[p]

    errors = []
    link_count = 0
    for path in files:
        try:
            rel = path.relative_to(ROOT)
        except ValueError:
            rel = path
        anchors, links, mermaid, headings = info(path)
        for lineno, target in links:
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):  # http:, mailto:, etc.
                continue
            link_count += 1
            target_path, _, anchor = target.partition("#")
            target_path = unquote(target_path)
            if target_path:
                dest = (path.parent / target_path).resolve()
                if not dest.exists():
                    errors.append(f"{rel}:{lineno}: broken link '{target}' (no such file)")
                    continue
            else:
                dest = path
            if anchor:
                if dest.is_dir() or dest.suffix.lower() != ".md":
                    continue
                dest_anchors = info(dest)[0]
                if anchor not in dest_anchors:
                    errors.append(f"{rel}:{lineno}: broken anchor '#{anchor}' in '{target or rel}'")
        for start, block in mermaid:
            check_mermaid(rel, start, block, errors)
        previous = 0
        for lineno, level in headings:
            if previous and level > previous + 1:
                errors.append(f"{rel}:{lineno}: heading level jumps from {previous} to {level}")
            previous = level

    for e in errors:
        print(e)
    print(f"Checked {len(files)} Markdown files, {link_count} relative links: "
          f"{'OK' if not errors else str(len(errors)) + ' problem(s)'}.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
