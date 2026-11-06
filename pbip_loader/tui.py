"""Interactive terminal front end for the shared PBIP Loader operations."""
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, ProgressBar, Select, Static

from . import cli
from .auth import AZURE_CLI_INSTALL_PAGE


class Prompt(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Отмена", show=False)]

    def __init__(self, title, body, *, accept=None, path=None):
        super().__init__()
        self.heading, self.body, self.accept, self.path = title, body, accept, path

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self.heading), classes="dialog-title")
            if self.path is not None:
                yield Input(self.path, placeholder="Путь к папке хранения", id="storage-path")
            with VerticalScroll(classes="dialog-body"):
                yield Static(Text(self.body))
            with Horizontal(classes="toolbar"):
                yield Button("Отмена" if self.accept else "Закрыть", id="cancel", classes="pixel")
                if self.accept:
                    yield Button(self.accept, id="accept", classes="pixel")

    def on_mount(self):
        if self.path is not None:
            self.query_one(Input).focus()
        else:
            self.query_one("#cancel").focus()

    def action_cancel(self):
        self.dismiss(None)

    @on(Input.Submitted)
    @on(Button.Pressed, "#accept")
    def accept_prompt(self):
        self.dismiss(self.query_one(Input).value if self.path is not None else True)

    @on(Button.Pressed, "#cancel")
    def cancel_prompt(self):
        self.action_cancel()


class OperationStatus(Message):
    def __init__(self, *, text=None, progress=None):
        super().__init__()
        self.text, self.progress = text, progress


class OperationFinished(Message):
    def __init__(self, callback, result=None, error=None):
        super().__init__()
        self.callback, self.result, self.error = callback, result, error


class Loader(App):
    TITLE = "PBIP Loader"
    BINDINGS = [
        Binding("ctrl+r", "refresh_catalog", "Обновить"),
        Binding("ctrl+s", "settings", "Настройки"),
        Binding("ctrl+d", "download", "Скачать"),
        Binding("ctrl+p", "publish", "Публикация"),
        Binding("ctrl+q", "quit", "Выход"),
    ]
    CSS = """
    Screen { padding: 0 1; }
    #title { text-style: bold; color: $accent; height: 1; }
    .toolbar { height: 3; }
    Button.pixel {
        height: 3; min-width: 1; margin-right: 1;
        background: transparent; background-tint: transparent; tint: transparent;
        color: #e0e5ec; border: round #646d79;
    }
    Button.pixel:hover { border: round #9ba8b8; }
    Button.pixel:focus { border: round #c3d2e2; }
    Button.pixel.model-action:enabled {
        color: #b9d9ff; border: round #6f9cc6;
    }
    Button.pixel.model-action:enabled:hover { border: round #a2c8ef; }
    Button.pixel.model-action:enabled:focus { border: round #b4d5f3; }
    Button.pixel.needs-settings {
        color: #f5adb8; border: round #b8757e;
    }
    Button.pixel.needs-settings:hover { border: round #dc8b96; }
    Button.pixel.needs-settings:focus { border: round #f5adb8; }
    Button.pixel:disabled {
        color: #8993a0; border: round #4a515a; text-opacity: 1;
    }
    #refresh, #filter { width: 24; }
    #download, #publish { width: 25; }
    #filter { margin-right: 1; }
    #filter > SelectCurrent, #filter > SelectOverlay, #storage-path { border: round #646d79; }
    #filter > SelectCurrent { background: transparent; background-tint: transparent; }
    #filter:focus > SelectCurrent, #storage-path:focus { border: round #91b4d6; }
    #search { width: 1fr; }
    #models { height: 1fr; }
    #models.unselected:blur > .datatable--cursor { background: $surface; color: $foreground; text-style: none; }
    #status { height: auto; max-height: 3; }
    #progress { height: 1; display: none; }
    #progress.active { display: block; }
    Prompt { align: center middle; }
    .dialog { width: 74; max-width: 100%; height: auto; max-height: 90%;
              padding: 1 2; border: round $accent; background: $surface; }
    .dialog-title { text-style: bold; padding-bottom: 1; }
    .dialog-body { height: auto; max-height: 14; }
    .dialog .toolbar { margin-top: 1; }
    """

    def __init__(self):
        super().__init__()
        self.commands = cli.parser()
        self.root = ""
        self.cli_available = False
        self.models = []
        self.filtered_models = []
        self.selected_key = None
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Static("PBIP Loader", id="title")
        with Horizontal(classes="toolbar"):
            yield Button("Обновить структуру", id="refresh", classes="pixel")
            yield Button("Настройки", id="settings", classes="pixel")
        with Horizontal(classes="toolbar"):
            yield Select([("Все модели", "all"), ("Есть локально", "yes"), ("Нет локально", "no")],
                         value="all", allow_blank=False, id="filter")
            yield Input(placeholder="Поиск по рабочей области или модели", id="search")
        yield DataTable(cursor_type="row", id="models", classes="unselected")
        with Horizontal(classes="toolbar"):
            yield Button("Скачать модель", id="download", classes="pixel model-action")
            yield Button("Опубликовать модель", id="publish", classes="pixel model-action")
        yield ProgressBar(show_eta=False, id="progress")
        yield Static("Чтение настроек…", id="status")
        yield Footer()

    def on_mount(self):
        self.cli_available = bool(cli.find_azure_cli())
        self.query_one(DataTable).add_columns("Рабочая область", "Семантическая модель", "Локально")
        self.query_one("#search").focus()
        self.load_catalog()

    def selected(self):
        return next((row for row in self.filtered_models if row["key"] == self.selected_key), None)

    def update_buttons(self):
        target = self.selected()
        configured = bool(self.root and self.cli_available)
        ready = configured and not self.busy
        settings = self.query_one("#settings", Button)
        settings.disabled = self.busy
        settings.set_class(not configured, "needs-settings")
        self.query_one(DataTable).set_class(target is None, "unselected")
        self.query_one("#refresh", Button).disabled = not ready
        self.query_one("#download", Button).disabled = not ready or target is None
        self.query_one("#publish", Button).disabled = not ready or not (target and target["local_path"])

    @on(DataTable.RowHighlighted)
    @on(DataTable.RowSelected)
    def update_selection(self, event):
        if isinstance(event, DataTable.RowHighlighted) and not event.data_table.has_focus:
            return
        self.selected_key = event.row_key.value
        self.update_buttons()

    @on(Input.Changed, "#search")
    @on(Select.Changed, "#filter")
    def populate(self):
        query = self.query_one("#search", Input).value.strip().casefold()
        local = self.query_one("#filter", Select).value
        self.filtered_models = [
            row for row in self.models
            if query in f"{row['workspace_name']} {row['model_name']}".casefold()
            and (local == "all" or bool(row["local_path"]) == (local == "yes"))
        ]
        table = self.query_one(DataTable)
        cursor = None
        with table.prevent(DataTable.RowHighlighted):
            table.clear()
            for index, row in enumerate(self.filtered_models):
                table.add_row(Text(row["workspace_name"]), Text(row["model_name"]),
                              "Да" if row["local_path"] else "—", key=row["key"])
                if self.selected_key == row["key"]:
                    cursor = index
            if cursor is None:
                self.selected_key = None
            else:
                table.move_cursor(row=cursor)
        self.update_buttons()

    def status(self, value):
        self.query_one("#status", Static).update(Text(value))

    def invoke(self, *arguments, reviewed=None):
        return cli.run(
            self.commands.parse_args(arguments), reviewed=reviewed,
            notify=lambda text: self.post_message(OperationStatus(text=text)),
            on_progress=lambda done, total: self.post_message(OperationStatus(progress=(done, total))),
        )

    def execute(self, operation, callback, *, progress=False, show_error=True):
        self.busy = True
        self.update_buttons()
        bar = self.query_one(ProgressBar)
        bar.set_class(progress, "active")
        if progress:
            bar.update(total=None, progress=0)

        def work():
            try:
                result = operation()
            except Exception as error:
                self.post_message(OperationFinished(callback, error=(str(error), show_error)))
            else:
                self.post_message(OperationFinished(callback, result))

        self.run_worker(work, thread=True)

    def on_operation_status(self, message):
        if message.text is not None:
            self.status(message.text)
        if message.progress is not None:
            done, total = message.progress
            self.query_one(ProgressBar).update(total=total or None, progress=done)

    def on_operation_finished(self, message):
        self.busy = False
        self.query_one(ProgressBar).remove_class("active")
        self.update_buttons()
        if message.error is None:
            message.callback(message.result)
        else:
            error, show_error = message.error
            self.status("Ошибка · " + error)
            if show_error:
                self.push_screen(Prompt("Операция остановлена", error))

    def load_catalog(self, status=None):
        def read():
            root = self.invoke("config")["root"]
            if not root or not Path(root).is_dir():
                return "", [], "Выберите папку хранения в настройках."
            try:
                models = self.invoke("search")["models"]
            except ValueError as error:
                return root, [], str(error)
            models.sort(key=lambda row: (row["workspace_name"].casefold(), row["model_name"].casefold()))
            return root, models, f"В справочнике {len(models)} моделей."

        def loaded(result):
            self.root, self.models, message = result
            self.populate()
            self.status(status or message)

        self.execute(read, loaded, show_error=False)

    @on(Button.Pressed, "#settings")
    def action_settings(self):
        if self.busy:
            return
        self.cli_available = bool(cli.find_azure_cli())
        self.update_buttons()
        cli_status = "Azure CLI найден." if self.cli_available else f"Установите Azure CLI.\n{AZURE_CLI_INSTALL_PAGE}"

        def save(path):
            self.cli_available = bool(cli.find_azure_cli())
            self.update_buttons()
            if path is not None:
                self.execute(lambda: self.invoke("config", "--root", path), lambda _: self.load_catalog())

        self.push_screen(Prompt("Папка хранения", cli_status, accept="Сохранить", path=self.root), save)

    @on(Button.Pressed, "#refresh")
    def action_refresh_catalog(self):
        if self.busy or not self.root or not self.cli_available:
            return
        self.execute(
            lambda: self.invoke("refresh", "--allow-login"),
            lambda r: self.load_catalog(f"Области: {r['workspaces']} · Модели: {r['models']} · Отчёты: {r['reports']}"),
            progress=True,
        )

    @on(Button.Pressed, "#download")
    def action_download(self):
        target = self.selected()
        if self.busy or not self.cli_available or not target:
            return
        path = target["local_path"]
        reviewed = self.reviewed(target, path)
        args = ("download", target["key"], "--allow-login")

        def download(accepted):
            if accepted:
                self.execute(
                    lambda: self.invoke(*args, *(("--overwrite",) if path else ()), reviewed=reviewed),
                    lambda r: self.load_catalog(f"Скачано и проверено · {r['path']}"),
                    progress=True,
                )

        if path:
            self.push_screen(Prompt("Замена локальной копии",
                                    f"{path}\n\nБудет заменена вся папка, включая локальные изменения.",
                                    accept="Заменить"), download)
        else:
            download(True)

    def reviewed(self, target, path):
        return {"root": self.root, "path": path,
                "target": {key: value for key, value in target.items() if key != "local_path"}}

    @on(Button.Pressed, "#publish")
    def action_publish(self):
        target = self.selected()
        if self.busy or not self.cli_available or not (target and target["local_path"]):
            return
        args = ("publish", target["key"])
        reviewed = self.reviewed(target, target["local_path"])

        def show(plan):
            reviewed["path"] = plan["path"]
            objects = "\n".join(
                f"{c['workspace_name']} / {c['kind']} / {c['name']}\n  {c['id']}"
                for c in plan["target"]["components"]
            )
            self.push_screen(Prompt("Публикация в Power BI Service",
                                    f"{plan['path']}\n\n{objects}\n\nОбновление данных не запускается.",
                                    accept="Опубликовать"), publish)

        def publish(accepted):
            if accepted:
                self.execute(
                    lambda: self.invoke(*args, "--yes", "--allow-login", reviewed=reviewed),
                    lambda r: self.status(f"Публикация проверена · {target['model_name']} · Объектов: {r['components']}"),
                    progress=True,
                )

        self.execute(lambda: self.invoke(*args, reviewed=reviewed), show)

    async def action_quit(self):
        if self.busy:
            self.status("Дождитесь завершения операции.")
        else:
            await super().action_quit()


def main():
    Loader().run()
