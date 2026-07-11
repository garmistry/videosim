from __future__ import annotations

import hmac
import ipaddress
import json
import os
import socket
import time
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse


class AuthenticationError(PermissionError):
    pass


class AuthorizationError(PermissionError):
    pass


class EndpointPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    kind: str
    groups: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SecurityConfig:
    mode: str = "off"
    proxy_shared_secret: str = ""
    proxy_secret_header: str = "X-VideoSim-Proxy-Secret"
    worker_id_header: str = "X-VideoSim-Worker-ID"
    operator_user_header: str = "X-VideoSim-User"
    operator_groups_header: str = "X-VideoSim-Groups"
    expected_origin_header: str = "X-VideoSim-Expected-Origin"
    viewer_group: str = "videosim-viewer"
    admin_group: str = "videosim-admin"
    allow_private_feed_hosts: bool = False
    allowed_feed_host_suffixes: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.mode == "trusted-proxy"

    @classmethod
    def from_env(cls) -> "SecurityConfig":
        mode = os.environ.get("VIDEOSIM_SECURITY_MODE", "off").strip().lower()
        if mode not in {"off", "trusted-proxy"}:
            raise ValueError("VIDEOSIM_SECURITY_MODE must be off or trusted-proxy")
        config = cls(
            mode=mode,
            proxy_shared_secret=os.environ.get("VIDEOSIM_PROXY_SHARED_SECRET", ""),
            viewer_group=os.environ.get("VIDEOSIM_VIEWER_GROUP", "videosim-viewer").strip(),
            admin_group=os.environ.get("VIDEOSIM_ADMIN_GROUP", "videosim-admin").strip(),
            allow_private_feed_hosts=os.environ.get("VIDEOSIM_ALLOW_PRIVATE_FEEDS", "0").strip().lower() in {"1", "true", "yes"},
            allowed_feed_host_suffixes=tuple(
                suffix.strip().lower()
                for suffix in os.environ.get("VIDEOSIM_ALLOWED_FEED_HOST_SUFFIXES", "").split(",")
                if suffix.strip()
            ),
        )
        if config.enabled and len(config.proxy_shared_secret) < 32:
            raise ValueError("VIDEOSIM_PROXY_SHARED_SECRET must be at least 32 characters in trusted-proxy mode")
        if config.enabled and (not config.viewer_group or not config.admin_group):
            raise ValueError("trusted-proxy viewer and admin groups are required")
        return config

    def validate_external_endpoint(self, url: str):
        if not self.enabled:
            return
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            raise EndpointPolicyError("external feed URLs must not contain inline credentials")
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if not hostname:
            raise EndpointPolicyError("external feed hostname is required")
        suffix_allowed = any(
            hostname == suffix.lstrip(".") or hostname.endswith(suffix if suffix.startswith(".") else f".{suffix}")
            for suffix in self.allowed_feed_host_suffixes
        )
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, parsed.port, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError) as exc:
            raise EndpointPolicyError(f"external feed hostname cannot be safely resolved: {hostname}") from exc
        if not addresses:
            raise EndpointPolicyError(f"external feed hostname has no addresses: {hostname}")
        forbidden = [
            address
            for address in addresses
            if address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ]
        if forbidden:
            raise EndpointPolicyError("external feed resolves to a forbidden special-purpose address")
        private = [address for address in addresses if address.is_private]
        if private and not (self.allow_private_feed_hosts or suffix_allowed):
            raise EndpointPolicyError(
                "external feed resolves to a private address; configure an explicit private-feed policy"
            )

    def authenticate_proxy_request(self, headers: Mapping[str, str]) -> Principal:
        if not self.enabled:
            return Principal(subject="local-proxy", kind="proxy")
        self._authenticate_proxy(headers)
        return Principal(subject="trusted-proxy", kind="proxy")

    def authenticate_worker(self, headers: Mapping[str, str], requested_worker_id: str) -> Principal:
        if not self.enabled:
            return Principal(subject=requested_worker_id, kind="worker")
        self._authenticate_proxy(headers)
        worker_id = (headers.get(self.worker_id_header) or "").strip()
        if not worker_id:
            raise AuthenticationError("verified worker identity is required")
        if not hmac.compare_digest(worker_id, requested_worker_id.strip()):
            raise AuthorizationError("verified worker identity does not match workerId")
        return Principal(subject=worker_id, kind="worker")

    def authenticate_operator(self, headers: Mapping[str, str], *, write: bool) -> Principal:
        if not self.enabled:
            return Principal(subject="local-operator", kind="operator", groups=frozenset({self.admin_group, self.viewer_group}))
        self._authenticate_proxy(headers)
        user = (headers.get(self.operator_user_header) or "").strip()
        if not user:
            raise AuthenticationError("OIDC operator identity is required")
        groups = frozenset(
            group.strip()
            for group in (headers.get(self.operator_groups_header) or "").split(",")
            if group.strip()
        )
        required = self.admin_group if write else self.viewer_group
        if required not in groups and self.admin_group not in groups:
            raise AuthorizationError(f"operator requires {required} group")
        if write:
            origin = (headers.get("Origin") or "").rstrip("/")
            expected_origin = (headers.get(self.expected_origin_header) or "").rstrip("/")
            if not origin or not expected_origin or not hmac.compare_digest(origin, expected_origin):
                raise AuthorizationError("operator mutation failed same-origin CSRF validation")
        return Principal(subject=user, kind="operator", groups=groups)

    def _authenticate_proxy(self, headers: Mapping[str, str]):
        supplied = headers.get(self.proxy_secret_header) or ""
        if not supplied or not hmac.compare_digest(supplied, self.proxy_shared_secret):
            raise AuthenticationError("trusted proxy authentication failed")


def audit_event(action: str, outcome: str, principal: Principal | None = None, **fields):
    payload = {
        "time": round(time.time(), 3),
        "action": action,
        "outcome": outcome,
        "subject": principal.subject if principal else "anonymous",
        "principalKind": principal.kind if principal else "unknown",
    }
    payload.update({key: value for key, value in fields.items() if value not in (None, "")})
    print(f"[videosim-audit] {json.dumps(payload, sort_keys=True)}", flush=True)
