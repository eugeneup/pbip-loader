from io import BytesIO
import base64
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from pbip_loader.api import Api, Indeterminate, Rejected
from pbip_loader.auth import BrowserCredential, FABRIC_SCOPE, PBI_SCOPE, find_azure_cli


class Response(BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(json.dumps(body).encode())
        self.status = status
        self.headers = headers or {}


class Credential:
    def token(self, scope):
        return "test-token"


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.api = Api(Credential())

    def test_pagination_reads_every_page(self):
        with patch.object(self.api, "get", side_effect=[
            {"value": [{"id": "a"}], "continuationToken": "next value"}, {"value": [{"id": "b"}]}]) as get:
            rows = self.api.rows("fabric", "workspaces/id/items")
        self.assertEqual(2, len(rows))
        self.assertEqual("workspaces/id/items?continuationToken=next%20value", get.call_args.args[1])

    def test_duplicate_and_repeated_pages_are_errors(self):
        with patch.object(self.api, "get", return_value={"value": [{"id": "same"}, {"id": "SAME"}]}):
            with self.assertRaises(ValueError):
                self.api.rows("fabric", "items")
        with patch.object(self.api, "get", return_value={"value": [], "continuationUri": "items"}):
            with self.assertRaises(ValueError):
                self.api.rows("fabric", "items")

    def test_operation_waits_through_not_started_and_running(self):
        location = "https://api.fabric.microsoft.com/v1/operations/test"
        responses = [(202, {"location": location, "retry-after": "1"}, {}),
                     (200, {}, {"status": "NotStarted"}), (200, {}, {"status": "Running"}),
                     (200, {}, {"status": "Succeeded"}), (200, {}, {"definition": {"parts": []}})]
        with patch.object(self.api, "request", side_effect=responses) as request, patch("pbip_loader.api.time.sleep"):
            self.api.operation("items/getDefinition", {}, result=True)
        self.assertEqual(location + "/result", request.call_args.args[1])

    def test_failed_operation_is_terminal(self):
        with patch.object(self.api, "request", side_effect=[(202, {"location": "https://api.fabric.microsoft.com/v1/operations/test"}, {}),
                         (200, {}, {"status": "Failed"})]), patch("pbip_loader.api.time.sleep"):
            with self.assertRaises(Rejected):
                self.api.operation("items/updateDefinition", {})

    def test_missing_operation_location_is_indeterminate(self):
        with patch.object(self.api, "request", return_value=(202, {}, {})):
            with self.assertRaises(Indeterminate):
                self.api.operation("items/updateDefinition", {})

    def test_forbidden_poll_does_not_prove_original_update_failed(self):
        location = "https://api.fabric.microsoft.com/v1/operations/test"
        with patch.object(self.api, "request", side_effect=[(202, {"location": location}, {}), Rejected("403 polling")]), patch("pbip_loader.api.time.sleep"):
            with self.assertRaisesRegex(Indeterminate, location):
                self.api.operation("items/updateDefinition", {})

    def test_accepted_mutation_location_is_recorded_before_polling(self):
        location = "https://api.fabric.microsoft.com/v1/operations/test"
        recorded = []
        self.api.on_operation = recorded.append
        with patch.object(self.api, "request", side_effect=[(202, {"location": location}, {}), (200, {}, {"status": "Succeeded"})]), patch("pbip_loader.api.time.sleep"):
            self.api.operation("items/updateDefinition", {})
        self.assertEqual([location], recorded)

    def test_foreign_pagination_url_never_receives_token(self):
        with patch.object(self.api.credential, "token") as token:
            with self.assertRaises(ValueError):
                self.api.request("fabric", "https://attacker.example/page")
            token.assert_not_called()

    def test_server_500_and_network_loss_never_retry_mutation(self):
        error = HTTPError("url", 500, "failure", {}, BytesIO(b'{}'))
        for failure in [error, URLError("timeout")]:
            with self.subTest(failure=failure), patch("pbip_loader.api.urlopen", side_effect=failure) as open_url:
                with self.assertRaises(Indeterminate):
                    self.api.request("fabric", "items/updateDefinition", payload={})
                self.assertEqual(1, open_url.call_count)

    def test_terminal_http_rejection_is_not_reported_as_success(self):
        error = HTTPError("url", 403, "forbidden", {}, BytesIO(b'{"errorCode":"Forbidden"}'))
        with patch("pbip_loader.api.urlopen", side_effect=error):
            with self.assertRaisesRegex(Rejected, "403"):
                self.api.request("fabric", "items/updateDefinition", payload={})

    def test_429_honors_retry_after(self):
        error = HTTPError("url", 429, "rate", {"Retry-After": "3"}, BytesIO(b'{}'))
        with patch("pbip_loader.api.urlopen", side_effect=[error, Response({})]), patch("pbip_loader.api.time.sleep") as sleep:
            self.assertEqual(200, self.api.request("fabric", "items", payload={})[0])
        sleep.assert_called_once_with(3)

    def test_update_prohibits_data_purge(self):
        with patch.object(self.api, "operation") as operation:
            self.api.update({"kind": "SemanticModel", "workspace_id": "w", "id": "m"}, {"definition": {"parts": []}})
        self.assertEqual({"allowPurgeData": False}, operation.call_args.args[1]["options"])

    def test_regional_operation_is_polled_on_canonical_fabric_endpoint(self):
        operation_id = "864cf1e1-37ed-4a31-996b-84bbd258c517"
        regional = "https://wabi-east-asia-b-primary-redirect.analysis.windows.net/v1/operations/" + operation_id
        canonical = "https://api.fabric.microsoft.com/v1/operations/" + operation_id
        replies = [Response({}, 202, {"Location": regional}),
                   Response({"status": "Succeeded"}), Response({"definition": {"parts": []}})]
        with patch("pbip_loader.api.urlopen", side_effect=replies) as open_url, patch("pbip_loader.api.time.sleep"):
            result = self.api.definition({"kind": "SemanticModel", "workspace_id": "w", "id": "m"})
        self.assertEqual({"definition": {"parts": []}}, result)
        self.assertEqual([canonical, canonical + "/result"],
                         [call.args[0].full_url for call in open_url.call_args_list[1:]])

    def test_operation_header_works_without_location(self):
        operation_id = "864cf1e1-37ed-4a31-996b-84bbd258c517"
        with patch.object(self.api, "request", side_effect=[
            (202, {"x-ms-operation-id": operation_id}, {}), (200, {}, {"status": "Succeeded"})
        ]) as request, patch("pbip_loader.api.time.sleep"):
            self.api.operation("items/updateDefinition", {})
        self.assertEqual("https://api.fabric.microsoft.com/v1/operations/" + operation_id, request.call_args.args[1])

    def test_regional_url_validation_does_not_accept_foreign_hosts(self):
        for host in ["wabi-east-redirect.analysis.windows.net.evil.example", "evil.example"]:
            with self.subTest(host=host), self.assertRaises(Indeterminate):
                self.api.operation_url({"location": f"https://{host}/v1/operations/864cf1e1-37ed-4a31-996b-84bbd258c517"})

    def test_transient_network_loss_retries_get_and_definition_reads(self):
        for payload, options in [(None, {}), ({}, {"safe_read": True})]:
            with self.subTest(payload=payload), patch("pbip_loader.api.urlopen", side_effect=[URLError("temporary DNS failure"), Response({"ok": True})]) as open_url, patch("pbip_loader.api.time.sleep"):
                reply = self.api.request("fabric", "items/getDefinition", payload=payload, **options)
            self.assertEqual({"ok": True}, reply[2])
            self.assertEqual(2, open_url.call_count)

    def test_transient_503_retries_only_read_requests(self):
        error = HTTPError("url", 503, "temporary failure", {}, BytesIO(b'{}'))
        with patch("pbip_loader.api.urlopen", side_effect=[error, Response({})]) as open_url, patch("pbip_loader.api.time.sleep"):
            self.api.get("pbi", "groups")
        self.assertEqual(2, open_url.call_count)

    def test_exhausted_read_retries_report_network_reason(self):
        with patch("pbip_loader.api.urlopen", side_effect=URLError("DNS unavailable")) as open_url, patch("pbip_loader.api.time.sleep"):
            with self.assertRaisesRegex(Rejected, "DNS unavailable"):
                self.api.get("pbi", "groups")
        self.assertEqual(4, open_url.call_count)

    def test_unknown_update_error_is_ambiguous(self):
        with patch.object(self.api, "operation", side_effect=ValueError("malformed reply")):
            with self.assertRaises(Indeterminate):
                self.api.update({"kind": "Report", "workspace_id": "w", "id": "r"}, {"definition": {"parts": []}})


class AuthTests(unittest.TestCase):
    @staticmethod
    def token_response(user="user", organization="org"):
        claims = base64.urlsafe_b64encode(json.dumps({"tid": organization, "oid": user, "exp": time.time() + 3600}).encode()).decode().rstrip("=")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"accessToken": "header." + claims + ".signature"}))

    def test_missing_azure_cli_has_actionable_error(self):
        with patch("pbip_loader.auth.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Azure CLI"):
                BrowserCredential().token(PBI_SCOPE)

    def test_cli_is_found_in_fresh_windows_path_after_install(self):
        with patch("pbip_loader.auth.shutil.which", side_effect=[None, r"C:\AzureCLI\az.cmd"]) as which, patch("pbip_loader.auth.windows_search_path", return_value=r"C:\AzureCLI"):
            self.assertEqual(r"C:\AzureCLI\az.cmd", find_azure_cli())
        self.assertEqual({"path": r"C:\AzureCLI"}, which.call_args.kwargs)

    def test_existing_cli_does_not_require_reading_windows_settings(self):
        with patch("pbip_loader.auth.shutil.which", return_value="az.cmd"), patch("pbip_loader.auth.windows_search_path") as fresh_path:
            self.assertEqual("az.cmd", find_azure_cli())
        fresh_path.assert_not_called()

    def test_browser_settings_are_per_process_and_do_not_modify_global_config(self):
        with patch("pbip_loader.auth.shutil.which", return_value="az.cmd"), patch("pbip_loader.auth.subprocess.run") as run:
            BrowserCredential().run(["login", "--allow-no-subscriptions"])
        args, kwargs = run.call_args
        self.assertEqual(["az.cmd", "login", "--allow-no-subscriptions"], args[0])
        self.assertEqual("false", kwargs["env"]["AZURE_CORE_ENABLE_BROKER_ON_WINDOWS"])
        self.assertEqual("off", kwargs["env"]["AZURE_CORE_LOGIN_EXPERIENCE_V2"])
        self.assertNotIn("shell", kwargs)

    def test_unknown_resource_is_rejected(self):
        with self.assertRaises(ValueError):
            BrowserCredential().token("https://attacker.example")

    def test_valid_cli_session_is_reused_without_login_and_token_is_cached(self):
        credential = BrowserCredential()
        with patch.object(credential, "run", return_value=self.token_response()) as run:
            first = credential.token(PBI_SCOPE)
            self.assertEqual(first, credential.token(PBI_SCOPE))
        self.assertEqual(1, run.call_count)
        run.assert_called_once_with(["account", "get-access-token", "--resource",
                                     PBI_SCOPE.removesuffix("/.default"), "--output", "json", "--only-show-errors"])
        self.assertEqual("org:user", credential.identity)

    def test_expired_session_opens_browser_then_acquires_token(self):
        notices = []
        credential = BrowserCredential(notify=notices.append)
        with patch.object(credential, "acquire", side_effect=[SimpleNamespace(returncode=1), self.token_response()]), patch.object(credential, "run", return_value=SimpleNamespace(returncode=0)) as run:
            credential.token(PBI_SCOPE)
        run.assert_called_once_with(["login", "--allow-no-subscriptions", "--output", "none", "--only-show-errors"],
                                    timeout=600)
        self.assertTrue(notices)

    def test_cross_resource_account_switch_is_blocked(self):
        for switched in (self.token_response("other"), self.token_response(organization="other")):
            with self.subTest():
                credential = BrowserCredential()
                with patch.object(credential, "acquire", side_effect=[self.token_response(), switched]):
                    credential.token(PBI_SCOPE)
                    with self.assertRaisesRegex(RuntimeError, "Учётная запись изменилась"):
                        credential.token(FABRIC_SCOPE)
