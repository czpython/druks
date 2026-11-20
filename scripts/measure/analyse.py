"""Per agent call of one downloaded ticket: turns, context read, cache writes, long pauses,
and where tool time went. Claude session.jsonl only; Codex calls are listed from run.json.

    python3 analyse.py ~/druks-transcripts/DRU-1200-event4579
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


def analyse(session):
    seen, turns, uses, tool_time, commands, slow = set(), [], {}, {}, {}, []
    for line in session.open():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        stamp = entry.get("timestamp")
        message = entry.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # ast-grep-ignore: rule-3
            for block in content:
                if not isinstance(block, dict):  # ast-grep-ignore: rule-3
                    continue
                if block.get("type") == "tool_use" and stamp:
                    command = (block.get("input") or {}).get("command", "")
                    key = classify.classify(command) if block.get("name") == "Bash" else block.get("name")
                    uses[block["id"]] = (ts(stamp), key)
                    commands[block["id"]] = command.replace("\n", " ")[:90] or key
                if block.get("type") == "tool_result" and stamp and block.get("tool_use_id") in uses:
                    started, key = uses.pop(block["tool_use_id"])
                    seconds = (ts(stamp) - started).total_seconds()
                    tool_time[key] = tool_time.get(key, 0) + seconds
                    if seconds > 120:
                        slow.append((seconds / 60, commands.get(block["tool_use_id"], key)))
        if entry.get("type") != "assistant" or message.get("id") in seen or not stamp:
            continue
        seen.add(message.get("id"))
        usage = message.get("usage") or {}
        creation = usage.get("cache_creation") or {}
        turns.append((
            ts(stamp),
            usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0),
            usage.get("cache_creation_input_tokens", 0),
            creation.get("ephemeral_1h_input_tokens", 0),
        ))
    if turns:
        gaps = [
            (b[0] - a[0]).total_seconds() / 60
            for a, b in zip(turns, turns[1:])
            if (b[0] - a[0]).total_seconds() > 300
        ]
        return {
            "turns": len(turns),
            "minutes": (turns[-1][0] - turns[0][0]).total_seconds() / 60,
            "read": sum(t[1] for t in turns),
            "max": max(t[1] for t in turns),
            "write": sum(t[2] for t in turns),
            "write_1h": sum(t[3] for t in turns),
            "gaps": gaps,
            "tools": {k: v / 60 for k, v in sorted(tool_time.items(), key=lambda kv: -kv[1])},
            "slow": slow,
        }


def main():
    root = Path(sys.argv[1]).expanduser()
    total_read = 0
    for run_json in sorted(root.rglob("run.json")):
        run = json.loads(run_json.read_text())
        print(f"\n== {run_json.parent.name}  ({run['kind'].split('.')[-1]}, {run['state']})")
        for call_dir in sorted(p for p in run_json.parent.iterdir() if p.is_dir()):
            meta = json.loads((call_dir / "manifest.json").read_text()) if (call_dir / "manifest.json").exists() else {}
            harness = meta.get("harness", "?")
            session = call_dir / "session.jsonl"
            if not session.exists():
                session = call_dir / "stdout.jsonl"
            result = analyse(session) if harness == "claude" and session.exists() else None
            name = call_dir.name.split("-", 1)[1].rsplit("-", 1)[0]
            if not result:
                print(f"  {name:<24} ({harness}, not analysed)")
                continue
            total_read += result["read"]
            tools = ", ".join(f"{k} {v:.0f}m" for k, v in result["tools"].items() if v >= 0.5)
            print(
                f"  {name:<24} {result['minutes']:5.0f} min {result['turns']:4} turns "
                f"read {result['read'] / 1e6:5.1f}M max ctx {result['max'] / 1e3:4.0f}k "
                f"write {result['write'] / 1e3:5.0f}k (1h {result['write_1h'] / 1e3:.0f}k) "
                f"pauses>5m {len(result['gaps'])}{' ' + str([round(g) for g in result['gaps']]) if result['gaps'] else ''}"
            )
            if tools:
                print(f"      tool time: {tools}")
            for minutes, command in result["slow"]:
                print(f"        {minutes:4.0f}m  {command}")
    print(f"\nclaude context read, all calls: {total_read / 1e6:.1f}M")


main()
