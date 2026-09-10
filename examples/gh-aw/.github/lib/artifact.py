#!/usr/bin/env python3
"""The newest unexpired GitHub Actions artifact with an exact name.

    artifact.py NAME        print its id
    artifact.py NAME DIR    also download it and extract memory.db into DIR

Exit 0 and the id on stdout when there is one; exit 3, with a note on stderr,
when there is none; any other failure is an error. Needs GITHUB_API_URL,
GITHUB_REPOSITORY and GH_TOKEN (the job's GITHUB_TOKEN, with actions: read).
Standard library only, so it runs on the runner's own python3.
"""
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _api(url):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "my-app-artifact-helper",
    })
    return urllib.request.build_opener(_NoRedirect()).open(req, timeout=60)


def newest(name):
    api, repo = os.environ["GITHUB_API_URL"], os.environ["GITHUB_REPOSITORY"]
    with _api(f"{api}/repos/{repo}/actions/artifacts?name={name}&per_page=100") as r:
        live = [a for a in json.load(r).get("artifacts", []) if not a.get("expired")]
    return max(live, key=lambda a: a["created_at"]) if live else None


def download(artifact, out_dir):
    # The archive URL answers with a redirect to blob storage. The token must
    # not travel there, so the redirect is followed by hand, without it.
    try:
        with _api(artifact["archive_download_url"]) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        with urllib.request.urlopen(e.headers["Location"], timeout=300) as r:
            data = r.read()
    zipfile.ZipFile(io.BytesIO(data)).extract("memory.db", out_dir)


def main(argv):
    if len(argv) not in (1, 2):
        print(__doc__, file=sys.stderr)
        return 2
    name = argv[0]
    artifact = newest(name)
    if artifact is None:
        print(f"no unexpired artifact named {name}", file=sys.stderr)
        return 3
    run = (artifact.get("workflow_run") or {}).get("id")
    print(f"{name}: artifact {artifact['id']}, {artifact['created_at']}, run {run}", file=sys.stderr)
    if len(argv) == 2:
        download(artifact, argv[1])
    print(artifact["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
