"""Browser login via Azure CLI; tokens stay in its standard user profile."""
import base64
import json
import shutil
import subprocess
import sys
from threading import Lock
import time
from pathlib import Path
import os

PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
AZURE_CLI_INSTALL_PAGE = (
    "https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-macos"
    if sys.platform == "darwin"
    else "https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-windows?view=azure-cli-latest&pivots=msi"
)


def windows_search_path():
    """Read fresh PATH values after installation, without changing system settings."""
    import winreg

    values = [os.environ.get("PATH", "")]
    for hive, subkey in (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, r"Environment"),
    ):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
                if isinstance(value, str):
                    values.append(winreg.ExpandEnvironmentStrings(value))
        except OSError:
            continue
    return os.pathsep.join(values)


def find_azure_cli():
    executable = shutil.which("az")
    if executable or os.name != "nt":
        return executable
    return shutil.which("az", path=windows_search_path())


def config_directory():
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PBIP Loader"


class BrowserCredential:
    def __init__(self, *, notify=lambda message: None, allow_login=True):
        self.notify = notify
        self.allow_login = allow_login
        self.identity = None
        self.cache = {}
        self.token_lock = Lock()

    def run(self, arguments, *, timeout=60):
        executable = find_azure_cli()
        if not executable:
            raise RuntimeError("Установите Microsoft Azure CLI и повторите операцию.")
        environment = os.environ.copy()
        # Per-process settings: browser login rather than Windows broker or subscription picker.
        environment["AZURE_CORE_ENABLE_BROKER_ON_WINDOWS"] = "false"
        environment["AZURE_CORE_LOGIN_EXPERIENCE_V2"] = "off"
        return subprocess.run(
            [executable, *arguments], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=environment, stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def acquire(self, scope):
        args = ["account", "get-access-token", "--resource", scope.removesuffix("/.default"),
                "--output", "json", "--only-show-errors"]
        return self.run(args)

    def token(self, scope):
        with self.token_lock:
            return self._token(scope)

    def _token(self, scope):
        if scope not in {PBI_SCOPE, FABRIC_SCOPE}:
            raise ValueError("Unsupported resource")
        cached = self.cache.get(scope)
        if cached and cached[1] > time.time() + 120:
            return cached[0]
        result = self.acquire(scope)
        if result.returncode:
            if not self.allow_login:
                raise RuntimeError("Нет действующей сессии Azure CLI. Выполните az login или добавьте --allow-login.")
            self.notify("Ожидание входа в браузере Microsoft…")
            args = ["login", "--allow-no-subscriptions", "--output", "none", "--only-show-errors"]
            login = self.run(args, timeout=600)
            if login.returncode:
                raise RuntimeError("Вход Microsoft не завершён. Проверьте окно браузера и политику организации.")
            result = self.acquire(scope)
        if result.returncode:
            raise RuntimeError("Не удалось получить токен ресурса. Проверьте учётную запись Azure CLI и права доступа.")
        payload = json.loads(result.stdout)
        token = payload["accessToken"]
        # Only use claims for matching local inventory to identity; not for authorization.
        encoded = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        identity = f"{claims['tid']}:{claims['oid']}"
        if self.identity and self.identity != identity:
            raise RuntimeError("Учётная запись изменилась. Перезапустите приложение и обновите справочник.")
        self.identity = identity
        self.cache[scope] = (token, float(claims["exp"]))
        return token
