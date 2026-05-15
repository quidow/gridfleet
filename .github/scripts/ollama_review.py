"""Post an Ollama-generated review on the current PR.

Reads `pr.patch` produced by `gh pr diff`, sends it to Ollama Cloud, expects
JSON back with a summary and per-line comments, then creates a GitHub review
via the REST API. Only lines that appear as added/context lines in the diff
hunks are eligible for inline comments; others fall back to the summary.
"""

from __future__ import annotations

import difflib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

OLLAMA_RETRIABLE = (
    urllib.error.URLError,
    http.client.HTTPException,
    TimeoutError,
    ConnectionError,
)


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
You will receive a unified diff, optionally preceded by reference docs fetched from Context7.

Rules:
- Only comment on real problems: bugs, race conditions, security issues, broken contracts, missing locks, type errors, test gaps, perf cliffs.
- Do NOT comment on style, formatting, or restating what the code does.
- When reference docs are present, treat them as ground truth for external libraries and tools. If reference docs do not cover a claim you'd make about an external dependency (version exists, action tag exists, function signature), do NOT raise it.
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

PY_STDLIB = {
    "os", "sys", "re", "json", "pathlib", "typing", "asyncio", "urllib", "subprocess",
    "dataclasses", "enum", "collections", "datetime", "functools", "itertools", "time",
    "logging", "__future__", "contextlib", "warnings", "inspect", "traceback", "io",
    "math", "random", "hashlib", "base64", "secrets", "uuid", "tempfile", "shutil",
    "glob", "argparse", "unittest", "abc", "copy", "string", "textwrap", "platform",
    "threading", "queue", "signal", "socket", "struct", "array", "csv", "html", "xml",
    "email", "http", "ssl", "decimal", "fractions", "weakref", "ast", "operator",
    "difflib", "heapq", "bisect", "pickle", "shelve", "sqlite3", "zipfile", "tarfile",
    "gzip", "bz2", "lzma", "configparser", "pprint", "reprlib", "types", "gc", "atexit",
    "concurrent", "multiprocessing", "selectors", "asyncore", "ipaddress", "mimetypes",
    "tokenize", "token", "keyword", "symtable", "site", "code", "codeop", "runpy",
    "importlib", "linecache", "fileinput", "filecmp", "stat", "fcntl", "errno",
}


def extract_identifiers(patch: str, limit: int = 6) -> list[tuple[str, str]]:
    """Pull libraries/actions worth looking up from added lines in the diff."""
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []

    def add(kind: str, ident: str) -> None:
        key = (kind, ident)
        if key in seen:
            return
        seen.add(key)
        ordered.append(key)

    for raw in patch.splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        line = raw[1:]
        m = re.search(r"uses:\s*([\w.-]+/[\w.-]+)(?:/[^@\s]+)?@", line)
        if m:
            add("github-action", m.group(1))
            continue
        m = re.match(r"""\s*import\s+(?:[\w*{},\s]+\s+from\s+)?['"]([^'".][@\w/.-]+)['"]""", line)
        if m:
            pkg = m.group(1)
            if pkg.startswith("@"):
                parts = pkg.split("/", 2)
                pkg = "/".join(parts[:2])
            else:
                pkg = pkg.split("/")[0]
            add("npm", pkg)
            continue
        m = re.match(r"\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+)\s*$)", line.rstrip())
        if m:
            mod = (m.group(1) or m.group(2)).split(".")[0]
            if mod and mod not in PY_STDLIB and not mod.startswith("_"):
                add("python", mod)
    return ordered[:limit]


def _ollama_post(
    url: str, payload: dict[str, Any], api_key: str, label: str, attempts: int = 3
) -> str | None:
    """POST to Ollama Cloud with retries on transient network failures.

    Returns the response body on success, None after exhausting attempts.
    Ollama Cloud sometimes drops the connection on long inference (seen as
    http.client.RemoteDisconnected) — retry with backoff before giving up.
    """
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            sys.stderr.write(f"{label}: HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}\n")
            return None
        except OLLAMA_RETRIABLE as e:
            sys.stderr.write(f"{label} attempt {attempt}/{attempts} failed: {type(e).__name__}: {e}\n")
            if attempt < attempts:
                time.sleep(2 ** attempt)
    return None


def _context7_get(url: str, api_key: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError) as e:
        sys.stderr.write(f"context7 GET {url}: {e}\n")
        return None


def fetch_context7_docs(identifiers: list[tuple[str, str]]) -> str:
    """Resolve each identifier via Context7 search and fetch a doc snippet."""
    api_key = os.environ.get("CONTEXT7_API_KEY", "")
    if not api_key or not identifiers:
        return ""

    blocks: list[str] = []
    total = 0
    max_total = 30_000

    for kind, ident in identifiers:
        if total >= max_total:
            break
        query = ident if kind != "github-action" else ident.split("/")[-1]
        search_raw = _context7_get(
            f"https://context7.com/api/v1/search?query={urllib.parse.quote(query)}",
            api_key,
        )
        if not search_raw:
            continue
        try:
            search = json.loads(search_raw.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        results = search.get("results") or []
        lib_id = None
        for r in results:
            candidate = r.get("id") or r.get("libraryId") or ""
            if kind == "github-action" and ident.lower() in candidate.lower():
                lib_id = candidate
                break
        if not lib_id and results:
            lib_id = results[0].get("id") or results[0].get("libraryId")
        if not lib_id:
            continue
        if not lib_id.startswith("/"):
            lib_id = "/" + lib_id

        docs_raw = _context7_get(
            f"https://context7.com/api/v1{lib_id}?type=txt&tokens=2000",
            api_key,
        )
        if not docs_raw:
            continue
        docs = docs_raw.decode("utf-8", "replace")[:8000]
        block = f"### {kind}: {ident} ({lib_id})\n{docs}\n"
        total += len(block)
        blocks.append(block)
        print(f"context7: fetched {kind}:{ident} -> {lib_id} ({len(docs)} chars)")

    if not blocks:
        return ""
    return "## Reference docs (via Context7)\n\n" + "\n".join(blocks)


def call_ollama(patch: str, docs: str = "") -> dict[str, Any]:
    url = env("OLLAMA_URL")
    model = env("OLLAMA_MODEL")
    api_key = env("OLLAMA_API_KEY")

    user_content = f"Review this diff:\n\n```diff\n{patch}\n```"
    if docs:
        user_content = f"{docs}\n\n---\n\n{user_content}"

    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "options": {"temperature": 0.2},
    }
    body = _ollama_post(url, payload, api_key, label="review")
    if body is None:
        sys.exit(1)

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        sys.stderr.write(f"ollama returned non-JSON body:\n{body[:1000]}\n")
        sys.exit(1)
    content = data.get("message", {}).get("content", "")
    if not content:
        sys.stderr.write(f"ollama returned no content: {body[:500]}\n")
        sys.exit(1)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        sys.stderr.write(f"ollama content was not JSON:\n{content[:1000]}\n")
        return {"summary": content[:2000], "comments": []}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def is_duplicate_of_resolved(
    path: str, line: int, body: str, resolved: list[dict[str, Any]], threshold: float = 0.7
) -> bool:
    """True if a resolved thread sits at the same (path, line) with a similar body."""
    body_n = _normalize(body)
    if not body_n:
        return False
    for r in resolved:
        if r.get("path") != path or r.get("line") != line:
            continue
        other = _normalize(r.get("body") or "")
        if not other:
            continue
        if other == body_n:
            return True
        ratio = difflib.SequenceMatcher(None, body_n, other).ratio()
        if ratio >= threshold:
            return True
    return False


def post_review(
    review: dict[str, Any],
    eligible: dict[str, set[int]],
    resolved_threads: list[dict[str, Any]] | None = None,
) -> None:
    repo = env("REPO")
    pr = env("PR_NUMBER")
    head_sha = env("PR_HEAD_SHA")
    token = env("GH_TOKEN")

    resolved = resolved_threads or []
    inline: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    dedup_count = 0
    for c in review.get("comments", []) or []:
        path = c.get("path")
        line = c.get("line")
        body = (c.get("body") or "").strip()
        if not path or not isinstance(line, int) or not body:
            continue
        if is_duplicate_of_resolved(path, line, body, resolved):
            dedup_count += 1
            continue
        if path in eligible and line in eligible[path]:
            inline.append({"path": path, "line": line, "side": "RIGHT", "body": body})
        else:
            skipped.append({"path": path, "line": line, "body": body})
    if dedup_count:
        print(f"dedup: dropped {dedup_count} comment(s) matching already-resolved threads")

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


BOT_LOGIN = "github-actions[bot]"


def _graphql(query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        body = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        sys.stderr.write(f"graphql call failed: {e}\n")
        return {}
    if body.get("errors"):
        sys.stderr.write(f"graphql errors: {body['errors']}\n")
    return body


def list_bot_threads(owner: str, name: str, pr_number: int, token: str) -> list[dict[str, Any]]:
    query = """
    query($owner:String!, $name:String!, $number:Int!) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$number) {
          reviewThreads(first:100) {
            nodes {
              id isResolved isOutdated path line
              comments(first:5) {
                nodes { databaseId author { login } body }
              }
            }
          }
        }
      }
    }
    """
    data = _graphql(query, {"owner": owner, "name": name, "number": pr_number}, token)
    nodes = (
        data.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    ) or []
    out: list[dict[str, Any]] = []
    for t in nodes:
        comments = ((t.get("comments") or {}).get("nodes")) or []
        if not comments:
            continue
        first = comments[0]
        author = ((first.get("author") or {}).get("login")) or ""
        if author != BOT_LOGIN:
            continue
        out.append(
            {
                "id": t["id"],
                "resolved": bool(t.get("isResolved")),
                "outdated": bool(t.get("isOutdated")),
                "path": t.get("path"),
                "line": t.get("line"),
                "body": first.get("body", ""),
                "first_comment_id": first.get("databaseId"),
            }
        )
    return out


def post_thread_reply(repo: str, pr_number: int, comment_id: int, body: str, token: str) -> bool:
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments/{comment_id}/replies"
    req = urllib.request.Request(
        url,
        data=json.dumps({"body": body}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError) as e:
        sys.stderr.write(f"reply on {comment_id} failed: {e}\n")
        return False


def ask_model_for_resolutions(threads: list[dict[str, Any]], patch: str) -> set[str]:
    if not threads:
        return set()
    url = env("OLLAMA_URL")
    model = env("OLLAMA_MODEL")
    api_key = env("OLLAMA_API_KEY")
    items = "\n".join(
        f"- id={t['id']} {t['path']}:{t['line']}: {(t['body'] or '')[:400]}" for t in threads
    )
    system = (
        "You decide whether prior code-review concerns are addressed by a new diff. "
        "Be conservative: if unsure, do NOT resolve. "
        "Respond with ONLY JSON: {\"resolved\": [\"<thread id>\", ...]} listing the thread IDs "
        "whose concern is clearly fixed in the new diff. No prose, no fences."
    )
    user = f"Open concerns:\n{items}\n\nNew diff:\n```diff\n{patch[:80_000]}\n```"
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {"temperature": 0.1},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    raw = _ollama_post(url, payload, api_key, label="resolve-check")
    if raw is None:
        return set()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write(f"resolve-check non-JSON body: {raw[:500]}\n")
        return set()
    content = data.get("message", {}).get("content", "")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        sys.stderr.write(f"resolve-check JSON parse failed: {content[:500]}\n")
        return set()
    return {x for x in (parsed.get("resolved") or []) if isinstance(x, str)}


def resolve_thread(thread_id: str, token: str) -> bool:
    mutation = """
    mutation($id:ID!) {
      resolveReviewThread(input:{threadId:$id}) { thread { id isResolved } }
    }
    """
    data = _graphql(mutation, {"id": thread_id}, token)
    if not data or data.get("errors"):
        return False
    return True


def reconcile_existing_threads(patch: str) -> list[dict[str, Any]]:
    """Resolve addressed bot threads. Returns ALL bot threads now resolved
    (pre-existing + newly resolved this run) for dedup of new comments.
    """
    repo = env("REPO")
    token = env("GH_TOKEN")
    head_sha = env("PR_HEAD_SHA")
    pr_number = int(env("PR_NUMBER"))
    owner, name = repo.split("/", 1)

    try:
        threads = list_bot_threads(owner, name, pr_number, token)
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"list threads failed: HTTP {e.code}\n")
        return []
    if not threads:
        return []

    open_threads = [t for t in threads if not t["resolved"]]

    outdated_ids = {t["id"] for t in open_threads if t["outdated"]}
    live = [t for t in open_threads if not t["outdated"]]
    addressed_ids: set[str] = set()
    if live:
        addressed_ids = ask_model_for_resolutions(live[:10], patch)

    by_id = {t["id"]: t for t in open_threads}
    resolved_now: list[dict[str, Any]] = []
    for tid in outdated_ids | addressed_ids:
        t = by_id.get(tid)
        if not t:
            continue
        comment_id = t.get("first_comment_id")
        if comment_id:
            reason = (
                "Resolving automatically: target line is no longer in the diff."
                if tid in outdated_ids
                else f"Resolving automatically: appears addressed in {head_sha[:7]}."
            )
            post_thread_reply(repo, pr_number, int(comment_id), reason, token)
        if resolve_thread(tid, token):
            resolved_now.append(t)

    print(
        f"reconcile: {len(open_threads)} open / {len(threads) - len(open_threads)} already-resolved bot threads, "
        f"{len(outdated_ids)} outdated, {len(resolved_now)} resolved this run"
    )

    return [t for t in threads if t["resolved"]] + resolved_now


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

    try:
        resolved_threads = reconcile_existing_threads(patch)
    except Exception as e:  # noqa: BLE001 - reconcile is non-critical
        sys.stderr.write(f"reconcile failed (continuing without it): {e}\n")
        import traceback

        traceback.print_exc(file=sys.stderr)
        resolved_threads = []

    eligible = parse_diff_positions(patch)
    identifiers = extract_identifiers(patch)
    if identifiers:
        print(f"context7 lookups: {identifiers}")
    docs = fetch_context7_docs(identifiers)
    review = call_ollama(patch, docs)
    post_review(review, eligible, resolved_threads)


if __name__ == "__main__":
    main()
