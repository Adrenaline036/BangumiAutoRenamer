import ast
import unittest
from pathlib import Path


class TestStartStorageSecretPolicy(unittest.TestCase):
    def test_storage_secret_comes_from_environment_without_default(self) -> None:
        source_path = Path(__file__).parents[1] / "src" / "start.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        ui_run_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "ui"
            and node.func.attr == "run"
        ]
        self.assertEqual(len(ui_run_calls), 1)

        storage_keywords = [
            keyword
            for keyword in ui_run_calls[0].keywords
            if keyword.arg == "storage_secret"
        ]
        self.assertEqual(len(storage_keywords), 1)
        value = storage_keywords[0].value
        self.assertIsInstance(value, ast.Call, "storage_secret must not be a literal")
        assert isinstance(value, ast.Call)
        self.assertIsInstance(value.func, ast.Attribute)
        assert isinstance(value.func, ast.Attribute)
        self.assertIsInstance(value.func.value, ast.Name)
        assert isinstance(value.func.value, ast.Name)
        self.assertEqual((value.func.value.id, value.func.attr), ("os", "getenv"))
        self.assertEqual(len(value.args), 1, "storage secret must not have a repository default")
        self.assertEqual(value.keywords, [])
        self.assertIsInstance(value.args[0], ast.Constant)
        assert isinstance(value.args[0], ast.Constant)
        self.assertEqual(value.args[0].value, "BAR_UI_STORAGE_SECRET")


if __name__ == "__main__":
    unittest.main()
