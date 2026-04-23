"""UDP discovery helpers for IDS CAN-to-Ethernet bridges.

The bridge advertises JSON payloads over UDP containing name/manufacturer/product
fields and a TCP port used for the data channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from dataclasses import dataclass

from ..const import DEFAULT_ETH_PORT, ETH_DISCOVERY_UDP_PORTS

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BridgeDiscoveryResult:
    """Discovered CAN-to-Ethernet bridge details."""

    name: str
    host: str
    port: int


def _normalize_payload_keys(payload: dict[str, object]) -> dict[str, object]:
    """Return a case-insensitive dict view using lowercase keys."""
    return {str(k).strip().lower(): v for k, v in payload.items()}


def _is_supported_bridge(payload: dict[str, object]) -> bool:
    normalized = _normalize_payload_keys(payload)
    mfg = str(normalized.get("mfg", "")).strip().upper()
    product = str(normalized.get("product", "")).strip().upper()
    return mfg == "IDS" or product == "CAN_TO_ETHERNET_GATEWAY"


def _parse_port(payload: dict[str, object]) -> int:
    normalized = _normalize_payload_keys(payload)
    raw = normalized.get("port", DEFAULT_ETH_PORT)
    if isinstance(raw, bool):
        return DEFAULT_ETH_PORT
    if isinstance(raw, int):
        port = raw
    elif isinstance(raw, str):
        try:
            port = int(raw)
        except ValueError:
            return DEFAULT_ETH_PORT
    else:
        return DEFAULT_ETH_PORT
    if 1 <= port <= 65535:
        return port
    return DEFAULT_ETH_PORT


async def discover_can_ethernet_bridges(
    timeout_s: float,
    listen_ports: tuple[int, ...] | None = None,
) -> list[BridgeDiscoveryResult]:
    """Listen for UDP bridge advertisements for a limited time window."""
    ports = listen_ports or ETH_DISCOVERY_UDP_PORTS
    socks: list[socket.socket] = []
    try:
        for port in ports:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", port))
            sock.setblocking(False)
            socks.append(sock)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        discovered: dict[tuple[str, int], BridgeDiscoveryResult] = {}

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break

            task_ports: dict[asyncio.Task[tuple[bytes, tuple[str, int]]], int] = {
                loop.create_task(loop.sock_recvfrom(sock, 2048)): port
                for sock, port in zip(socks, ports)
            }
            pending: set[asyncio.Task[tuple[bytes, tuple[str, int]]]] = set()
            try:
                done, pending = await asyncio.wait(
                    task_ports.keys(),
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in pending:
                    task.cancel()

            if not done:
                break

            completed_task = next(iter(done))
            listen_port = task_ports.get(completed_task, -1)
            try:
                data, remote = completed_task.result()
            except OSError as exc:
                _LOGGER.debug("Ethernet discovery receive error: %s", exc)
                continue

            try:
                payload = json.loads(data.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if not _is_supported_bridge(payload):
                continue

            host = remote[0]
            port = _parse_port(payload)
            normalized = _normalize_payload_keys(payload)
            name = str(normalized.get("name", "CAN Bridge")).strip() or "CAN Bridge"
            _LOGGER.debug(
                "Bridge discovery packet accepted from %s:%d via UDP listen port %d (name=%s, bridge_port=%d)",
                remote[0],
                remote[1],
                listen_port,
                name,
                port,
            )
            key = (host, port)
            discovered[key] = BridgeDiscoveryResult(name=name, host=host, port=port)

        return sorted(discovered.values(), key=lambda item: (item.name.lower(), item.host))
    finally:
        for sock in socks:
            sock.close()
