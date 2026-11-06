import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import Button, DataTable, Input, Select

from pbip_loader import cli
from pbip_loader.tui import Loader, Prompt
from tests.test_workflows import FakeApi


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_catalog_search_download_and_review_before_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            api = FakeApi()
            app = Loader()
            with (
                patch.object(cli, "settings_path", return_value=base / "settings.json"),
                patch.object(cli, "find_azure_cli", return_value="az"),
                patch.object(cli, "Api", return_value=api),
            ):
                async with app.run_test(size=(90, 28)) as pilot:
                    async def settled():
                        async with asyncio.timeout(5):
                            await pilot.pause()
                            while app.busy:
                                await asyncio.sleep(0.02)
                            await pilot.pause()

                    await settled()
                    self.assertTrue(app.query_one("#refresh", Button).disabled)
                    self.assertTrue(app.query_one("#settings", Button).has_class("needs-settings"))
                    self.assertEqual(app.query_one("#refresh").region.column_span,
                                     app.query_one("#filter").region.column_span)
                    self.assertEqual(app.query_one("#download").region.width,
                                     app.query_one("#publish").region.width)
                    await pilot.click("#settings")
                    app.screen.query_one(Input).value = str(base / "projects")
                    await pilot.press("enter")
                    await settled()
                    self.assertEqual(base / "projects", Path(app.root))
                    self.assertFalse(app.query_one("#settings", Button).has_class("needs-settings"))

                    await pilot.click("#refresh")
                    await settled()
                    search = app.query_one("#search", Input)
                    search.value = "unmatched"
                    await pilot.pause()
                    self.assertEqual(0, app.query_one(DataTable).row_count)
                    search.value = "модель"
                    await pilot.pause()
                    self.assertEqual(1, app.query_one(DataTable).row_count)
                    self.assertTrue(app.query_one("#download", Button).disabled)
                    self.assertTrue(app.query_one("#publish", Button).disabled)

                    app.query_one(DataTable).focus()
                    await pilot.press("enter")
                    self.assertFalse(app.query_one("#download", Button).disabled)
                    self.assertTrue(app.query_one("#publish", Button).disabled)

                    await pilot.click("#download")
                    await settled()
                    app.query_one("#filter", Select).value = "yes"
                    await pilot.pause()
                    self.assertEqual(1, app.query_one(DataTable).row_count)
                    self.assertTrue(Path(app.selected()["local_path"]).is_dir())
                    self.assertFalse(app.query_one("#publish", Button).disabled)
                    await pilot.click("#download")
                    self.assertIsInstance(app.screen, Prompt)
                    await pilot.press("escape")

                    await pilot.click("#publish")
                    await settled()
                    self.assertIn("SemanticModel", app.screen.body)
                    self.assertIn("Report", app.screen.body)
                    await pilot.press("escape")
                    self.assertFalse(api.updates)
                    await pilot.click("#publish")
                    await settled()
                    expected = len(app.selected()["components"])
                    await pilot.click("#accept")
                    await settled()
                    self.assertEqual(expected, len(api.updates))
