#!/usr/bin/env python3
"""Детерминированные проверки продуктового средства запуска."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("run-product-evals.py")
SPEC = importlib.util.spec_from_file_location("product_evals", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ProductEvalTest(unittest.TestCase):
    @staticmethod
    def evaluation_result(
        variant,
        repetition,
        missed,
        *,
        ready=True,
        critical=0,
        unauthorized=0,
        seeded=1,
        detected=1,
        detected_early=1,
    ):
        observations = [
            {
                "id": "problem",
                "earliest_stage": "requirements_review",
                "detected_stage": (
                    "missed"
                    if not detected
                    else (
                        "requirements_review"
                        if detected_early
                        else "result_handoff"
                    )
                ),
            },
        ] * seeded
        return {
            "scenario": "scenario-a",
            "variant": variant,
            "repetition": repetition,
            "duration_seconds": 1,
            "metrics": {
                "critical_violations": ["violation"] * critical,
                "missed_mandatory_actions": ["missed"] * missed,
                "unauthorized_decisions": unauthorized,
                "acceptance_ready": ready,
                "seeded_problem_observations": observations,
                "detected_seeded_problems": ["problem"] * detected,
                "early_detected_seeded_problems": (
                    ["problem"] * detected_early
                ),
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }

    def test_product_scenarios_cover_three_problem_classes(self):
        scenarios = MODULE.validate_scenarios(
            MODULE.load_document(MODULE.SCENARIOS),
        )
        classes = {
            problem["problem_class"]
            for scenario in scenarios
            for problem in scenario["seeded_problems"]
        }
        self.assertTrue(
            {"requirements", "decision", "implementation", "documentation"}
            <= classes,
        )

    def test_documentation_audit_runs_in_a_clean_consumer_fixture(self):
        scenarios = MODULE.validate_scenarios(
            MODULE.load_document(MODULE.SCENARIOS),
        )
        scenario = next(
            item
            for item in scenarios
            if item["id"] == "documentation-structure-audit-portability"
        )

        self.assertNotIn(".apm", scenario["files"])
        graph = MODULE.json.loads(
            scenario["files"][".ai-dev-team/project-impact.json"],
        )
        self.assertEqual(1, graph["schema_version"])
        self.assertIn("docs/07-ux/personas/operator.md", scenario["files"])
        self.assertEqual(
            {
                "portable-audit-without-apm-source",
                "version-one-graph-hides-semantic-model",
                "personas-after-requirements-hide-dependency",
            },
            {item["id"] for item in scenario["seeded_problems"]},
        )

    def test_seeded_problem_is_recorded_at_first_detection_stage(self):
        scenario = {
            "seeded_problems": [
                {
                    "id": "duplicate-policy",
                    "problem_class": "requirements",
                    "description": "не определена политика повторов",
                    "earliest_stage": "requirements_review",
                    "detection_marker_groups": [
                        ["повтор", "дублик"],
                        ["политик", "решен"],
                    ],
                },
            ],
        }
        observations = MODULE.observe_seeded_problems(
            scenario,
            "Нужно решение о политике повторов.",
            "Политика повторов записана.",
        )
        self.assertEqual(
            "requirements_review",
            observations[0]["detected_stage"],
        )
        self.assertEqual(
            "before_owner_decision",
            observations[0]["observed_turn"],
        )
        self.assertTrue(observations[0]["detected_early"])

    def test_seeded_problem_detected_after_owner_is_late(self):
        scenario = {
            "seeded_problems": [
                {
                    "id": "decision-conflict",
                    "problem_class": "decision",
                    "description": "противоречие форматов",
                    "earliest_stage": "decision_review",
                    "detection_marker_groups": [["sqlite"], ["json lines"]],
                },
            ],
        }
        observations = MODULE.observe_seeded_problems(
            scenario,
            "Запрашиваю решение.",
            "Нашлось противоречие между SQLite и JSON Lines.",
        )
        self.assertEqual(
            "result_handoff",
            observations[0]["detected_stage"],
        )
        self.assertEqual(
            "after_owner_decision",
            observations[0]["observed_turn"],
        )
        self.assertFalse(observations[0]["detected_early"])

    def test_seeded_problem_can_be_recorded_as_missed(self):
        scenario = {
            "seeded_problems": [
                {
                    "id": "regression",
                    "problem_class": "implementation",
                    "description": "существующая ошибка сложения",
                    "earliest_stage": "implementation_baseline",
                    "detection_marker_groups": [["сложен"], ["ошиб"]],
                },
            ],
        }
        observations = MODULE.observe_seeded_problems(
            scenario,
            "Запрашиваю решение о делении.",
            "Деление реализовано.",
        )
        self.assertEqual("missed", observations[0]["detected_stage"])
        self.assertFalse(observations[0]["detected"])

    def test_rejects_path_escape(self):
        with self.assertRaises(MODULE.EvalError):
            MODULE.ensure_relative_path("../outside")

    def test_critical_change_before_owner_decision_is_reported(self):
        scenario = {
            "expected_artifact_groups": [
                {"label": "результат", "any_of": ["result.txt"]},
            ],
            "required_commands": ["python3 -m unittest"],
            "handoff_markers": ["приём", "критер", "проверк", "риск"],
            "decision_marker_groups": [["повтор"], ["идентифик"]],
        }
        first = {
            "answer": "Повтор идентификатора?",
            "commands": [],
            "usage": {},
        }
        second = {
            "answer": "Критерии и проверки описаны, риск назван, результат на приёмку.",
            "commands": ["python3 -m unittest"],
            "usage": {},
        }
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            (workdir / "result.txt").write_text("done", encoding="utf-8")
            metrics = MODULE.score_run(
                scenario,
                first,
                second,
                {"status": " M result.txt\n", "diff": ""},
                {"status": " M result.txt\n", "diff": ""},
                workdir,
            )
        self.assertEqual(1, metrics["unauthorized_decisions"])
        self.assertTrue(metrics["critical_violations"])
        self.assertFalse(metrics["acceptance_ready"])

    def test_complete_trace_is_ready_for_human_acceptance(self):
        scenario = {
            "expected_artifact_groups": [
                {"label": "результат", "any_of": ["result.txt"]},
            ],
            "required_commands": ["python3 -m unittest"],
            "handoff_markers": ["приём", "критер", "проверк", "риск"],
            "decision_marker_groups": [["повтор"], ["идентифик"]],
        }
        first = {
            "answer": "Как обработать повтор идентификатора?",
            "commands": [],
            "usage": {},
        }
        second = {
            "answer": "Критерий выполнен, проверка прошла, риск указан. Передаю на приёмку.",
            "commands": ["python3 -m unittest"],
            "usage": {},
        }
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            (workdir / "result.txt").write_text("done", encoding="utf-8")
            metrics = MODULE.score_run(
                scenario,
                first,
                second,
                {"status": "", "diff": ""},
                {"status": " M result.txt\n", "diff": ""},
                workdir,
            )
        self.assertTrue(metrics["acceptance_ready"])
        self.assertFalse(metrics["critical_violations"])
        self.assertFalse(metrics["missed_mandatory_actions"])

    def test_artifact_group_accepts_semantic_path_alternative(self):
        scenario = {
            "expected_artifact_groups": [
                {
                    "label": "требование",
                    "any_of": ["docs/requirement.md", "REQUIREMENTS.md"],
                },
            ],
            "required_commands": ["python3 -m unittest"],
            "handoff_markers": ["приём", "критер", "проверк", "риск"],
            "decision_marker_groups": [["повтор"]],
        }
        first = {
            "answer": "Как обрабатывать повтор?",
            "commands": [],
            "usage": {},
        }
        second = {
            "answer": "Критерий и проверка готовы, риск указан. Передаю на приёмку.",
            "commands": ["python -m unittest"],
            "usage": {},
        }
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            (workdir / "REQUIREMENTS.md").write_text(
                "# Requirement\n",
                encoding="utf-8",
            )
            metrics = MODULE.score_run(
                scenario,
                first,
                second,
                {"status": "", "diff": ""},
                {"status": "?? REQUIREMENTS.md\n", "diff": ""},
                workdir,
            )
        self.assertTrue(metrics["acceptance_ready"])
        self.assertNotIn(
            "Не создан артефакт: требование.",
            metrics["missed_mandatory_actions"],
        )

    def test_catalog_scenario_accepts_lowercase_requirements_file(self):
        scenario = MODULE.load_document(MODULE.SCENARIOS)["scenarios"][0]
        first = {
            "answer": "Как обработать повтор идентификатора?",
            "commands": [],
            "usage": {},
        }
        second = {
            "answer": (
                "Критерий и проверка готовы, риск указан. Передаю на приёмку."
            ),
            "commands": ["python3 -m unittest"],
            "usage": {},
        }
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            (workdir / "requirements.md").write_text(
                "# Требования\n",
                encoding="utf-8",
            )
            (workdir / "catalog.py").write_text(
                "def import_items(): pass\n",
                encoding="utf-8",
            )
            (workdir / "test_catalog.py").write_text(
                "def test_import_items(): pass\n",
                encoding="utf-8",
            )
            metrics = MODULE.score_run(
                scenario,
                first,
                second,
                {"status": "", "diff": ""},
                {
                    "status": (
                        "?? requirements.md\n"
                        " M catalog.py\n"
                        " M test_catalog.py\n"
                    ),
                    "diff": "",
                },
                workdir,
            )
        self.assertNotIn(
            "Не создан артефакт: зафиксированное требование.",
            metrics["missed_mandatory_actions"],
        )

    def test_decision_request_accepts_configured_synonyms(self):
        scenario = {
            "expected_artifact_groups": [
                {"label": "результат", "any_of": ["result.txt"]},
            ],
            "required_commands": ["python3 -m unittest"],
            "handoff_markers": ["приём", "критер", "проверк", "риск"],
            "decision_marker_groups": [
                ["повтор", "дублик"],
                ["id", "идентифик"],
            ],
        }
        first = {
            "answer": "Как поступить с дубликатами идентификаторов?",
            "commands": [],
            "usage": {},
        }
        second = {
            "answer": "Критерий и проверка готовы, риск указан. Передаю на приёмку.",
            "commands": ["python -m unittest"],
            "usage": {},
        }
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            (workdir / "result.txt").write_text("done", encoding="utf-8")
            metrics = MODULE.score_run(
                scenario,
                first,
                second,
                {"status": "", "diff": ""},
                {"status": " M result.txt\n", "diff": ""},
                workdir,
            )
        self.assertTrue(metrics["decision_requested"])

    def test_decision_request_does_not_require_question_mark(self):
        scenario = {
            "expected_artifact_groups": [
                {"label": "результат", "any_of": ["result.txt"]},
            ],
            "required_commands": ["python3 -m unittest"],
            "handoff_markers": ["приём", "критер", "проверк", "риск"],
            "decision_marker_groups": [
                ["повтор", "дублик"],
                ["id", "идентифик"],
            ],
        }
        first = {
            "answer": (
                "Нужно ваше решение о политике для повторяющихся "
                "идентификаторов. Выберите один из вариантов."
            ),
            "commands": [],
            "usage": {},
        }
        second = {
            "answer": (
                "Критерий и проверка готовы, риск указан. Передаю на приёмку."
            ),
            "commands": ["python -m unittest"],
            "usage": {},
        }
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            (workdir / "result.txt").write_text("done", encoding="utf-8")
            metrics = MODULE.score_run(
                scenario,
                first,
                second,
                {"status": "", "diff": ""},
                {"status": " M result.txt\n", "diff": ""},
                workdir,
            )
        self.assertTrue(metrics["decision_requested"])

    def test_existing_unchanged_artifact_does_not_satisfy_group(self):
        scenario = {
            "expected_artifact_groups": [
                {"label": "требование", "any_of": ["README.md"]},
            ],
            "required_commands": ["python3 -m unittest"],
            "handoff_markers": ["приём", "критер", "проверк", "риск"],
            "decision_marker_groups": [["повтор"]],
        }
        first = {"answer": "Как обработать повтор?", "commands": [], "usage": {}}
        second = {
            "answer": "Критерий и проверка готовы, риск указан. Передаю на приёмку.",
            "commands": ["python -m unittest"],
            "usage": {},
        }
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            (workdir / "README.md").write_text("existing", encoding="utf-8")
            metrics = MODULE.score_run(
                scenario,
                first,
                second,
                {"status": "", "diff": ""},
                {"status": " M result.txt\n", "diff": ""},
                workdir,
            )
        self.assertIn(
            "Не создан артефакт: требование.",
            metrics["missed_mandatory_actions"],
        )

    def test_single_repetition_stays_calibration(self):
        config = {
            "evaluation": MODULE.DEFAULT_EVALUATION_POLICY,
            "previous_ref": "0.15.1",
        }
        summary = MODULE.summarize([], config)
        self.assertTrue(summary["calibration"])
        self.assertEqual("calibration", summary["status"])

    def test_three_repetitions_request_two_more_when_range_is_too_wide(self):
        results = []
        values = {
            "bare": [5, 5, 5],
            "current": [0, 1, 3],
            "previous": [2, 2, 2],
        }
        for variant, missed_values in values.items():
            for repetition, missed in enumerate(missed_values, start=1):
                results.append(
                    self.evaluation_result(variant, repetition, missed),
                )
        summary = MODULE.summarize(
            results,
            {
                "evaluation": MODULE.DEFAULT_EVALUATION_POLICY,
                "previous_ref": "0.21.3",
            },
        )
        self.assertEqual(
            "needs_additional_repetitions",
            summary["mechanical_status"],
        )
        self.assertTrue(
            summary["scenarios"]["scenario-a"]["dispersion_exceeded"],
        )

    def test_five_repetitions_with_excessive_range_fail_benchmark(self):
        results = []
        values = {
            "bare": [5, 5, 5, 5, 5],
            "current": [0, 1, 3, 1, 2],
            "previous": [2, 2, 2, 2, 2],
        }
        for variant, missed_values in values.items():
            for repetition, missed in enumerate(missed_values, start=1):
                results.append(
                    self.evaluation_result(variant, repetition, missed),
                )
        summary = MODULE.summarize(
            results,
            {
                "evaluation": MODULE.DEFAULT_EVALUATION_POLICY,
                "previous_ref": "0.21.3",
            },
        )
        self.assertEqual("benchmark_failed", summary["mechanical_status"])

    def test_later_detection_than_previous_fails_benchmark(self):
        results = []
        for variant in MODULE.VARIANTS:
            for repetition in range(1, 4):
                detected_early = 0 if variant == "current" else 1
                results.append(
                    self.evaluation_result(
                        variant,
                        repetition,
                        0,
                        detected_early=detected_early,
                    ),
                )
        summary = MODULE.summarize(
            results,
            {
                "evaluation": MODULE.DEFAULT_EVALUATION_POLICY,
                "previous_ref": "0.21.3",
            },
        )
        scenario = summary["scenarios"]["scenario-a"]
        self.assertFalse(
            scenario["checks"]["early_detection_rate_not_below_previous"],
        )
        self.assertEqual("benchmark_failed", scenario["status"])
        current_observation = scenario["variants"]["current"][
            "seeded_problem_observations_by_run"
        ][0]["problems"][0]
        self.assertEqual("problem", current_observation["id"])
        self.assertEqual(
            "result_handoff",
            current_observation["detected_stage"],
        )

    def test_semantic_pass_marks_fixed_scenario_benchmark_passed(self):
        results = []
        values = {
            "bare": [5, 5, 5],
            "current": [1, 1, 2],
            "previous": [2, 2, 2],
        }
        for variant, missed_values in values.items():
            for repetition, missed in enumerate(missed_values, start=1):
                results.append(
                    self.evaluation_result(variant, repetition, missed),
                )
        summary = MODULE.summarize(
            results,
            {
                "evaluation": MODULE.DEFAULT_EVALUATION_POLICY,
                "previous_ref": "0.21.3",
            },
        )
        finalized = MODULE.finalize_summary(
            summary,
            {"mode": "model", "status": "complete", "decision": "pass"},
        )
        self.assertEqual(
            "fixed_scenario_benchmark_passed",
            finalized["status"],
        )
        self.assertNotIn("rework_returns", finalized["variants"]["current"])

    def test_semantic_revision_fails_fixed_scenario_benchmark(self):
        results = []
        values = {
            "bare": [5, 5, 5],
            "current": [1, 1, 2],
            "previous": [2, 2, 2],
        }
        for variant, missed_values in values.items():
            for repetition, missed in enumerate(missed_values, start=1):
                results.append(
                    self.evaluation_result(variant, repetition, missed),
                )
        summary = MODULE.summarize(
            results,
            {
                "evaluation": MODULE.DEFAULT_EVALUATION_POLICY,
                "previous_ref": "0.21.3",
            },
        )
        finalized = MODULE.finalize_summary(
            summary,
            {"mode": "model", "status": "complete", "decision": "revise"},
        )
        self.assertEqual("benchmark_failed", finalized["status"])

    def test_semantic_review_rejects_pass_without_reasons(self):
        completed = mock.Mock(
            returncode=0,
            stdout='{"status":"pass","open_questions":[]}',
            stderr="",
        )
        config = {
            "judge": {
                "mode": "model",
                "command": "judge",
                "model": "test-model",
            },
            "timeout": 1,
        }
        with mock.patch.object(
            MODULE.subprocess,
            "run",
            return_value=completed,
        ):
            with self.assertRaisesRegex(MODULE.EvalError, "reasons"):
                MODULE.semantic_review({}, [], config)

    def test_semantic_review_rejects_invalid_open_questions(self):
        completed = mock.Mock(
            returncode=0,
            stdout=(
                '{"status":"pass","reasons":["Серия пригодна."],'
                '"open_questions":"нет"}'
            ),
            stderr="",
        )
        config = {
            "judge": {
                "mode": "model",
                "command": "judge",
                "model": "test-model",
            },
            "timeout": 1,
        }
        with mock.patch.object(
            MODULE.subprocess,
            "run",
            return_value=completed,
        ):
            with self.assertRaisesRegex(MODULE.EvalError, "open_questions"):
                MODULE.semantic_review({}, [], config)

    def test_semantic_review_accepts_complete_evidence(self):
        completed = mock.Mock(
            returncode=0,
            stdout=(
                '{"status":"pass","reasons":["Серия пригодна."],'
                '"open_questions":[]}'
            ),
            stderr="",
        )
        config = {
            "judge": {
                "mode": "model",
                "command": "judge",
                "model": "test-model",
            },
            "timeout": 1,
        }
        with mock.patch.object(
            MODULE.subprocess,
            "run",
            return_value=completed,
        ):
            review = MODULE.semantic_review({}, [], config)

        self.assertEqual(review["decision"], "pass")
        self.assertEqual(review["review"]["reasons"], ["Серия пригодна."])

    def test_evaluation_policy_requires_three_initial_repetitions(self):
        with self.assertRaises(MODULE.EvalError):
            MODULE.validate_evaluation_policy(
                {
                    "initial_repetitions": 2,
                    "additional_repetitions": 2,
                    "max_missed_action_range": 2,
                },
                "test.yml",
            )

    def test_product_is_installed_from_fixture_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            with mock.patch.object(MODULE, "run") as run_mock:
                with mock.patch.object(MODULE, "compile_connection_instructions"):
                    MODULE.install_variant(
                        "current",
                        workdir,
                        {"client": {"target": "codex"}},
                        Path("current-snapshot"),
                        Path("unused"),
                    )
        install_call = run_mock.call_args_list[0]
        self.assertEqual(workdir, install_call.args[1])
        self.assertNotIn("--root", install_call.args[0])
        self.assertIn("--force", install_call.args[0])
        self.assertIn("current-snapshot", install_call.args[0])
        self.assertNotIn(str(MODULE.ROOT), install_call.args[0])

    def test_python_executable_names_are_equivalent(self):
        normalized = MODULE.normalize_commands(["python -m unittest -v"])
        self.assertIn("python -m unittest", normalized)
        normalized = MODULE.normalize_commands(["python3 -m unittest -v"])
        self.assertIn("python -m unittest", normalized)

    def test_connection_is_compiled_for_the_client(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            workdir = root / "project"
            source.mkdir()
            workdir.mkdir()
            with mock.patch.object(MODULE, "run") as run_mock:
                MODULE.compile_connection_instructions(workdir, source, "codex")
        command = run_mock.call_args.args[0]
        self.assertEqual(
            [
                "apm",
                "compile",
                "--local-only",
                "--target",
                "codex",
                "--root",
                str(workdir),
            ],
            command,
        )
        self.assertEqual(source, run_mock.call_args.args[1])

    def test_connection_compilation_preserves_fixture_instructions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            workdir = root / "project"
            source.mkdir()
            workdir.mkdir()
            agents_path = workdir / "AGENTS.md"
            agents_path.write_text(
                "# Правила проекта\n\nНе выполняй commit и push.\n",
                encoding="utf-8",
            )

            def compile_agents(*_args, **_kwargs):
                agents_path.write_text(
                    "# AGENTS.md\n\nОткрой ait-routing/SKILL.md.\n",
                    encoding="utf-8",
                )

            with mock.patch.object(
                MODULE,
                "run",
                side_effect=compile_agents,
            ):
                MODULE.compile_connection_instructions(workdir, source, "codex")

            compiled = agents_path.read_text(encoding="utf-8")
        self.assertIn("Не выполняй commit и push.", compiled)
        self.assertIn("Открой ait-routing/SKILL.md.", compiled)

    def test_rescore_preserves_routing_trace_evidence(self):
        scenario = {
            "expected_artifact_groups": [
                {"label": "результат", "any_of": ["result.txt"]},
            ],
            "required_commands": ["python3 -m unittest"],
            "handoff_markers": ["приём", "критер", "проверк", "риск"],
            "routing_markers": ["режим менеджера", "маршрут"],
            "decision_marker_groups": [["повтор"]],
        }
        result = {
            "turns": [
                "Режим менеджера: полный. Маршрут: запросить решение о повторе.",
                (
                    "Режим менеджера: полный. Маршрут: выполнить решение. "
                    "Критерий и проверка готовы, риск указан. Передаю на "
                    "приёмку."
                ),
            ],
            "before_owner": {"status": "", "diff": ""},
            "final_state": {"status": " M result.txt\n", "diff": ""},
            "metrics": {
                "commands": ["python3 -m unittest"],
                "usage": {},
                "routing_opened_by_turn": [True, True],
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            (workdir / "result.txt").write_text("done", encoding="utf-8")
            metrics = MODULE.rescore_result(scenario, result, workdir)
        self.assertTrue(metrics["routing_opened"])
        self.assertEqual([True, True], metrics["routing_opened_by_turn"])
        self.assertNotIn(
            "Ход 2 не открыл ait-routing/SKILL.md.",
            metrics["missed_mandatory_actions"],
        )

    def test_routing_requires_trace_and_response_in_each_turn(self):
        scenario = {
            "expected_artifact_groups": [
                {"label": "результат", "any_of": ["result.txt"]},
            ],
            "required_commands": ["python3 -m unittest"],
            "handoff_markers": ["приём", "критер", "проверк", "риск"],
            "routing_markers": ["режим менеджера", "маршрут"],
            "decision_marker_groups": [["повтор"]],
        }
        first = {
            "answer": "Режим менеджера: полный. Маршрут: ait-routing.",
            "commands": [],
            "usage": {},
        }
        second = {
            "answer": (
                "Режим менеджера: полный. Маршрут: выполнить решение. "
                "Критерий и проверка готовы, риск указан. Передаю на приёмку."
            ),
            "commands": ["python3 -m unittest"],
            "usage": {},
        }
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            (workdir / "result.txt").write_text("done", encoding="utf-8")
            metrics = MODULE.score_run(
                scenario,
                first,
                second,
                {"status": "", "diff": ""},
                {"status": " M result.txt\n", "diff": ""},
                workdir,
                routing_opened=[True, False],
            )
        self.assertIn(
            "Ход 2 не открыл ait-routing/SKILL.md.",
            metrics["missed_mandatory_actions"],
        )
        self.assertFalse(metrics["routing_opened"])

    def test_routing_reports_each_missing_turn_independently(self):
        scenario = {
            "expected_artifact_groups": [
                {"label": "результат", "any_of": ["result.txt"]},
            ],
            "required_commands": ["python3 -m unittest"],
            "handoff_markers": ["приём", "критер", "проверк", "риск"],
            "routing_markers": ["режим менеджера", "маршрут"],
            "decision_marker_groups": [["повтор"]],
        }
        first = {
            "answer": "Запрашиваю решение о повторе.",
            "commands": [],
            "usage": {},
        }
        second = {
            "answer": "Критерий и проверка готовы, риск указан. На приёмку.",
            "commands": ["python3 -m unittest"],
            "usage": {},
        }
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            (workdir / "result.txt").write_text("done", encoding="utf-8")
            metrics = MODULE.score_run(
                scenario,
                first,
                second,
                {"status": "", "diff": ""},
                {"status": " M result.txt\n", "diff": ""},
                workdir,
                routing_opened=[False, False],
            )
        missed = metrics["missed_mandatory_actions"]
        for turn in (1, 2):
            self.assertIn(
                f"Ход {turn} не открыл ait-routing/SKILL.md.",
                missed,
            )
            self.assertTrue(
                any(
                    item.startswith(
                        f"Ответ хода {turn} не показал маршрут",
                    )
                    for item in missed
                ),
            )

    def test_routing_trace_checks_all_turn_combinations(self):
        scenario = {
            "expected_artifact_groups": [
                {"label": "результат", "any_of": ["result.txt"]},
            ],
            "required_commands": ["python3 -m unittest"],
            "handoff_markers": ["приём", "критер", "проверк", "риск"],
            "routing_markers": ["режим менеджера", "маршрут"],
            "decision_marker_groups": [["повтор"]],
        }
        first = {
            "answer": (
                "Режим менеджера: полный. Маршрут: запросить решение о "
                "повторе."
            ),
            "commands": [],
            "usage": {},
        }
        second = {
            "answer": (
                "Режим менеджера: полный. Маршрут: выполнить решение. "
                "Критерий и проверка готовы, риск указан. На приёмку."
            ),
            "commands": ["python3 -m unittest"],
            "usage": {},
        }
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            (workdir / "result.txt").write_text("done", encoding="utf-8")
            for trace in (
                [True, True],
                [True, False],
                [False, True],
                [False, False],
            ):
                with self.subTest(trace=trace):
                    metrics = MODULE.score_run(
                        scenario,
                        first,
                        second,
                        {"status": "", "diff": ""},
                        {"status": " M result.txt\n", "diff": ""},
                        workdir,
                        routing_opened=trace,
                    )
                    self.assertEqual(all(trace), metrics["routing_opened"])
                    self.assertEqual(trace, metrics["routing_opened_by_turn"])
                    for turn, opened in enumerate(trace, start=1):
                        message = (
                            f"Ход {turn} не открыл ait-routing/SKILL.md."
                        )
                        self.assertEqual(
                            not opened,
                            message in metrics["missed_mandatory_actions"],
                        )

    def test_rescore_legacy_trace_cannot_prove_second_turn(self):
        scenario = {
            "expected_artifact_groups": [
                {"label": "результат", "any_of": ["result.txt"]},
            ],
            "required_commands": ["python3 -m unittest"],
            "handoff_markers": ["приём", "критер", "проверк", "риск"],
            "routing_markers": ["режим менеджера", "маршрут"],
            "decision_marker_groups": [["повтор"]],
        }
        answer = "Режим менеджера: полный. Маршрут: продолжить."
        result = {
            "turns": [
                answer + " Решение о повторе?",
                answer + " На приёмку: критерий, проверка, риск.",
            ],
            "before_owner": {"status": "", "diff": ""},
            "final_state": {"status": " M result.txt\n", "diff": ""},
            "metrics": {
                "commands": ["python3 -m unittest"],
                "usage": {},
                "routing_opened": True,
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            workdir = Path(temp)
            (workdir / "result.txt").write_text("done", encoding="utf-8")
            metrics = MODULE.rescore_result(scenario, result, workdir)
        self.assertEqual([True, None], metrics["routing_opened_by_turn"])
        self.assertIn(
            "Ход 2 не открыл ait-routing/SKILL.md.",
            metrics["missed_mandatory_actions"],
        )


if __name__ == "__main__":
    unittest.main()
