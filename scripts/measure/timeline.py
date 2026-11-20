"""Phase timeline of one downloaded ticket: every agent call and the gaps between them, and
inside each Claude call its stretches of work, full-suite runs, pushes and CI waits.

    python3 -I timeline.py ~/druks-transcripts/DRU-1200-event4579
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
classify = __import__("classify")


def ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def kind_of(block):
    name = block.get("name")
    if name != "Bash":
        return "output" if name == "StructuredOutput" else "work"
    return classify.classify((block.get("input") or {}).get("command", ""))


def turns_of(path):
    """(time, kind, context, full-price tokens) per API response."""
    seen, turns = set(), []
    for line in path.open():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = entry.get("message") or {}
        if entry.get("type") != "assistant" or not entry.get("timestamp"):
            continue
        kinds = [kind_of(b) for b in message.get("content") or []
                 # ast-grep-ignore: rule-3
                 if isinstance(b, dict) and b.get("type") == "tool_use"]
        rank = ("waiting", "full suite", "output", "work")
        kind = min(kinds, key=rank.index) if kinds else "work"
        if message.get("id") in seen:
            if kinds and turns:
                previous = turns[-1]
                turns[-1] = (previous[0], min(previous[1], kind, key=rank.index), previous[2], previous[3])
            continue
        seen.add(message.get("id"))
        usage = message.get("usage") or {}
        write = usage.get("cache_creation_input_tokens", 0)
        uncached = usage.get("input_tokens", 0)
        turns.append((ts(entry["timestamp"]), kind,
                      uncached + write + usage.get("cache_read_input_tokens", 0), write + uncached))
    return turns


def segments(turns, end):
    out = []
    for index, (at, kind, ctx, full) in enumerate(turns):
        nxt = turns[index + 1][0] if index + 1 < len(turns) else end
        if out and out[-1]["kind"] == kind:
            seg = out[-1]
        else:
            seg = {"kind": kind, "start": at, "read": 0, "full": 0, "ctx": 0, "turns": 0}
            out.append(seg)
        seg["end"] = nxt
        seg["read"] += ctx
        seg["full"] += full
        seg["ctx"] = max(seg["ctx"], ctx)
        seg["turns"] += 1
    merged = []
    for seg in out:
        short = (seg["end"] - seg["start"]).total_seconds() < 90 and seg["kind"] != "waiting"
        if merged and (short or merged[-1]["kind"] == seg["kind"]):
            last = merged[-1]
            last["end"] = seg["end"]
            for key in ("read", "full", "turns"):
                last[key] += seg[key]
            last["ctx"] = max(last["ctx"], seg["ctx"])
        else:
            merged.append(seg)
    return merged


def why(call_dir, name):
    prompt = (call_dir / "prompt.md").read_text() if (call_dir / "prompt.md").exists() else ""
    reasons = [label for marker, label in (("## CI is red", "CI red"), ("## Revision request", "revision request"),
               ("## Prior implementation review", "prior review"), ("## Human feedback", "human feedback"))
               if marker in prompt]
    try:
        output = json.loads((call_dir / "output.json").read_text())
    except (OSError, ValueError):
        output = {}
    text = output.get("summary") or output.get("where") or output.get("verdict") or output.get("decision") or ""
    if not text:
        text = json.dumps(output)[:300]
    return reasons, " ".join(str(text).split())[:420], output.get("status") or output.get("verdict") or ""


def main():
    root = Path(sys.argv[1]).expanduser()
    calls = []
    for run_json in root.rglob("run.json"):
        run = json.loads(run_json.read_text())
        dirs = sorted(p for p in run_json.parent.iterdir() if p.is_dir())
        for call, call_dir in zip(run.get("agentCalls", []), dirs):
            if call.get("startedAt"):
                calls.append((ts(call["startedAt"]), ts(call["finishedAt"]) if call.get("finishedAt") else None,
                              call, call_dir))
    calls.sort(key=lambda c: c[0])
    zero = calls[0][0]

    def clock(at):
        minutes = int((at - zero).total_seconds() // 60)
        return f"{minutes // 60}:{minutes % 60:02d}"

    previous_end = None
    for start, end, call, call_dir in calls:
        name = call["agent"].split(".")[-1]
        if previous_end and (start - previous_end).total_seconds() > 120:
            print(f"{clock(previous_end)}-{clock(start)}  GAP {(start - previous_end).total_seconds() / 60:.0f} min")
        reasons, text, status = why(call_dir, name)
        cost = call.get("costUsd") or 0
        print(f"{clock(start)}-{clock(end) if end else '?'}  CALL {name} [{call_dir.parent.name[-8:]}] "
              f"${cost:.2f} {status} {('(' + ', '.join(reasons) + ')') if reasons else ''}")
        print(f"      -> {text}")
        stream = call_dir / "stdout.jsonl"
        manifest = json.loads((call_dir / "manifest.json").read_text()) if (call_dir / "manifest.json").exists() else {}
        if manifest.get("harness") == "claude" and stream.exists():
            turns = turns_of(stream)
            if turns:
                total_read = sum(t[2] for t in turns)
                total_full = sum(t[3] for t in turns)
                print(f"      claude: {len(turns)} turns, read {total_read / 1e6:.1f}M, full price {total_full / 1e6:.2f}M, max ctx {max(t[2] for t in turns) / 1e3:.0f}k")
                if name in ("implement",):
                    for seg in segments(turns, end or turns[-1][0]):
                        print(f"      {clock(seg['start'])}-{clock(seg['end'])} {seg['kind']:<11} "
                              f"{(seg['end'] - seg['start']).total_seconds() / 60:4.0f}m  ctx {seg['ctx'] / 1e3:4.0f}k  "
                              f"read {seg['read'] / 1e6:4.1f}M  full {seg['full'] / 1e6:4.2f}M  turns {seg['turns']}")
        previous_end = max(previous_end, end) if previous_end and end else (end or previous_end)


main()
