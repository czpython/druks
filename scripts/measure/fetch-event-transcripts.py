#!/usr/bin/env python3
"""Download every agent-call file behind one Druks event, through the dashboard API.

    python3 fetch-event-transcripts.py 2785 --base https://druks.example
    python3 fetch-event-transcripts.py 2785 --base https://druks.example --out ~/druks-transcripts

The event names a subject. The script reads that subject's timeline, so it fetches
every run of the subject, not only the event's run. For a Software Factory work item
with a pull request, it also fetches the runs of that pull request's reviews.

Authentication is a session cookie, read from a header file holding one line,
``Cookie: <value>``. The value is never printed. Only GET requests are made.
"""

import argparse
import json
import os
import re
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request


def read_cookie(path):
    with open(os.path.expanduser(path)) as handle:
        line = handle.read().strip()
    name, _, value = line.partition(":")
    if name.strip().lower() != "cookie" or not value.strip():
        sys.exit(f"{path} must hold one line: 'Cookie: <value>'")
    return value.strip()


class Api:
    def __init__(self, base, cookie):
        self.base = base.rstrip("/")
        self.cookie = cookie

    def _open(self, path):
        request = urllib.request.Request(self.base + path, headers={"Cookie": self.cookie})
        try:
            return urllib.request.urlopen(request, timeout=120)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                sys.exit(f"{error.code} on {path}: the session cookie expired or is wrong.")
            raise

    def json(self, path):
        with self._open(path) as response:
            return json.load(response)

    def download(self, path, dest):
        with self._open(path) as response, open(dest, "wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)


def find_event(api, event_id):
    page = api.json(f"/api/events?before={event_id + 1}&limit=1")
    for item in page.get("items", []):
        if item.get("seq") == event_id:
            return item
    sys.exit(f"Event {event_id} is not in the feed.")


def subject_path(app, subject_type, subject_id):
    return f"/api/{app}/{subject_type}/{urllib.parse.quote(str(subject_id), safe='/')}"


def safe(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")


def fetch_subject(api, app, path, out_dir):
    subject = api.json(path)
    with open(os.path.join(out_dir, "subject.json"), "w") as handle:
        json.dump(subject, handle, indent=1)
    files = 0
    for run in subject.get("timeline", []):
        run_dir = os.path.join(
            out_dir,
            "%s-%s-%s"
            % (run["createdAt"][:16].replace(":", ""), safe(run["label"].lower()), run["id"][:8]),
        )
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "run.json"), "w") as handle:
            json.dump(run, handle, indent=1)
        for index, call in enumerate(run.get("agentCalls", []), 1):
            call_dir = os.path.join(
                run_dir, "%02d-%s-%s" % (index, call["agent"].split(".")[-1], call["id"][:8])
            )
            os.makedirs(call_dir, exist_ok=True)
            listing = api.json(f"/api/{app}/transcripts/{call['id']}/files")
            for entry in listing.values():
                # The listing also carries an artifact descriptor, which is no file.
                if not (entry and "sizeBytes" in entry):
                    continue
                name = entry["name"]
                try:
                    api.download(
                        f"/api/{app}/transcripts/{call['id']}/files/"
                        f"{urllib.parse.quote(name)}?disposition=attachment",
                        os.path.join(call_dir, name),
                    )
                    files += 1
                except urllib.error.HTTPError as error:
                    print(f"  FAILED {call['id']} {name}: {error.code}", file=sys.stderr)
    return subject, files


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("event", type=int, help="the event id, as in /events?selected=<id>")
    parser.add_argument("--base", required=True)
    parser.add_argument("--out", default="~/druks-transcripts")
    parser.add_argument("--cookie", default="~/.druks-cloud-cookie", help="header file")
    args = parser.parse_args()

    api = Api(args.base, read_cookie(args.cookie))
    event = find_event(api, args.event)
    app, subject_type, subject_id = event.get("app"), event.get("subjectType"), event.get("subjectId")
    if not (app and subject_type and subject_id):
        sys.exit(f"Event {args.event} ({event.get('topic')}) names no subject.")
    print(f"event {args.event}: {event.get('topic')} on {app} {subject_type} {subject_id}")

    out_root = os.path.expanduser(args.out)
    probe = api.json(subject_path(app, subject_type, subject_id))
    summary = probe.get("summary") or {}
    label = summary.get("ticketKey") or f"{subject_type}-{subject_id}"
    folder = f"{safe(label)}-event{args.event}"
    out_dir = os.path.join(out_root, folder)
    if os.path.exists(out_dir):
        sys.exit(f"{out_dir} exists. Remove it, or pick another --out.")
    os.makedirs(out_dir)

    subject, files = fetch_subject(
        api, app, subject_path(app, subject_type, subject_id), out_dir
    )
    print(f"  {subject_type} {subject_id}: {len(subject.get('timeline', []))} runs")

    # A Software Factory build reviews its pull request in runs of another subject.
    if subject_type == "work_item" and summary.get("repo") and summary.get("prNumber"):
        pr_id = f"{summary['repo']}#{summary['prNumber']}"
        pr_dir = os.path.join(out_dir, f"pull_request-{summary['prNumber']}")
        os.makedirs(pr_dir)
        pr, pr_files = fetch_subject(api, app, subject_path(app, "pull_request", pr_id), pr_dir)
        files += pr_files
        print(f"  pull_request {pr_id}: {len(pr.get('timeline', []))} runs")

    archive = out_dir + ".tgz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(out_dir, arcname=folder)
    print(f"{files} files in {out_dir}\n{archive}")


if __name__ == "__main__":
    main()
