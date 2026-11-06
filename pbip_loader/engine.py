"""All-or-error workflows. Local atomic replacement; remote compensation journal."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil

from .api import Indeterminate
from .auth import PBI_SCOPE
from .catalog import collect, live_target, targets
from .project import (component_folder, decode, folder_name, make_entries, normalized, part_path,
                      publish_definition, validate, verify_fingerprints, verify_remote, write_component)
from .storage import dump, now


def component_ids(components):
    return [(c["workspace_id"], c["id"], c["kind"]) for c in components]


def component_summary(components):
    return [{key: component[key] for key in ("workspace_id", "workspace_name", "id", "name", "kind")}
            for component in components]


class Engine:
    def __init__(self, store, api, notify=lambda message: None, *, on_progress=lambda done, total: None):
        self.store, self.api, self.notify = store, api, notify
        self.on_progress = on_progress

    def check_identity(self):
        catalog = self.store.inventory()
        if catalog is None:
            raise ValueError("Сначала обновите структуру областей, моделей и отчётов.")
        self.api.credential.token(PBI_SCOPE)
        if catalog["identity"] != self.api.credential.identity:
            raise ValueError("Справочник принадлежит другой учётной записи. Обновите структуру.")
        return catalog

    def current(self, target):
        catalog = self.check_identity()
        current = next((t for t in targets(catalog) if t["key"] == target["key"]), None)
        if current != target:
            raise ValueError("Выбор устарел. Выберите модель из обновлённого справочника.")
        return catalog

    def live(self, target, catalog=None):
        workspace_ids = [w["id"] for w in catalog["workspaces"]] if catalog else ()
        live_target(self.api, target, workspace_ids)

    def definitions(self, components):
        # Bound the number of large definition responses held in memory.
        with ThreadPoolExecutor(max_workers=2) as pool:
            for start in range(0, len(components), 2):
                batch = components[start:start + 2]
                yield from zip(batch, pool.map(self.api.definition, batch), strict=True)

    def refresh(self):
        with self.store.locked():
            self.notify("Чтение областей, моделей и отчётов…")
            catalog = collect(self.api, self.notify, on_progress=self.on_progress)
            self.store.save_inventory(catalog)
        return {"workspaces": len(catalog["workspaces"]), "models": len(targets(catalog)),
                "reports": sum(len(w["reports"]) for w in catalog["workspaces"])}

    def download(self, target, *, overwrite=False, expected_folder=None, expected_previous_path=None):
        with self.store.locked():
            count = len(target["components"])
            total = 2 * count + 2
            self.on_progress(0, total)
            self.notify("Предварительная проверка модели и отчётов…")
            catalog = self.current(target)
            self.require_resolved(target)
            previous = self.store.project(target["key"])
            if previous and previous["identity"] != catalog["identity"]:
                raise ValueError("Локальная папка принадлежит другой учётной записи.")
            folder = folder_name(self.store, target, migrate_flat=True)
            if expected_folder is not None and folder != expected_folder:
                raise ValueError("Путь проекта изменился. Повторите скачивание.")
            output = self.store.project_path(folder)
            previous_path = self.store.project_path(previous["folder"]) if previous else output
            if expected_previous_path is not None and (
                not previous or previous_path != Path(expected_previous_path) or not previous_path.is_dir()
            ):
                raise ValueError("Локальная копия изменилась. Заново выберите модель.")
            migrating = previous_path != output
            replacing = bool(previous and previous_path.exists())
            if (previous_path.exists() or output.exists()) and not overwrite:
                raise FileExistsError("Модель уже скачана. Подтвердите замену всей папки.")
            if output.exists() and (migrating or not replacing):
                raise FileExistsError("Папка назначения уже занята: " + str(output))
            self.live(target, catalog)
            self.on_progress(1, total)
            session = self.store.new_session("download")
            incoming = session / "incoming"
            incoming.mkdir()
            record = {"key": target["key"], "identity": catalog["identity"], "folder": folder,
                      "downloaded_at": now(), "target": copy.deepcopy(target), "components": []}
            old_components = dict(zip(component_ids(previous["components"]), previous["components"])) if previous else {}
            old_folders = {} if migrating else {key: c["folder"] for key, c in old_components.items()}
            occupied = {folder.casefold() for folder in old_folders.values()}
            try:
                self.notify("Скачивание определений из Power BI Service…")
                for index, (component, definition) in enumerate(self.definitions(copy.deepcopy(target["components"])), 1):
                    self.notify(f"Сохранение: {component['name']}")
                    old_folder = old_folders.get((component["workspace_id"], component["id"], component["kind"]))
                    component["folder"] = old_folder or component_folder(component, occupied)
                    occupied.add(component["folder"].casefold())
                    component["verification"] = write_component(incoming, component, definition)
                    record["components"].append(component)
                    self.on_progress(1 + index, total)
                if len(record["components"]) != len(target["components"]):
                    raise ValueError("Скачаны не все компоненты")
                make_entries(incoming, record["components"])
                # Preserve custom PBIP settings if the entry still points at the same report.
                if previous and previous_path.exists():
                    for component in record["components"][1:]:
                        registered = old_components.get(component_ids([component])[0])
                        if not registered or len(part_path(registered["entry"]).parts) != 1:
                            continue
                        old = previous_path / registered["entry"]
                        if old.is_file():
                            if old.is_symlink():
                                raise ValueError("PBIP entry must not be a link")
                            payload = json.loads(old.read_text(encoding="utf-8-sig"))
                            if payload.get("artifacts") == [{"report": {"path": registered["folder"]}}]:
                                entry = incoming / component["entry"]
                                if registered["folder"] == component["folder"]:
                                    entry.write_bytes(old.read_bytes())
                                else:
                                    payload["artifacts"] = [{"report": {"path": component["folder"]}}]
                                    dump(entry, payload)
                validate(incoming, record["components"])
                self.notify("Повторное чтение определений для проверки снимка…")
                for index, (component, definition) in enumerate(self.definitions(record["components"]), 1):
                    self.notify(f"Проверка снимка: {component['name']}")
                    verify_fingerprints(component["verification"]["hashes"], definition)
                    self.on_progress(1 + count + index, total)
                self.notify("Проверка связей и сохранение проекта…")
                self.live(target, catalog)
                if output.exists() and (migrating or not replacing):
                    raise FileExistsError("Папка появилась во время скачивания. Подтвердите замену и повторите операцию.")
                self.store.install(session, record, previous_folder=previous["folder"] if migrating else None)
                self.on_progress(total, total)
            except BaseException:
                # Keep previous if filesystem rollback itself failed.
                if not (session / "previous").exists():
                    shutil.rmtree(session, ignore_errors=True)
                raise
        return {"status": "verified", "downloaded": component_summary(record["components"]),
                "path": str(output), "components": len(record["components"]),
                "parts": sum(c["verification"]["part_count"] for c in record["components"])}

    def local_record(self, target, *, verify_files=True):
        record = self.store.project(target["key"])
        if record is None:
            raise ValueError("Модель ещё не скачана в выбранную папку хранения.")
        if component_ids(record["components"]) != component_ids(target["components"]):
            raise ValueError("Состав компонентов изменился. Повторно скачайте модель перед публикацией.")
        root = self.store.project_path(record["folder"])
        if not root.is_dir():
            raise ValueError("Локальная папка модели отсутствует: " + str(root))
        if verify_files:
            validate(root, record["components"])
        # Refresh the display names, keeping the registered local paths and IDs.
        for component, current in zip(record["components"], target["components"], strict=True):
            component.update(current)
        return root, record

    def unresolved_publications(self):
        # An interrupted remote mutation must be reviewed before another update or pull.
        pending = {}
        for path in self.store.sessions.glob("publish-*/journal.json"):
            journal = json.loads(path.read_text(encoding="utf-8"))
            if journal["state"] not in {"succeeded", "rolled_back", "preflight_failed"}:
                pending[journal["target"]["key"]] = path
        return pending

    def require_resolved(self, target):
        path = self.unresolved_publications().get(target["key"])
        if path is not None:
            raise RuntimeError("Есть незавершённая публикация. Сначала проверьте сервис и журнал: " + str(path))

    def publish(self, target, *, expected_path=None):
        with self.store.locked():
            count = len(target["components"])
            total = 3 * count + 2
            self.on_progress(0, total)
            self.notify("Предварительная проверка модели и отчётов…")
            catalog = self.current(target)
            self.require_resolved(target)
            root, record = self.local_record(target)
            if expected_path is not None and root != Path(expected_path):
                raise ValueError("Путь локальной копии изменился. Заново проверьте план.")
            if record["identity"] != catalog["identity"]:
                raise ValueError("Проект принадлежит другой учётной записи.")
            self.live(target)
            components = record["components"]
            self.on_progress(1, total)
            desired = []
            session = self.store.new_session("publish")
            journal = {"state": "preparing", "created_at": now(), "target": target,
                       "components": components, "attempted": [], "completed": [], "verified": []}
            path = session / "journal.json"
            dump(path, journal)
            backups = []
            backup_hashes = []

            def accepted(location):
                journal.setdefault("operations", []).append({
                    "phase": journal["state"],
                    "index": journal.get("rollback_inflight", journal.get("inflight")), "url": location})
                dump(path, journal)

            self.api.on_operation = accepted
            try:
                self.notify("Чтение резервных определений из Power BI Service…")
                for index, (component, backup) in enumerate(self.definitions(components)):
                    # Validate every backup before sending even the first update.
                    backup_files = decode(backup)
                    backup_hashes.append(normalized(backup_files))
                    backups.append(backup)
                    # Use the current service reference, including after a remote rename.
                    source = component
                    if component["kind"] == "Report":
                        reference = json.loads(backup_files["definition.pbir"]).get("datasetReference")
                        source = {**component, "service_reference": reference}
                    desired.append(publish_definition(root, source, components[0]))
                    dump(session / f"backup-{index}.json", backup)
                    dump(session / f"desired-{index}.json", desired[index])
                    self.on_progress(index + 2, total)
                self.notify("Проверка связей перед публикацией…")
                self.live(target, catalog)
                journal["state"] = "publishing"
                dump(path, journal)
                for index, component in enumerate(components):
                    self.notify(f"Публикация: {component['name']}")
                    journal["attempted"].append(index)
                    journal["inflight"] = index
                    dump(path, journal)
                    self.api.update(component, desired[index])
                    journal["completed"].append(index)
                    journal.pop("inflight", None)
                    dump(path, journal)
                    self.on_progress(count + index + 2, total)
                journal["state"] = "verifying"
                dump(path, journal)
                self.notify("Чтение опубликованных определений из Power BI Service…")
                for index, (component, actual) in enumerate(self.definitions(components)):
                    self.notify(f"Проверка определения: {component['name']}")
                    parts = verify_remote(desired[index], actual)
                    journal["verified"].append({"index": index, "parts": parts})
                    dump(path, journal)
                    self.on_progress(2 * count + index + 2, total)
                self.notify("Проверка связей опубликованных компонентов…")
                self.live(target, catalog)
                if len(journal["verified"]) != len(components):
                    raise ValueError("Подтверждены не все компоненты публикации")
            except BaseException as exc:
                if not journal["attempted"]:
                    journal["state"] = "preflight_failed"
                elif isinstance(exc, Indeterminate) and "inflight" in journal:
                    journal["state"] = "indeterminate"
                elif "inflight" in journal and not isinstance(exc, Exception):
                    journal["state"] = "indeterminate"
                else:
                    self.on_progress(0, 0)
                    journal["state"] = "rolling_back"
                    journal["rollback_completed"] = []
                    dump(path, journal)
                    try:
                        # Restore model first, then its reports. Data purge stays disabled.
                        for index in sorted(journal["attempted"]):
                            self.notify("Откат: " + components[index]["name"])
                            journal["rollback_inflight"] = index
                            dump(path, journal)
                            self.api.update(components[index], backups[index])
                            verify_fingerprints(backup_hashes[index], self.api.definition(components[index]))
                            journal["rollback_completed"].append(index)
                            journal.pop("rollback_inflight", None)
                            dump(path, journal)
                        journal["state"] = "rolled_back"
                    except BaseException:
                        journal["state"] = "rollback_failed"
                dump(path, journal)
                raise RuntimeError(f"Публикация не завершена ({journal['state']}). {exc}\nЖурнал и резервные определения: {session}") from exc
            finally:
                self.api.on_operation = None
            journal["state"] = "succeeded"
            dump(path, journal)
            result = {"status": "verified", "published": component_summary(components), "components": len(components),
                      "parts": sum(x["parts"] for x in journal["verified"])}
            shutil.rmtree(session, ignore_errors=True)
            self.on_progress(total, total)
            return result
