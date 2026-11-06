"""Central inventory and project registry; no per-project pull.json files."""
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import uuid

RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)


def now():
    return datetime.now(UTC).isoformat()


def dump(path, value):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def linked(path):
    return path.is_symlink() or path.is_junction()


class Store:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta = self.root / ".pbip-loader"
        if linked(self.meta):
            raise ValueError("Служебная папка не должна быть ссылкой.")
        self.meta.mkdir(exist_ok=True)
        self.sessions = self.meta / "sessions"
        if linked(self.sessions):
            raise ValueError("Папка сессий не должна быть ссылкой.")
        self.sessions.mkdir(exist_ok=True)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise ValueError("Эта версия справочника требует более новой версии PBIP Loader.")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inventory (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS projects (key TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.meta / "catalog.sqlite3", timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def locked(self):
        """OS lock is released on process exit, including crashes."""
        with (self.meta / "operation.lock").open("a+b") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("В этой папке уже выполняется другая операция PBIP Loader.") from exc
            try:
                self.recover()
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def inventory(self):
        with self.connect() as db:
            row = db.execute("SELECT data FROM inventory WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def save_inventory(self, catalog):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO inventory VALUES (1, ?)", (json.dumps(catalog),))

    def project(self, key):
        with self.connect() as db:
            row = db.execute("SELECT data FROM projects WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def projects(self):
        with self.connect() as db:
            return [json.loads(data) for (data,) in db.execute("SELECT data FROM projects ORDER BY key")]

    def project_path(self, folder):
        pieces = folder.split("/") if isinstance(folder, str) else []
        if not 1 <= len(pieces) <= 2 or any(
            not piece or piece.casefold() == ".pbip-loader" or piece.endswith((".", " "))
            or RESERVED.match(piece) or re.search(r'[<>:"\\|?*\x00-\x1f]', piece)
            for piece in pieces
        ):
            raise ValueError("Invalid project folder")
        path = self.root
        for piece in pieces:
            path = path / piece
            if linked(path) or not path.resolve().is_relative_to(self.root.resolve()):
                raise ValueError("Папка проекта не должна быть ссылкой или выходить за каталог хранения.")
        return path

    def new_session(self, prefix):
        session = self.sessions / (prefix + "-" + uuid.uuid4().hex)
        session.mkdir()
        return session

    def session_path(self, name):
        if Path(name).name != name or not name.startswith("download-"):
            raise ValueError("Invalid recovery session")
        session = self.sessions / name
        if linked(session) or session.resolve().parent != self.sessions.resolve():
            raise ValueError("Unsafe recovery session")
        return session

    def recover(self):
        """Undo filesystem swaps interrupted before the registry commit."""
        with self.connect() as db:
            pending = db.execute("SELECT id, data FROM pending").fetchall()
        for operation_id, encoded in pending:
            record = json.loads(encoded)
            output = self.project_path(record["folder"])
            previous = self.project_path(record.get("previous_folder", record["folder"]))
            session = self.session_path(record["session"])
            backup, incoming = session / "previous", session / "incoming"
            if backup.exists():
                if linked(backup):
                    raise ValueError("Unsafe recovery backup")
                if output.exists() and not incoming.exists():
                    shutil.rmtree(output)
                if previous.exists():
                    raise RuntimeError("Прежний путь занят. Сохранены журнал и сессия: " + str(session))
                backup.rename(previous)
            elif record["had_previous"] and not (
                previous.is_dir() and (incoming.is_dir() or (previous != output and not output.exists()))
            ):
                raise RuntimeError("Не найдена прежняя версия для восстановления. Сохранены журнал и сессия: " + str(session))
            elif not record["had_previous"] and not incoming.exists() and output.exists():
                shutil.rmtree(output)
            if record.get("created_parent") and output.parent != self.root:
                try:
                    output.parent.rmdir()
                except OSError:
                    pass  # Leave the folder if anything else is using it.
            with self.connect() as db:
                db.execute("DELETE FROM pending WHERE id=?", (operation_id,))
            shutil.rmtree(session, ignore_errors=True)

    def install(self, session, record, *, previous_folder=None):
        output = self.project_path(record["folder"])
        previous = self.project_path(previous_folder) if previous_folder else output
        incoming, backup = session / "incoming", session / "previous"
        if previous != output and output.exists():
            raise FileExistsError("Новая папка проекта уже занята: " + str(output))
        if output.exists() and not output.is_dir():
            raise ValueError("Назначение занято файлом.")
        if previous.exists() and not previous.is_dir():
            raise ValueError("Прежний путь проекта занят файлом.")
        created_parent = not output.parent.exists()
        operation_id = session.name
        with self.connect() as db:
            db.execute("INSERT INTO pending VALUES (?, ?)", (operation_id, json.dumps({
                "folder": record["folder"], "previous_folder": previous_folder or record["folder"],
                "session": session.name, "had_previous": previous.exists(), "created_parent": created_parent})))
        try:
            output.parent.mkdir(exist_ok=True)
            if previous.exists():
                previous.rename(backup)
            incoming.rename(output)
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO projects VALUES (?, ?)", (record["key"], json.dumps(record)))
                db.execute("DELETE FROM pending WHERE id=?", (operation_id,))
        except BaseException:
            self.recover()
            raise
        # Registry commit is the commit point. Cleanup failure is not a failed download.
        shutil.rmtree(session, ignore_errors=True)
