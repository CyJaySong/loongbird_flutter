#!/usr/bin/env python3
"""Fetch staged Loongbird engine artifacts from GitHub Actions Artifacts.

Used by pure-engine verification jobs (`verify-linux` / `verify-windows`) so
they can test the freshly built artifacts from the current workflow run — or,
when build shards were skipped, the latest non-expired GitHub Actions artifacts
for the same `engine.version` — with zero dependency on external gateways or CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


class _NoAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strip Authorization header when GitHub redirects to Azure Blob SAS URLs."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            old_host = urllib.parse.urlparse(req.full_url).netloc.lower()
            new_host = urllib.parse.urlparse(newurl).netloc.lower()
            if old_host != new_host:
                new_req.remove_header("Authorization")
                new_req.remove_header("Accept")
        return new_req


def _gh_get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "loongbird-engine-verify",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download_zip(url: str, token: str, dest: Path) -> None:
    opener = urllib.request.build_opener(_NoAuthRedirectHandler())
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "loongbird-engine-verify",
        },
    )
    with opener.open(req, timeout=300) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 1024)


def _find_artifact(repo: str, token: str, name: str, current_run_id: str | None) -> dict:
    # 1. Prefer an artifact produced by the current workflow run if available.
    if current_run_id:
        run_url = (
            f"https://api.github.com/repos/{repo}/actions/runs/{current_run_id}"
            f"/artifacts?name={urllib.parse.quote(name)}&per_page=10"
        )
        data = _gh_get_json(run_url, token)
        candidates = [a for a in data.get("artifacts", []) if not a.get("expired")]
        if candidates:
            candidates.sort(key=lambda a: a.get("created_at", ""), reverse=True)
            return candidates[0]

    # 2. Fall back to the latest non-expired artifact in the repository matching `name`.
    repo_url = (
        f"https://api.github.com/repos/{repo}/actions/artifacts"
        f"?name={urllib.parse.quote(name)}&per_page=20"
    )
    data = _gh_get_json(repo_url, token)
    candidates = [a for a in data.get("artifacts", []) if not a.get("expired")]
    if not candidates:
        raise RuntimeError(
            f"No non-expired GitHub Actions artifact named '{name}' found in {repo}"
        )
    candidates.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    return candidates[0]


def _extract_merge(zip_path: Path, output_dir: Path) -> int:
    count = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = Path(info.filename)
            if rel.is_absolute() or ".." in rel.parts:
                raise RuntimeError(f"Unsafe zip member path: {info.filename}")
            dest = output_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            count += 1
    return count


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Loongbird staged artifacts from GitHub Actions Artifacts."
    )
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--engine-rev", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--current-run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required to download GitHub Actions artifacts.")
    if not args.repo:
        raise RuntimeError("--repo or GITHUB_REPOSITORY is required.")

    rev = args.engine_rev.strip()
    plat = args.platform
    artifact_names = [
        f"loongbird-{plat}-host-release-{rev}",
        f"loongbird-{plat}-host-debug-{rev}",
        f"loongbird-{plat}-android-arm64-{rev}",
        f"loongbird-{plat}-android-arm-{rev}",
        f"loongbird-{plat}-android-x64-{rev}",
    ]

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="lb_gha_dl_") as tmp:
        tmp_dir = Path(tmp)
        for name in artifact_names:
            art = _find_artifact(args.repo, token, name, args.current_run_id or None)
            art_id = art["id"]
            size_mib = art["size_in_bytes"] / (1024 * 1024)
            run_id = art.get("workflow_run", {}).get("id", "unknown")
            print(
                f"[fetch] {name} (id={art_id}, run={run_id}, {size_mib:.2f} MiB)...",
                flush=True,
            )
            outer_zip = tmp_dir / f"{name}.zip"
            _download_zip(art["archive_download_url"], token, outer_zip)
            extracted = _extract_merge(outer_zip, output_dir)
            outer_zip.unlink(missing_ok=True)
            print(f"[fetch]   extracted {extracted} staged file(s).", flush=True)

    print(f"[fetch] All {len(artifact_names)} GHA artifacts merged into {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
