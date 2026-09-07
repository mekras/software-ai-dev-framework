#!/usr/bin/env python3
"""Адаптер Codex CLI для многоходовой продуктовой оценки."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def extract_answer(events: list[Any], fallback: str) -> str:
    """Собрать все видимые сообщения ассистента за ход."""
    messages = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        value = item.get("text")
        if isinstance(value, str) and value.strip():
            messages.append(value)
    return "\n\n".join(messages) if messages else fallback


def main() -> int:
    if len(sys.argv) != 6 or sys.argv[1] not in {"start", "resume"}:
        print(
            "Использование: codex.py <start|resume> <модель> "
            "<рабочий каталог> <сеанс или -> <файл следа>",
            file=sys.stderr,
        )
        return 2
    operation, model, workdir, session_id, trace_name = sys.argv[1:]
    prompt = sys.stdin.read()
    trace_path = Path(trace_name)
    answer_path = trace_path.with_suffix(".answer.txt")
    common = [
        "--ignore-user-config",
        "--skip-git-repo-check",
        "-c",
        'approval_policy="never"',
        "-c",
        'sandbox_mode="workspace-write"',
        "--model",
        model,
        "--json",
        "--output-last-message",
        str(answer_path),
    ]
    if operation == "start":
        command = [
            "codex",
            "exec",
            "--sandbox",
            "workspace-write",
            "--color",
            "never",
            "-C",
            workdir,
            *common,
            "-",
        ]
    else:
        command = ["codex", "exec", "resume", *common, session_id, "-"]
    completed = subprocess.run(
        command,
        cwd=workdir,
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    trace_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode:
        print(completed.stderr, file=sys.stderr)
        return completed.returncode

    events = []
    for line in completed.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    identifiers = []
    commands = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    for node in walk(events):
        if not isinstance(node, dict):
            continue
        for key in ("thread_id", "session_id"):
            value = node.get(key)
            if isinstance(value, str):
                identifiers.append(value)
        command_value = node.get("command")
        if isinstance(command_value, str):
            commands.append(command_value)
        for key in usage:
            value = node.get(key)
            if isinstance(value, int):
                usage[key] = max(usage[key], value)
    active_session = identifiers[0] if identifiers else session_id
    if not active_session or active_session == "-":
        print("Codex CLI не сообщил идентификатор сеанса.", file=sys.stderr)
        return 1
    result = {
        "session_id": active_session,
        "answer": extract_answer(
            events,
            answer_path.read_text(encoding="utf-8")
            if answer_path.exists()
            else "",
        ),
        "commands": sorted(set(commands)),
        "usage": usage,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
