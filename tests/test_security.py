import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from videosim.gui import GuiHandler, GuiState
from videosim.security import AuthenticationError, AuthorizationError, EndpointPolicyError, SecurityConfig


SECRET = "s" * 32


def security_config(**overrides):
    values = {
        "mode": "trusted-proxy",
        "proxy_shared_secret": SECRET,
        "viewer_group": "videosim-viewer",
        "admin_group": "videosim-admin",
    }
    values.update(overrides)
    return SecurityConfig(**values)


class SecurityConfigTest(unittest.TestCase):
    def test_trusted_proxy_mode_requires_long_random_secret(self):
        with patch.dict(
            "os.environ",
            {"VIDEOSIM_SECURITY_MODE": "trusted-proxy", "VIDEOSIM_PROXY_SHARED_SECRET": "short"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                SecurityConfig.from_env()

    def test_worker_identity_must_match_verified_certificate_header(self):
        config = security_config()
        headers = {
            "X-VideoSim-Proxy-Secret": SECRET,
            "X-VideoSim-Worker-ID": "worker-1",
        }

        principal = config.authenticate_worker(headers, "worker-1")

        self.assertEqual(principal.subject, "worker-1")
        with self.assertRaises(AuthorizationError):
            config.authenticate_worker(headers, "worker-2")
        with self.assertRaises(AuthenticationError):
            config.authenticate_worker({"X-VideoSim-Worker-ID": "worker-1"}, "worker-1")

    def test_operator_groups_enforce_viewer_and_admin_roles(self):
        config = security_config()
        viewer = {
            "X-VideoSim-Proxy-Secret": SECRET,
            "X-VideoSim-User": "viewer@example.com",
            "X-VideoSim-Groups": "videosim-viewer",
        }
        admin = viewer | {
            "X-VideoSim-User": "admin@example.com",
            "X-VideoSim-Groups": "videosim-admin",
            "Origin": "https://videosim.example",
            "X-VideoSim-Expected-Origin": "https://videosim.example",
        }

        self.assertEqual(config.authenticate_operator(viewer, write=False).subject, "viewer@example.com")
        with self.assertRaises(AuthorizationError):
            config.authenticate_operator(viewer, write=True)
        self.assertEqual(config.authenticate_operator(admin, write=True).subject, "admin@example.com")

    def test_secure_endpoint_policy_rejects_private_addresses_by_default(self):
        config = security_config()
        private = [(2, 1, 6, "", ("127.0.0.1", 9000))]

        with patch("videosim.security.socket.getaddrinfo", return_value=private), self.assertRaises(EndpointPolicyError):
            config.validate_external_endpoint("srt://camera.example:9000?mode=caller")

    def test_secure_endpoint_policy_allows_public_or_explicit_suffix(self):
        public = [(2, 1, 6, "", ("93.184.216.34", 9000))]
        private = [(2, 1, 6, "", ("10.1.2.3", 9000))]

        with patch("videosim.security.socket.getaddrinfo", return_value=public):
            security_config().validate_external_endpoint("srt://camera.example:9000?mode=caller")
        with patch("videosim.security.socket.getaddrinfo", return_value=private):
            security_config(allowed_feed_host_suffixes=(".media.example",)).validate_external_endpoint(
                "srt://camera.media.example:9000?mode=caller"
            )

    def test_private_feed_opt_in_never_allows_loopback(self):
        loopback = [(2, 1, 6, "", ("127.0.0.1", 9000))]

        with patch("videosim.security.socket.getaddrinfo", return_value=loopback), self.assertRaises(EndpointPolicyError):
            security_config(allow_private_feed_hosts=True).validate_external_endpoint(
                "srt://camera.media.example:9000?mode=caller"
            )

    def test_secure_endpoint_policy_rejects_inline_credentials(self):
        with self.assertRaises(EndpointPolicyError):
            security_config().validate_external_endpoint("https://user:password@example.com/manifest.mpd")


class SecureProxyApiTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = GuiState(
            monitor_state_path=str(Path(self.directory.name) / "monitor.json"),
            worker_base_url="https://proxy:9443",
            security=security_config(),
        )
        handler = type("SecureProxyHandler", (GuiHandler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(self, path, headers=None, data=None):
        request = Request(self.base_url + path, headers=headers or {}, data=data)
        return urlopen(request, timeout=2)

    def test_health_is_public_but_operator_state_requires_oidc_headers(self):
        with self.request("/healthz") as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as raised:
            self.request("/state.json")
        self.assertEqual(raised.exception.code, 401)
        raised.exception.close()

        headers = {
            "X-VideoSim-Proxy-Secret": SECRET,
            "X-VideoSim-User": "viewer@example.com",
            "X-VideoSim-Groups": "videosim-viewer",
        }
        with self.request("/state.json", headers=headers) as response:
            self.assertEqual(response.status, 200)

    def test_viewer_feed_routes_do_not_mutate_shared_selection(self):
        first = self.state.create_stream(name="First")
        second = self.state.create_stream(name="Second")
        self.state.select_stream(first.id)
        headers = {
            "X-VideoSim-Proxy-Secret": SECRET,
            "X-VideoSim-User": "viewer@example.com",
            "X-VideoSim-Groups": "videosim-viewer",
        }

        with self.request(f"/feeds/{second.id}", headers=headers) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.state.selected_stream_id, first.id)
        with self.request("/", headers=headers) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.state.selected_stream_id, first.id)

    def test_generated_dash_assignment_uses_explicit_https_worker_origin(self):
        stream = self.state.create_stream(name="DASH", protocol="dash")
        stream.process = Mock(pid=123, stdout=[])
        stream.process.poll.return_value = None
        headers = {
            "X-VideoSim-Proxy-Secret": SECRET,
            "X-VideoSim-Worker-ID": "worker-1",
            "X-Forwarded-Proto": "https",
        }

        with self.request("/api/workers/assignments?worker_id=worker-1", headers=headers) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(payload["streams"][0]["endpoint"], "https://proxy:9443/dash/stream-1/manifest.mpd")
        self.assertEqual(payload["streams"][0]["monitorEndpoint"], payload["streams"][0]["endpoint"])

    def test_worker_api_rejects_spoof_and_accepts_matching_mtls_identity(self):
        base_headers = {"X-VideoSim-Proxy-Secret": SECRET}
        with self.assertRaises(HTTPError) as raised:
            self.request(
                "/api/workers/assignments?worker_id=worker-1",
                headers=base_headers | {"X-VideoSim-Worker-ID": "worker-2"},
            )
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

        with patch("builtins.print") as audit:
            with self.request(
                "/api/workers/assignments?worker_id=worker-1",
                headers=base_headers | {"X-VideoSim-Worker-ID": "worker-1"},
            ) as response:
                self.assertEqual(response.status, 200)
        self.assertTrue(any("videosim-audit" in call.args[0] and "worker_auth" in call.args[0] for call in audit.call_args_list))

    def test_viewer_cannot_mutate_but_admin_can_reach_mutation_handler(self):
        viewer = {
            "X-VideoSim-Proxy-Secret": SECRET,
            "X-VideoSim-User": "viewer@example.com",
            "X-VideoSim-Groups": "videosim-viewer",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        with self.assertRaises(HTTPError) as raised:
            self.request("/streams/create", headers=viewer, data=b"name=Denied")
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()
        self.assertEqual(self.state.streams, {})

        admin = viewer | {
            "X-VideoSim-User": "admin@example.com",
            "X-VideoSim-Groups": "videosim-admin",
            "Origin": self.base_url,
            "X-VideoSim-Expected-Origin": self.base_url,
        }
        with self.request("/streams/create", headers=admin, data=b"name=Allowed") as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.state.streams), 1)

    def test_admin_mutation_rejects_missing_or_cross_site_origin(self):
        admin = {
            "X-VideoSim-Proxy-Secret": SECRET,
            "X-VideoSim-User": "admin@example.com",
            "X-VideoSim-Groups": "videosim-admin",
            "X-VideoSim-Expected-Origin": self.base_url,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        for origin in (None, "https://attacker.example"):
            with self.subTest(origin=origin):
                headers = dict(admin)
                if origin:
                    headers["Origin"] = origin
                with self.assertRaises(HTTPError) as raised:
                    self.request("/streams/create", headers=headers, data=b"name=Denied")
                self.assertEqual(raised.exception.code, 403)
                raised.exception.close()
        self.assertEqual(self.state.streams, {})

    def test_request_body_over_one_mib_is_rejected_before_processing(self):
        headers = {
            "X-VideoSim-Proxy-Secret": SECRET,
            "X-VideoSim-User": "admin@example.com",
            "X-VideoSim-Groups": "videosim-admin",
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(1024 * 1024 + 1),
        }
        request = Request(self.base_url + "/streams/create", headers=headers, data=b"")
        request.add_unredirected_header("Content-Length", str(1024 * 1024 + 1))
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 413)
        raised.exception.close()


if __name__ == "__main__":
    unittest.main()
