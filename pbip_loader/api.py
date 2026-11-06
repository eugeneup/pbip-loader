"""Power BI/Fabric HTTP adapter. Mutations are never blindly retried."""
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from .auth import FABRIC_SCOPE, PBI_SCOPE

PBI = "https://api.powerbi.com/v1.0/myorg/"
FABRIC = "https://api.fabric.microsoft.com/v1/"


class Rejected(RuntimeError):
    """The server returned a definite terminal failure."""


class Indeterminate(RuntimeError):
    """An operation may still be running; automatically overwriting it is unsafe."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise Indeterminate("API вернул неожиданное перенаправление. Токен не передан другому адресу.")


def urlopen(request, timeout):
    return build_opener(NoRedirect()).open(request, timeout=timeout)


class Api:
    def __init__(self, credential, *, timeout=900, notify=lambda message: None):
        self.credential = credential
        self.timeout = timeout
        self.last_operation = None
        self.on_operation = None
        self.notify = notify

    def request(self, service, path, *, payload=None, safe_read=False):
        base, scope = (PBI, PBI_SCOPE) if service == "pbi" else (FABRIC, FABRIC_SCOPE)
        url = path if path.startswith("https://") else base + path
        parsed, expected = urlparse(url), urlparse(base)
        if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
            raise ValueError("API returned an untrusted continuation/operation URL.")
        headers = {"Authorization": f"Bearer {self.credential.token(scope)}", "Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        retry_read = payload is None or safe_read
        for attempt in range(4):
            try:
                with urlopen(Request(url, data=data, headers=headers), timeout=120) as response:
                    raw = response.read()
                    body = json.loads(raw) if raw else {}
                    return response.status, dict((k.lower(), v) for k, v in response.headers.items()), body
            except HTTPError as exc:
                # 429 explicitly rejects the request; safe to retry even for POST.
                if exc.code == 429 and attempt < 3:
                    self.notify(f"API временно ограничил запросы · Повтор {attempt + 1}/3")
                    time.sleep(max(1, int(exc.headers.get("Retry-After", "5"))))
                    continue
                if retry_read and (exc.code in {408, 500, 502, 503, 504}) and attempt < 3:
                    self.notify(f"API HTTP {exc.code} · Повтор чтения {attempt + 1}/3")
                    time.sleep(2 ** attempt)
                    continue
                try:
                    error = json.loads(exc.read())
                    code = error.get("errorCode") or error.get("error", {}).get("code", "Unknown")
                except (ValueError, AttributeError):
                    code = "Unknown"
                message = f"API HTTP {exc.code} ({code}). Проверьте права, consent и поддержку item."
                if (exc.code >= 500 or exc.code == 408) and payload is not None:
                    raise Indeterminate(message) from exc
                raise Rejected(message) from exc
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                if retry_read and attempt < 3:
                    self.notify(f"Сбой связи с API · Повтор чтения {attempt + 1}/3")
                    time.sleep(2 ** attempt)
                    continue
                reason = str(getattr(exc, "reason", exc)) or type(exc).__name__
                if retry_read:
                    raise Rejected(f"Не удалось прочитать ответ API после 4 попыток. "
                                   f"Проверьте соединение. Причина: {reason}") from exc
                raise Indeterminate("Нет достоверного ответа API; операция могла продолжиться в сервисе. "
                                    f"Причина: {reason}") from exc
        raise RuntimeError("API retry limit")

    def get(self, service, path):
        status, _, body = self.request(service, path)
        if status != 200 or not isinstance(body, dict):
            raise ValueError("Invalid API response")
        return body

    def rows(self, service, path):
        rows, seen = [], set()
        while path:
            if path in seen:
                raise ValueError("Repeated API page")
            seen.add(path)
            page = self.get(service, path)
            values = page.get("value")
            if not isinstance(values, list) or any(not isinstance(x, dict) for x in values):
                raise ValueError("Incomplete API collection")
            rows.extend(values)
            next_path = page.get("@odata.nextLink") or page.get("continuationUri")
            token = page.get("continuationToken")
            if token and not next_path:
                next_path = path.split("?", 1)[0] + "?continuationToken=" + quote(token, safe="")
            path = next_path
        ids = [r["id"].lower() for r in rows if "id" in r]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate items in API collection")
        return rows

    def operation(self, path, payload, *, result=False):
        if not result:
            self.last_operation = None
        options = {"safe_read": True} if result else {}
        status, headers, body = self.request("fabric", path, payload=payload, **options)
        if status in {200, 201}:
            return body
        if status != 202 or not (headers.get("location") or headers.get("x-ms-operation-id")):
            raise Indeterminate("Нет адреса операции Fabric.")
        location = self.operation_url(headers)
        if not result:
            self.last_operation = location
            if self.on_operation:
                self.on_operation(location)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            delay = max(1, int(headers.get("retry-after", "2")))
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            try:
                _, headers, body = self.request("fabric", location)
            except Exception as exc:
                raise Indeterminate(f"Не удалось узнать состояние принятой операции: {location}\n{exc}") from exc
            if body.get("status") == "Succeeded":
                return self.get("fabric", location.rstrip("/") + "/result") if result else {}
            if body.get("status") in {"Failed", "Cancelled"}:
                raise Rejected("Операция Fabric завершилась с ошибкой.")
            if body.get("status") not in {"Running", "NotStarted"}:
                raise Indeterminate("Неизвестное состояние операции Fabric.")
        raise Indeterminate(f"Истекло время ожидания. Проверьте операцию: {location}")

    @staticmethod
    def operation_url(headers):
        """Poll by ID on the documented Fabric endpoint, including regional LROs."""
        operation_id = headers.get("x-ms-operation-id")
        if operation_id:
            try:
                return FABRIC + "operations/" + str(UUID(operation_id))
            except ValueError as exc:
                raise Indeterminate("API вернул некорректный ID операции Fabric.") from exc
        location = headers.get("location", "")
        parsed = urlparse(location)
        if (parsed.scheme == "https" and parsed.netloc == urlparse(FABRIC).netloc
                and not parsed.query and not parsed.fragment and parsed.path.startswith("/v1/operations/")):
            return location.rstrip("/")
        # Regional Location is emitted by Fabric. Extract only its operation ID;
        # credentials are always sent to api.fabric.microsoft.com.
        if (parsed.scheme == "https" and parsed.hostname and parsed.netloc == parsed.hostname
                and re.fullmatch(r"wabi-[a-z0-9-]+-redirect\.analysis\.windows\.net", parsed.hostname)
                and not parsed.query and not parsed.fragment):
            match = re.fullmatch(r"/v1/operations/([0-9a-fA-F-]{36})/?", parsed.path)
            if match:
                try:
                    return FABRIC + "operations/" + str(UUID(match[1]))
                except ValueError:
                    pass
        raise Indeterminate("API вернул неподдерживаемый адрес операции Fabric.")

    def definition(self, component):
        return self.operation(self.item_path(component) + "/getDefinition", {}, result=True)

    def update(self, component, definition):
        payload = {"definition": definition["definition"]}
        if component["kind"] == "SemanticModel":
            payload["options"] = {"allowPurgeData": False}
        try:
            self.operation(self.item_path(component) + "/updateDefinition", payload)
        except (Rejected, Indeterminate):
            raise
        except Exception as exc:
            raise Indeterminate("Не удалось определить итог запроса обновления.") from exc

    @staticmethod
    def item_path(component):
        collection = "semanticModels" if component["kind"] == "SemanticModel" else "reports"
        return f"workspaces/{component['workspace_id']}/{collection}/{component['id']}"
