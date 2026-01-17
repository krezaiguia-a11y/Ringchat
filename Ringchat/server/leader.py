"""
Leader-spezifisches Verhalten für RingChat.
Implementiert die zentrale Nachrichtenordnung und Client-Kommunikation (A3, A12, A13).
"""

import asyncio
import logging
import uuid
import time
from typing import Callable, Optional, Dict, List, Any

from common.config import get_client_port, MAX_PENDING_MESSAGES
from common.messages import MessageType, create_message, serialize_message, deserialize_message
from server.state import ChatEntry

logger = logging.getLogger(__name__)


class LeaderService:
    """
    Service für Leader-spezifische Funktionen.

    Der Leader ist verantwortlich für:
    - Empfang von Client-Nachrichten (A3)
    - Festlegung der globalen Nachrichtenreihenfolge (A12)
    - Broadcast an alle Clients (A13)
    - Initiierung der Replikation an Follower (A17)
    """

    def __init__(
        self,
        server_id: str,
        server_address: str,
        server_port: int,
        get_next_sequence: Callable[[], int],
        add_message: Callable[[ChatEntry], bool],
        is_message_delivered: Callable[[str], bool],
        replicate_message: Callable[[Dict], asyncio.coroutine],
        get_history: Callable[[int], List[ChatEntry]],
        get_connected_clients: Callable[[], Dict[str, Dict]]
    ):
        self.server_id = server_id
        self.server_address = server_address
        self.server_port = server_port
        self.get_next_sequence = get_next_sequence
        self.add_message = add_message
        self.is_message_delivered = is_message_delivered
        self.replicate_message = replicate_message
        self.get_history = get_history
        self.get_connected_clients = get_connected_clients

        self._is_active = False
        self._client_connections: Dict[str, asyncio.StreamWriter] = {}
        self._pending_client_messages: List[Dict] = []
        self._client_server: Optional[asyncio.Server] = None
        self._message_lock = asyncio.Lock()

    @property
    def client_port(self) -> int:
        """Port für Client-Verbindungen."""
        return get_client_port(self.server_port)

    async def activate(self):
        """Aktiviert den Leader-Service."""
        if self._is_active:
            return

        self._is_active = True
        logger.info(f"Leader Service aktiviert auf Port {self.client_port}")

        # Starte Client-Server
        await self._start_client_server()

        # Verarbeite gepufferte Nachrichten (A26)
        await self._process_pending_messages()

    async def deactivate(self):
        """Deaktiviert den Leader-Service."""
        self._is_active = False

        # Stoppe Client-Server
        if self._client_server:
            self._client_server.close()
            await self._client_server.wait_closed()
            self._client_server = None

        # Schließe alle Client-Verbindungen
        for writer in self._client_connections.values():
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        self._client_connections.clear()

        logger.info("Leader Service deaktiviert")

    async def _start_client_server(self):
        """Startet den Server für Client-Verbindungen."""
        try:
            self._client_server = await asyncio.start_server(
                self._handle_client_connection,
                self.server_address,
                self.client_port
            )
            logger.info(f"Client-Server gestartet auf {self.server_address}:{self.client_port}")
        except Exception as e:
            logger.error(f"Fehler beim Starten des Client-Servers: {e}")

    async def _handle_client_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter
    ):
        """Verarbeitet eine neue Client-Verbindung."""
        addr = writer.get_extra_info('peername')
        client_id = None

        try:
            logger.info(f"Neue Client-Verbindung von {addr}")

            while self._is_active:
                # Lese Nachrichtenlänge (4 Bytes)
                length_data = await reader.read(4)
                if not length_data:
                    break

                msg_length = int.from_bytes(length_data, 'big')
                if msg_length > 65536:
                    logger.warning(f"Nachricht zu groß: {msg_length}")
                    break

                # Lese Nachricht
                data = await reader.read(msg_length)
                if not data:
                    break

                message = deserialize_message(data)
                msg_type = message.get("msg_type")

                if msg_type == MessageType.CLIENT_CONNECT.value:
                    client_id = message.get("client_id")
                    username = message.get("username", "Anonymous")
                    self._client_connections[client_id] = writer
                    logger.info(f"Client verbunden: {username} ({client_id})")

                    # Sende Historie an Client
                    await self._send_history_to_client(client_id, writer)

                elif msg_type == MessageType.CLIENT_MESSAGE.value:
                    await self._process_client_message(message)

                elif msg_type == MessageType.CLIENT_DISCONNECT.value:
                    logger.info(f"Client trennt Verbindung: {client_id}")
                    break

                elif msg_type == MessageType.HISTORY_REQUEST.value:
                    from_seq = message.get("from_sequence", 0)
                    await self._send_history_to_client(client_id, writer, from_seq)

                elif msg_type == MessageType.CLIENT_PING.value:
                    await self._handle_client_ping(writer)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Fehler bei Client-Verbindung {addr}: {e}")
        finally:
            if client_id and client_id in self._client_connections:
                del self._client_connections[client_id]
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info(f"Client-Verbindung geschlossen: {addr}")

    async def _process_client_message(self, message: Dict):
        """
        Verarbeitet eine Client-Nachricht (A12, A13, A14).
        Legt Reihenfolge fest, repliziert und broadcastet.
        """
        async with self._message_lock:
            message_id = message.get("message_id") or str(uuid.uuid4())
            client_id = message.get("client_id", "")
            username = message.get("username", "Anonymous")
            content = message.get("content", "")

            # Prüfe auf Duplikate (A14)
            if self.is_message_delivered(message_id):
                logger.debug(f"Duplikat ignoriert: {message_id}")
                return

            # Vergebe globale Sequenznummer (A12)
            sequence = self.get_next_sequence()

            # Erstelle Chat-Entry
            entry = ChatEntry(
                message_id=message_id,
                sequence_number=sequence,
                client_id=client_id,
                username=username,
                content=content,
                timestamp=time.time(),
                delivered=True
            )

            # Speichere lokal
            if not self.add_message(entry):
                logger.warning(f"Nachricht konnte nicht gespeichert werden: {message_id}")
                return

            logger.info(f"[{sequence}] {username}: {content}")

            # Repliziere an andere Server (A17)
            entry_dict = entry.to_dict()
            asyncio.create_task(self.replicate_message(entry_dict))

            # Broadcast an alle Clients (A13)
            await self._broadcast_to_clients(entry_dict)

    async def _broadcast_to_clients(self, message: Dict):
        """Sendet eine Nachricht an alle verbundenen Clients (A13)."""
        broadcast_msg = create_message(
            MessageType.CHAT_BROADCAST,
            self.server_id,
            **message
        )
        data = serialize_message(broadcast_msg)

        # Bereite Daten für Senden vor
        length = len(data)
        send_data = length.to_bytes(4, 'big') + data

        # Sende an alle Clients
        disconnected = []
        for client_id, writer in self._client_connections.items():
            try:
                writer.write(send_data)
                await writer.drain()
            except Exception as e:
                logger.debug(f"Broadcast an {client_id} fehlgeschlagen: {e}")
                disconnected.append(client_id)

        # Entferne getrennte Clients
        for client_id in disconnected:
            if client_id in self._client_connections:
                del self._client_connections[client_id]

    async def _send_history_to_client(
        self,
        client_id: str,
        writer: asyncio.StreamWriter,
        from_sequence: int = 0
    ):
        """Sendet die Chat-Historie an einen Client."""
        history = self.get_history(from_sequence)

        response = create_message(
            MessageType.HISTORY_RESPONSE,
            self.server_id,
            messages=[e.to_dict() for e in history],
            from_sequence=from_sequence
        )
        data = serialize_message(response)

        length = len(data)
        send_data = length.to_bytes(4, 'big') + data

        try:
            writer.write(send_data)
            await writer.drain()
            logger.debug(f"Historie gesendet an {client_id}: {len(history)} Nachrichten")
        except Exception as e:
            logger.error(f"Fehler beim Senden der Historie: {e}")

    def buffer_message(self, message: Dict):
        """Puffert eine Nachricht während Leaderwechsel (A26)."""
        if len(self._pending_client_messages) < MAX_PENDING_MESSAGES:
            self._pending_client_messages.append(message)
            logger.debug(f"Nachricht gepuffert: {message.get('message_id')}")

    async def _process_pending_messages(self):
        """Verarbeitet gepufferte Nachrichten nach Leaderwechsel (A26)."""
        if not self._pending_client_messages:
            return

        logger.info(f"Verarbeite {len(self._pending_client_messages)} gepufferte Nachrichten")

        messages = self._pending_client_messages.copy()
        self._pending_client_messages.clear()

        for message in messages:
            await self._process_client_message(message)

    async def send_leader_info_to_client(self, writer: asyncio.StreamWriter):
        """Sendet Leader-Info an einen Client."""
        message = create_message(
            MessageType.LEADER_INFO,
            self.server_id,
            leader_id=self.server_id,
            leader_address=self.server_address,
            leader_port=self.client_port,
            is_available=self._is_active
        )
        data = serialize_message(message)

        length = len(data)
        send_data = length.to_bytes(4, 'big') + data

        try:
            writer.write(send_data)
            await writer.drain()
        except Exception as e:
            logger.error(f"Fehler beim Senden der Leader-Info: {e}")

    async def _handle_client_ping(self, writer: asyncio.StreamWriter):
        """Antwortet auf einen Client-Ping zur Verbindungsprグfung."""
        response = create_message(
            MessageType.CLIENT_PING_ACK,
            self.server_id,
            leader_id=self.server_id,
            leader_address=self.server_address,
            leader_port=self.client_port,
            is_available=self._is_active
        )
        data = serialize_message(response)
        length = len(data)
        send_data = length.to_bytes(4, 'big') + data

        try:
            writer.write(send_data)
            await writer.drain()
        except Exception as e:
            logger.debug(f"Fehler beim Senden des Ping-Acks: {e}")

    def get_client_count(self) -> int:
        """Gibt die Anzahl verbundener Clients zurück."""
        return len(self._client_connections)

    def is_active(self) -> bool:
        """Prüft ob der Leader-Service aktiv ist."""
        return self._is_active
