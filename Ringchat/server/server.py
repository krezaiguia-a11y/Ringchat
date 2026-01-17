"""
Hauptmodul für den RingChat Server.
Koordiniert alle Server-Komponenten und implementiert die Rollenlogik.
"""

import asyncio
import logging
import argparse
import signal
import sys
import os

# Füge Parent-Verzeichnis zum Python-Path hinzu
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Optional, Dict, Any

from common.config import (
    create_server_id,
    SERVER_BASE_PORT,
    get_server_number
)
from common.messages import MessageType, create_message, serialize_message, deserialize_message
from server.state import ServerState, ServerInfo, ChatEntry
from server.discovery import DiscoveryService
from server.heartbeat import HeartbeatService
from server.election import ElectionService
from server.replication import ReplicationService, ReplicationManager
from server.leader import LeaderService

# Logging konfigurieren
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


class RingChatServer:
    """
    Hauptklasse für den RingChat Server.

    Implementiert:
    - A1: Client-Server-Architektur
    - A2: Mehrere Server mit Leader-Rolle
    - A4: Ring-Topologie
    - A5: Nachbarschaftskommunikation
    """

    def __init__(self, server_id: str, address: str = "127.0.0.1", port: int = None):
        # Server-Identität (A34)
        self.server_id = server_id
        self.address = address
        self.port = port or SERVER_BASE_PORT

        # State Management
        self.state = ServerState(self.server_id, self.address, self.port)

        # Netzwerk
        self._server_socket: Optional[asyncio.Server] = None
        self._neighbor_connections: Dict[str, tuple] = {}  # server_id -> (reader, writer)

        # Services werden in start() initialisiert
        self.discovery: Optional[DiscoveryService] = None
        self.heartbeat: Optional[HeartbeatService] = None
        self.election: Optional[ElectionService] = None
        self.replication: Optional[ReplicationService] = None
        self.replication_manager: Optional[ReplicationManager] = None
        self.leader_service: Optional[LeaderService] = None

        self._running = False
        self._shutdown_event = asyncio.Event()
        self._status_task: Optional[asyncio.Task] = None
        self._deactivate_task: Optional[asyncio.Task] = None

    @staticmethod
    def _compare_ids(left: str, right: str) -> int:
        """Vergleicht zwei Server-IDs numerisch, fallback lexikografisch."""
        ln = get_server_number(left)
        rn = get_server_number(right)
        if ln and rn:
            return (ln > rn) - (ln < rn)
        return (left > right) - (left < right)

    async def start(self):
        """Startet den Server und alle Services."""
        if self._running:
            return

        self._running = True
        logger.info(f"=== RingChat Server startet ===")
        logger.info(f"Server-ID: {self.server_id}")
        logger.info(f"Adresse: {self.address}:{self.port}")

        # Initialisiere Services
        self._init_services()

        # Starte Server-Socket für Ring-Kommunikation
        await self._start_server_socket()

        # Starte alle Services
        await self.discovery.start()
        await self.heartbeat.start()

        # Warte kurz auf Discovery
        await asyncio.sleep(2)

        # Prüfe ob wir andere Server gefunden haben
        if self.state.get_server_count() == 1:
            # Wir sind allein - werde Leader
            logger.info("Keine anderen Server gefunden - werde Leader")
            await self._become_leader()
        else:
            # Starte Election
            logger.info(f"{self.state.get_server_count()} Server gefunden - starte Election")
            await self.election.start_election()

        # Starte Replication Manager
        await self.replication_manager.start()

        # Starte Status-Ausgabe alle 5 Sekunden
        self._status_task = asyncio.create_task(self._status_loop())

        logger.info("Server vollständig gestartet")

        # Warte auf Shutdown
        await self._shutdown_event.wait()

    async def stop(self):
        """Stoppt den Server sauber."""
        if not self._running:
            return

        logger.info("Server wird heruntergefahren...")
        self._running = False

        # Stoppe Status-Task
        if self._status_task:
            self._status_task.cancel()
            try:
                await self._status_task
            except asyncio.CancelledError:
                pass

        # Stoppe Services
        if self.leader_service and self.leader_service.is_active():
            if self._deactivate_task and not self._deactivate_task.done():
                await self._deactivate_task
            else:
                await self.leader_service.deactivate()

        if self._deactivate_task and not self._deactivate_task.done():
            try:
                await self._deactivate_task
            except asyncio.CancelledError:
                pass

        if self.replication_manager:
            await self.replication_manager.stop()

        if self.heartbeat:
            await self.heartbeat.stop()

        if self.discovery:
            await self.discovery.stop()

        # Schließe Verbindungen
        for server_id, (reader, writer) in self._neighbor_connections.items():
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        self._neighbor_connections.clear()

        # Stoppe Server-Socket
        if self._server_socket:
            self._server_socket.close()
            await self._server_socket.wait_closed()

        self._shutdown_event.set()
        logger.info("Server gestoppt")

    def _init_services(self):
        """Initialisiert alle Service-Komponenten."""

        # Discovery Service (A6, A7, A8)
        self.discovery = DiscoveryService(
            server_id=self.server_id,
            server_address=self.address,
            server_port=self.port,
            on_server_discovered=self._on_server_discovered,
            get_leader_info=lambda: self.state.get_leader_info()
        )

        # Heartbeat Service (A21, A22, A23)
        self.heartbeat = HeartbeatService(
            server_id=self.server_id,
            get_neighbors=lambda: self.state.get_neighbors(),
            get_leader_info=lambda: self.state.get_leader_info(),
            send_to_neighbor=self._send_to_neighbor,
            on_neighbor_failed=self._on_neighbor_failed,
            on_leader_failed=self._on_leader_failed
        )

        # Election Service (A9, A34-A47)
        self.election = ElectionService(
            server_id=self.server_id,
            server_address=self.address,
            server_port=self.port,
            get_right_neighbor=lambda: self.state.get_neighbors()[1],
            send_to_right=self._send_to_right_neighbor,
            on_leader_elected=self._on_leader_elected,
            get_server_count=lambda: self.state.get_server_count()
        )

        # Replication Service (A16, A17, A18, A19)
        self.replication = ReplicationService(
            server_id=self.server_id,
            get_history=lambda: self.state.get_all_history(),
            set_history=lambda h: self.state.set_history(h),
            add_message=lambda m: self.state.add_message(ChatEntry.from_dict(m)),
            get_current_sequence=lambda: self.state.get_current_sequence(),
            set_sequence=lambda s: self.state.set_sequence(s),
            get_all_servers=lambda: self.state.get_all_servers(),
            send_to_server=self._send_to_server,
            is_leader=lambda: self.state.is_leader
        )

        self.replication_manager = ReplicationManager(self.replication)

        # Leader Service (A3, A12, A13)
        self.leader_service = LeaderService(
            server_id=self.server_id,
            server_address=self.address,
            server_port=self.port,
            get_next_sequence=lambda: self.state.get_next_sequence(),
            add_message=lambda e: self.state.add_message(e),
            is_message_delivered=lambda m: self.state.is_message_delivered(m),
            replicate_message=self.replication_manager.queue_replication,
            get_history=lambda s: self.state.get_history(s),
            get_connected_clients=lambda: self.state.get_connected_clients()
        )

    async def _start_server_socket(self):
        """Startet den Server-Socket für Ring-Kommunikation."""
        self._server_socket = await asyncio.start_server(
            self._handle_server_connection,
            self.address,
            self.port
        )
        logger.info(f"Server-Socket gestartet auf {self.address}:{self.port}")

    async def _handle_server_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter
    ):
        """Verarbeitet eingehende Server-Verbindungen."""
        addr = writer.get_extra_info('peername')
        sender_id = None

        try:
            while self._running:
                # Lese Nachrichtenlänge
                length_data = await reader.read(4)
                if not length_data:
                    break

                msg_length = int.from_bytes(length_data, 'big')
                if msg_length > 65536:
                    break

                # Lese Nachricht
                data = await reader.read(msg_length)
                if not data:
                    break

                message = deserialize_message(data)
                sender_id = message.get("sender_id")
                msg_type = message.get("msg_type")

                # Speichere Verbindung
                if sender_id and sender_id not in self._neighbor_connections:
                    self._neighbor_connections[sender_id] = (reader, writer)

                # Verarbeite Nachricht basierend auf Typ
                response = await self._handle_server_message(message)

                # Sende Antwort wenn vorhanden
                if response:
                    length = len(response)
                    writer.write(length.to_bytes(4, 'big') + response)
                    await writer.drain()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Verbindungsfehler mit {addr}: {e}")
        finally:
            if sender_id and sender_id in self._neighbor_connections:
                del self._neighbor_connections[sender_id]
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_server_message(self, message: Dict) -> Optional[bytes]:
        """Verarbeitet eine Server-zu-Server Nachricht."""
        msg_type = message.get("msg_type")

        # Heartbeat
        if msg_type == MessageType.HEARTBEAT.value:
            return self.heartbeat.handle_heartbeat(message)

        elif msg_type == MessageType.HEARTBEAT_ACK.value:
            self.heartbeat.handle_heartbeat_ack(message)
            return None

        # Election (A37, A38)
        elif msg_type in [
            MessageType.ELECTION_START.value,
            MessageType.ELECTION_VOTE.value,
            MessageType.ELECTION_LEADER.value
        ]:
            await self.election.handle_election_message(message)
            return None

        # Replication
        elif msg_type == MessageType.REPLICATION_SYNC.value:
            return await self.replication.handle_replication_sync(message)

        elif msg_type == MessageType.REPLICATION_ACK.value:
            self.replication.handle_replication_ack(message)
            return None

        elif msg_type == MessageType.SYNC_REQUEST.value:
            return await self.replication.handle_sync_request(message)

        elif msg_type == MessageType.SYNC_RESPONSE.value:
            self.replication.handle_sync_response(message)
            return None

        # Ring Management
        elif msg_type == MessageType.RING_JOIN_REQUEST.value:
            return await self._handle_join_request(message)

        elif msg_type == MessageType.RING_JOIN_ACCEPT.value:
            await self._handle_join_accept(message)
            return None

        return None

    def _on_server_discovered(self, message: Dict, addr: tuple):
        """Callback wenn ein Server entdeckt wird (A6)."""
        sender_id = message.get("sender_id")
        server_address = message.get("server_address")
        server_port = message.get("server_port")

        if sender_id == self.server_id:
            return

        # Prüfe ob Server bereits bekannt
        known_servers = {s.server_id for s in self.state.get_all_servers()}
        if sender_id in known_servers:
            # Aktualisiere Heartbeat
            self.heartbeat.reset_neighbor_timeout(sender_id)
            return

        logger.info(f"Neuer Server entdeckt: {sender_id} auf {server_address}:{server_port}")

        # Füge Server zum Ring hinzu
        new_server = ServerInfo(
            server_id=sender_id,
            address=server_address,
            port=server_port,
            is_leader=message.get("is_leader", False)
        )
        self.state.add_server(new_server)
        self.state._update_neighbors()

        # Verbinde mit neuem Server
        asyncio.create_task(self._connect_to_server(sender_id, server_address, server_port))

        # Prüfe ob Election nötig ist (A47)
        leader_id, _, _ = self.state.get_leader_info()
        if self.election.should_trigger_election_on_join(sender_id, leader_id):
            logger.info(f"Neuer Server {sender_id} hat höhere ID als Leader - starte Election")
            asyncio.create_task(self._trigger_election_after_sync(sender_id))

    async def _trigger_election_after_sync(self, new_server_id: str):
        """Startet Election nach Sync mit neuem Server."""
        await asyncio.sleep(2)  # Warte auf Sync
        await self.election.start_election()

    async def _connect_to_server(self, server_id: str, address: str, port: int):
        """Stellt eine Verbindung zu einem Server her."""
        if server_id in self._neighbor_connections:
            return

        try:
            reader, writer = await asyncio.open_connection(address, port)
            self._neighbor_connections[server_id] = (reader, writer)
            logger.debug(f"Verbindung zu {server_id} hergestellt")

            # Starte Empfangs-Loop
            asyncio.create_task(self._receive_from_server(server_id, reader, writer))

        except Exception as e:
            logger.debug(f"Verbindung zu {server_id} fehlgeschlagen: {e}")

    async def _receive_from_server(
        self,
        server_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter
    ):
        """Empfängt Nachrichten von einem verbundenen Server."""
        try:
            while self._running and server_id in self._neighbor_connections:
                length_data = await reader.read(4)
                if not length_data:
                    break

                msg_length = int.from_bytes(length_data, 'big')
                data = await reader.read(msg_length)
                if not data:
                    break

                message = deserialize_message(data)
                await self._handle_server_message(message)

        except Exception as e:
            logger.debug(f"Verbindungsfehler mit {server_id}: {e}")
        finally:
            if server_id in self._neighbor_connections:
                del self._neighbor_connections[server_id]

    async def _send_to_neighbor(self, server_id: str, data: bytes):
        """Sendet Daten an einen Nachbarn."""
        if server_id not in self._neighbor_connections:
            # Versuche Verbindung herzustellen
            server = self.state.ring.all_servers.get(server_id)
            if server:
                await self._connect_to_server(server_id, server.address, server.port)

        if server_id in self._neighbor_connections:
            _, writer = self._neighbor_connections[server_id]
            length = len(data)
            writer.write(length.to_bytes(4, 'big') + data)
            await writer.drain()

    async def _send_to_right_neighbor(self, data: bytes):
        """Sendet Daten an den rechten Nachbarn (A5, A37)."""
        _, right = self.state.get_neighbors()
        if right:
            await self._send_to_neighbor(right.server_id, data)

    async def _send_to_server(self, server_id: str, data: bytes):
        """Sendet Daten an einen beliebigen Server."""
        await self._send_to_neighbor(server_id, data)

    def _on_neighbor_failed(self, server_id: str):
        """Callback wenn ein Nachbar ausgefallen ist (A11, A28)."""
        logger.warning(f"Nachbar {server_id} ausgefallen - rekonfiguriere Ring")

        # Entferne Server aus Ring
        self.state.remove_server(server_id)
        self.heartbeat.remove_server(server_id)

        # Schließe Verbindung
        if server_id in self._neighbor_connections:
            _, writer = self._neighbor_connections[server_id]
            try:
                writer.close()
            except Exception:
                pass
            del self._neighbor_connections[server_id]

        # Aktualisiere Ring
        self.state._update_neighbors()

    def _on_leader_failed(self):
        """Callback wenn der Leader ausgefallen ist (A10, A23)."""
        logger.warning("Leader ausgefallen - starte Neuwahl")

        # Lösche Leader-Info
        self.state.clear_leader()

        # Laufende Election zurücksetzen
        if self.election:
            self.election.cancel_election()

        # Deaktiviere eigenen Leader-Service falls aktiv
        if self.leader_service.is_active():
            asyncio.create_task(self.leader_service.deactivate())

        # Starte neue Election (A10)
        asyncio.create_task(self.election.start_election())

    def _on_leader_elected(self, leader_id: str, leader_address: str, leader_port: int):
        """Callback wenn ein Leader gewählt wurde (A41, A42)."""
        current_leader_id, _, _ = self.state.get_leader_info()

        # Down-Grade verhindern: akzeptiere nur Leader mit >= aktueller/ eigener ID
        if self._compare_ids(leader_id, self.server_id) < 0:
            logger.info(f"Ignoriere Leader-Announcement von {leader_id} (eigene ID höher) und starte Election")
            asyncio.create_task(self.election.start_election())
            return
        if current_leader_id and self._compare_ids(current_leader_id, leader_id) > 0:
            logger.info(f"Ignoriere Leader-Announcement von {leader_id} (aktueller Leader höher: {current_leader_id})")
            return

        logger.info(f"Leader gewählt: {leader_id}")

        # Speichere Leader-Info
        self.state.set_leader(leader_id, leader_address, leader_port)

        # Aktiviere/Deaktiviere Leader-Service
        if leader_id == self.server_id:
            asyncio.create_task(self._become_leader())
        else:
            if self.leader_service.is_active():
                self._deactivate_task = asyncio.create_task(self.leader_service.deactivate())

            # Synchronisiere mit Leader (A27)
            asyncio.create_task(self.replication.request_full_sync(leader_id))

    async def _become_leader(self):
        """Wird aufgerufen wenn dieser Server Leader wird."""
        # Stelle sicher, dass eine laufende Deaktivierung abgeschlossen ist,
        # bevor wir den Client-Server neu binden.
        if self._deactivate_task and not self._deactivate_task.done():
            try:
                await self._deactivate_task
            except asyncio.CancelledError:
                pass
            finally:
                self._deactivate_task = None

        logger.info("=== Ich bin jetzt der Leader ===")
        self.state.is_leader = True
        await self.leader_service.activate()

    async def _handle_join_request(self, message: Dict) -> bytes:
        """Verarbeitet eine Join-Anfrage (A27)."""
        sender_id = message.get("sender_id")
        logger.info(f"Join-Anfrage von {sender_id}")

        # Akzeptiere den Join
        response = create_message(
            MessageType.RING_JOIN_ACCEPT,
            self.server_id,
            ring_order=self.state.ring.ring_order,
            leader_id=self.state._leader_id,
            leader_address=self.state._leader_address,
            leader_port=self.state._leader_port
        )

        return serialize_message(response)

    async def _handle_join_accept(self, message: Dict):
        """Verarbeitet eine Join-Akzeptierung."""
        logger.info("Ring-Join akzeptiert")

        # Aktualisiere Leader-Info
        leader_id = message.get("leader_id")
        if leader_id:
            self.state.set_leader(
                leader_id,
                message.get("leader_address"),
                message.get("leader_port")
            )

    async def _status_loop(self):
        """Gibt alle 5 Sekunden den Server-Status aus."""
        while self._running:
            try:
                await asyncio.sleep(5)
                if self._running:
                    self._print_status()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fehler in Status Loop: {e}")

    def _print_status(self):
        """Gibt den aktuellen Ring-Status aus."""
        left, right = self.state.get_neighbors()
        role = "LEADER" if self.state.is_leader else "FOLLOWER"

        left_id = left.server_id if left else "---"
        right_id = right.server_id if right else "---"

        print(f"\n{'='*60}")
        print(f"[STATUS] {self.server_id} | Rolle: {role}")
        print(f"[RING]   Links: {left_id} | Rechts: {right_id}")
        print(f"[INFO]   Server im Ring: {self.state.get_server_count()} | Nachrichten: {len(self.state.get_all_history())}")
        print(f"{'='*60}\n")

    def get_status(self) -> Dict:
        """Gibt den aktuellen Server-Status zurück."""
        leader_id, leader_addr, leader_port = self.state.get_leader_info()
        left, right = self.state.get_neighbors()

        return {
            "server_id": self.server_id,
            "address": self.address,
            "port": self.port,
            "is_leader": self.state.is_leader,
            "leader_id": leader_id,
            "server_count": self.state.get_server_count(),
            "message_count": len(self.state.get_all_history()),
            "left_neighbor": left.server_id if left else None,
            "right_neighbor": right.server_id if right else None,
            "client_count": self.leader_service.get_client_count() if self.state.is_leader else 0
        }


async def main():
    """
    Hauptfunktion zum Starten des Servers.

    Verwendung:
        python -m server.server <ID> --port <PORT>

    Beispiele:
        python -m server.server 1 --port 5001
        python -m server.server 2 --port 5002
        python -m server.server 3 -p 5003
    """
    parser = argparse.ArgumentParser(
        description='RingChat Server',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Beispiele:
  python -m server.server 1 --port 5001
  python -m server.server 2 --port 5002
  python -m server.server 3 -p 5003
        '''
    )
    parser.add_argument('id', type=int, help='Server-ID (Zahl, z.B. 1, 2, 3)')
    parser.add_argument('--port', '-p', type=int, required=True, help='Server-Port (z.B. 5001)')
    parser.add_argument('--address', '-a', default='127.0.0.1', help='Server-Adresse (default: 127.0.0.1)')
    args = parser.parse_args()

    # Validierung
    if args.id < 1:
        print("Fehler: Server-ID muss >= 1 sein!")
        sys.exit(1)

    if args.port < 1024 or args.port > 65535:
        print("Fehler: Port muss zwischen 1024 und 65535 liegen!")
        sys.exit(1)

    server_id = create_server_id(args.id)
    port = args.port

    print(f"\n{'='*50}")
    print(f"  RingChat Server startet...")
    print(f"  ID: {server_id} | Port: {port}")
    print(f"{'='*50}\n")

    server = RingChatServer(server_id=server_id, address=args.address, port=port)

    # Signal-Handler für sauberes Herunterfahren
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("Shutdown-Signal empfangen")
        asyncio.create_task(server.stop())

    # Unter Windows: Verwende alternative Signal-Behandlung
    if sys.platform == 'win32':
        # Unter Windows können wir SIGINT abfangen
        signal.signal(signal.SIGINT, lambda s, f: signal_handler())
    else:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, signal_handler)

    try:
        await server.start()
    except KeyboardInterrupt:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
