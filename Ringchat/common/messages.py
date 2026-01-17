"""
Nachrichtenformate für das RingChat-System.
Definiert alle Nachrichtentypen für Server-Server und Client-Server Kommunikation.
"""

import json
import time
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional, List, Dict, Any


class MessageType(Enum):
    """Alle Nachrichtentypen im System."""
    # Discovery
    DISCOVERY_ANNOUNCE = "discovery_announce"
    DISCOVERY_RESPONSE = "discovery_response"

    # Ring-Management
    RING_JOIN_REQUEST = "ring_join_request"
    RING_JOIN_ACCEPT = "ring_join_accept"
    RING_UPDATE = "ring_update"
    RING_LEAVE = "ring_leave"

    # Heartbeat
    HEARTBEAT = "heartbeat"
    HEARTBEAT_ACK = "heartbeat_ack"

    # Election (Ring-basiert)
    ELECTION_START = "election_start"
    ELECTION_VOTE = "election_vote"
    ELECTION_LEADER = "election_leader"

    # Chat-Nachrichten
    CHAT_MESSAGE = "chat_message"
    CHAT_BROADCAST = "chat_broadcast"
    CHAT_ACK = "chat_ack"

    # Replikation
    REPLICATION_SYNC = "replication_sync"
    REPLICATION_ACK = "replication_ack"
    SYNC_REQUEST = "sync_request"
    SYNC_RESPONSE = "sync_response"

    # Client-Kommunikation
    CLIENT_CONNECT = "client_connect"
    CLIENT_DISCONNECT = "client_disconnect"
    CLIENT_MESSAGE = "client_message"
    LEADER_INFO = "leader_info"
    MESSAGE_DELIVERED = "message_delivered"
    HISTORY_REQUEST = "history_request"
    HISTORY_RESPONSE = "history_response"
    CLIENT_PING = "client_ping"
    CLIENT_PING_ACK = "client_ping_ack"


@dataclass
class BaseMessage:
    """Basis-Nachrichtenklasse."""
    msg_type: str
    sender_id: str
    timestamp: float

    def to_json(self) -> str:
        """Serialisiert die Nachricht zu JSON."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> 'BaseMessage':
        """Deserialisiert JSON zu einer Nachricht."""
        d = json.loads(data)
        return cls(**d)

    def to_bytes(self) -> bytes:
        """Konvertiert zu Bytes für Netzwerkübertragung."""
        return self.to_json().encode('utf-8')


@dataclass
class DiscoveryMessage(BaseMessage):
    """Discovery-Nachricht für UDP-Multicast."""
    server_address: str
    server_port: int
    is_leader: bool = False
    leader_id: Optional[str] = None
    leader_address: Optional[str] = None
    leader_port: Optional[int] = None


@dataclass
class RingMessage(BaseMessage):
    """Nachrichten für Ring-Management."""
    ring_position: int = 0
    left_neighbor_id: Optional[str] = None
    right_neighbor_id: Optional[str] = None
    server_address: str = ""
    server_port: int = 0


@dataclass
class HeartbeatMessage(BaseMessage):
    """Heartbeat-Nachricht zwischen Ring-Nachbarn."""
    sequence_number: int = 0
    is_leader: bool = False


@dataclass
class ElectionMessage(BaseMessage):
    """Election-Nachricht für Ring-basierte Leaderwahl."""
    candidate_id: str = ""
    candidate_address: str = ""
    candidate_port: int = 0
    hop_count: int = 0
    visited_nodes: List[str] = None

    def __post_init__(self):
        if self.visited_nodes is None:
            self.visited_nodes = []


@dataclass
class ChatMessage(BaseMessage):
    """Chat-Nachricht von Client oder für Broadcast."""
    message_id: str = ""
    client_id: str = ""
    username: str = ""
    content: str = ""
    sequence_number: int = 0  # Globale Reihenfolge vom Leader


@dataclass
class ReplicationMessage(BaseMessage):
    """Nachricht für Replikation zwischen Servern."""
    messages: List[Dict[str, Any]] = None
    last_sequence: int = 0
    is_full_sync: bool = False

    def __post_init__(self):
        if self.messages is None:
            self.messages = []


@dataclass
class ClientMessage(BaseMessage):
    """Nachricht für Client-Server Kommunikation."""
    client_id: str = ""
    username: str = ""
    content: str = ""
    action: str = ""  # connect, disconnect, message, history


@dataclass
class LeaderInfoMessage(BaseMessage):
    """Information über aktuellen Leader für Clients."""
    leader_id: str = ""
    leader_address: str = ""
    leader_port: int = 0
    is_available: bool = True


def create_message(msg_type: MessageType, sender_id: str, **kwargs) -> Dict[str, Any]:
    """Erstellt eine Nachricht als Dictionary."""
    msg = {
        "msg_type": msg_type.value,
        "sender_id": sender_id,
        "timestamp": time.time(),
        **kwargs
    }
    return msg


def serialize_message(msg: Dict[str, Any]) -> bytes:
    """Serialisiert eine Nachricht zu Bytes."""
    return json.dumps(msg).encode('utf-8')


def deserialize_message(data: bytes) -> Dict[str, Any]:
    """Deserialisiert Bytes zu einer Nachricht."""
    return json.loads(data.decode('utf-8'))


def is_valid_message(msg: Dict[str, Any]) -> bool:
    """Prüft ob eine Nachricht gültig ist."""
    required_fields = ["msg_type", "sender_id", "timestamp"]
    return all(field in msg for field in required_fields)
