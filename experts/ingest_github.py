"""Build expert evidence from GitHub commits and PRs: python -m experts.ingest_github [--fixture]

Reads sources.yaml (repos, and a GitHub login or commit email -> expert id map) and
writes evidence.json, which the matcher loads as extra chunks. Run offline from the
checked-in fixture with --fixture; against the API it reads GITHUB_TOKEN. This runs
as a separate step, never at match time, so the Router makes no GitHub calls.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

import yaml

from experts.embed import tokens
from experts.matcher import EVIDENCE_PATH, load_directory

SOURCES_PATH = Path(__file__).with_name("sources.yaml")
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "github_activity.json"
MIN_TOKENS = 3  # "fix typo", "wip": too short to say what someone knows
MAX_PR_CHARS = 300


@dataclass
class Evidence:
    expert_id: str
    text: str
    source: str  # "github_commit" or "github_pr"
    date: str
    url: str


@dataclass
class Sources:
    repos: list[str]
    authors: dict[str, str]  # lowercased GitHub login or commit email -> expert id
    ignore_terms: list[str]
    max_per_expert: int


def load_sources(path: Path = SOURCES_PATH, expert_ids: Optional[set[str]] = None) -> Sources:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)["github"]
    authors = {str(k).lower(): v for k, v in (raw.get("authors") or {}).items()}
    if expert_ids is not None:
        unknown = sorted(set(authors.values()) - expert_ids)
        if unknown:
            raise ValueError(f"{path.name} maps authors to unknown expert ids: {', '.join(unknown)}")
    return Sources(
        repos=list(raw.get("repos") or []),
        authors=authors,
        ignore_terms=list(raw.get("ignore_terms") or []),
        max_per_expert=int(raw.get("max_per_expert", 50)),
    )


class GitHub(Protocol):
    """Raw REST API items: /repos/{repo}/commits and /repos/{repo}/pulls?state=all."""

    def commits(self, repo: str, since: Optional[str]) -> list[dict]: ...
    def pulls(self, repo: str) -> list[dict]: ...


class GitHubClient:
    def __init__(self, token: Optional[str] = None, base_url: str = "https://api.github.com",
                 timeout_s: float = 10.0, max_pages: int = 5):
        self.token, self.base_url, self.timeout_s, self.max_pages = token, base_url, timeout_s, max_pages

    def _get(self, path: str, params: dict) -> list[dict]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        out: list[dict] = []
        for page in range(1, self.max_pages + 1):
            query = urllib.parse.urlencode({**params, "per_page": 100, "page": page})
            req = urllib.request.Request(f"{self.base_url}{path}?{query}", headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                batch = json.load(resp)
            out.extend(batch)
            if len(batch) < 100:
                break
        return out

    def commits(self, repo: str, since: Optional[str]) -> list[dict]:
        return self._get(f"/repos/{repo}/commits", {"since": since} if since else {})

    def pulls(self, repo: str) -> list[dict]:
        return self._get(f"/repos/{repo}/pulls", {"state": "all"})


class FixtureGitHub:
    """API-shaped items from a JSON file: {repo: {"commits": [...], "pulls": [...]}}."""

    def __init__(self, path: Path = FIXTURE_PATH):
        with open(path, encoding="utf-8") as f:
            self.data = json.load(f)

    def commits(self, repo: str, since: Optional[str]) -> list[dict]:
        items = self.data.get(repo, {}).get("commits", [])
        return [c for c in items if not since or c["commit"]["author"]["date"] >= since]

    def pulls(self, repo: str) -> list[dict]:
        return self.data.get(repo, {}).get("pulls", [])


# Leading "[P2] ", "fix(export): " style labels, and "(#42)" references.
_PREFIX = re.compile(r"^\s*(?:\[[^\]]*\]\s*)*(?:[a-z]+(?:\([^)]*\))?!?:\s+)?", re.IGNORECASE)
_REFS = re.compile(r"\(?#\d+\)?")
_MERGE = re.compile(r"^merge (?:pull request|branch|remote-tracking)", re.IGNORECASE)
_MD_HEADING = re.compile(r"^\s*#+\s.*$", re.MULTILINE)
_MD_MARKS = re.compile(r"[*`>]+|(?<!\w)_+|_+(?!\w)")  # keeps snake_case


def first_paragraph(body: Optional[str]) -> str:
    """First prose paragraph of a PR body, without markdown headings (## Summary) or emphasis."""
    text = _MD_MARKS.sub("", _MD_HEADING.sub("", body or ""))
    return re.split(r"\n\s*\n", text.strip(), maxsplit=1)[0]


def clean(text: str, ignore_terms: list[str]) -> str:
    text = _REFS.sub("", _PREFIX.sub("", text, count=1))
    for term in ignore_terms:
        text = re.sub(rf"(?<![\w-]){re.escape(term)}(?![\w-])", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" .-:")


def _login(item: Optional[dict]) -> str:
    return ((item or {}).get("login") or "").lower()


def build_evidence(client: GitHub, sources: Sources, since: Optional[str] = None) -> list[Evidence]:
    found: list[Evidence] = []

    def add(author_keys: list[str], raw_text: str, source: str, date: str, url: str) -> None:
        if any(k.endswith("[bot]") for k in author_keys if k):
            return
        expert_id = next((sources.authors[k] for k in author_keys if k in sources.authors), None)
        text = clean(raw_text, sources.ignore_terms)
        if expert_id and len(tokens(text)) >= MIN_TOKENS:
            found.append(Evidence(expert_id, text, source, date, url))

    for repo in sources.repos:
        for c in client.commits(repo, since):
            subject = c["commit"]["message"].split("\n", 1)[0]
            if _MERGE.match(subject):
                continue
            author = c["commit"]["author"]
            add([_login(c.get("author")), (author.get("email") or "").lower()],
                subject, "github_commit", author["date"], c["html_url"])
        for pr in client.pulls(repo):
            if since and pr["created_at"] < since:
                continue
            first_para = first_paragraph(pr.get("body"))
            text = f"{pr['title']}. {first_para}"[:MAX_PR_CHARS] if first_para else pr["title"]
            add([_login(pr.get("user"))], text, "github_pr", pr["created_at"], pr["html_url"])

    # Newest first; a squash-merged PR repeats its title as a commit, so keep one copy per text.
    found.sort(key=lambda e: e.date, reverse=True)
    seen: set[tuple[str, str]] = set()
    per_expert: Counter[str] = Counter()
    out = []
    for e in found:
        key = (e.expert_id, e.text.lower())
        if key in seen or per_expert[e.expert_id] >= sources.max_per_expert:
            continue
        seen.add(key)
        per_expert[e.expert_id] += 1
        out.append(e)
    return out


def write_evidence(evidence: list[Evidence], path: Path = EVIDENCE_PATH) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chunks": [asdict(e) for e in evidence],
    }
    # Write then rename, so the Router never loads a half-written file.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--fixture", nargs="?", const=FIXTURE_PATH, type=Path,
                        help="read API-shaped JSON instead of calling GitHub (default: the checked-in fixture)")
    parser.add_argument("--sources", type=Path, default=SOURCES_PATH)
    parser.add_argument("--out", type=Path, default=EVIDENCE_PATH)
    parser.add_argument("--since", help="ISO date; only activity on or after it")
    args = parser.parse_args(argv)

    sources = load_sources(args.sources, {e.id for e in load_directory()})
    client = FixtureGitHub(args.fixture) if args.fixture else GitHubClient(os.environ.get("GITHUB_TOKEN"))
    evidence = build_evidence(client, sources, args.since)
    write_evidence(evidence, args.out)
    counts = Counter(e.expert_id for e in evidence)
    print(f"wrote {len(evidence)} chunks to {args.out}: "
          + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
