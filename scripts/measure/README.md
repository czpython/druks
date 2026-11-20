# Build measurements

These scripts measure each Software Factory build the same way. Measure the builds
before a change and the builds after it with the same scripts and the same flags, then
compare the same numbers.

The scripts read the dashboard API with your session cookie. They send GET requests
only, and they use only the Python standard library. The transcript measures read
Claude stream-json only: `analyse.py` and `timeline.py` list a Codex call without
measuring its turns.

## The session cookie

1. Sign in to the dashboard and copy the value of the `Cookie` request header.
2. Save it as one line, `Cookie: <value>`, in `~/.druks-cloud-cookie`. Keep the file
   outside the repository.
3. Let only yourself read it: `chmod 600 ~/.druks-cloud-cookie`.
4. Never commit the cookie file. The scripts never print its value.
5. Sign out of the dashboard when you finish.

Each script that reads the API takes `--cookie FILE` (default `~/.druks-cloud-cookie`)
and a required `--base URL`, the dashboard to read. A 401 or a 403 stops the script:
the cookie expired or is wrong.

## Scripts

Run them with `python3 scripts/measure/<script>`.

| Script and arguments | Output |
| --- | --- |
| `fetch-event-transcripts.py EVENT [--out DIR]` | The folder `DIR/<ticket>-event<EVENT>/` and its archive `DIR/<ticket>-event<EVENT>.tgz`. `DIR` is `~/druks-transcripts` by default. |
| `find-long-runs.py --since YYYY-MM-DD [--app APP] [--type TYPE] [--top N] [--fetch N] [--tickets KEYS] [--out DIR]` | A table on stdout: the top `N` subjects with an event since the date, by agent minutes. `--fetch` and `--tickets` run `fetch-event-transcripts.py` for those rows. |
| `ci-survey.py --since YYYY-MM-DD` | A table on stdout, progress on stderr, and `ci-survey.json` beside the script. Git ignores that file. |
| `timeline.py FOLDER` | The phase table of one downloaded ticket on stdout. |
| `analyse.py FOLDER` | For each call of one downloaded ticket: turns, context, cache writes, pauses, and tool time, on stdout. |
| `queue-snapshot.py` | One new file in `.tmp/queue/` of this repository, and two counts on stdout. |

By default, `find-long-runs.py` uses `--app software_factory`, `--type work_item`,
`--top 15`, and `--fetch 0`. `--tickets` takes ticket keys, separated by commas.

`EVENT` is the id in `/events?selected=<id>`. `fetch-event-transcripts.py` fetches every
run of the event's subject and every call of each run. For a work item with a pull
request, it also fetches the runs that review the pull request, into
`pull_request-<number>/`. A run folder holds `run.json`. A call folder holds the files
that the call's file listing names, but not the artifact descriptor. The listing does
not name `session.jsonl`. The script stops when the folder exists.

`FOLDER` is a folder that `fetch-event-transcripts.py` wrote. `analyse.py` reads the
`session.jsonl` of a call when the call folder has one, and `stdout.jsonl` if not.
`timeline.py` reads `stdout.jsonl`. Both read the transcript of a call only when its
`manifest.json` names the `claude` harness.

## Measures

| Measure | Target for a small change | Where the number comes from |
| --- | --- | --- |
| Implement minutes | ≤ 10 implement min | `ci-survey.py` `impl min`: for each implement call, the last minus the first `timestamp` in its `stdout.jsonl`, added up per ticket. `timeline.py`: the `startedAt` and `finishedAt` of each implement `CALL` line. |
| Clock excluding human gates | ≤ 25 min excluding gates | Not available: no script finds the time that a gate waits. `find-long-runs.py` `span h` is the latest `finishedAt` minus the earliest `startedAt` of the work item's finished calls. `timeline.py` shows a clock from the first call and a `GAP` line for more than 2 minutes between calls. A gap can be a gate, CI, or a queue. |
| Turn minutes spent on CI polling, `sleep`, or full-suite commands | 0 | The minutes from a Bash `tool_use` to its `tool_result`: `ci-survey.py` `CI wait` and `suite`, and `analyse.py` `tool time`. `timeline.py`: the minutes of each segment of an implement call, from one turn to the next. A segment shorter than 90 seconds joins the segment before it, except a `waiting` segment. |
| Context read per implement call | ≤ 2M for a small change | `analyse.py` and `timeline.py` `read`: for the distinct assistant messages of a call, the sum of `usage.input_tokens + cache_read_input_tokens + cache_creation_input_tokens`. `max ctx` is the largest message. Plan size is not available: no script measures it. |
| Red CI caused by the agent's own code | No increase | Not available. `timeline.py` marks a call with `CI red` when its prompt has `## CI is red`. No script finds the cause. |
| Review rounds | No increase | `timeline.py` prints one `CALL` line for each call: `review_plan` for a plan review, and `review_pull_request` for a pull request review. No script counts them. |
| Pauses over 5 minutes | No target. They decide the 1-hour cache issue. | `analyse.py` `pauses>5m`: the gaps of more than 300 seconds between distinct assistant messages, in minutes. |
| Cache writes | No target | `analyse.py` `write`: the sum of `cache_creation_input_tokens`. `1h`: the sum of `cache_creation.ephemeral_1h_input_tokens`. `timeline.py` `full price`: the sum of `cache_creation_input_tokens + input_tokens`. |
| In-turn suite time left | No target. It sizes the verification box. | `ci-survey.py` `suite`, `analyse.py` `full-suite`, and `timeline.py` `suite`. |

`find-long-runs.py` adds up the work item's calls of every agent: `agent min` from
`startedAt` to `finishedAt`, `in tokens` from `tokens.inputTokens`, and `cost` from
`costUsd`. Its agent minutes exclude the pull request reviews, because those run on the
`pull_request` subject.

One classifier, `classify.py`, puts a Bash command in a group, and `ci-survey.py`,
`timeline.py`, and `analyse.py` all import it, so a command counts the same way
everywhere. The groups are estimates from the command text:

- **waiting**: the command contains a `sleep`, or `gh pr checks`, `gh run watch`,
  `gh run view`, `gh run list`, or `gh run rerun`. A `gh run view --log` or
  `--log-failed` reads the logs, so it is `work`.
- **full suite**: `pytest` on the directory `backend/` or `backend/tests` itself, or
  with `--locked` at the repo root. A `pytest` on a file path, a `::` test id, or
  `-k` selects less, so it is `work`.
- **work**: everything else.

The 0-minute target is the sum of the waiting and the full-suite minutes.

Missing data is not zero. `ci-survey.py` skips an implement call whose `stdout.jsonl`
it cannot download, but counts the call in `impl calls`. `fetch-event-transcripts.py`
prints `FAILED` for a file that it cannot download and continues. `analyse.py` and
`timeline.py` measure no turns for a call without a Claude transcript. Such a call adds
no minutes, so it does not show that the call did not wait.

## Builds waiting for a box

`queue-snapshot.py` reads the work item board, `GET /api/software_factory/work_item`.
A build is in flight when its row has `status.kind` `software_factory.build` and
`status.state` `running` or `scheduled`. For each build in flight, the script reads the
work item detail. The build waits for a box while the detail's `phase` is
`queued_for_vm`. The script counts each `status.run` once.

Each invocation writes one new file, `.tmp/queue/<UTC time>.json`, in this repository.
Git ignores `.tmp/`. The file holds `taken_at`, `board_rows`, and the run ids in
`in_flight_builds` and `queued_for_vm`. Do not add up the counts of two snapshots,
because one build can wait in both. Compare the run ids.

A snapshot shows one moment. A wait that starts and ends between two snapshots does not
show, so snapshots count fewer waits than occur. The board lists only the 500 most
recently updated unresolved work items, and a snapshot sees only those.

The script changes nothing.

## Tests

```bash
uv run pytest --confcutdir=backend/tests/reporting backend/tests/reporting
```

The test runs `queue-snapshot.py` on synthetic files against a fake dashboard.
`--confcutdir` keeps out `backend/tests/conftest.py`, which needs Postgres and Redis.
