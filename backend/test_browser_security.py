import ipaddress
import socket
import unittest

from browser_security import (
    build_host_resolver_rules,
    normalize_domain,
    validate_browser_websocket_url,
    validate_browser_url,
)


def resolver_for(addresses):
    def resolve(host, _port, type):
        assert type == socket.SOCK_STREAM
        values = addresses[host]
        results = []
        for value in values:
            address = ipaddress.ip_address(value)
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            sockaddr = (value, 443, 0, 0) if address.version == 6 else (value, 443)
            results.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return results

    return resolve


class BrowserSecurityTests(unittest.TestCase):
    def test_https_allowlist_normalization_and_pinned_dns_rules(self):
        resolver = resolver_for(
            {
                "example.com": ["93.184.216.34"],
                "www.example.com": ["93.184.216.34"],
            }
        )
        url, addresses = validate_browser_url(
            "https://www.example.com/path?q=1",
            ("example.com", "www.example.com"),
            resolver=resolver,
        )
        self.assertEqual(url, "https://www.example.com/path?q=1")
        self.assertEqual(addresses, ("93.184.216.34",))
        rules = build_host_resolver_rules(
            ("example.com", "www.example.com"), resolver=resolver
        )
        self.assertIn("MAP www.example.com 93.184.216.34", rules)
        self.assertTrue(rules.endswith("MAP * ~NOTFOUND"))
        self.assertEqual(normalize_domain("Example.COM."), "example.com")

    def test_rejects_non_https_credentials_ports_private_addresses_and_redirect_hosts(self):
        public = resolver_for({"example.com": ["93.184.216.34"]})
        invalid_urls = [
            "http://example.com/",
            "https://user:secret@example.com/",
            "https://example.com:8443/",
            "https://127.0.0.1/",
        ]
        for url in invalid_urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_browser_url(url, ("example.com",), resolver=public)
        with self.assertRaisesRegex(ValueError, "allowlist"):
            validate_browser_url(
                "https://attacker.test/",
                ("example.com",),
                resolver=public,
            )
        with self.assertRaisesRegex(ValueError, "restricted network"):
            validate_browser_url(
                "https://example.com/",
                ("example.com",),
                resolver=resolver_for({"example.com": ["127.0.0.1"]}),
            )

    def test_rejects_domains_that_resolve_to_any_restricted_address(self):
        with self.assertRaisesRegex(ValueError, "restricted network"):
            validate_browser_url(
                "https://example.com/",
                ("example.com",),
                resolver=resolver_for(
                    {"example.com": ["93.184.216.34", "169.254.169.254"]}
                ),
            )
        with self.assertRaises(ValueError):
            normalize_domain("127.0.0.1")

    def test_credentials_and_session_tokens_in_query_are_rejected(self):
        for parameter in ("access_token", "client-secret", "session_id", "code", "api_key"):
            with self.subTest(parameter=parameter):
                with self.assertRaisesRegex(ValueError, "credentials or session tokens"):
                    validate_browser_url(
                        f"https://example.com/?{parameter}=private",
                        ("example.com",),
                        resolver=resolver_for(
                            {"example.com": ["93.184.216.34"]}
                        ),
                    )

    def test_websocket_policy_requires_wss_443_allowlisted_public_destination(self):
        public = resolver_for({"example.com": ["93.184.216.34"]})
        url, addresses = validate_browser_websocket_url(
            "wss://example.com/socket",
            ("example.com",),
            resolver=public,
        )
        self.assertEqual(url, "wss://example.com/socket")
        self.assertEqual(addresses, ("93.184.216.34",))
        for invalid in (
            "ws://example.com/socket",
            "wss://example.com:8443/socket",
            "wss://localhost/socket",
            "wss://metadata.google.internal/socket",
        ):
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                validate_browser_websocket_url(
                    invalid,
                    ("example.com",),
                    resolver=public,
                )
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fe80::1"):
            with self.subTest(address=address), self.assertRaisesRegex(
                ValueError, "restricted network"
            ):
                validate_browser_websocket_url(
                    "wss://example.com/socket",
                    ("example.com",),
                    resolver=resolver_for({"example.com": [address]}),
                )
        with self.assertRaisesRegex(ValueError, "restricted network"):
            validate_browser_websocket_url(
                "wss://example.com/socket",
                ("example.com",),
                resolver=resolver_for(
                    {"example.com": ["93.184.216.34", "169.254.169.254"]}
                ),
            )
        with self.assertRaisesRegex(ValueError, "could not be resolved"):
            validate_browser_websocket_url(
                "wss://example.com/socket",
                ("example.com",),
                resolver=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    OSError("DNS failed")
                ),
            )

    def test_rejects_non_public_ipv4_and_ipv6_transition_ranges(self):
        non_public_addresses = (
            "100.64.0.1",
            "192.0.2.1",
            "192.88.99.1",
            "198.18.0.1",
            "198.51.100.1",
            "203.0.113.1",
            "224.0.0.1",
            "240.0.0.1",
            "64:ff9b::127.0.0.1",
            "64:ff9b:1::7f00:1",
            "::ffff:127.0.0.1",
            "2002:7f00:1::",
            "2001:0:7f00:1::",
            "2001:db8::1",
            "2001::1",
            "2001:4860:64::7f00:1",
            "3fff::1",
        )
        for address in non_public_addresses:
            with self.subTest(address=address), self.assertRaisesRegex(
                ValueError, "restricted network"
            ):
                validate_browser_url(
                    "https://example.com/",
                    ("example.com",),
                    resolver=resolver_for({"example.com": [address]}),
                )

        _, addresses = validate_browser_url(
            "https://example.com/",
            ("example.com",),
            resolver=resolver_for({"example.com": ["2606:4700:4700::1111"]}),
        )
        self.assertEqual(addresses, ("2606:4700:4700::1111",))


if __name__ == "__main__":
    unittest.main()
