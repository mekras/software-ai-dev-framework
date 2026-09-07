import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_adapter(filename, module_name):
    path = ROOT / "tools" / "product-eval-adapters" / f"{filename}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CODEX = load_adapter("codex", "product_eval_codex")
CLAUDE = load_adapter("claude", "product_eval_claude")


class ProductEvalAdapterTest(unittest.TestCase):
    def test_codex_collects_all_visible_agent_messages(self):
        events = [
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Маршрут"},
            },
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "text": "не ответ"},
            },
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Итог"},
            },
        ]
        self.assertEqual(
            "Маршрут\n\nИтог",
            CODEX.extract_answer(events, "последний ответ"),
        )

    def test_codex_uses_fallback_without_agent_messages(self):
        self.assertEqual("итог", CODEX.extract_answer([], "итог"))

    def test_claude_collects_text_blocks_from_all_assistant_messages(self):
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Режим менеджера"},
                        {"type": "tool_use", "name": "Bash"},
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Итог"}],
                },
            },
        ]
        self.assertEqual(
            "Режим менеджера\n\nИтог",
            CLAUDE.extract_answer(events, "последний ответ"),
        )

    def test_claude_uses_result_as_fallback(self):
        self.assertEqual("итог", CLAUDE.extract_answer([], "итог"))


if __name__ == "__main__":
    unittest.main()
