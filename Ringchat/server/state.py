"""
In-Memory State Management für RingChat.
Verwaltet die Chat-Historie und Server-Zustand ausschließlich im RAM.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from collections import OrderedDict

from common.config import get_server_number


@dataclass
class ChatEntry:
    """Ein einzelner Chat-Eintrag."""
    message_id: str
    sequence_number: int
    client_id: str
    username: str
    content: str
    timestamp: float
    delivered: bool = False

    def to_dict(self) -> Dict:
        return {
            "message_id": self.message_id,
            "sequence_number": self.sequence_number,
            "client_id": self.client_id,
            "username": self.username,
            "content": self.content,
            "timestamp": self.timestamp,
            "delivered": self.delivered
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'ChatEntry':
        return cls(**data)


@dataclass
class ServerInfo:
    """Informationen über einen Server im Ring."""
    server_id: str
    address: str
    port: int
    is_leader: bool = False
    last_heartbeat: float = 0.0
    is_alive: bool = True

    def to_dict(self) -> Dict:
        return {
            "server_id": self.server_id,
            "address": self.address,
            "port": self.port,
            "is_leader": self.is_leader,
            "last_heartbeat": self.last_heartbeat,
            "is_alive": self.is_alive
        }


@dataclass
class RingState:
    """Zustand des Server-Rings."""
    left_neighbor: Optional[ServerInfo] = None
    right_neighbor: Optional[ServerInfo] = None
    all_servers: Dict[str, ServerInfo] = field(default_factory=dict)
    ring_order: List[str] = field(default_factory=list)  # Server-IDs in Ring-Reihenfolge


class ServerState:
    """Verwaltet den gesamten Server-Zustand im RAM."""

    def __init__(self, server_id: str, address: str, port: int):
        self.server_id = server_id
        self.address = address
        self.port = port

        # Chat-Historie (A16: ausschließlich im RAM)
        self._chat_history: OrderedDict[str, ChatEntry] = OrderedDict()
        self._sequence_counter: int = 0
        self._delivered_message_ids: Set[str] = set()

        # Ring-Zustand
        self.ring = RingState()
        self.ring.all_servers[server_id] = ServerInfo(
            server_id=server_id,
            address=address,
            port=port
        )

        # Leader-Zustand
        self._is_leader: bool = False
        self._leader_id: Optional[str] = None
        self._leader_address: Optional[str] = None
        self._leader_port: Optional[int] = None

        # Election-Zustand
        self._election_in_progress: bool = False
        self._voted_in_election: bool = False

        # Pending Messages (A26: Pufferung während Leaderwechsel)
        self._pending_messages: List[Dict] = []

        # Connected Clients
        self._connected_clients: Dict[str, Dict] = {}

        # Thread-Sicherheit
        self._lock = threading.RLock()

    # === Chat-Historie Methoden ===

    def add_message(self, entry: ChatEntry) -> bool:
        """Fügt eine Nachricht zur Historie hinzu (A14: keine Duplikate)."""
        with self._lock:
            if entry.message_id in self._delivered_message_ids:
                return False  # Duplikat

            self._chat_history[entry.message_id] = entry
            self._delivered_message_ids.add(entry.message_id)
            return True

    def get_next_sequence(self) -> int:
        """Holt die nächste Sequenznummer (nur Leader, A12)."""
        with self._lock:
            self._sequence_counter += 1
            return self._sequence_counter

    def get_current_sequence(self) -> int:
        """Gibt die aktuelle Sequenznummer zurück."""
        with self._lock:
            return self._sequence_counter

    def set_sequence(self, seq: int):
        """Setzt die Sequenznummer (für Synchronisation)."""
        with self._lock:
            self._sequence_counter = max(self._sequence_counter, seq)

    def get_history(self, from_sequence: int = 0) -> List[ChatEntry]:
        """Holt Chat-Historie ab einer Sequenznummer (A13: totale Ordnung)."""
        with self._lock:
            entries = [
                e for e in self._chat_history.values()
                if e.sequence_number >= from_sequence
            ]
            return sorted(entries, key=lambda x: x.sequence_number)

    def get_all_history(self) -> List[Dict]:
        """Holt die gesamte Historie als Liste von Dicts."""
        with self._lock:
            return [e.to_dict() for e in sorted(
                self._chat_history.values(),
                key=lambda x: x.sequence_number
            )]

    def set_history(self, entries: List[Dict]):
        """Setzt die komplette Historie (für Sync, A27)."""
        with self._lock:
            self._chat_history.clear()
            self._delivered_message_ids.clear()
            self._sequence_counter = 0

            for entry_dict in entries:
                entry = ChatEntry.from_dict(entry_dict)
                self._chat_history[entry.message_id] = entry
                self._delivered_message_ids.add(entry.message_id)
                self._sequence_counter = max(
                    self._sequence_counter,
                    entry.sequence_number
                )

    def is_message_delivered(self, message_id: str) -> bool:
        """Prüft ob eine Nachricht bereits zugestellt wurde."""
        with self._lock:
            return message_id in self._delivered_message_ids

    def clear_history(self):
        """Löscht die komplette Historie (A20: Totalausfall)."""
        with self._lock:
            self._chat_history.clear()
            self._delivered_message_ids.clear()
            self._sequence_counter = 0

    # === Ring-Management Methoden ===

    def add_server(self, server_info: ServerInfo):
        """Fügt einen Server zum Ring hinzu."""
        with self._lock:
            self.ring.all_servers[server_info.server_id] = server_info
            self._update_ring_order()

    def remove_server(self, server_id: str):
        """Entfernt einen Server aus dem Ring (A11: Re-Konfiguration)."""
        with self._lock:
            if server_id in self.ring.all_servers:
                del self.ring.all_servers[server_id]
                self._update_ring_order()
                self._update_neighbors()

    def _update_ring_order(self):
        """Aktualisiert die Ring-Reihenfolge basierend auf Server-IDs."""
        self.ring.ring_order = sorted(
            self.ring.all_servers.keys(),
            key=get_server_number
        )

    def _update_neighbors(self):
        """Aktualisiert die Nachbar-Referenzen im Ring (A5)."""
        if not self.ring.ring_order:
            self.ring.left_neighbor = None
            self.ring.right_neighbor = None
            return

        try:
            my_index = self.ring.ring_order.index(self.server_id)
        except ValueError:
            return

        n = len(self.ring.ring_order)
        if n == 1:
            self.ring.left_neighbor = None
            self.ring.right_neighbor = None
            return

        left_index = (my_index - 1) % n
        right_index = (my_index + 1) % n

        left_id = self.ring.ring_order[left_index]
        right_id = self.ring.ring_order[right_index]

        self.ring.left_neighbor = self.ring.all_servers.get(left_id)
        self.ring.right_neighbor = self.ring.all_servers.get(right_id)

    def get_neighbors(self) -> tuple:
        """Gibt die Nachbarn zurück (left, right)."""
        with self._lock:
            return (self.ring.left_neighbor, self.ring.right_neighbor)

    def get_all_servers(self) -> List[ServerInfo]:
        """Gibt alle bekannten Server zurück."""
        with self._lock:
            return list(self.ring.all_servers.values())

    def get_server_count(self) -> int:
        """Gibt die Anzahl der Server im Ring zurück."""
        with self._lock:
            return len(self.ring.all_servers)

    def update_server_heartbeat(self, server_id: str):
        """Aktualisiert den Heartbeat-Timestamp eines Servers."""
        with self._lock:
            if server_id in self.ring.all_servers:
                self.ring.all_servers[server_id].last_heartbeat = time.time()
                self.ring.all_servers[server_id].is_alive = True

    def mark_server_dead(self, server_id: str):
        """Markiert einen Server als ausgefallen (A22, A28)."""
        with self._lock:
            if server_id in self.ring.all_servers:
                self.ring.all_servers[server_id].is_alive = False

    # === Leader-Management Methoden ===

    @property
    def is_leader(self) -> bool:
        with self._lock:
            return self._is_leader

    @is_leader.setter
    def is_leader(self, value: bool):
        with self._lock:
            self._is_leader = value
            if value:
                self._leader_id = self.server_id
                self._leader_address = self.address
                self._leader_port = self.port
                if self.server_id in self.ring.all_servers:
                    self.ring.all_servers[self.server_id].is_leader = True

    def set_leader(self, leader_id: str, address: str, port: int):
        """Setzt den aktuellen Leader (A2)."""
        with self._lock:
            # Alten Leader entfernen
            for server in self.ring.all_servers.values():
                server.is_leader = False

            self._leader_id = leader_id
            self._leader_address = address
            self._leader_port = port
            self._is_leader = (leader_id == self.server_id)

            if leader_id in self.ring.all_servers:
                self.ring.all_servers[leader_id].is_leader = True

    def get_leader_info(self) -> tuple:
        """Gibt Leader-Informationen zurück."""
        with self._lock:
            return (self._leader_id, self._leader_address, self._leader_port)

    def has_leader(self) -> bool:
        """Prüft ob ein Leader bekannt ist."""
        with self._lock:
            return self._leader_id is not None

    def clear_leader(self):
        """Löscht die Leader-Information (vor Neuwahl)."""
        with self._lock:
            for server in self.ring.all_servers.values():
                server.is_leader = False
            self._leader_id = None
            self._leader_address = None
            self._leader_port = None
            self._is_leader = False

    # === Election-Management ===

    @property
    def election_in_progress(self) -> bool:
        with self._lock:
            return self._election_in_progress

    @election_in_progress.setter
    def election_in_progress(self, value: bool):
        with self._lock:
            self._election_in_progress = value
            if not value:
                self._voted_in_election = False

    @property
    def voted_in_election(self) -> bool:
        with self._lock:
            return self._voted_in_election

    @voted_in_election.setter
    def voted_in_election(self, value: bool):
        with self._lock:
            self._voted_in_election = value

    # === Pending Messages (A26) ===

    def add_pending_message(self, message: Dict):
        """Fügt eine Nachricht zum Puffer hinzu."""
        with self._lock:
            self._pending_messages.append(message)

    def get_pending_messages(self) -> List[Dict]:
        """Holt alle gepufferten Nachrichten."""
        with self._lock:
            messages = self._pending_messages.copy()
            self._pending_messages.clear()
            return messages

    def has_pending_messages(self) -> bool:
        """Prüft ob gepufferte Nachrichten vorhanden sind."""
        with self._lock:
            return len(self._pending_messages) > 0

    # === Client-Management ===

    def add_client(self, client_id: str, info: Dict):
        """Registriert einen verbundenen Client."""
        with self._lock:
            self._connected_clients[client_id] = info

    def remove_client(self, client_id: str):
        """Entfernt einen Client."""
        with self._lock:
            if client_id in self._connected_clients:
                del self._connected_clients[client_id]

    def get_connected_clients(self) -> Dict[str, Dict]:
        """Gibt alle verbundenen Clients zurück."""
        with self._lock:
            return self._connected_clients.copy()

    def get_client_count(self) -> int:
        """Gibt die Anzahl verbundener Clients zurück."""
        with self._lock:
            return len(self._connected_clients)
