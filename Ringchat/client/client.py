"""
RingChat Client.
Implementiert die Client-zu-Leader Kommunikation (A3, A24, A25, A26).
"""

import asyncio
import logging
import argparse
import signal
import sys
import os
import uuid
import time
from typing import Optional, Dict, List, Callable

# Füge Parent-Verzeichnis zum Python-Path hinzu
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import (
    LEADER_DISCOVERY_TIMEOUT,
    get_client_port
)
from common.messages import (
    MessageType,
    create_message,
    serialize_message,
    deserialize_message
)
from server.discovery import ClientDiscovery

# Logging konfigurieren
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


class RingChatClient:
    """
    Client für das RingChat System.

    Implementiert:
    - A3: Kommunikation nur mit Leader
    - A24: Transparenter Leaderwechsel
    - A25: Automatische Leader-Neuentdeckung
    - A26: Nachrichtenpufferung während Leaderwechsel
    - A46: Keine Beteiligung an Election
    """

    def __init__(self, username: str = "Anonymous"):
        self.client_id = str(uuid.uuid4())
        self.username = username

        self._leader_address: Optional[str] = None
        self._leader_port: Optional[int] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

        self._connected = False
        self._running = False
        self._last_pong: float = 0.0
        self._ping_interval: float = 1.0
        self._ping_timeout: float = 3.0
        self._max_missed_pings: int = 3
        self._missed_pings: int = 0
        self._ping_task: Optional[asyncio.Task] = None
        self._pending_messages: List[Dict] = []
        self._message_history: List[Dict] = []
        self._last_sequence: int = 0

        self._receive_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._discovery = ClientDiscovery(self.client_id)

        self._on_message_callback: Optional[Callable[[Dict], None]] = None

    async def connect(self) -> bool:
        """
        Verbindet mit dem Leader (A3, A25).
        Findet den Leader automatisch über Discovery.
        """
        if self._connected:
            return True

        logger.info(f"Client {self.username} verbindet...")

        # Finde Leader über Discovery (A25)
        leader_info = await self._find_leader()
        if not leader_info:
            logger.error("Kein Leader gefunden")
            return False

        self._leader_address = leader_info.get("leader_address")
        self._leader_port = leader_info.get("leader_port")

        # Verbinde mit Leader
        return await self._connect_to_leader()

    async def _find_leader(self) -> Optional[Dict]:
        """Findet den Leader über UDP Multicast Discovery."""
        for attempt in range(3):
            logger.info(f"Suche Leader... (Versuch {attempt + 1}/3)")
            leader_info = await self._discovery.find_leader(LEADER_DISCOVERY_TIMEOUT)

            if leader_info and leader_info.get("leader_address"):
                return leader_info

            await asyncio.sleep(1)

        return None

    async def _connect_to_leader(self) -> bool:
        """Stellt die TCP-Verbindung zum Leader her."""
        if not self._leader_address or not self._leader_port:
            return False

        try:
            logger.info(f"Verbinde mit Leader auf {self._leader_address}:{self._leader_port}")

            self._reader, self._writer = await asyncio.open_connection(
                self._leader_address,
                self._leader_port
            )

            # Sende Connect-Nachricht
            connect_msg = create_message(
                MessageType.CLIENT_CONNECT,
                self.client_id,
                client_id=self.client_id,
                username=self.username
            )
            await self._send_message(connect_msg)

            self._connected = True
            self._running = True
            self._last_pong = time.time()

            # Starte Empfangs-Task
            self._receive_task = asyncio.create_task(self._receive_loop())
            # Starte Ping/Keepalive Task
            if self._ping_task and not self._ping_task.done():
                self._ping_task.cancel()
                try:
                    await self._ping_task
                except asyncio.CancelledError:
                    pass
            self._ping_task = asyncio.create_task(self._ping_loop())

            logger.info("Verbindung hergestellt!")
            return True

        except Exception as e:
            logger.error(f"Verbindungsfehler: {e}")
            return False

    async def disconnect(self):
        """Trennt die Verbindung zum Server."""
        if not self._connected:
            return

        self._running = False
        self._connected = False

        # Sende Disconnect-Nachricht
        if self._writer:
            try:
                disconnect_msg = create_message(
                    MessageType.CLIENT_DISCONNECT,
                    self.client_id,
                    client_id=self.client_id
                )
                await self._send_message(disconnect_msg)
            except Exception:
                pass

        # Stoppe Tasks
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
            self._ping_task = None

        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        # Schließe Verbindung
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

        self._reader = None
        self._writer = None

        logger.info("Verbindung getrennt")

    async def send_chat_message(self, content: str) -> bool:
        """
        Sendet eine Chat-Nachricht (A26).
        Puffert die Nachricht falls nicht verbunden.
        """
        message = create_message(
            MessageType.CLIENT_MESSAGE,
            self.client_id,
            client_id=self.client_id,
            username=self.username,
            content=content,
            message_id=str(uuid.uuid4())
        )

        if not self._connected:
            # Puffere Nachricht (A26)
            self._pending_messages.append(message)
            logger.debug(f"Nachricht gepuffert (nicht verbunden)")

            # Starte Reconnect falls nicht bereits aktiv
            if not self._reconnect_task or self._reconnect_task.done():
                self._reconnect_task = asyncio.create_task(self._reconnect_loop())

            return False

        return await self._send_message(message)

    async def _send_message(self, message: Dict) -> bool:
        """Sendet eine Nachricht an den Server."""
        if not self._writer:
            return False

        try:
            data = serialize_message(message)
            length = len(data)
            self._writer.write(length.to_bytes(4, 'big') + data)
            await self._writer.drain()
            return True

        except Exception as e:
            logger.error(f"Sendefehler: {e}")
            await self._handle_disconnect()
            return False

    async def _receive_loop(self):
        """Empfangs-Loop für Server-Nachrichten."""
        try:
            while self._running and self._reader:
                # Lese Nachrichtenlänge
                length_data = await self._reader.read(4)
                if not length_data:
                    break

                msg_length = int.from_bytes(length_data, 'big')
                if msg_length > 65536:
                    break

                # Lese Nachricht
                data = await self._reader.read(msg_length)
                if not data:
                    break

                message = deserialize_message(data)
                await self._handle_message(message)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Empfangsfehler: {e}")
        finally:
            if self._running:
                await self._handle_disconnect()

    async def _handle_message(self, message: Dict):
        """Verarbeitet eine empfangene Nachricht."""
        msg_type = message.get("msg_type")

        if msg_type == MessageType.CHAT_BROADCAST.value:
            # Chat-Nachricht empfangen (A13)
            self._handle_chat_message(message)

        elif msg_type == MessageType.HISTORY_RESPONSE.value:
            # Historie empfangen
            self._handle_history_response(message)

        elif msg_type == MessageType.LEADER_INFO.value:
            # Leader-Info Update (A24)
            await self._handle_leader_info(message)

        elif msg_type == MessageType.MESSAGE_DELIVERED.value:
            logger.debug(f"Nachricht zugestellt: {message.get('message_id')}")

        elif msg_type == MessageType.CLIENT_PING_ACK.value:
            self._last_pong = time.time()
            self._missed_pings = 0

    def _handle_chat_message(self, message: Dict):
        """Verarbeitet eine empfangene Chat-Nachricht."""
        sequence = message.get("sequence_number", 0)

        # Prüfe auf Duplikate (A14)
        if sequence <= self._last_sequence:
            return

        self._last_sequence = sequence

        # Speichere in Historie
        self._message_history.append(message)

        # Zeige Nachricht an
        username = message.get("username", "Unknown")
        content = message.get("content", "")
        timestamp = message.get("timestamp", time.time())

        time_str = time.strftime("%H:%M:%S", time.localtime(timestamp))
        print(f"\r[{time_str}] {username}: {content}")

        # Callback aufrufen falls gesetzt
        if self._on_message_callback:
            self._on_message_callback(message)

        # Prompt wieder anzeigen
        print(f"{self.username}> ", end="", flush=True)

    def _handle_history_response(self, message: Dict):
        """Verarbeitet eine Historie-Antwort."""
        messages = message.get("messages", [])
        logger.info(f"Historie empfangen: {len(messages)} Nachrichten")

        for msg in messages:
            sequence = msg.get("sequence_number", 0)
            if sequence > self._last_sequence:
                self._last_sequence = sequence
                self._message_history.append(msg)

                # Zeige historische Nachricht an
                username = msg.get("username", "Unknown")
                content = msg.get("content", "")
                timestamp = msg.get("timestamp", time.time())
                time_str = time.strftime("%H:%M:%S", time.localtime(timestamp))
                print(f"[{time_str}] {username}: {content}")

    async def _handle_leader_info(self, message: Dict):
        """Verarbeitet Leader-Info Update (A24, A25)."""
        new_leader_address = message.get("leader_address")
        new_leader_port = message.get("leader_port")
        is_available = message.get("is_available", True)

        if not is_available:
            logger.warning("Leader nicht mehr verfügbar - reconnecte...")
            await self._handle_disconnect()
            return

        if (new_leader_address != self._leader_address or
            new_leader_port != self._leader_port):
            logger.info("Leader hat gewechselt - reconnecte...")
            self._leader_address = new_leader_address
            self._leader_port = new_leader_port
            await self._reconnect()

    async def _handle_disconnect(self):
        """Behandelt eine unerwartete Verbindungstrennung (A24)."""
        if not self._running:
            return

        self._connected = False
        logger.warning("Verbindung verloren")

        # Schließe alte Verbindung
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

        self._reader = None
        self._writer = None

        # Starte Reconnect (A24, A25)
        if self._running:
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

        # Stoppe Ping Task
        current = asyncio.current_task()
        if self._ping_task and self._ping_task is not current:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
            self._ping_task = None

    async def _reconnect_loop(self):
        """Versucht automatisch neu zu verbinden (A24, A25)."""
        retry_count = 0
        max_retries = 10

        while self._running and not self._connected and retry_count < max_retries:
            retry_count += 1
            logger.info(f"Reconnect-Versuch {retry_count}/{max_retries}...")

            # Suche neuen Leader (A25)
            leader_info = await self._find_leader()

            if leader_info:
                self._leader_address = leader_info.get("leader_address")
                self._leader_port = leader_info.get("leader_port")

                if await self._connect_to_leader():
                    # Verbunden - sende gepufferte Nachrichten (A26)
                    await self._send_pending_messages()
                    return

            await asyncio.sleep(2)

        if not self._connected:
            logger.error("Reconnect fehlgeschlagen")

    async def _reconnect(self):
        """Verbindet neu mit dem aktuellen Leader."""
        # Schließe alte Verbindung
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

        self._connected = False
        self._reader = None
        self._writer = None

        # Neu verbinden
        if await self._connect_to_leader():
            await self._send_pending_messages()

    async def _send_pending_messages(self):
        """Sendet gepufferte Nachrichten (A26)."""
        if not self._pending_messages:
            return

        logger.info(f"Sende {len(self._pending_messages)} gepufferte Nachrichten")

        messages = self._pending_messages.copy()
        self._pending_messages.clear()

        for message in messages:
            await self._send_message(message)

    def set_message_callback(self, callback: Callable[[Dict], None]):
        """Setzt einen Callback f?r empfangene Nachrichten."""
        self._on_message_callback = callback

    def get_history(self) -> List[Dict]:
        """Gibt die lokale Nachrichtenhistorie zur?ck."""
        return self._message_history.copy()

    def is_connected(self) -> bool:
        """Pr?ft ob der Client verbunden ist."""
        return self._connected

    async def _ping_loop(self):
        """Sendet periodisch Pings, um Leader-Ausfall proaktiv zu erkennen."""
        try:
            while self._running:
                await asyncio.sleep(self._ping_interval)

                if not self._connected:
                    continue

                ping_msg = create_message(
                    MessageType.CLIENT_PING,
                    self.client_id,
                    client_id=self.client_id
                )
                await self._send_message(ping_msg)

                if time.time() - self._last_pong > self._ping_timeout:
                    self._missed_pings += 1
                    if self._missed_pings >= self._max_missed_pings:
                        logger.warning("Leader nicht erreichbar (Ping Timeout) - reconnecte...")
                        await self._handle_disconnect()
                        return
        except asyncio.CancelledError:
            pass


async def interactive_client(username: str):
    """Interaktiver Chat-Client für die Kommandozeile."""
    client = RingChatClient(username=username)

    # Verbinde
    if not await client.connect():
        print("Verbindung fehlgeschlagen!")
        return

    print(f"\n=== RingChat - Verbunden als {username} ===")
    print("Befehle: /quit zum Beenden, /history für Historie")
    print("=" * 45)

    # Input-Loop
    running = True

    async def input_loop():
        nonlocal running
        loop = asyncio.get_event_loop()

        while running and client.is_connected():
            try:
                # Lese Input asynchron
                print(f"{username}> ", end="", flush=True)

                # Unter Windows: Verwende threading für Input
                line = await loop.run_in_executor(None, sys.stdin.readline)
                line = line.strip()

                if not line:
                    continue

                if line == "/quit":
                    running = False
                    break

                elif line == "/history":
                    history = client.get_history()
                    print(f"\n--- Historie ({len(history)} Nachrichten) ---")
                    for msg in history[-10:]:
                        ts = time.strftime("%H:%M:%S", time.localtime(msg.get("timestamp", 0)))
                        print(f"[{ts}] {msg.get('username')}: {msg.get('content')}")
                    print("---")
                    continue

                elif line.startswith("/"):
                    print("Unbekannter Befehl")
                    continue

                # Sende Chat-Nachricht
                await client.send_chat_message(line)

            except EOFError:
                running = False
                break
            except Exception as e:
                logger.debug(f"Input-Fehler: {e}")

    try:
        await input_loop()
    except KeyboardInterrupt:
        pass
    finally:
        await client.disconnect()
        print("\nAuf Wiedersehen!")


def main():
    """Hauptfunktion für den Client."""
    parser = argparse.ArgumentParser(description='RingChat Client')
    parser.add_argument('--username', '-u', default=None, help='Benutzername')
    args = parser.parse_args()

    # Frage nach Benutzername falls nicht angegeben
    username = args.username
    if not username:
        username = input("Benutzername: ").strip()
        if not username:
            username = "Anonymous"

    try:
        asyncio.run(interactive_client(username))
    except KeyboardInterrupt:
        print("\nAbgebrochen")


if __name__ == "__main__":
    main()
