"""ZIP manifest is explicit: no credentials, virtualenvs, logs, samples or Git state."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("_package_tests", ROOT / "tools/build_package.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def test_source_only_package_and_schema_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "plugin.zip"
            count = package.build(ROOT, destination)
            with ZipFile(destination) as archive:
                self.assertIsNone(archive.testzip())
                names = archive.namelist()
                self.assertEqual(len(names), count)
                prefix = package.NAME + "/"
                self.assertTrue(all(name.startswith(prefix) for name in names))
                for required in (
                    "runtime.py",
                    "main.py",
                    "LICENSE",
                    "README.md",
                    "requirements-engine.txt",
                ):
                    self.assertIn(prefix + required, names)
                for forbidden in (
                    ".git/",
                    ".engine/",
                    "__pycache__/",
                    "artifacts/",
                    ".publish",
                    ".env",
                    "token.txt",
                    "engine_home/",
                ):
                    self.assertFalse(any(forbidden in name for name in names))
                schema = json.loads(archive.read(prefix + "_conf_schema.json"))
                self.assertEqual(schema["engine_python"]["default"], "")
                self.assertIs(schema["auto_install_engine"]["default"], True)
                self.assertEqual(schema["engine_setup_timeout"]["default"], 1800)
                self.assertIn(b"version: v0.2.0", archive.read(prefix + "metadata.yaml"))
            with self.assertRaises(FileExistsError):
                package.build(ROOT, destination)


if __name__ == "__main__":
    unittest.main()
