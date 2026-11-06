import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pbip_loader.api import Indeterminate, Rejected
from pbip_loader.catalog import collect, targets
from pbip_loader.engine import Engine
from pbip_loader.project import decode, encode, folder_name, part_path, safe_name, verify_remote
from pbip_loader.storage import Store, dump

W = "00000000-0000-0000-0000-000000000001"
M = "10000000-0000-0000-0000-000000000001"
R1 = "20000000-0000-0000-0000-000000000001"
R2 = "20000000-0000-0000-0000-000000000002"


def definition(files):
    return encode({k: v.encode() if isinstance(v, str) else v for k, v in files.items()})


class Credential:
    identity = "org:user"

    def token(self, scope):
        return "test-token"


class FakeApi:
    def __init__(self):
        self.credential = Credential()
        self.groups = [{"id": W, "name": "Область"}]
        self.models = {W: [{"id": M, "name": "Модель"}]}
        self.reports = {W: [{"id": R1, "name": "Отчёт A", "datasetId": M},
                            {"id": R2, "name": "Отчёт B", "datasetId": M}]}
        self.remote = {
            M: definition({"definition.pbism": '{"version":"4.0"}', "definition/model.tmdl": "model Model\n"}),
            R1: self.report(), R2: self.report(),
        }
        self.updates = []
        self.fail_id = None
        self.fail_mode = "rejected"
        self.fail_once = True
        self.readback_corrupt = False
        self.download_fail_id = None

    @staticmethod
    def report():
        return definition({"definition.pbir": json.dumps({"version": "4.0", "datasetReference": {
            "byConnection": {"connectionString": "semanticmodelid=" + M}}}), "definition/report.json": "{}"})

    def rows(self, service, path):
        if path.startswith("groups?"):
            return copy.deepcopy(self.groups) if "$skip=0" in path else []
        _, wid, collection = path.split("/")
        return copy.deepcopy(self.models[wid] if collection == "datasets" else self.reports[wid])

    def get(self, service, path):
        _, wid, _, item_id = path.split("/")
        for item in self.models[wid] + self.reports[wid]:
            if item["id"] == item_id:
                return {"id": item_id, "workspaceId": wid, "type": "SemanticModel" if item_id in {m["id"] for m in self.models[wid]} else "Report", "displayName": item["name"]}
        raise Rejected("not found")

    def definition(self, component):
        if self.download_fail_id == component["id"]:
            raise Rejected("cannot download report")
        result = copy.deepcopy(self.remote[component["id"]])
        if self.readback_corrupt and self.updates:
            self.readback_corrupt = False
            result["definition"]["parts"].pop()
        return result

    def update(self, component, desired):
        item_id = component["id"]
        self.updates.append(item_id)
        if self.fail_id == item_id and self.fail_once:
            self.fail_once = False
            if self.fail_mode == "indeterminate":
                self.remote[item_id] = copy.deepcopy(desired)
                raise Indeterminate("timeout after request")
            if self.fail_mode == "rollback":
                self.fail_once = True
            raise Rejected("terminal error")
        self.remote[item_id] = copy.deepcopy(desired)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "storage")
        self.api = FakeApi()
        self.engine = Engine(self.store, self.api)
        self.engine.refresh()
        self.target = targets(self.store.inventory())[0]

    def pull(self):
        return Path(self.engine.download(self.target)["path"])

    def test_refresh_reports_cumulative_counts_across_workspace_pages(self):
        other = "00000000-0000-0000-0000-000000000002"
        self.api.groups.append({"id": other, "name": "Вторая область"})
        self.api.models[other], self.api.reports[other] = [], []
        original_rows = self.api.rows

        def paged_rows(service, path):
            if path.startswith("groups?"):
                skip = int(path.split("$skip=")[1])
                return self.api.groups[skip:skip + 1]
            return original_rows(service, path)

        messages, fractions = [], []
        with patch.object(self.api, "rows", side_effect=paged_rows):
            result = Engine(self.store, self.api, messages.append,
                            on_progress=lambda *pair: fractions.append(pair)).refresh()
        self.assertEqual({"workspaces": 2, "models": 1, "reports": 2}, result)
        self.assertIn("Найдено областей: 2 · Обработано: 0 · Моделей: 0 · Отчётов: 0", messages)
        self.assertIn("Найдено областей: 2 · Обработано: 0 · Моделей: 1 · Отчётов: 0", messages)
        self.assertIn("Найдено областей: 2 · Обработано: 1 · Моделей: 1 · Отчётов: 2", messages)
        self.assertEqual("Найдено областей: 2 · Обработано: 2 · Моделей: 1 · Отчётов: 2", messages[-1])
        self.assertEqual([(0, 2), (0, 2), (1, 2), (1, 2), (2, 2)], fractions)

    def test_download_uses_readable_paths_and_resolves_case_insensitive_collisions(self):
        root = self.pull()
        self.assertEqual(self.store.root / "Область" / "Модель", root)
        self.assertTrue((root / "Модель.SemanticModel").is_dir())
        self.assertTrue((root / "Отчёт A.Report").is_dir())
        self.assertTrue((root / "Отчёт A.pbip").is_file())
        same_workspace = {**self.target, "key": "other-model", "model_name": "модель"}
        self.assertEqual("Область/модель (2)", folder_name(self.store, same_workspace))
        other_workspace = {**self.target, "key": "other-area", "workspace_id": "other-area"}
        self.assertEqual("Область (2)/Модель", folder_name(self.store, other_workspace))

    def test_publish_and_download_keep_the_same_path_after_service_renames(self):
        root = self.pull()
        self.api.groups[0]["name"] = "Новая область"
        self.api.models[W][0]["name"] = "Новая модель"
        self.api.reports[W][0]["name"] = "Новый отчёт"
        self.engine.refresh()
        current = targets(self.store.inventory())[0]
        self.assertEqual(root, self.engine.local_record(current)[0])
        self.engine.publish(current)
        self.assertEqual([M, R1, R2], self.api.updates)
        self.assertEqual(root, Path(self.engine.download(current, overwrite=True)["path"]))
        self.assertTrue((root / "Отчёт A.pbip").is_file())

    def test_flat_registered_project_migrates_only_after_confirmed_success(self):
        root = self.pull()
        record = self.store.project(self.target["key"])
        record["folder"] = "Area.Model.abcdef123456"
        legacy = self.store.root / record["folder"]
        root.rename(legacy)
        root.parent.rmdir()
        with self.store.connect() as db:
            db.execute("UPDATE projects SET data=? WHERE key=?", (json.dumps(record), record["key"]))
        self.assertEqual(legacy, self.engine.local_record(self.target)[0])
        with self.assertRaises(FileExistsError):
            self.engine.download(self.target)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_migration BEFORE INSERT ON projects BEGIN SELECT RAISE(ABORT, 'migration failed'); END")
        with self.assertRaisesRegex(Exception, "migration failed"):
            self.engine.download(self.target, overwrite=True)
        self.assertTrue(legacy.is_dir())
        self.assertFalse(root.exists())
        self.assertEqual(record, self.store.project(self.target["key"]))
        with self.store.connect() as db:
            db.execute("DROP TRIGGER reject_migration")
        new_path = Path(self.engine.download(self.target, overwrite=True)["path"])
        self.assertEqual(root, new_path)
        self.assertFalse(legacy.exists())
        self.assertEqual(root, self.engine.local_record(self.target)[0])

    def test_download_progress_reaches_full_only_after_install(self):
        progress = []
        self.engine.on_progress = lambda done, total: progress.append((done, total, self.store.project(self.target["key"]) is not None))
        self.pull()
        total = 2 * len(self.target["components"]) + 2
        self.assertEqual([(step, total, step == total) for step in range(total + 1)], progress)

    def journal(self):
        return json.loads(next(self.store.sessions.glob("publish-*/journal.json")).read_text(encoding="utf-8"))

    def edit_model(self, root):
        model = self.store.project(self.target["key"])["components"][0]
        (root / model["folder"] / "definition/model.tmdl").write_text("model Changed\n", encoding="utf-8")

    def test_all_reports_get_local_links_and_individual_pbip_entries(self):
        root = self.pull()
        record = self.store.project(self.target["key"])
        self.assertEqual(3, len(record["components"]))
        self.assertEqual(2, len(list(root.glob("*.pbip"))))
        self.assertFalse((root / "pull.json").exists())
        for report in record["components"][1:]:
            pbir = json.loads((root / report["folder"] / "definition.pbir").read_text(encoding="utf-8"))
            self.assertEqual({"byPath": {"path": "../" + record["components"][0]["folder"]}}, pbir["datasetReference"])

    def test_second_pull_requires_confirmation(self):
        self.pull()
        with self.assertRaises(FileExistsError):
            self.engine.download(self.target)

    def test_replacement_removes_stale_files_and_preserves_pbip_settings(self):
        root = self.pull()
        entry = next(root.glob("*.pbip"))
        payload = json.loads(entry.read_text(encoding="utf-8"))
        payload["settings"]["enableAutoRecovery"] = False
        dump(entry, payload)
        (root / "stale.txt").write_text("stale")
        self.engine.download(self.target, overwrite=True)
        self.assertFalse((root / "stale.txt").exists())
        self.assertEqual(payload, json.loads(entry.read_text(encoding="utf-8")))
        self.assertEqual([], list(self.store.sessions.iterdir()))

    def test_last_report_failure_leaves_old_project_and_registry(self):
        root = self.pull()
        (root / "local.txt").write_text("keep")
        old_record = self.store.project(self.target["key"])
        self.api.download_fail_id = R2
        with self.assertRaises(Rejected):
            self.engine.download(self.target, overwrite=True)
        self.assertEqual("keep", (root / "local.txt").read_text())
        self.assertEqual(old_record, self.store.project(self.target["key"]))

    def test_inventory_failure_preserves_previous_catalog(self):
        original = self.store.inventory()
        with patch.object(self.api, "rows", side_effect=Rejected("no access")):
            with self.assertRaises(Rejected):
                self.engine.refresh()
        self.assertEqual(original, self.store.inventory())

    def test_missing_local_report_blocks_before_remote_writes(self):
        root = self.pull()
        report = self.store.project(self.target["key"])["components"][2]
        (root / report["folder"] / "definition/report.json").unlink()
        with self.assertRaises(ValueError):
            self.engine.publish(self.target)
        self.assertEqual([], self.api.updates)

    def test_new_report_in_service_blocks_stale_selection(self):
        self.pull()
        self.api.reports[W].append({"id": "20000000-0000-0000-0000-000000000003", "name": "New", "datasetId": M})
        with self.assertRaisesRegex(ValueError, "Состав"):
            self.engine.publish(self.target)
        self.assertEqual([], self.api.updates)

    def test_success_requires_all_three_remote_definitions(self):
        root = self.pull()
        self.edit_model(root)
        result = self.engine.publish(self.target)
        self.assertEqual("verified", result["status"])
        self.assertEqual([M, R1, R2], [item["id"] for item in result["published"]])
        self.assertEqual((3, 6), (result["components"], result["parts"]))
        self.assertEqual([M, R1, R2], self.api.updates)
        self.assertEqual([], list(self.store.sessions.iterdir()))

    def test_terminal_last_report_failure_restores_all_attempted_definitions(self):
        root = self.pull()
        old = copy.deepcopy(self.api.remote)
        self.edit_model(root)
        self.api.fail_id = R2
        with self.assertRaisesRegex(RuntimeError, "rolled_back"):
            self.engine.publish(self.target)
        self.assertEqual(old, self.api.remote)
        self.assertEqual([M, R1, R2, M, R1, R2], self.api.updates)
        self.assertEqual("rolled_back", self.journal()["state"])

    def test_ambiguous_timeout_never_races_with_running_update_or_reports_success(self):
        self.pull()
        self.api.fail_id, self.api.fail_mode = R1, "indeterminate"
        with self.assertRaisesRegex(RuntimeError, "indeterminate"):
            self.engine.publish(self.target)
        self.assertEqual([M, R1], self.api.updates)
        self.assertEqual("indeterminate", self.journal()["state"])
        with self.assertRaisesRegex(RuntimeError, "незавершённая"):
            self.engine.publish(self.target)
        with self.assertRaisesRegex(RuntimeError, "незавершённая"):
            self.engine.download(self.target, overwrite=True)

    def test_failed_rollback_retains_backups_and_blocks_future_mutation(self):
        self.pull()
        self.api.fail_id, self.api.fail_mode = R2, "rollback"
        with self.assertRaisesRegex(RuntimeError, "rollback_failed"):
            self.engine.publish(self.target)
        self.assertEqual("rollback_failed", self.journal()["state"])
        self.assertEqual(3, len(list(self.store.sessions.glob("publish-*/backup-*.json"))))
        with self.assertRaises(RuntimeError):
            self.engine.require_resolved(self.target)

    def test_readback_count_mismatch_rolls_back(self):
        self.pull()
        original = copy.deepcopy(self.api.remote)
        self.api.readback_corrupt = True
        with self.assertRaisesRegex(RuntimeError, "rolled_back"):
            self.engine.publish(self.target)
        self.assertEqual(original, self.api.remote)

    def test_different_account_cannot_use_old_catalog(self):
        self.api.credential = Credential()
        self.api.credential.identity = "org:someone-else"
        with self.assertRaisesRegex(ValueError, "другой учётной"):
            self.engine.download(self.target)

    def test_no_reports_still_exports_and_publishes_a_semantic_model(self):
        self.api.reports[W] = []
        self.engine.refresh()
        self.target = targets(self.store.inventory())[0]
        root = self.pull()
        self.assertEqual([], list(root.glob("*.pbip")))
        self.assertEqual(1, self.engine.publish(self.target)["components"])

    def test_cache_is_not_downloaded(self):
        self.api.remote[M]["definition"]["parts"].append({
            "path": ".pbi/cache.abf", "payloadType": "InlineBase64", "payload": base64.b64encode(b"data").decode()})
        root = self.pull()
        self.assertEqual([], list(root.rglob("*.abf")))

    def test_unapplied_desktop_changes_block_publication(self):
        root = self.pull()
        component = self.store.project(self.target["key"])["components"][0]
        pending = root / component["folder"] / ".pbi/unappliedChanges.json"
        pending.parent.mkdir()
        pending.write_text("{}")
        with self.assertRaisesRegex(ValueError, "неприменённые"):
            self.engine.publish(self.target)
        self.assertEqual([], self.api.updates)

    def test_unrelated_report_with_same_name_is_not_exported(self):
        self.api.reports[W][1]["datasetId"] = "10000000-0000-0000-0000-000000000002"
        self.api.reports[W][1]["name"] = self.api.reports[W][0]["name"]
        self.engine.refresh()
        self.target = targets(self.store.inventory())[0]
        self.assertEqual(2, len(self.target["components"]))

    def test_reports_with_identical_names_have_separate_folders(self):
        self.api.reports[W][1]["name"] = self.api.reports[W][0]["name"]
        self.engine.refresh()
        self.target = targets(self.store.inventory())[0]
        root = self.pull()
        self.assertEqual(2, len(list(root.glob("*.Report"))))
        self.assertEqual(2, len(list(root.glob("*.pbip"))))

    def test_related_report_in_another_visible_workspace_is_included(self):
        other = "00000000-0000-0000-0000-000000000002"
        self.api.groups.append({"id": other, "name": "Другая область"})
        self.api.models[other] = []
        self.api.reports[other] = [self.api.reports[W].pop()]
        self.engine.refresh()
        self.target = targets(self.store.inventory())[0]
        self.pull()
        self.assertEqual(3, self.engine.publish(self.target)["components"])
        self.assertEqual(other, self.target["components"][2]["workspace_id"])

    def test_changed_remote_snapshot_does_not_replace_local_project(self):
        root = self.pull()
        (root / "keep").write_text("keep")
        original = self.api.definition
        reads = []

        def changed(component):
            value = original(component)
            if component["id"] == M:
                reads.append(M)
                if len(reads) == 2:
                    return definition({"definition.pbism": '{"version":"4.0"}', "definition/model.tmdl": "model Changed"})
            return value

        with patch.object(self.api, "definition", side_effect=changed):
            with self.assertRaises(ValueError):
                self.engine.download(self.target, overwrite=True)
        self.assertTrue((root / "keep").is_file())

    def test_new_cross_workspace_binding_during_backup_blocks_publication(self):
        other = "00000000-0000-0000-0000-000000000002"
        self.api.groups.append({"id": other, "name": "Other"})
        self.api.models[other], self.api.reports[other] = [], []
        self.engine.refresh()
        self.pull()
        original = self.api.definition

        def changed(component):
            if component["id"] == M:
                self.api.reports[other].append({"id": "20000000-0000-0000-0000-000000000003",
                                                "name": "New", "datasetId": M})
            return original(component)

        with patch.object(self.api, "definition", side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, "привязка изменились"):
                self.engine.publish(self.target)
        self.assertEqual([], self.api.updates)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def prepare(self, old=True):
        folder = "Area/Project"
        output = self.store.project_path(folder)
        output.parent.mkdir()
        if old:
            output.mkdir()
            (output / "old").write_text("old")
        session = self.store.new_session("download")
        (session / "incoming").mkdir()
        (session / "incoming/new").write_text("new")
        return output, session, {"key": "project", "folder": folder}

    def test_filesystem_install_failure_restores_previous(self):
        output, session, record = self.prepare()
        rename = Path.rename

        def fail_incoming(path, target):
            if path.name == "incoming":
                raise OSError("locked by Desktop")
            return rename(path, target)

        with patch.object(Path, "rename", fail_incoming):
            with self.assertRaises(OSError):
                self.store.install(session, record)
        self.assertTrue((output / "old").exists())
        self.assertIsNone(self.store.project("project"))

    def test_registry_commit_failure_restores_previous(self):
        output, session, record = self.prepare()
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail BEFORE INSERT ON projects BEGIN SELECT RAISE(ABORT, 'disk error'); END")
        with self.assertRaisesRegex(Exception, "disk error"):
            self.store.install(session, record)
        self.assertTrue((output / "old").exists())
        self.assertFalse((output / "new").exists())

    def test_restart_recovers_swap_interrupted_before_registry_commit(self):
        output, session, record = self.prepare()
        with self.store.connect() as db:
            db.execute("INSERT INTO pending VALUES (?, ?)", (session.name, json.dumps({"folder": record["folder"], "session": session.name, "had_previous": True})))
        output.rename(session / "previous")
        (session / "incoming").rename(output)
        with self.store.locked():
            pass
        self.assertTrue((output / "old").exists())
        self.assertFalse((output / "new").exists())

    def test_restart_removes_uncommitted_first_download(self):
        output, session, record = self.prepare(old=False)
        with self.store.connect() as db:
            db.execute("INSERT INTO pending VALUES (?, ?)", (session.name, json.dumps({"folder": record["folder"], "session": session.name, "had_previous": False})))
        (session / "incoming").rename(output)
        with self.store.locked():
            pass
        self.assertFalse(output.exists())

    def test_parallel_operations_are_blocked(self):
        second = Store(self.temp.name)
        with self.store.locked():
            with self.assertRaisesRegex(RuntimeError, "другая операция"):
                with second.locked():
                    self.fail("must not enter")

    def test_project_destination_must_stay_within_storage(self):
        for folder in ["../outside", "C:\\outside", ".pbip-loader", ".", "..", "Area/../outside",
                       "Area//Project", "Area/.PBIP-LOADER", "Area/NUL.txt", "Area/Project ", "Area\\Project"]:
            with self.subTest(folder=folder), self.assertRaises(ValueError):
                self.store.project_path(folder)


class DefinitionTests(unittest.TestCase):
    def test_windows_unsafe_paths_are_rejected(self):
        for path in ["../secret", "/absolute", "C:/secret", "a\\b", "a/../b", "a//b", "a/./b", "NUL.txt", "a:stream", "a./file", "a /file", "x\x00.json"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                part_path(path)

    def test_duplicate_case_insensitive_parts_are_rejected(self):
        result = definition({"A.json": "{}", "a.json": "{}"})
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            decode(result)

    def test_invalid_base64_and_json_are_rejected(self):
        result = definition({"a.json": "broken"})
        with self.assertRaises(ValueError):
            decode(result)
        result["definition"]["parts"][0]["payload"] = "!bad"
        with self.assertRaises(ValueError):
            decode(result)

    def test_same_count_different_content_is_not_success(self):
        with self.assertRaises(ValueError):
            verify_remote(definition({"definition/model.tmdl": "model A"}), definition({"definition/model.tmdl": "model B"}))

    def test_json_key_order_and_tmdl_line_endings_do_not_create_false_mismatch(self):
        self.assertEqual(2, verify_remote(
            definition({"a.json": '{"a":1,"b":2}', "definition/model.tmdl": "model A\r\n"}),
            definition({"a.json": '{"b":2, "a":1}', "definition/model.tmdl": "model A\n"})))

    def test_windows_folder_names_are_safe(self):
        self.assertEqual("_CON", safe_name("CON"))
        self.assertEqual("a_b_", safe_name("a:b?"))


if __name__ == "__main__":
    unittest.main()
