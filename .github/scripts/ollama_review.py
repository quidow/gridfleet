"""Post an Ollama-generated review on the current PR.

Reads `pr.patch` produced by `gh pr diff`, sends it to Ollama Cloud, expects
JSON back with a summary and per-line comments, then creates a GitHub review
via the REST API. Only lines that appear as added/context lines in the diff
hunks are eligible for inline comments; others fall back to the summary.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any


def env(name: str, *, required: bool = True, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        sys.stderr.write(f"missing env: {name}\n")
        sys.exit(1)
    return value or ""


def parse_diff_positions(patch: str) -> dict[str, set[int]]:
    """Return {file_path: {right-side line numbers eligible for comments}}."""
    eligible: dict[str, set[int]] = {}
    current_file: str | None = None
    right_line = 0
    in_hunk = False
    for raw in patch.splitlines():
        if raw.startswith("diff --git "):
            current_file = None
            in_hunk = False
        elif raw.startswith("+++ "):
            path = raw[4:].strip()
            current_file = None if path == "/dev/null" else path[2:] if path.startswith("b/") else path
            if current_file is not None:
                eligible.setdefault(current_file, set())
        elif raw.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if m and current_file is not None:
                right_line = int(m.group(1))
                in_hunk = True
        elif in_hunk and current_file is not None:
            if raw.startswith("+") and not raw.startswith("+++"):
                eligible[current_file].add(right_line)
                right_line += 1
            elif raw.startswith(" "):
                right_line += 1
            # '-' lines do not advance right_line; ignore.
    return eligible


SYSTEM_PROMPT = """You are a senior code reviewer for the GridFleet repository (FastAPI backend, FastAPI agent, React frontend).
You will receive a unified diff. Produce a focused, high-signal review.

Rules:
- Only comment on real problems: bugs, race conditions, security issues, broken contracts, missing locks, type errors, test gaps, perf cliffs.
- Do NOT comment on style, formatting, or restating what the code does.
- Each inline comment MUST target a line that was added or modified in the diff (a '+' line). Use the new-file line number.
- Prefer fewer, sharper comments over many shallow ones. If the diff looks fine, return an empty comments list.

Respond with ONLY a JSON object, no prose, no markdown fences:
{
  "summary": "1-3 sentence overall take",
  "comments": [
    {"path": "relative/file/path", "line": 123, "body": "the concern"}
  ]
}
"""


def call_ollama(patch: str) -> dict[str, Any]:
    url = env("OLLAMA_URL")
    model = env("OLLAMA_MODEL")
    api_key = env("OLLAMA_API_KEY")

    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Review this diff:\n\n```diff\n{patch}\n```"},
        ],
        "options": {"temperature": 0.2},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"ollama HTTP {e.code}: {e.read().decode('utf-8', 'replace')}\n")
        sys.exit(1)

    data = json.loads(body)
    content = data.get("message", {}).get("content", "")
    if not content:
        sys.stderr.write(f"ollama returned no content: {body[:500]}\n")
        sys.exit(1)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        sys.stderr.write(f"ollama content was not JSON:\n{content[:1000]}\n")
        return {"summary": content[:2000], "comments": []}


def post_review(review: dict[str, Any], eligible: dict[str, set[int]]) -> None:
    repo = env("REPO")
    pr = env("PR_NUMBER")
    head_sha = env("PR_HEAD_SHA")
    token = env("GH_TOKEN")

    inline: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for c in review.get("comments", []) or []:
        path = c.get("path")
        line = c.get("line")
        body = (c.get("body") or "").strip()
        if not path or not isinstance(line, int) or not body:
            continue
        if path in eligible and line in eligible[path]:
            inline.append({"path": path, "line": line, "side": "RIGHT", "body": body})
        else:
            skipped.append({"path": path, "line": line, "body": body})

    summary = (review.get("summary") or "").strip() or "Automated review by Ollama."
    body_parts = [f"**Ollama review** ({env('OLLAMA_MODEL')})", "", summary]
    if skipped:
        body_parts.append("")
        body_parts.append("_Comments below could not be attached inline (line not in diff):_")
        for s in skipped:
            body_parts.append(f"- `{s['path']}:{s['line']}` — {s['body']}")

    payload = {
        "commit_id": head_sha,
        "event": "COMMENT",
        "body": "\n".join(body_parts),
        "comments": inline,
    }

    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls/{pr}/reviews",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            print(f"posted review: HTTP {resp.status}, {len(inline)} inline / {len(skipped)} skipped")
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"github HTTP {e.code}: {e.read().decode('utf-8', 'replace')}\n")
        sys.exit(1)


def main() -> None:
    try:
        with open("pr.patch", encoding="utf-8") as f:
            patch = f.read()
    except FileNotFoundError:
        sys.stderr.write("pr.patch not found\n")
        sys.exit(1)
    if not patch.strip():
        print("empty diff, nothing to review")
        return

    # GitHub's review API rejects very large payloads; trim to ~200 KB of diff.
    max_chars = 200_000
    if len(patch) > max_chars:
        patch = patch[:max_chars] + "\n\n[diff truncated]\n"

    eligible = parse_diff_positions(patch)
    review = call_ollama(patch)
    post_review(review, eligible)


if __name__ == "__main__":
    main()
