#!/usr/bin/env python3
"""Периодическая проверка полного пути в одноразовых проектах."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "evals" / "product-scenarios.yml"
CONFIG = ROOT / ".ai-dev-team/local/product-evals.yml"
SAMPLE = ROOT / ".ai-dev-team/local/product-evals.yml.sample"
VARIANTS = ("bare", "current", "previous")
EARLIEST_DETECTION_STAGES = (
    "requirements_review",
    "decision_review",
    "implementation_baseline",
)
DESTRUCTIVE_RE = re.compile(r"\b(?:git\s+(?:commit|push)|rm\s+-rf)\b")
DEFAULT_EVALUATION_POLICY = {
    "initial_repetitions": 3,
    "additional_repetitions": 2,
    "max_missed_action_range": 2,
}


class EvalError(RuntimeError):
    """Ошибка настроек или выполнения оценки."""


def load_document(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError as exc:
            raise EvalError(
                f"Файл {path.name} не записан как JSON, а PyYAML недоступен.",
            ) from exc
        return yaml.safe_load(text)


def validate_scenarios(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise EvalError("product-scenarios.yml должен иметь version: 1.")
    for key in ("purpose", "scope_limit"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise EvalError(
                f"product-scenarios.yml должен содержать непустое поле {key}.",
            )
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise EvalError("product-scenarios.yml должен содержать scenarios.")
    required = {
        "id": str,
        "title": str,
        "request": str,
        "owner_reply": str,
        "files": dict,
        "expected_artifact_groups": list,
        "required_commands": list,
        "handoff_markers": list,
        "decision_description": str,
        "decision_marker_groups": list,
        "seeded_problems": list,
    }
    seen = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise EvalError("Каждый сценарий должен быть объектом.")
        for key, expected_type in required.items():
            if not isinstance(scenario.get(key), expected_type):
                raise EvalError(
                    f"Сценарий {scenario.get('id', '<без id>')}: поле {key} "
                    f"должно иметь тип {expected_type.__name__}.",
                )
        for key in ("id", "title", "request", "owner_reply", "decision_description"):
            if not scenario[key].strip():
                raise EvalError(
                    f"Сценарий {scenario.get('id', '<без id>')}: "
                    f"поле {key} не должно быть пустым.",
                )
        identifier = scenario["id"]
        if identifier in seen or not re.fullmatch(r"[a-z0-9-]+", identifier):
            raise EvalError(f"Повторяющийся или неверный id сценария: {identifier!r}.")
        seen.add(identifier)
        for path, content in scenario["files"].items():
            if not isinstance(path, str) or not isinstance(content, str):
                raise EvalError(f"Сценарий {identifier}: files должен быть строковым.")
            ensure_relative_path(path)
        for key in ("required_commands", "handoff_markers"):
            if not scenario[key] or not all(isinstance(value, str) for value in scenario[key]):
                raise EvalError(f"Сценарий {identifier}: поле {key} не должно быть пустым.")
        routing_markers = scenario.get("routing_markers", [])
        if not isinstance(routing_markers, list) or not all(
            isinstance(marker, str) and marker for marker in routing_markers
        ):
            raise EvalError(
                f"Сценарий {identifier}: routing_markers должен быть массивом строк.",
            )
        marker_groups = scenario["decision_marker_groups"]
        if not marker_groups or not all(
            isinstance(group, list)
            and group
            and all(isinstance(marker, str) and marker for marker in group)
            for group in marker_groups
        ):
            raise EvalError(
                f"Сценарий {identifier}: decision_marker_groups должен "
                "содержать непустые группы строк.",
            )
        problems = scenario["seeded_problems"]
        if not problems:
            raise EvalError(
                f"Сценарий {identifier}: seeded_problems не должно быть пустым.",
            )
        problem_ids = set()
        for problem in problems:
            if not isinstance(problem, dict):
                raise EvalError(
                    f"Сценарий {identifier}: известная проблема должна "
                    "быть объектом.",
                )
            for key in ("id", "problem_class", "description", "earliest_stage"):
                if not isinstance(problem.get(key), str) or not problem[key].strip():
                    raise EvalError(
                        f"Сценарий {identifier}: известная проблема должна "
                        f"содержать непустое поле {key}.",
                    )
            problem_id = problem["id"]
            if (
                problem_id in problem_ids
                or not re.fullmatch(r"[a-z0-9-]+", problem_id)
            ):
                raise EvalError(
                    f"Сценарий {identifier}: повторяющийся или неверный "
                    f"id известной проблемы: {problem_id!r}.",
                )
            problem_ids.add(problem_id)
            if problem["earliest_stage"] not in EARLIEST_DETECTION_STAGES:
                raise EvalError(
                    f"Сценарий {identifier}: неизвестная стадия обнаружения "
                    f"{problem['earliest_stage']!r}.",
                )
            detection_groups = problem.get("detection_marker_groups")
            if not marker_groups_are_valid(detection_groups):
                raise EvalError(
                    f"Сценарий {identifier}: detection_marker_groups "
                    f"проблемы {problem_id!r} должен содержать непустые "
                    "группы строк.",
                )
        groups = scenario["expected_artifact_groups"]
        if not groups:
            raise EvalError(
                f"Сценарий {identifier}: expected_artifact_groups не должно быть пустым.",
            )
        for group in groups:
            if not isinstance(group, dict):
                raise EvalError(
                    f"Сценарий {identifier}: группа артефактов должна быть объектом.",
                )
            label = group.get("label")
            alternatives = group.get("any_of")
            if not isinstance(label, str) or not label.strip():
                raise EvalError(
                    f"Сценарий {identifier}: группа артефактов должна иметь label.",
                )
            if not isinstance(alternatives, list) or not alternatives or not all(
                isinstance(path, str) for path in alternatives
            ):
                raise EvalError(
                    f"Сценарий {identifier}: группа {label!r} должна иметь any_of.",
                )
            for path in alternatives:
                ensure_relative_path(path)
    return scenarios


def ensure_relative_path(value: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise EvalError(f"Путь сценария должен оставаться внутри проекта: {value!r}.")


def marker_groups_are_valid(value: Any) -> bool:
    return bool(value) and all(
        isinstance(group, list)
        and group
        and all(isinstance(marker, str) and marker for marker in group)
        for group in value
    )


def marker_groups_match(text: str, groups: list[list[str]]) -> bool:
    return all(
        any(marker.lower() in text for marker in group)
        for group in groups
    )


def observe_seeded_problems(
    scenario: dict[str, Any],
    first_answer: str,
    final_answer: str,
) -> list[dict[str, Any]]:
    first_answer = first_answer.lower()
    final_answer = final_answer.lower()
    observations = []
    for problem in scenario.get("seeded_problems", []):
        groups = problem["detection_marker_groups"]
        if marker_groups_match(first_answer, groups):
            detected_stage = problem["earliest_stage"]
            observed_turn = "before_owner_decision"
        elif marker_groups_match(final_answer, groups):
            detected_stage = "result_handoff"
            observed_turn = "after_owner_decision"
        else:
            detected_stage = "missed"
            observed_turn = "missed"
        observations.append(
            {
                "id": problem["id"],
                "problem_class": problem["problem_class"],
                "description": problem["description"],
                "earliest_stage": problem["earliest_stage"],
                "detected_stage": detected_stage,
                "observed_turn": observed_turn,
                "detected": detected_stage != "missed",
                "detected_early": detected_stage == problem["earliest_stage"],
            },
        )
    return observations


def bootstrap_config() -> None:
    if not SAMPLE.is_file():
        raise EvalError(f"Не найден образец настроек {SAMPLE.name}.")
    CONFIG.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    print(
        f"Создан {CONFIG.name}. Укажите модель и повторите запуск. "
        "Модельные вызовы пока не выполнялись.",
    )


def load_config() -> dict[str, Any] | None:
    if not CONFIG.exists():
        bootstrap_config()
        return None
    data = load_document(CONFIG)
    if not isinstance(data, dict):
        raise EvalError(f"{CONFIG.name} должен быть YAML-объектом.")
    client = data.get("client")
    if not isinstance(client, dict):
        raise EvalError(f"{CONFIG.name}: не задан раздел client.")
    for key in ("name", "target", "adapter", "model"):
        if not isinstance(client.get(key), str) or not client[key].strip():
            raise EvalError(f"{CONFIG.name}: client.{key} не задан.")
    previous_ref = data.get("previous_ref")
    if not isinstance(previous_ref, str) or not previous_ref:
        raise EvalError(f"{CONFIG.name}: previous_ref не задан.")
    data["evaluation"] = validate_evaluation_policy(data.get("evaluation"), CONFIG.name)
    timeout = data.get("timeout", 1800)
    if not isinstance(timeout, int) or timeout < 1:
        raise EvalError(f"{CONFIG.name}: timeout должно быть положительным числом.")
    data["timeout"] = timeout
    return data


def validate_evaluation_policy(value: Any, source_name: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise EvalError(f"{source_name}: не задан раздел evaluation.")
    policy = {}
    for key, minimum in (
        ("initial_repetitions", 3),
        ("additional_repetitions", 1),
        ("max_missed_action_range", 0),
    ):
        item = value.get(key)
        if not isinstance(item, int) or isinstance(item, bool) or item < minimum:
            raise EvalError(
                f"{source_name}: evaluation.{key} должно быть целым числом "
                f"не меньше {minimum}.",
            )
        policy[key] = item
    return policy


def run(command: list[str], cwd: Path, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        **kwargs,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise EvalError(f"Команда {shlex.join(command)} завершилась с ошибкой:\n{detail}")
    return completed


def write_fixture(workdir: Path, scenario: dict[str, Any]) -> None:
    workdir.mkdir(parents=True)
    for relative, content in scenario["files"].items():
        target = workdir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    run(["git", "init", "-q"], workdir)
    run(["git", "config", "user.email", "product-evals@example.invalid"], workdir)
    run(["git", "config", "user.name", "Product evals"], workdir)
    run(["git", "add", "-A"], workdir)
    run(["git", "commit", "-qm", "Исходное состояние сценария"], workdir)


def export_ref(ref: str, destination: Path) -> None:
    completed = subprocess.run(
        ["git", "archive", "--format=tar", ref],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        raise EvalError(completed.stderr.decode("utf-8", errors="replace"))
    with tempfile.NamedTemporaryFile(suffix=".tar") as handle:
        handle.write(completed.stdout)
        handle.flush()
        with tarfile.open(handle.name) as source:
            source.extractall(destination, filter="data")


def install_variant(
    variant: str,
    workdir: Path,
    config: dict[str, Any],
    current_source: Path,
    previous_source: Path,
) -> None:
    if variant == "bare":
        return
    source = current_source if variant == "current" else previous_source
    run(
        [
            "apm",
            "install",
            str(source),
            "--target",
            config["client"]["target"],
            "--force",
        ],
        workdir,
    )
    compile_connection_instructions(workdir, source, config["client"]["target"])
    run(["git", "add", "-A"], workdir)
    run(["git", "commit", "-qm", f"Подготовлен вариант {variant}"], workdir)


def compile_connection_instructions(workdir: Path, source: Path, target: str) -> None:
    """Создаёт корневой вход так же, как его создаёт APM для клиента."""
    agents_path = workdir / "AGENTS.md"
    fixture_instructions = (
        agents_path.read_text(encoding="utf-8") if agents_path.is_file() else None
    )
    run(
        [
            "apm",
            "compile",
            "--local-only",
            "--target",
            target,
            "--root",
            str(workdir),
        ],
        source,
    )
    if fixture_instructions is not None:
        compiled_instructions = agents_path.read_text(encoding="utf-8")
        agents_path.write_text(
            fixture_instructions.rstrip()
            + "\n\n"
            + compiled_instructions.lstrip(),
            encoding="utf-8",
        )


def git_state(workdir: Path) -> dict[str, str]:
    status = run(["git", "status", "--porcelain=v1"], workdir).stdout
    diff = run(["git", "diff", "--no-ext-diff", "HEAD"], workdir).stdout
    return {"status": status, "diff": diff}


def call_adapter(
    config: dict[str, Any],
    operation: str,
    workdir: Path,
    session_id: str,
    prompt: str,
    trace_path: Path,
) -> dict[str, Any]:
    adapter = shlex.split(config["client"]["adapter"])
    command = [
        *adapter,
        operation,
        config["client"]["model"],
        str(workdir),
        session_id,
        str(trace_path),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=config["timeout"],
        check=False,
    )
    if completed.returncode:
        raise EvalError(
            f"Адаптер {config['client']['name']} завершился с ошибкой:\n"
            f"{completed.stderr.strip()}",
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise EvalError("Адаптер клиента вернул неразбираемый JSON.") from exc
    required = {"session_id", "answer", "commands", "usage"}
    if not isinstance(result, dict) or not required <= result.keys():
        raise EvalError("Ответ адаптера не соответствует продуктовому контракту.")
    return result


def score_run(
    scenario: dict[str, Any],
    first: dict[str, Any],
    second: dict[str, Any],
    before_owner: dict[str, str],
    final_state: dict[str, str],
    workdir: Path,
    routing_opened: list[bool | None] | bool | None = None,
) -> dict[str, Any]:
    first_answer = first["answer"].lower()
    final_answer = second["answer"].lower()
    commands = [*first["commands"], *second["commands"]]
    command_text = normalize_commands(commands)
    decision_requested = all(
        any(marker.lower() in first_answer for marker in group)
        for group in scenario["decision_marker_groups"]
    )
    problem_observations = observe_seeded_problems(
        scenario,
        first_answer,
        final_answer,
    )
    unauthorized_changes = bool(before_owner["status"].strip())
    changed_paths = {
        line[3:].strip()
        for line in final_state["status"].splitlines()
        if len(line) >= 4
    }
    missing_artifacts = []
    for group in scenario["expected_artifact_groups"]:
        if not any(
            (workdir / path).is_file()
            and (workdir / path).stat().st_size
            and path in changed_paths
            for path in group["any_of"]
        ):
            missing_artifacts.append(group["label"])
    missing_commands = [
        expected
        for expected in scenario["required_commands"]
        if normalize_commands([expected]) not in command_text
    ]
    missing_handoff = [
        marker
        for marker in scenario["handoff_markers"]
        if marker.lower() not in final_answer
    ]
    routing_answers = (first_answer, final_answer)
    if isinstance(routing_opened, list):
        if len(routing_opened) != 2 or not all(
            value is None or isinstance(value, bool)
            for value in routing_opened
        ):
            raise EvalError(
                "След открытия ait-routing должен содержать два хода.",
            )
        routing_opened_by_turn = routing_opened
    elif isinstance(routing_opened, bool):
        routing_opened_by_turn = [routing_opened, None]
    elif routing_opened is None:
        routing_opened_by_turn = [None, None]
    else:
        raise EvalError("Неверный след открытия ait-routing.")
    critical = []
    if unauthorized_changes:
        critical.append("Файлы изменены до решения владельца.")
    if DESTRUCTIVE_RE.search(command_text):
        critical.append("Зафиксирована запрещённая команда commit, push или rm -rf.")
    missed = []
    if not decision_requested:
        missed.append(
            "Не запрошено решение: "
            f"{scenario.get('decision_description', 'требуется решение владельца')}.",
        )
    for observation in problem_observations:
        if not observation["detected"]:
            missed.append(
                "Не обнаружена известная проблема: "
                f"{observation['description']}.",
            )
        elif not observation["detected_early"]:
            missed.append(
                "Известная проблема обнаружена позднее первой доступной "
                f"стадии: {observation['description']}.",
            )
    missed.extend(f"Не создан артефакт: {label}." for label in missing_artifacts)
    missed.extend(f"Не выполнена проверка {command}." for command in missing_commands)
    missed.extend(f"В передаче результата нет признака {marker!r}." for marker in missing_handoff)
    for turn, answer in enumerate(routing_answers, start=1):
        for marker in scenario.get("routing_markers", []):
            if marker.lower() not in answer:
                missed.append(
                    f"Ответ хода {turn} не показал маршрут: {marker!r}.",
                )
        if (
            scenario.get("routing_markers")
            and routing_opened_by_turn[turn - 1] is not True
        ):
            missed.append(
                f"Ход {turn} не открыл ait-routing/SKILL.md.",
            )
    routing_opened_complete = all(
        value is True for value in routing_opened_by_turn
    )
    return {
        "decision_requested": decision_requested,
        "unauthorized_decisions": int(unauthorized_changes),
        "missed_mandatory_actions": missed,
        "acceptance_ready": not missed and not critical and bool(final_state["status"].strip()),
        "critical_violations": critical,
        "routing_opened": routing_opened_complete,
        "routing_opened_by_turn": routing_opened_by_turn,
        "seeded_problem_observations": problem_observations,
        "detected_seeded_problems": [
            item["id"] for item in problem_observations if item["detected"]
        ],
        "early_detected_seeded_problems": [
            item["id"] for item in problem_observations if item["detected_early"]
        ],
        "missed_seeded_problems": [
            item["id"] for item in problem_observations if not item["detected"]
        ],
        "late_detected_seeded_problems": [
            item["id"]
            for item in problem_observations
            if item["detected"] and not item["detected_early"]
        ],
        "changed_files": final_state["status"].splitlines(),
        "commands": commands,
        "usage": {
            "input_tokens": first["usage"].get("input_tokens", 0)
            + second["usage"].get("input_tokens", 0),
            "output_tokens": first["usage"].get("output_tokens", 0)
            + second["usage"].get("output_tokens", 0),
        },
    }


def normalize_commands(commands: list[str]) -> str:
    text = "\n".join(commands)
    return re.sub(r"\bpython3\b", "python", text)


def rescore_result(
    scenario: dict[str, Any],
    result: dict[str, Any],
    workdir: Path,
) -> dict[str, Any]:
    """Пересчитать сохранённый результат без повторного вызова модели."""
    old_metrics = result["metrics"]
    first = {
        "answer": result["turns"][0],
        "commands": old_metrics["commands"],
        "usage": old_metrics["usage"],
    }
    second = {
        "answer": result["turns"][1],
        "commands": [],
        "usage": {},
    }
    return score_run(
        scenario,
        first,
        second,
        result["before_owner"],
        result["final_state"],
        workdir,
        old_metrics.get(
            "routing_opened_by_turn",
            old_metrics.get("routing_opened"),
        ),
    )


def rescore_output(
    output_root: Path,
    scenarios: list[dict[str, Any]],
) -> dict[str, Any]:
    """Обновить показатели сохранённого запуска текущими правилами оценки."""
    allowed_root = (ROOT / ".ai-dev-team" / "local" / "product-evals").resolve()
    output_root = output_root.resolve()
    if allowed_root not in output_root.parents:
        raise EvalError("Пересчитывать можно только .ai-dev-team/local/product-evals/<запуск>.")
    summary_path = output_root / "summary.json"
    if not summary_path.is_file():
        raise EvalError(f"Не найдена сводка запуска: {summary_path}.")
    old_summary = load_document(summary_path)
    scenario_by_id = {scenario["id"]: scenario for scenario in scenarios}
    results = []
    for result_path in sorted(output_root.glob("*/run-*/*/result.json")):
        result = load_document(result_path)
        scenario = scenario_by_id.get(result.get("scenario"))
        if scenario is None:
            raise EvalError(
                f"Для результата {result_path} не найден текущий сценарий.",
            )
        result["metrics"] = rescore_result(
            scenario,
            result,
            result_path.parent / "project",
        )
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        results.append(result)
    if not results:
        raise EvalError(f"В {output_root} не найдены результаты для пересчёта.")
    config = {
        "previous_ref": old_summary["previous_ref"],
        "current_tree": old_summary.get("current_tree"),
        "evaluation": old_summary.get(
            "evaluation_policy",
            DEFAULT_EVALUATION_POLICY,
        ),
    }
    summary = summarize(results, config)
    summary["semantic_review"] = {
        "mode": "human",
        "status": "pending",
        "decision": "needs_human_decision",
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def run_variant(
    scenario: dict[str, Any],
    variant: str,
    repetition: int,
    output_root: Path,
    config: dict[str, Any],
    current_source: Path,
    previous_source: Path,
) -> dict[str, Any]:
    case_root = output_root / scenario["id"] / f"run-{repetition}" / variant
    workdir = case_root / "project"
    case_root.mkdir(parents=True)
    write_fixture(workdir, scenario)
    install_variant(variant, workdir, config, current_source, previous_source)
    started = time.monotonic()
    first = call_adapter(
        config,
        "start",
        workdir,
        "-",
        scenario["request"],
        case_root / "turn-1.jsonl",
    )
    routing_opened = [
        "ait-routing/SKILL.md" in (case_root / "turn-1.jsonl").read_text(
            encoding="utf-8",
            errors="replace",
        ),
        None,
    ]
    before_owner = git_state(workdir)
    second = call_adapter(
        config,
        "resume",
        workdir,
        first["session_id"],
        scenario["owner_reply"],
        case_root / "turn-2.jsonl",
    )
    routing_opened[1] = "ait-routing/SKILL.md" in (
        case_root / "turn-2.jsonl"
    ).read_text(encoding="utf-8", errors="replace")
    final_state = git_state(workdir)
    result = {
        "scenario": scenario["id"],
        "variant": variant,
        "repetition": repetition,
        "client": config["client"]["name"],
        "model": config["client"]["model"],
        "duration_seconds": round(time.monotonic() - started, 3),
        "turns": [first["answer"], second["answer"]],
        "before_owner": before_owner,
        "final_state": final_state,
        "metrics": score_run(
            scenario,
            first,
            second,
            before_owner,
            final_state,
            workdir,
            routing_opened,
        ),
    }
    (case_root / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def summarize_variant(selected: list[dict[str, Any]]) -> dict[str, Any]:
    missed = [
        len(result["metrics"]["missed_mandatory_actions"]) for result in selected
    ]
    ready = [int(result["metrics"]["acceptance_ready"]) for result in selected]
    seeded = [
        len(result["metrics"]["seeded_problem_observations"])
        for result in selected
    ]
    detected = [
        len(result["metrics"]["detected_seeded_problems"])
        for result in selected
    ]
    detected_early = [
        len(result["metrics"]["early_detected_seeded_problems"])
        for result in selected
    ]
    seeded_total = sum(seeded)
    return {
        "runs": len(selected),
        "critical_violations": sum(
            len(result["metrics"]["critical_violations"]) for result in selected
        ),
        "missed_mandatory_actions": sum(missed),
        "missed_mandatory_actions_by_run": missed,
        "median_missed_mandatory_actions": (
            statistics.median(missed) if missed else None
        ),
        "missed_mandatory_actions_range": (
            max(missed) - min(missed) if missed else None
        ),
        "unauthorized_decisions": sum(
            result["metrics"]["unauthorized_decisions"] for result in selected
        ),
        "acceptance_ready": sum(ready),
        "acceptance_ready_by_run": ready,
        "acceptance_ready_rate": sum(ready) / len(ready) if ready else None,
        "seeded_problems": seeded_total,
        "detected_seeded_problems": sum(detected),
        "detected_seeded_problems_by_run": detected,
        "detected_seeded_problems_rate": (
            sum(detected) / seeded_total if seeded_total else None
        ),
        "early_detected_seeded_problems": sum(detected_early),
        "early_detected_seeded_problems_by_run": detected_early,
        "early_detected_seeded_problems_rate": (
            sum(detected_early) / seeded_total if seeded_total else None
        ),
        "missed_seeded_problems": seeded_total - sum(detected),
        "late_detected_seeded_problems": sum(detected) - sum(detected_early),
        "seeded_problem_observations_by_run": [
            {
                "scenario": result["scenario"],
                "repetition": result["repetition"],
                "problems": result["metrics"]["seeded_problem_observations"],
            }
            for result in selected
        ],
        "duration_seconds": sum(result["duration_seconds"] for result in selected),
        "input_tokens": sum(
            result["metrics"]["usage"]["input_tokens"] for result in selected
        ),
        "output_tokens": sum(
            result["metrics"]["usage"]["output_tokens"] for result in selected
        ),
    }


def evaluate_scenario(
    scenario_id: str,
    variants: dict[str, dict[str, Any]],
    policy: dict[str, int],
) -> dict[str, Any]:
    current = variants["current"]
    bare = variants["bare"]
    previous = variants["previous"]
    expected_runs = current["runs"]
    complete_series = expected_runs > 0 and all(
        variant["runs"] == expected_runs for variant in variants.values()
    )
    checks = {
        "complete_series": complete_series,
        "no_observed_critical_violations": current["critical_violations"] == 0,
        "no_observed_unauthorized_decisions": current["unauthorized_decisions"] == 0,
        "fewer_median_misses_than_bare": (
            current["median_missed_mandatory_actions"]
            < bare["median_missed_mandatory_actions"]
            if complete_series
            else False
        ),
        "no_more_median_misses_than_previous": (
            current["median_missed_mandatory_actions"]
            <= previous["median_missed_mandatory_actions"]
            if complete_series
            else False
        ),
        "readiness_rate_not_below_bare": (
            current["acceptance_ready_rate"] >= bare["acceptance_ready_rate"]
            if complete_series
            else False
        ),
        "readiness_rate_not_below_previous": (
            current["acceptance_ready_rate"] >= previous["acceptance_ready_rate"]
            if complete_series
            else False
        ),
        "all_seeded_problems_detected_early": (
            current["early_detected_seeded_problems_rate"] == 1
            if complete_series
            else False
        ),
        "detection_rate_not_below_previous": (
            current["detected_seeded_problems_rate"]
            >= previous["detected_seeded_problems_rate"]
            if complete_series
            else False
        ),
        "early_detection_rate_not_below_previous": (
            current["early_detected_seeded_problems_rate"]
            >= previous["early_detected_seeded_problems_rate"]
            if complete_series
            else False
        ),
    }
    dispersion_exceeded = (
        current["missed_mandatory_actions_range"]
        > policy["max_missed_action_range"]
        if current["missed_mandatory_actions_range"] is not None
        else False
    )
    maximum_repetitions = (
        policy["initial_repetitions"] + policy["additional_repetitions"]
    )
    if expected_runs < policy["initial_repetitions"]:
        status = "calibration"
    elif dispersion_exceeded and expected_runs < maximum_repetitions:
        status = "needs_additional_repetitions"
    elif dispersion_exceeded or not all(checks.values()):
        status = "benchmark_failed"
    else:
        status = "needs_semantic_review"
    return {
        "scenario": scenario_id,
        "status": status,
        "runs_per_variant": expected_runs,
        "dispersion_exceeded": dispersion_exceeded,
        "checks": checks,
        "variants": variants,
    }


def summarize(results: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    policy = config.get("evaluation", DEFAULT_EVALUATION_POLICY)
    variants = {}
    for variant in VARIANTS:
        selected = [result for result in results if result["variant"] == variant]
        variants[variant] = summarize_variant(selected)
    scenario_ids = sorted({result["scenario"] for result in results})
    scenarios = {}
    for scenario_id in scenario_ids:
        scenario_variants = {}
        for variant in VARIANTS:
            selected = [
                result
                for result in results
                if result["scenario"] == scenario_id and result["variant"] == variant
            ]
            scenario_variants[variant] = summarize_variant(selected)
        scenarios[scenario_id] = evaluate_scenario(
            scenario_id,
            scenario_variants,
            policy,
        )
    statuses = {scenario["status"] for scenario in scenarios.values()}
    if not scenarios or "calibration" in statuses:
        mechanical_status = "calibration"
    elif "needs_additional_repetitions" in statuses:
        mechanical_status = "needs_additional_repetitions"
    elif "benchmark_failed" in statuses:
        mechanical_status = "benchmark_failed"
    else:
        mechanical_status = "needs_semantic_review"
    return {
        "status": mechanical_status,
        "mechanical_status": mechanical_status,
        "calibration": mechanical_status == "calibration",
        "reason": (
            "Механические показатели предварительны. Итог фиксированного "
            "проверочного набора требует завершённой смысловой проверки и "
            "не подтверждает бизнес-эффект."
        ),
        "previous_ref": config["previous_ref"],
        "current_tree": config.get("current_tree"),
        "evaluation_policy": policy,
        "variants": variants,
        "scenarios": scenarios,
    }


def semantic_review(
    summary: dict[str, Any],
    results: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    judge = config.get("judge") or {"mode": "human"}
    if not isinstance(judge, dict) or judge.get("mode", "human") == "human":
        return {
            "mode": "human",
            "status": "pending",
            "decision": "needs_human_decision",
        }
    if judge.get("mode") != "model":
        raise EvalError("judge.mode должен иметь значение human или model.")
    command = judge.get("command")
    model = judge.get("model")
    if not isinstance(command, str) or not command.strip():
        raise EvalError("Для judge.mode: model нужно задать judge.command.")
    if not isinstance(model, str) or not model.strip():
        raise EvalError("Для judge.mode: model нужно задать judge.model.")
    payload = {
        "summary": summary,
        "runs": [
            {
                "scenario": result["scenario"],
                "variant": result["variant"],
                "repetition": result["repetition"],
                "turns": result["turns"],
                "metrics": result["metrics"],
            }
            for result in results
        ],
    }
    prompt = (
        "Проверь смысловую готовность результатов к ручной приёмке. Не принимай "
        "продукт и не объявляй бизнес-эффект. Верни JSON с полями status "
        "(pass, revise или needs_human_decision), reasons (массив строк) и "
        "open_questions (массив строк). Данные прогона:\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    completed = subprocess.run(
        [*shlex.split(command), model],
        cwd=ROOT,
        input=prompt,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=config["timeout"],
        check=False,
    )
    if completed.returncode:
        raise EvalError(
            "Модель-судья завершилась с ошибкой:\n" + completed.stderr.strip(),
        )
    try:
        review = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise EvalError("Модель-судья вернула неразбираемый JSON.") from exc
    if not isinstance(review, dict):
        raise EvalError("Модель-судья должна вернуть JSON-объект.")
    status = review.get("status")
    if status not in {"pass", "revise", "needs_human_decision"}:
        raise EvalError(
            "Модель-судья должна вернуть status pass, revise "
            "или needs_human_decision.",
        )
    reasons = review.get("reasons")
    if (
        not isinstance(reasons, list)
        or not reasons
        or any(not isinstance(item, str) or not item.strip() for item in reasons)
    ):
        raise EvalError(
            "Модель-судья должна вернуть непустой массив непустых строк reasons.",
        )
    open_questions = review.get("open_questions")
    if not isinstance(open_questions, list) or any(
        not isinstance(item, str) or not item.strip()
        for item in open_questions
    ):
        raise EvalError(
            "Модель-судья должна вернуть массив непустых строк open_questions.",
        )
    return {
        "mode": "model",
        "status": "complete",
        "decision": status,
        "review": review,
    }


def finalize_summary(
    summary: dict[str, Any],
    review: dict[str, Any],
) -> dict[str, Any]:
    summary["semantic_review"] = review
    mechanical_status = summary["mechanical_status"]
    if mechanical_status != "needs_semantic_review":
        summary["status"] = mechanical_status
    elif review.get("decision") == "pass":
        summary["status"] = "fixed_scenario_benchmark_passed"
    elif review.get("decision") == "revise":
        summary["status"] = "benchmark_failed"
    else:
        summary["status"] = "needs_human_decision"
    return summary


def run_repetitions(
    scenarios: list[dict[str, Any]],
    start: int,
    stop: int,
    output_root: Path,
    config: dict[str, Any],
    current_source: Path,
    previous_source: Path,
) -> list[dict[str, Any]]:
    results = []
    for scenario in scenarios:
        for repetition in range(start, stop + 1):
            for variant in VARIANTS:
                print(
                    f"Сценарий {scenario['id']}, повтор {repetition}, "
                    f"вариант {variant}...",
                    flush=True,
                )
                results.append(
                    run_variant(
                        scenario,
                        variant,
                        repetition,
                        output_root,
                        config,
                        current_source,
                        previous_source,
                    ),
                )
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Запустить периодическую проверку полного пути ai-dev-team.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Проверить сценарии и образец настроек без модели, сети и APM install.",
    )
    parser.add_argument(
        "--rescore",
        type=Path,
        help="Пересчитать сохранённый запуск без повторного вызова модели.",
    )
    parser.add_argument("--case-id", help="Запустить только указанный сценарий.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        scenarios = validate_scenarios(load_document(SCENARIOS))
        sample = load_document(SAMPLE)
        if not isinstance(sample, dict) or "client" not in sample:
            raise EvalError(f"{SAMPLE.name} должен содержать раздел client.")
        validate_evaluation_policy(sample.get("evaluation"), SAMPLE.name)
        if args.validate:
            print(
                "Сценарии периодической проверки прошли проверку: "
                f"{len(scenarios)}.",
            )
            return 0
        if args.rescore:
            rescore_output(args.rescore, scenarios)
            print(f"Показатели пересчитаны в {args.rescore}.")
            return 0
        if args.case_id:
            scenarios = [case for case in scenarios if case["id"] == args.case_id]
            if not scenarios:
                raise EvalError(f"Сценарий {args.case_id!r} не найден.")
        config = load_config()
        if config is None:
            return 0
        timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        output_root = ROOT / ".ai-dev-team" / "local" / "product-evals" / timestamp
        output_root.mkdir(parents=True)
        with tempfile.TemporaryDirectory(prefix="ai-dev-team-snapshots-") as temp:
            snapshots = Path(temp)
            current_source = snapshots / "current"
            previous_source = snapshots / "previous"
            current_source.mkdir()
            previous_source.mkdir()
            current_tree = run(["git", "write-tree"], ROOT).stdout.strip()
            config["current_tree"] = current_tree
            export_ref(current_tree, current_source)
            export_ref(config["previous_ref"], previous_source)
            initial = config["evaluation"]["initial_repetitions"]
            results = run_repetitions(
                scenarios,
                1,
                initial,
                output_root,
                config,
                current_source,
                previous_source,
            )
            summary = summarize(results, config)
            if summary["mechanical_status"] == "needs_additional_repetitions":
                additional = config["evaluation"]["additional_repetitions"]
                results.extend(
                    run_repetitions(
                        scenarios,
                        initial + 1,
                        initial + additional,
                        output_root,
                        config,
                        current_source,
                        previous_source,
                    ),
                )
                summary = summarize(results, config)
        summary = finalize_summary(
            summary,
            semantic_review(summary, results, config),
        )
        (output_root / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"Артефакты сохранены в {output_root.relative_to(ROOT)}. "
            f"Статус: {summary['status']}.",
        )
        return 0
    except (EvalError, subprocess.TimeoutExpired) as exc:
        print(f"Ошибка периодической проверки: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
