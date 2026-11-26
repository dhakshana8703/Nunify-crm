import ast
import subprocess
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PURE_MODULES = {"crm_core", "organization_identity", "source_identity", "source_schedule"}
PURE_DEPENDENCIES = {"idna"}


class CoreBoundaryTest(unittest.TestCase):
    def test_hb05_every_core_module_loads_without_adapters(self):
        code = """
import importlib
import pkgutil
import sys

for name in ('flask', 'sqlite3', 'psycopg2', 'werkzeug', 'app', 'workflow_store', 'workflow_web',
             'ingest_store', 'source_ops', 'cvent_import', 'directory_import'):
    sys.modules[name] = None

dependency = importlib.util.spec_from_file_location('idna', sys.argv[1])
module = importlib.util.module_from_spec(dependency)
sys.modules['idna'] = module
dependency.loader.exec_module(module)

import crm_core

for module in pkgutil.walk_packages(crm_core.__path__, 'crm_core.'):
    importlib.import_module(module.name)
"""
        result = subprocess.run([sys.executable, "-B", "-S", "-c", code, find_spec("idna").origin],
                                cwd=ROOT, capture_output=True,
                                text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hb05_core_and_shared_rules_only_import_pure_dependencies(self):
        paths = list((ROOT / "crm_core").rglob("*.py"))
        paths.extend(ROOT / f"{name}.py" for name in PURE_MODULES - {"crm_core"})
        violations = []
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    modules = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    modules = [node.module]
                else:
                    continue
                for module in modules:
                    root = module.split(".")[0]
                    if root == "sqlite3" or root not in sys.stdlib_module_names | PURE_MODULES | PURE_DEPENDENCIES:
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno} imports {module}")
        self.assertEqual(violations, [])

    def test_hb05_flask_endpoints_do_not_execute_sql(self):
        violations = []
        for path in sorted(ROOT.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.FunctionDef) or not any(
                    isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
                    and item.func.attr in {"get", "post", "route"} for item in node.decorator_list
                ):
                    continue
                for operation in ast.walk(node):
                    if (isinstance(operation, ast.Call) and isinstance(operation.func, ast.Attribute)
                            and operation.func.attr in {"execute", "executemany", "executescript"}):
                        violations.append(f"{path.name}:{operation.lineno} ({node.name})")
        self.assertEqual(violations, [], "Direct SQL in Flask endpoints: " + ", ".join(violations))
