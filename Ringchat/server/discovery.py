"""
UDP-Multicast Discovery für RingChat.
Ermöglicht dynamische Server-Erkennung ohne statische Konfiguration (A6, A7, A8).
"""

import asyncio
import socket
import struct
import json
import logging
from typing import Callable, Optional, Dict, Any

from common.config import (
    MULTICAST_GROUP,
    MULTICAST_PORT,
    DISCOVERY_INTERVAL,
    MESSAGE_BUFFER_SIZE,
    get_client_port
)
from common.messages import MessageType, create_message, serialize_message, deserialize_message

logger = logging.getLogger(__name__)


class DiscoveryProtocol(asyncio.DatagramProtocol):
    """Asyncio Protocol für UDP Multicast Discovery."""

    def __init__(self, server_id: str, on_discovery: Callable[[Dict, tuple], None]):
        self.server_id = server_id
        self.on_discovery = on_discovery
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple):
        try:
            message = deserialize_message(data)
            # Ignoriere eigene Nachrichten
            if message.get("sender_id") != self.server_id:
                self.on_discovery(message, addr)
        except Exception as e:
            logger.debug(f"Discovery: Ungültige Nachricht von {addr}: {e}")

    def error_received(self, exc):
        logger.error(f"Discovery Protocol Error: {exc}")


class DiscoveryService:
    """Service für dynamische Server-Erkennung via UDP Multicast."""

    def __init__(
        self,
        server_id: str,
        server_address: str,
        server_port: int,
        on_server_discovered: Callable[[Dict, tuple], None],
        get_leader_info: Callable[[], tuple]
    ):
        self.server_id = server_id
        self.server_address = server_address
        self.server_port = server_port
        self.on_server_discovered = on_server_discovered
        self.get_leader_info = get_leader_info

        self._running = False
        self._send_socket: Optional[socket.socket] = None
        self._recv_transport = None
        self._recv_protocol = None
        self._announce_task: Optional[asyncio.Task] = None

    async def start(self):
        """Startet den Discovery Service."""
        if self._running:
            return

        self._running = True
        logger.info(f"Discovery Service startet auf {MULTICAST_GROUP}:{MULTICAST_PORT}")

        # Setup Multicast Receive Socket
        await self._setup_receive_socket()

        # Setup Send Socket
        self._setup_send_socket()

        # Starte periodische Announcements
        self._announce_task = asyncio.create_task(self._announce_loop())

        # Initiales Announcement
        await self._send_announcement()

    async def stop(self):
        """Stoppt den Discovery Service."""
        self._running = False

        if self._announce_task:
            self._announce_task.cancel()
            try:
                await self._announce_task
            except asyncio.CancelledError:
                pass

        if self._recv_transport:
            self._recv_transport.close()

        if self._send_socket:
            self._send_socket.close()

        logger.info("Discovery Service gestoppt")

    def _setup_send_socket(self):
        """Konfiguriert den UDP Send Socket für Multicast."""
        self._send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._send_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        # Erlaube mehrere Sockets auf gleichem Port
        self._send_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    async def _setup_receive_socket(self):
        """Konfiguriert den UDP Receive Socket für Multicast."""
        loop = asyncio.get_event_loop()

        # Erstelle UDP Socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Unter Windows: Binde an INADDR_ANY
        sock.bind(('', MULTICAST_PORT))

        # Multicast-Gruppe beitreten
        mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        sock.setblocking(False)

        # Erstelle asyncio Transport/Protocol
        self._recv_transport, self._recv_protocol = await loop.create_datagram_endpoint(
            lambda: DiscoveryProtocol(self.server_id, self._handle_discovery),
            sock=sock
        )

    def _handle_discovery(self, message: Dict, addr: tuple):
        """Verarbeitet empfangene Discovery-Nachrichten."""
        msg_type = message.get("msg_type")
        server_port = message.get("server_port")
        server_address = message.get("server_address")

        if msg_type == MessageType.DISCOVERY_ANNOUNCE.value:
            # Client-Anfrage erkennen: server_port fehlt/ist 0 -> keine Server-Entdeckung
            is_client_probe = not server_port or server_port == 0 or not server_address

            if not is_client_probe:
                # Anderer Server wurde entdeckt
                logger.debug(f"Server entdeckt: {message.get('sender_id')} von {addr}")
                self.on_server_discovered(message, addr)

            # Antwort senden
            asyncio.create_task(self._send_response(message, addr))

        elif msg_type == MessageType.DISCOVERY_RESPONSE.value:
            # Antwort auf eigenes Announcement
            logger.debug(f"Discovery Response von: {message.get('sender_id')}")
            self.on_server_discovered(message, addr)

    async def _send_announcement(self):
        """Sendet ein Discovery Announcement."""
        if not self._send_socket:
            return

        leader_id, leader_address, leader_port = self.get_leader_info()

        # Client-Port berechnen (Server-Port + 100)
        client_port = get_client_port(leader_port) if leader_port else None

        message = create_message(
            MessageType.DISCOVERY_ANNOUNCE,
            self.server_id,
            server_address=self.server_address,
            server_port=self.server_port,
            is_leader=(leader_id == self.server_id),
            leader_id=leader_id,
            leader_address=leader_address,
            leader_port=client_port  # Client-Port für Clients
        )

        try:
            data = serialize_message(message)
            self._send_socket.sendto(data, (MULTICAST_GROUP, MULTICAST_PORT))
            logger.debug(f"Discovery Announcement gesendet")
        except Exception as e:
            logger.error(f"Fehler beim Senden des Announcements: {e}")

    async def _send_response(self, original_message: Dict, addr: tuple):
        """Sendet eine Discovery Response."""
        if not self._send_socket:
            return

        leader_id, leader_address, leader_port = self.get_leader_info()

        # Client-Port berechnen (Server-Port + 100)
        client_port = get_client_port(leader_port) if leader_port else None

        message = create_message(
            MessageType.DISCOVERY_RESPONSE,
            self.server_id,
            server_address=self.server_address,
            server_port=self.server_port,
            is_leader=(leader_id == self.server_id),
            leader_id=leader_id,
            leader_address=leader_address,
            leader_port=client_port  # Client-Port für Clients
        )

        try:
            data = serialize_message(message)

            # Bestimme Zieladresse/-port: Fallback auf Absender, wenn Felder fehlen/ungültig
            target_addr = original_message.get("server_address") or addr[0]
            target_port = original_message.get("server_port") or addr[1]

            # Wenn die Zieladresse leer oder ungültig ist, nicht senden
            if not target_addr or target_addr == "":
                logger.debug("Discovery Response übersprungen: ungültige Zieladresse")
                return

            # Clients erwarten Response auf Multicast-Port, Server auf ihren Port
            port = MULTICAST_PORT if not target_port or target_port == 0 else target_port
            self._send_socket.sendto(data, (target_addr, port))
        except Exception as e:
            logger.error(f"Fehler beim Senden der Response: {e}")

    async def _announce_loop(self):
        """Periodische Discovery Announcements."""
        while self._running:
            try:
                await asyncio.sleep(DISCOVERY_INTERVAL)
                if self._running:
                    await self._send_announcement()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fehler im Announce Loop: {e}")

    async def request_leader_info(self) -> Optional[Dict]:
        """Sendet eine Discovery-Anfrage um Leader-Info zu erhalten (A25)."""
        await self._send_announcement()
        # Die Antworten werden über den Callback verarbeitet
        return None


class ClientDiscovery:
    """Discovery Service für Clients um den Leader zu finden (A25)."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        self._socket: Optional[socket.socket] = None
        self._running = False

    async def find_leader(self, timeout: float = 2.0) -> Optional[Dict]:
        """Findet den aktuellen Leader über Multicast Discovery."""
        logger.info("Suche nach Leader...")

        try:
            # Setup Socket - binde an Multicast-Port um Antworten zu empfangen
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # Unter Windows: SO_REUSEADDR vor bind
            if hasattr(socket, 'SO_REUSEPORT'):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)

            # Binde an Multicast-Port
            sock.bind(('', MULTICAST_PORT))

            # Multicast-Gruppe beitreten
            mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

            # Multicast TTL setzen
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

            # Timeout setzen
            sock.settimeout(0.5)  # Kurzer Timeout für non-blocking

            # Discovery Request senden
            message = create_message(
                MessageType.DISCOVERY_ANNOUNCE,
                self.client_id,
                server_address="",
                server_port=0,
                is_leader=False,
                leader_id=None,
                leader_address=None,
                leader_port=None
            )

            data = serialize_message(message)
            sock.sendto(data, (MULTICAST_GROUP, MULTICAST_PORT))
            logger.debug("Discovery-Anfrage gesendet")

            # Auf Antworten warten
            leader_info = None
            import time
            end_time = time.time() + timeout

            while time.time() < end_time:
                try:
                    response_data, addr = sock.recvfrom(MESSAGE_BUFFER_SIZE)
                    response = deserialize_message(response_data)

                    # Ignoriere eigene Nachrichten
                    if response.get("sender_id") == self.client_id:
                        continue

                    logger.debug(f"Antwort von {addr}: {response.get('sender_id')}, is_leader={response.get('is_leader')}")

                    # Prüfe auf Leader-Info
                    if response.get("is_leader") or response.get("leader_id"):
                        leader_info = {
                            "leader_id": response.get("leader_id") or response.get("sender_id"),
                            "leader_address": response.get("leader_address") or response.get("server_address"),
                            "leader_port": response.get("leader_port") or get_client_port(response.get("server_port")) if response.get("server_port") else None
                        }
                        if leader_info["leader_id"] and leader_info["leader_address"] and leader_info["leader_port"]:
                            logger.info(f"Leader gefunden: {leader_info['leader_id']} auf {leader_info['leader_address']}:{leader_info['leader_port']}")
                            break
                except socket.timeout:
                    # Sende erneut
                    sock.sendto(data, (MULTICAST_GROUP, MULTICAST_PORT))
                    continue
                except Exception as e:
                    logger.debug(f"Fehler beim Empfangen: {e}")
                    continue

            return leader_info

        except Exception as e:
            logger.error(f"Fehler bei Leader-Suche: {e}")
            return None
        finally:
            try:
                sock.close()
            except:
                pass
