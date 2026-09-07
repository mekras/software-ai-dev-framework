#!/usr/bin/env python3
"""Адаптер Claude Code CLI для многоходовой продуктовой оценки."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
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
    """Собрать все видимые текстовые сообщения ассистента за ход."""
    messages = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        texts = [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"].strip()
        ]
        if texts:
            messages.append("\n".join(texts))
    return "\n\n".join(messages) if messages else fallback


def main() -> int:
    if len(sys.argv) != 6 or sys.argv[1] not in {"start", "resume"}:
        print(
            "Использование: claude.py <start|resume> <модель> "
            "<рабочий каталог> <сеанс или -> <файл следа>",
            file=sys.stderr,
        )
        return 2
    operation, model, workdir, session_id, trace_name = sys.argv[1:]
    if operation == "start":
        session_id = str(uuid.uuid4())
        session_args = ["--session-id", session_id]
    else:
        session_args = ["--resume", session_id]
    command = [
        "claude",
        "--print",
        "--verbose",
        "--output-format",
        "stream-json",
        "--permission-mode",
        "acceptEdits",
        "--strict-mcp-config",
        "--model",
        model,
        *session_args,
    ]
    completed = subprocess.run(
        command,
        cwd=workdir,
        input=sys.stdin.read(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    Path(trace_name).write_text(completed.stdout, encoding="utf-8")
    if completed.returncode:
        print(completed.stderr, file=sys.stderr)
        return completed.returncode
    events = []
    for line in completed.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    answer = ""
    commands = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    for node in walk(events):
        if not isinstance(node, dict):
            continue
        if node.get("type") == "result" and isinstance(node.get("result"), str):
            answer = node["result"]
        if node.get("name") == "Bash" and isinstance(node.get("input"), dict):
            value = node["input"].get("command")
            if isinstance(value, str):
                commands.append(value)
        for key in usage:
            value = node.get(key)
            if isinstance(value, int):
                usage[key] = max(usage[key], value)
    print(
        json.dumps(
            {
                "session_id": session_id,
                "answer": extract_answer(events, answer),
                "commands": sorted(set(commands)),
                "usage": usage,
            },
            ensure_ascii=False,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
