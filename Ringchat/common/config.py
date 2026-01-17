"""
Konfigurationsmodul für RingChat.
Enthält alle Ports, Timeouts und Systemkonstanten.
"""

import uuid

# Netzwerk-Konfiguration
MULTICAST_GROUP = "239.255.255.250"
MULTICAST_PORT = 5000
DISCOVERY_INTERVAL = 2.0  # Sekunden zwischen Discovery-Broadcasts

# Server-Ports
SERVER_BASE_PORT = 6000  # Basis-Port für Server-Kommunikation
CLIENT_PORT_OFFSET = 100  # Offset für Client-Verbindungen

# Timeouts
HEARTBEAT_INTERVAL = 1.0  # Sekunden zwischen Heartbeats
HEARTBEAT_TIMEOUT = 3.0  # Sekunden bis Server als ausgefallen gilt
ELECTION_TIMEOUT = 5.0  # Timeout für Election-Nachrichten
LEADER_DISCOVERY_TIMEOUT = 2.0  # Client wartet auf Leader-Info

# Replikation
REPLICATION_TIMEOUT = 2.0  # Timeout für Replikations-ACKs
SYNC_CHUNK_SIZE = 100  # Nachrichten pro Sync-Chunk

# Ring-Konfiguration
RING_STABILIZE_INTERVAL = 1.0  # Interval für Ring-Stabilisierung
JOIN_RETRY_INTERVAL = 2.0  # Retry-Interval für Server-Join

# Buffer-Größen
MESSAGE_BUFFER_SIZE = 65536  # UDP-Buffer-Größe
MAX_PENDING_MESSAGES = 1000  # Max gepufferte Nachrichten während Leaderwechsel


def generate_server_id() -> str:
    """Generiert eine eindeutige Server-ID basierend auf UUID."""
    return str(uuid.uuid4())


def create_server_id(number: int) -> str:
    """Erstellt eine menschenlesbare Server-ID aus einer Zahl."""
    return f"Server {number}"


def get_server_number(server_id: str) -> int:
    """Extrahiert die Nummer aus einer Server-ID."""
    if not server_id:
        return 0

    cleaned = str(server_id).strip()

    # Reine Zahl direkt zurグckgeben
    if cleaned.isdigit():
        return int(cleaned)

    prefixes = ["Server ", "Server-", "Server"]
    for prefix in prefixes:
        if cleaned.startswith(prefix):
            try:
                number_part = cleaned[len(prefix):].strip().lstrip("-").strip()
                return int(number_part)
            except ValueError:
                return 0

    return 0


def get_server_port(base_port: int, offset: int = 0) -> int:
    """Berechnet den Server-Port."""
    return base_port + offset


def get_client_port(server_port: int) -> int:
    """Berechnet den Client-Port basierend auf Server-Port."""
    return server_port + CLIENT_PORT_OFFSET
