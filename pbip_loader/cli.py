"""Console entry point with JSON results and progress on stderr."""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from .api import Api
from .auth import BrowserCredential, find_azure_cli
from .catalog import targets
from .engine import Engine
from .settings import load_settings, save_settings, settings_path
from .storage import Store


def parser():
    root = argparse.ArgumentParser(prog="pbip-loader-cli")
    commands = root.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="Show or update shared PBIP Loader settings")
    config.add_argument("--root", metavar="PATH")
    search = commands.add_parser("search", help="Search the saved catalog offline")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--local", choices=("all", "yes", "no"), default="all")
    refresh = commands.add_parser("refresh", help="Refresh the shared catalog")
    download = commands.add_parser("download", help="Download a project by key")
    download.add_argument("key", help="Model key returned by search")
    download.add_argument("--overwrite", action="store_true", help="Replace the entire registered local copy")
    publish = commands.add_parser("publish", help="Preview a publication or apply it with --yes")
    publish.add_argument("key", help="Model key returned by search")
    publish.add_argument("--yes", action="store_true", help="Apply the publication to the listed remote items")
    for command in (refresh, download, publish):
        command.add_argument("--allow-login", action="store_true", help="Allow opening a browser to sign in")
    return root


def run(args, *, notify=None, on_progress=lambda done, total: None, reviewed=None):
    path = settings_path()
    settings = load_settings(path)
    if args.command == "config":
        if args.root is not None:
            if not args.root.strip():
                raise ValueError("Укажите непустой путь к папке хранения.")
            store = Store(Path(args.root).expanduser())
            with store.locked():
                pass
            settings = save_settings(path, root=str(store.root))
        return {"settings_file": str(path), "root": settings.get("root", "")}

    root = settings.get("root")
    if not root or not Path(root).is_dir():
        raise ValueError("Укажите папку хранения через config --root PATH или настройки приложения.")
    store = Store(root)
    if reviewed is not None and Path(reviewed["root"]) != store.root:
        raise ValueError("Папка хранения изменилась. Заново выберите модель.")
    if args.command != "refresh":
        catalog = store.inventory()
        if catalog is None:
            raise ValueError("Сначала обновите справочник командой refresh.")
        models = targets(catalog)
    if args.command == "search":
        query = args.query.strip().casefold()
        records = {record["key"]: record for record in store.projects()}
        results = []
        for target in models:
            if query not in f"{target['workspace_name']} {target['model_name']}".casefold():
                continue
            record = records.get(target["key"])
            local = None
            if record:
                folder = store.project_path(record["folder"])
                if folder.is_dir():
                    local = str(folder)
            if args.local == "yes" and local is None or args.local == "no" and local is not None:
                continue
            results.append({**target, "local_path": local})
        return {"models": results}

    if args.command != "refresh":
        target = next((model for model in models if model["key"] == args.key), None)
        if target is None:
            raise ValueError("Модель не найдена. Возьмите key из результата search.")
        if reviewed is not None and reviewed["target"] != target:
            raise ValueError("Состав объектов изменился. Заново выберите модель.")
        if args.command == "publish" and not args.yes:
            engine = Engine(store, None)
            engine.require_resolved(target)
            project, _ = engine.local_record(target, verify_files=False)
            return {"applied": False, "path": str(project), "target": target}

    if not find_azure_cli():
        raise RuntimeError("Установите Microsoft Azure CLI для операций с Power BI.")

    if notify is None:
        def notify(message):
            print(message, file=sys.stderr, flush=True)
    credential = BrowserCredential(notify=notify, allow_login=args.allow_login)
    engine = Engine(store, Api(credential, notify=notify), notify, on_progress=on_progress)
    if args.command == "refresh":
        return engine.refresh()
    if args.command == "download":
        previous = reviewed["path"] if reviewed is not None and args.overwrite else None
        return engine.download(target, overwrite=args.overwrite, expected_previous_path=previous)
    path = reviewed["path"] if reviewed is not None else None
    return {"applied": True, **engine.publish(target, expected_path=path)}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = {"ok": True, **run(args)}
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "Interrupted"}))
        return 130
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
