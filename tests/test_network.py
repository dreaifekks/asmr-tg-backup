from __future__ import annotations

import os
import unittest
from unittest import mock

from ytb_tg_backup.extension_api import (
    HttpRequest,
    HttpResponse,
    RouteLease,
    RouteRequest,
)
from ytb_tg_backup.network import ConnectionRuntime, RoutedConnection


class _Policy:
    def __init__(self, lease: RouteLease):
        self.lease = lease
        self.acquired: list[RouteRequest] = []
        self.reported = []

    def acquire(self, request: RouteRequest) -> RouteLease:
        self.acquired.append(request)
        return self.lease

    def report(self, request, lease, outcome) -> None:
        self.reported.append((request, lease, outcome))


class NetworkRuntimeTest(unittest.TestCase):
    def test_one_route_lease_is_held_for_the_whole_operation(self):
        policy = _Policy(
            RouteLease(
                route_id="proxy-a",
                mode="proxy",
                proxy_url="socks5h://user:secret@127.0.0.1:7891",
            )
        )
        runtime = ConnectionRuntime(policy)
        request = RouteRequest(scope="media.download", job_id=9, attempt=2)

        with runtime.route(request) as route:
            self.assertEqual(route.route_id, "proxy-a")
            self.assertEqual(
                route.yt_dlp_args(),
                ["--proxy", "socks5h://user:secret@127.0.0.1:7891"],
            )
            self.assertEqual(len(policy.acquired), 1)

        self.assertEqual(len(policy.acquired), 1)
        self.assertTrue(policy.reported[0][2].success)
        self.assertNotIn("secret", repr(policy.lease))

    def test_operation_failure_is_reported_without_being_swallowed(self):
        policy = _Policy(RouteLease(route_id="direct", mode="direct"))
        runtime = ConnectionRuntime(policy)
        with self.assertRaisesRegex(OSError, "offline"):
            with runtime.route(RouteRequest(scope="media.probe")):
                raise OSError("offline")

        outcome = policy.reported[0][2]
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.error_type, "OSError")

    def test_loopback_target_forces_direct_and_bypasses_plugin_policy(self):
        policy = _Policy(
            RouteLease(
                route_id="remote",
                mode="proxy",
                proxy_url="socks5h://127.0.0.1:7891",
            )
        )
        runtime = ConnectionRuntime(policy)
        with runtime.route(
            RouteRequest(
                scope="telegram.delivery.bot_api",
                target_url="http://127.0.0.1:8081/botTOKEN/sendAudio",
            )
        ) as route:
            self.assertEqual(route.route_id, "loopback")
            self.assertEqual(route.curl_args(), ["--noproxy", "*"])

        self.assertEqual(policy.acquired, [])
        self.assertEqual(policy.reported, [])

    def test_direct_process_environment_removes_all_proxy_variants(self):
        route = RoutedConnection(RouteLease(route_id="direct", mode="direct"))
        environment = {
            "HTTP_PROXY": "http://127.0.0.1:7890",
            "https_proxy": "http://127.0.0.1:7890",
            "ALL_PROXY": "socks5://127.0.0.1:7891",
            "NO_PROXY": "localhost",
            "PATH": "/usr/bin",
        }
        self.assertEqual(route.process_environment(environment), {"PATH": "/usr/bin"})

    def test_request_adds_target_url_and_reports_transport_result(self):
        policy = _Policy(RouteLease(route_id="direct", mode="direct"))
        transport = mock.Mock()
        transport.request.return_value = HttpResponse(
            status=204,
            url="https://example.test/health",
            headers={},
            body=b"",
        )
        runtime = ConnectionRuntime(policy, transport)
        response = runtime.request(
            HttpRequest(url="https://example.test/health"),
            RouteRequest(scope="source.discovery", provider="rss"),
        )

        self.assertEqual(response.status, 204)
        self.assertEqual(policy.acquired[0].target_url, "https://example.test/health")
        transport.request.assert_called_once()
        self.assertTrue(policy.reported[0][2].success)

    def test_telethon_proxy_preserves_remote_dns_choice_and_credentials(self):
        route = RoutedConnection(
            RouteLease(
                route_id="socks",
                mode="proxy",
                proxy_url="socks5h://user:p%40ss@127.0.0.1:7891",
            )
        )
        self.assertEqual(
            route.telethon_proxy(),
            ("socks5", "127.0.0.1", 7891, True, "user", "p@ss"),
        )


if __name__ == "__main__":
    unittest.main()
