"""MkDocs hooks: search/answer-engine friendly output without touching the Markdown.

* Gives every page a meta description from its first paragraph (GitHub would show a
  front-matter block as a table, so the descriptions are derived at build time).
* Rewrites relative links that point at repository files outside ``docs/``
  (README, CHANGELOG, SECURITY, ...) to their GitHub URLs, so the site has no dead links.
* Collects the questions of the FAQ page for FAQPage structured data.
* After the build, writes ``llms.txt`` and ``llms-full.txt`` (a plain-text index and the
  full text of the docs, for language models) and ``robots.txt`` with the sitemap.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin

REPO_BLOB = "https://github.com/agentanywhere/agentanywhere-universal-gateway/blob/main/"
_LINK = re.compile(r"(\]\()([^)\s#]+)((?:#[^)\s]*)?\))")
_FENCE = re.compile(r"^(```|~~~)")

_pages: list[dict] = []


def _first_paragraph(markdown: str) -> str:
    in_fence = False
    para: list[str] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line:
            if para:
                break
            continue
        if line.startswith(("#", "|", "-", "*", ">", "<", "!", "[!", "```", "**Goal:**")) and not para:
            if line.startswith("**Goal:**"):
                para.append(line[len("**Goal:**"):].strip())
            continue
        para.append(line)
    text = " ".join(para)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)      # links -> text
    text = re.sub(r"[`*_]", "", text)                          # inline markup
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 300:
        text = text[:297].rsplit(" ", 1)[0] + "..."
    return text


def _faq(markdown: str) -> list[dict]:
    items: list[dict] = []
    question, answer = None, []
    in_fence = False
    for raw in markdown.splitlines():
        if _FENCE.match(raw.strip()):
            in_fence = not in_fence
        if not in_fence and raw.startswith("## "):
            if question and answer:
                items.append({"q": question, "a": _plain(answer)})
            question, answer = raw[3:].strip(), []
        elif question:
            answer.append(raw)
    if question and answer:
        items.append({"q": question, "a": _plain(answer)})
    return [i for i in items if i["a"]]


def _plain(lines: list[str]) -> str:
    text = " ".join(l.strip() for l in lines if l.strip() and not l.strip().startswith(("```", "|")))
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`*_]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:900]


def _rewrite_outside_links(markdown: str, page, config) -> str:
    docs_dir = Path(config["docs_dir"]).resolve()
    repo_root = docs_dir.parent
    here = (docs_dir / page.file.src_uri).parent

    def fix(match: re.Match) -> str:
        target = match.group(2)
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("/"):
            return match.group(0)
        resolved = (here / target).resolve()
        try:
            resolved.relative_to(docs_dir)
            return match.group(0)
        except ValueError:
            pass
        try:
            rel = resolved.relative_to(repo_root).as_posix()
        except ValueError:
            return match.group(0)
        if not resolved.exists():
            return match.group(0)
        kind = "tree" if resolved.is_dir() else "blob"
        base = REPO_BLOB.replace("/blob/", f"/{kind}/")
        return f"{match.group(1)}{base}{rel}{match.group(3)}"

    out, in_fence = [], False
    for line in markdown.split("\n"):
        if _FENCE.match(line.strip()):
            in_fence = not in_fence
        out.append(line if in_fence else _LINK.sub(fix, line))
    return "\n".join(out)


def on_page_markdown(markdown, page, config, files):
    if not page.meta.get("description"):
        text = _first_paragraph(markdown)
        if text and re.search(r"^\*\*Goal:\*\*", markdown, re.M):
            text = "How to " + text                      # how-to pages open with "**Goal:** <verb> ..."
        elif text:
            text = text[0].upper() + text[1:]
        page.meta["description"] = text or config["site_description"]
    h1 = re.search(r"^# +(.+?)\s*$", markdown, re.M)
    if h1 and not page.meta.get("title") and not page.is_homepage:
        page.meta["title"] = re.sub(r"[`*_]", "", h1.group(1))      # <title> matches the page's own heading
    if page.file.src_uri == "faq.md":
        page.meta["faq_jsonld"] = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {"@type": "Question", "name": i["q"], "acceptedAnswer": {"@type": "Answer", "text": i["a"]}}
                for i in _faq(markdown)
            ],
        }
    markdown = _rewrite_outside_links(markdown, page, config)
    _pages.append({"title": None, "page": page, "markdown": markdown})
    return markdown


def on_post_build(config):
    site_dir = Path(config["site_dir"])
    site_url = config["site_url"].rstrip("/") + "/"
    name, summary = config["site_name"], config["site_description"].strip()

    lines = [f"# {name}", "", f"> {summary}", ""]
    full = [f"# {name}", "", f"> {summary}", ""]
    section = None
    groups = {"tutorials": "Tutorials", "how-to": "How-to guides", "reference": "Reference",
              "explanation": "Concepts", "docs": "Help"}
    order = {"README.md": 0, "tutorials": 1, "how-to": 2, "reference": 3, "explanation": 4}

    def sort_key(entry):
        src = entry["page"].file.src_uri
        head = src if src == "README.md" else (src.split("/")[0] if "/" in src else "docs")
        return (order.get(head, 5), src)

    # Pages that only point somewhere else are noise for a reader or a model.
    live = [e for e in _pages if not e["page"].meta.get("description", "").startswith("This page has moved")]
    for entry in sorted(live, key=sort_key):
        page = entry["page"]
        url = urljoin(site_url, page.url)
        src = page.file.src_uri
        group = "home" if src == "README.md" else (src.split("/")[0] if "/" in src else "docs")
        if group != section:
            section = group
            if group != "home":
                lines += ["", f"## {groups[group]}", ""]
        lines.append(f"- [{page.title}]({url}): {page.meta.get('description', '')}")
        full += ["", "---", "", f"Source: {url}", "", entry["markdown"].strip(), ""]
    (site_dir / "llms.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (site_dir / "llms-full.txt").write_text("\n".join(full) + "\n", encoding="utf-8")
    (site_dir / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\n\nSitemap: {site_url}sitemap.xml\n", encoding="utf-8"
    )
    _pages.clear()
