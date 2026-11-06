import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pbip_loader import cli
from tests.test_workflows import FakeApi


class CliTests(unittest.TestCase):
    def test_shared_catalog_workflow_and_explicit_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "settings.json"
            storage = Path(folder) / "projects"
            api = FakeApi()

            def invoke(*args, exit_code=0):
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(io.StringIO()):
                    self.assertEqual(exit_code, cli.main(list(args)))
                return json.loads(output.getvalue())

            with patch.object(cli, "settings_path", return_value=settings), \
                    patch.object(cli, "find_azure_cli", return_value="az.cmd"), \
                    patch.object(cli, "Api", return_value=api) as make_api:
                self.assertEqual(str(storage), invoke("config", "--root", str(storage))["root"])
                self.assertEqual({"root": str(storage)}, json.loads(settings.read_text(encoding="utf-8")))
                invoke("refresh")
                model, = invoke("search", "модель")["models"]
                self.assertIsNone(model["local_path"])
                key = model["key"]
                downloaded = invoke("download", key)
                self.assertTrue(Path(downloaded["path"]).is_dir())
                self.assertFalse(invoke("download", key, exit_code=1)["ok"])
                self.assertEqual(downloaded["path"], invoke("search", "--local", "yes")["models"][0]["local_path"])
                api_calls = make_api.call_count
                plan = invoke("publish", key)
                self.assertFalse(plan["applied"])
                self.assertEqual(key, plan["target"]["key"])
                self.assertEqual(api_calls, make_api.call_count)
                self.assertFalse(api.updates)
                self.assertTrue(invoke("publish", key, "--yes")["applied"])
                self.assertEqual(len(model["components"]), len(api.updates))

    def test_config_ignores_legacy_settings_and_saves_only_the_storage_path(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "settings.json"
            storage = str(Path(folder) / "projects")
            settings.write_text(json.dumps({"root": storage, "old_option": "unused"}), encoding="utf-8")
            with patch.object(cli, "settings_path", return_value=settings):
                read = cli.run(cli.parser().parse_args(["config"]))
                self.assertEqual({"settings_file": str(settings), "root": storage}, read)
                cli.run(cli.parser().parse_args(["config", "--root", storage]))
            self.assertEqual({"root": storage}, json.loads(settings.read_text(encoding="utf-8")))
