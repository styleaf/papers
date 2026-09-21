"""Fetch CHI 2026 main-track paper metadata from Crossref (no ACM / Cloudflare)."""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

CROSSREF = "https://api.crossref.org/works"
PAPER_DOI_PREFIX = "10.1145/3772318."
UA = "CHI26PaperList/1.0 (mailto:chi26-download@local; personal research metadata)"
OUT_PATH = Path(__file__).resolve().parent / "data" / "chi26_papers.json"


def crossref_get(cursor: str) -> dict:
    params = {
        "filter": "from-pub-date:2026-04-13,until-pub-date:2026-04-13,prefix:10.1145",
        "rows": "1000",
        "cursor": cursor,
        "select": "DOI,title,author,container-title,link,URL",
    }
    url = CROSSREF + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))["message"]


def authors_of(item: dict) -> list[str]:
    names = []
    for author in item.get("author") or []:
        given = (author.get("given") or "").strip()
        family = (author.get("family") or "").strip()
        name = " ".join(part for part in (given, family) if part)
        if name:
            names.append(name)
    return names


def main() -> None:
    # Python's SSL handshake can fail when cwd contains non-ASCII characters.
    os.chdir(tempfile.gettempdir())

    papers: list[dict] = []
    seen: set[str] = set()
    cursor = "*"
    page = 0

    while True:
        msg = crossref_get(cursor)
        items = msg.get("items") or []
        page += 1
        print(f"Crossref page {page}: {len(items)} records")
        for item in items:
            doi = (item.get("DOI") or "").lower()
            if not doi.startswith(PAPER_DOI_PREFIX):
                continue
            if doi in seen:
                continue
            seen.add(doi)
            title = ((item.get("title") or [""])[0] or "untitled").strip()
            papers.append(
                {
                    "doi": doi,
                    "title": title,
                    "authors": authors_of(item),
                    "pdf_url": f"https://dl.acm.org/doi/pdf/{doi}",
                    "article_url": f"https://dl.acm.org/doi/{doi}",
                    "status": "pending",
                    "path": None,
                    "error": None,
                }
            )
        cursor = msg.get("next-cursor")
        if not items or not cursor:
            break
        time.sleep(0.2)

    papers.sort(key=lambda row: row["doi"])
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "crossref",
        "venue": "CHI 2026 Papers",
        "proceedings_doi": "10.1145/3772318",
        "count": len(papers),
        "papers": papers,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(papers)} papers to {OUT_PATH}")
    if len(papers) != 1702:
        print(f"Warning: expected 1702 papers, got {len(papers)}")


if __name__ == "__main__":
    main()
