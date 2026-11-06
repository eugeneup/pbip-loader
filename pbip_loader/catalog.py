"""Inventory keyed by stable IDs, with all visible related reports."""
from uuid import UUID
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from .storage import now


def identifier(value):
    return str(UUID(str(value)))


def key(workspace_id, model_id):
    return f"{identifier(workspace_id)}:{identifier(model_id)}"


def collect(api, notify=lambda message: None, *, on_progress=lambda done, total: None):
    groups, seen, skip = [], set(), 0
    model_count, report_count, found = 0, 0, 0

    def progress():
        notify(f"Найдено областей: {found} · Обработано: {len(groups)} · "
               f"Моделей: {model_count} · Отчётов: {report_count}")
        on_progress(len(groups), found)

    notify("Получение полного списка областей…")
    workspaces = []
    while True:
        page = api.rows("pbi", f"groups?$top=100&$skip={skip}")
        if not page:
            break
        for group in page:
            wid = identifier(group["id"])
            if wid in seen:
                raise ValueError("Список областей изменился во время сбора. Повторите операцию.")
            seen.add(wid)
            workspaces.append(group)
        skip += len(page)
    found = len(workspaces)
    progress()
    for start in range(0, found, 100):
        for group in workspaces[start:start + 100]:
            wid = identifier(group["id"])
            models = api.rows("pbi", f"groups/{wid}/datasets")
            model_count += len(models)
            progress()
            reports = api.rows("pbi", f"groups/{wid}/reports")
            report_count += len(reports)
            groups.append({
                "id": wid, "name": group["name"],
                "models": [{"id": identifier(m["id"]), "name": m["name"]} for m in models],
                "reports": [{"id": identifier(r["id"]), "name": r["name"],
                             "model_id": identifier(r["datasetId"]) if r.get("datasetId") else None,
                             "kind": r.get("reportType", "PowerBIReport")} for r in reports]})
            progress()
    return {"schema_version": 1, "generated_at": now(), "identity": api.credential.identity,
            "workspaces": groups, "scope": "accessible workspaces; excludes My workspace"}


def targets(catalog):
    result = []
    reports_by_model = defaultdict(list)
    for workspace in catalog["workspaces"]:
        for report in workspace["reports"]:
            kind = "Report" if report["kind"] == "PowerBIReport" else report["kind"]
            reports_by_model[report["model_id"]].append({
                "kind": kind, "id": report["id"], "name": report["name"],
                "workspace_id": workspace["id"], "workspace_name": workspace["name"]})
    for workspace in catalog["workspaces"]:
        for model in workspace["models"]:
            components = [{"kind": "SemanticModel", "id": model["id"], "name": model["name"],
                           "workspace_id": workspace["id"], "workspace_name": workspace["name"]}]
            components.extend(c.copy() for c in reports_by_model[model["id"]])
            result.append({"key": key(workspace["id"], model["id"]),
                           "workspace_id": workspace["id"], "workspace_name": workspace["name"],
                           "model_id": model["id"], "model_name": model["name"],
                           "components": sorted(components, key=lambda c: (c["kind"] != "SemanticModel", c["workspace_id"], c["id"]))})
    return result


def live_target(api, target, workspace_ids=()):
    """Validate identity, names, report membership and bindings before a mutation."""
    model = target["components"][0]
    wid = model["workspace_id"]
    models = api.rows("pbi", f"groups/{wid}/datasets")
    if not any(identifier(m["id"]) == model["id"] and m["name"] == model["name"] for m in models):
        raise ValueError("Модель изменилась. Обновите справочник.")
    groups = {c["workspace_id"] for c in target["components"]}
    groups.update(workspace_ids)
    groups.add(wid)
    expected_reports = defaultdict(set)
    for component in target["components"][1:]:
        expected_reports[component["workspace_id"]].add((component["id"], component["name"]))

    def check_reports(report_wid):
        actual = {(identifier(r["id"]), r["name"])
                  for r in api.rows("pbi", f"groups/{report_wid}/reports")
                  if r.get("datasetId") and identifier(r["datasetId"]) == model["id"]}
        if actual != expected_reports.get(report_wid, set()):
            raise ValueError("Состав отчётов или их привязка изменились. Обновите справочник.")
    def check_item(component):
        if component["kind"] not in {"SemanticModel", "Report"}:
            raise ValueError("Связанный тип отчёта не поддерживает PBIP: " + component["kind"])
        item = api.get("fabric", f"workspaces/{component['workspace_id']}/items/{component['id']}")
        if (identifier(item["id"]) != component["id"] or item.get("type") != component["kind"]
                or item.get("displayName") != component["name"]):
            raise ValueError("Объект Fabric изменился. Обновите справочник.")

    with ThreadPoolExecutor(max_workers=8) as pool:
        for _ in pool.map(check_reports, sorted(groups)):
            pass
        for _ in pool.map(check_item, target["components"]):
            pass
