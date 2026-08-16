#!/usr/bin/env python3
"""
collect_prompts.py — Alaga Health Venture Sprint

Exports YOUR prompts from your Claude Code sessions for this project so you
can submit them with your case. We read how you prompt, not just what you built.

What it does:
  1. Finds the Claude Code session logs for this project directory
     (Claude Code stores transcripts locally under ~/.claude/projects/).
  2. Extracts only the prompts you typed (never API keys, never file contents
     you didn't paste into a prompt).
  3. Writes PROMPTS.md (a readable, chronological log) and prompt_stats.json.

How to run, from your project directory:
  python3 collect_prompts.py

Then REVIEW PROMPTS.md before submitting. Delete anything you consider
private. An edited log is fine; an empty one defeats the purpose.

If you used a different AI coding tool (Codex CLI, Cursor, etc.), submit that
tool's session export or your prompt history in any readable format instead.
"""

import json
import os
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def encode_project_path(p: Path) -> str:
    # Claude Code names project folders by the absolute path with separators as dashes
    return str(p.resolve()).replace("/", "-").replace("\\", "-")


def find_session_files(project_dir: Path):
    candidates = []
    if not CLAUDE_PROJECTS.exists():
        return candidates
    encoded = encode_project_path(project_dir)
    for d in CLAUDE_PROJECTS.iterdir():
        if not d.is_dir():
            continue
        if d.name == encoded:
            candidates.extend(sorted(d.glob("*.jsonl")))
    if candidates:
        return candidates
    # Fallback: scan every project folder and match on the recorded cwd
    target = str(project_dir.resolve())
    for d in CLAUDE_PROJECTS.iterdir():
        if not d.is_dir():
            continue
        for f in d.glob("*.jsonl"):
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("cwd") == target:
                            candidates.append(f)
                            break
                        break  # only inspect the first parseable line per file
            except OSError:
                continue
    return sorted(set(candidates))


def is_human_prompt(obj) -> bool:
    if obj.get("type") != "user":
        return False
    if obj.get("isMeta"):
        return False
    msg = obj.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # Tool results also arrive as user-role messages; those are not prompts
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return False
    text = text.strip()
    if not text:
        return False
    # Skip slash-command scaffolding that Claude Code injects
    if text.startswith("<command-") or text.startswith("<local-command"):
        return False
    return True


def prompt_text(obj) -> str:
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def tool_uses(obj):
    if obj.get("type") != "assistant":
        return []
    content = (obj.get("message") or {}).get("content") or []
    if not isinstance(content, list):
        return []
    return [b.get("name", "unknown") for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def parse_ts(obj):
    ts = obj.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def main():
    project_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    files = find_session_files(project_dir)
    if not files:
        print(f"No Claude Code sessions found for {project_dir.resolve()}.")
        print("Run this from your project directory. If you used another tool,")
        print("submit that tool's session export instead.")
        sys.exit(1)

    sessions = []
    tool_counts = {}
    for f in files:
        prompts = []
        first_ts, last_ts = None, None
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = parse_ts(obj)
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts
                for name in tool_uses(obj):
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                if is_human_prompt(obj):
                    prompts.append((ts, prompt_text(obj)))
        if prompts:
            sessions.append({"file": f.name, "start": first_ts, "end": last_ts, "prompts": prompts})

    if not sessions:
        print("Sessions were found but contained no prompts. Nothing to export.")
        sys.exit(1)

    sessions.sort(key=lambda s: (s["start"] or datetime.min.replace(tzinfo=None)).isoformat())

    all_prompts = [p for s in sessions for _, p in s["prompts"]]
    lengths = [len(p.split()) for p in all_prompts]

    out_md = project_dir / "PROMPTS.md"
    with open(out_md, "w", encoding="utf-8") as out:
        out.write("# Prompt Log — Alaga Venture Sprint\n\n")
        out.write(f"Project: `{project_dir.resolve()}`  \n")
        out.write(f"Sessions: {len(sessions)} · Prompts: {len(all_prompts)}\n\n")
        out.write("_Reviewed by the candidate before submission. Edits are allowed and should be noted._\n\n")
        n = 0
        for i, s in enumerate(sessions, 1):
            span = ""
            if s["start"] and s["end"]:
                span = f" · {s['start'].strftime('%Y-%m-%d %H:%M')} to {s['end'].strftime('%H:%M')}"
            out.write(f"## Session {i}{span}\n\n")
            for ts, p in s["prompts"]:
                n += 1
                t = ts.strftime("%H:%M") if ts else "--:--"
                body = re.sub(r"\n{3,}", "\n\n", p)
                out.write(f"**{n}. [{t}]**\n\n{body}\n\n---\n\n")

    stats = {
        "sessions": len(sessions),
        "total_prompts": len(all_prompts),
        "median_prompt_words": int(statistics.median(lengths)) if lengths else 0,
        "longest_prompt_words": max(lengths) if lengths else 0,
        "shortest_prompt_words": min(lengths) if lengths else 0,
        "assistant_tool_calls": dict(sorted(tool_counts.items(), key=lambda kv: -kv[1])),
    }
    out_json = project_dir / "prompt_stats.json"
    with open(out_json, "w", encoding="utf-8") as out:
        json.dump(stats, out, indent=2)

    print(f"Wrote {out_md.name} ({len(all_prompts)} prompts across {len(sessions)} sessions)")
    print(f"Wrote {out_json.name}")
    print("\nReview PROMPTS.md before you submit. Remove anything private and note the removal.")


if __name__ == "__main__":
    main()
