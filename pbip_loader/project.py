"""Definition round trips, Windows-safe paths and complete project validation."""
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import unicodedata

from .storage import RESERVED, dump, linked

JSON_SUFFIXES = {".json", ".pbir", ".pbism", ".bim", ".pbip"}


def safe_name(name, limit=55):
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:limit].rstrip(" .") or "Untitled"
    return "_" + name if RESERVED.match(name) else name


def unique_name(base, occupied, suffix=""):
    name, index = base + suffix, 2
    while name.casefold() in occupied:
        name = f"{base} ({index}){suffix}"
        index += 1
    return name


def folder_name(store, target, *, migrate_flat=False):
    records = store.projects()
    paths = []
    workspace = None
    for record in records:
        if record["key"] == target["key"] and not (migrate_flat and "/" not in record["folder"]):
            return record["folder"]
        path = store.project_path(record["folder"])
        paths.append(path)
        if (path.parent != store.root
                and record.get("target", {}).get("workspace_id") == target["workspace_id"]):
            workspace = path.parent
    if workspace is None:
        occupied = {p.name.casefold() for p in store.root.iterdir()}
        occupied.update((p.parent if p.parent != store.root else p).name.casefold() for p in paths)
        workspace = store.root / unique_name(safe_name(target["workspace_name"]), occupied)
    occupied = {p.name.casefold() for p in workspace.iterdir()} if workspace.is_dir() else set()
    occupied.update(p.name.casefold() for p in paths if p.parent == workspace)
    model = unique_name(safe_name(target["model_name"]), occupied)
    return f"{workspace.name}/{model}"


def component_folder(component, occupied):
    return unique_name(safe_name(component["name"], 35), occupied, "." + component["kind"])


def part_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Invalid definition part path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise ValueError("Unsafe definition part path")
    for piece in path.parts:
        if (piece in {".", ".."} or piece.endswith((".", " ")) or RESERVED.match(piece)
                or re.search(r'[<>:"|?*\x00-\x1f]', piece)):
            raise ValueError("Unsafe Windows definition part path: " + value)
    return path


def runtime_part(path):
    return path.lower().endswith(".abf") or path.split("/", 1)[0].casefold() in {
        ".pbi", ".git", "daxqueries", "tmdlscripts"}


def decode(definition):
    parts = definition.get("definition", {}).get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("API returned no definition parts")
    files, seen = {}, set()
    for part in parts:
        if not isinstance(part, dict) or part.get("payloadType") != "InlineBase64":
            raise ValueError("Unsupported definition part")
        path = part_path(part.get("path")).as_posix()
        folded = path.casefold()
        if folded in seen:
            raise ValueError("Duplicate definition part: " + path)
        seen.add(folded)
        content = base64.b64decode(part["payload"], validate=True)
        if Path(path).suffix.lower() in JSON_SUFFIXES or path == ".platform":
            json.loads(content.decode("utf-8-sig"))
        if not runtime_part(path) and path != ".platform":
            files[path] = content
    if not files:
        raise ValueError("Definition contains no project files")
    return files


def encode(files):
    return {"definition": {"parts": [{"path": path, "payloadType": "InlineBase64",
                                       "payload": base64.b64encode(content).decode("ascii")}
                                      for path, content in sorted(files.items())]}}


def normalized(files):
    result = {}
    for path, content in files.items():
        if Path(path).suffix.lower() in JSON_SUFFIXES:
            value = json.dumps(json.loads(content.decode("utf-8-sig")), sort_keys=True, ensure_ascii=False)
            content = value.encode("utf-8")
        elif Path(path).suffix.lower() == ".tmdl":
            content = content.decode("utf-8-sig").replace("\r\n", "\n").rstrip().encode("utf-8")
        result[path] = hashlib.sha256(content).hexdigest()
    return result


def verify_remote(expected, actual):
    return verify_fingerprints(normalized(decode(expected)), actual)


def verify_fingerprints(left, actual):
    right = normalized(decode(actual))
    if left != right:
        missing, extra = sorted(left.keys() - right.keys()), sorted(right.keys() - left.keys())
        changed = sorted(p for p in left.keys() & right.keys() if left[p] != right[p])
        raise ValueError(f"Проверка определения не пройдена: частей {len(left)}/{len(right)}; "
                         f"нет {missing}; лишние {extra}; отличаются {changed}")
    return len(left)


def write_component(root, component, definition):
    files = decode(definition)
    if len(part_path(component["folder"]).parts) != 1:
        raise ValueError("Invalid component folder")
    if component["kind"] == "Report":
        reference = json.loads(files["definition.pbir"].decode("utf-8-sig")).get("datasetReference")
        component["service_reference"] = reference
    destination = root / component["folder"]
    destination.mkdir()
    for relative, content in files.items():
        path = destination.joinpath(*PurePosixPath(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if path.read_bytes() != content:
            raise OSError("Записанный файл не соответствует полученному: " + relative)
    dump(destination / ".platform", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": component["kind"], "displayName": component["name"]},
        "config": {"version": "2.0", "logicalId": component["id"]}})
    return {"part_count": len(files), "hashes": normalized(files), "paths": sorted(files)}


def make_entries(root, components):
    model_folder = components[0]["folder"]
    for report in components[1:]:
        pbir = root / report["folder"] / "definition.pbir"
        value = json.loads(pbir.read_text(encoding="utf-8-sig"))
        value["datasetReference"] = {"byPath": {"path": f"../{model_folder}"}}
        dump(pbir, value)
        report["entry"] = report["folder"].removesuffix(".Report") + ".pbip"
        dump(root / report["entry"], {"version": "1.0", "artifacts": [{"report": {"path": report["folder"]}}],
                                      "settings": {"enableAutoRecovery": True}})


def read_component(root, component):
    folder = root / component["folder"]
    if not folder.is_dir() or linked(folder):
        raise ValueError("Отсутствует локальный компонент: " + component["name"])
    if (folder / ".pbi/unappliedChanges.json").exists():
        raise ValueError("Есть неприменённые изменения Desktop: " + str(folder / ".pbi/unappliedChanges.json"))
    files = {}
    source_paths = set(component.get("verification", {}).get("paths", []))
    for path in folder.rglob("*"):
        if linked(path):
            raise ValueError("Ссылки в проекте не поддерживаются: " + str(path))
        if not path.is_file():
            continue
        relative = path.relative_to(folder).as_posix()
        part_path(relative)
        if runtime_part(relative) or relative == ".platform":
            continue
        # Original API parts + known definition roots; local notes aren't API parts.
        if (relative in source_paths or relative.split("/", 1)[0] in {"definition", "StaticResources", "Copilot"}
                or relative in {"definition.pbism", "model.bim", "definition.pbir", "report.json", "diagramLayout.json",
                                "semanticModelDiagramLayout.json"}):
            content = path.read_bytes()
            if path.suffix.lower() in JSON_SUFFIXES:
                json.loads(content.decode("utf-8-sig"))
            files[relative] = content
    if component["kind"] == "SemanticModel":
        if "definition.pbism" not in files or not ({"definition/model.tmdl", "model.bim"} & files.keys()):
            raise ValueError("Неполное определение модели")
    elif ("definition.pbir" not in files or not ({"definition/report.json", "report.json"} & files.keys())):
        raise ValueError("Неполное определение отчёта")
    return files


def validate(root, components):
    expected = {c["folder"] for c in components}
    if len({name.casefold() for name in expected}) != len(components):
        raise ValueError("Компоненты имеют конфликтующие локальные пути")
    actual = {p.name for p in root.iterdir() if p.is_dir() and p.suffix in {".SemanticModel", ".Report"}}
    if actual != expected:
        raise ValueError("Количество или состав локальных компонентов отличается от справочника")
    for component in components:
        read_component(root, component)
        if component["kind"] == "Report":
            entry = json.loads((root / component["entry"]).read_text(encoding="utf-8-sig"))
            if entry.get("artifacts") != [{"report": {"path": component["folder"]}}]:
                raise ValueError("Некорректная ссылка PBIP")
            value = json.loads((root / component["folder"] / "definition.pbir").read_text(encoding="utf-8-sig"))
            if value.get("datasetReference") != {"byPath": {"path": "../" + components[0]["folder"]}}:
                raise ValueError("Отчёт ссылается на другую локальную модель")
    # Every byPath in any component must resolve within this project.
    def references(value, parent):
        if isinstance(value, dict):
            if "byPath" in value:
                reference = value["byPath"]
                if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
                    raise ValueError("Invalid byPath reference")
                target = (parent / reference["path"]).resolve()
                if not target.is_relative_to(root.resolve()) or not target.exists():
                    raise ValueError("Отсутствует локальная зависимость компонента")
            for child in value.values():
                references(child, parent)
        elif isinstance(value, list):
            for child in value:
                references(child, parent)
    for path in root.rglob("*"):
        if linked(path):
            raise ValueError("Ссылки в проекте не поддерживаются")
        if path.is_file() and path.suffix.lower() in JSON_SUFFIXES and ".pbi" not in path.relative_to(root).parts:
            references(json.loads(path.read_text(encoding="utf-8-sig")), path.parent)


def publish_definition(root, component, model):
    files = read_component(root, component)
    if component["kind"] == "Report":
        value = json.loads(files["definition.pbir"].decode("utf-8-sig"))
        reference = component.get("service_reference", {})
        if isinstance(reference, dict) and isinstance(reference.get("byConnection", {}).get("connectionString"), str):
            value["datasetReference"] = reference
        else:
            def quoted(text):
                return '"' + text.replace('"', '""') + '"'
            connection = (f"Data Source={quoted('powerbi://api.powerbi.com/v1.0/myorg/' + model['workspace_name'])};"
                          f"Initial Catalog={quoted(model['name'])};Integrated Security=ClaimsToken;"
                          f"semanticmodelid={model['id']}")
            value["$schema"] = "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json"
            value["datasetReference"] = {"byConnection": {"connectionString": connection}}
        files["definition.pbir"] = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return encode(files)
