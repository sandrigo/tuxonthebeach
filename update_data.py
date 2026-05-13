#!/usr/bin/env python3
"""Update areas.json / gems.json / quests.json from HeartofPhos/exile-leveling.

Checks GitHub for new versions and downloads only when the blob SHA changed.
The exile-leveling repo has no PoE version tags, so we use the file's git blob
SHA as the version identifier and report the latest commit touching each file
(date + short SHA + message) so you can see what changed.

State is kept in .exile_data_versions.json next to the data files.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "HeartofPhos/exile-leveling"
BRANCH = "main"
SRC_DIR = "common/data/json"
FILES = ["areas.json", "gems.json", "quests.json"]

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / ".exile_data_versions.json"

API = f"https://api.github.com/repos/{REPO}"
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"


def http_get(url: str, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "tuxonthebeach-update-data",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def remote_blob_sha(filename: str) -> str:
    path = f"{SRC_DIR}/{filename}"
    data = json.loads(http_get(f"{API}/contents/{path}?ref={BRANCH}"))
    return data["sha"]


def remote_last_commit(filename: str) -> dict | None:
    path = f"{SRC_DIR}/{filename}"
    data = json.loads(
        http_get(f"{API}/commits?path={path}&sha={BRANCH}&per_page=1")
    )
    if not data:
        return None
    c = data[0]
    return {
        "sha": c["sha"][:7],
        "date": c["commit"]["committer"]["date"],
        "message": c["commit"]["message"].splitlines()[0],
    }


def download(filename: str) -> bytes:
    return http_get(f"{RAW}/{SRC_DIR}/{filename}", accept="*/*")


def update_file(filename: str, state: dict, *, force: bool) -> bool:
    target = HERE / filename
    local_sha = state.get(filename, {}).get("sha")
    try:
        remote_sha = remote_blob_sha(filename)
        commit = remote_last_commit(filename)
    except urllib.error.HTTPError as e:
        print(f"  ! GitHub API error for {filename}: {e}", file=sys.stderr)
        return False

    commit_info = (
        f"{commit['sha']} {commit['date'][:10]}  {commit['message']}"
        if commit
        else "(no commit info)"
    )

    if not force and local_sha == remote_sha and target.exists():
        print(f"  = {filename}  up-to-date  [{remote_sha[:7]}]  {commit_info}")
        return False

    print(f"  > {filename}  updating  [{(local_sha or '-')[:7]} -> {remote_sha[:7]}]")
    print(f"      {commit_info}")
    payload = download(filename)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(target)

    state[filename] = {
        "sha": remote_sha,
        "last_commit": commit,
    }
    return True


def main() -> int:
    force = "--force" in sys.argv[1:]
    print(f"Checking {REPO}@{BRANCH}:{SRC_DIR}")
    state = load_state()
    changed = 0
    for f in FILES:
        if update_file(f, state, force=force):
            changed += 1
    save_state(state)
    print(f"Done. {changed} file(s) updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
