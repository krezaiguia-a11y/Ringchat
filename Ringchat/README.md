# RingChat - Verteiltes Chat-System mit Ring-Topologie

RingChat ist ein verteiltes Chat-System, das eine Ring-Topologie zur Organisation von Servern verwendet. Das System implementiert automatische Leader-Wahl, Fehlertoleranz und transparente Client-Server-Kommunikation.

## Inhaltsverzeichnis

- [Überblick](#überblick)
- [Architektur](#architektur)
- [Features](#features)
- [Installation](#installation)
- [Verwendung](#verwendung)
- [Systemanforderungen](#systemanforderungen)
- [Projektstruktur](#projektstruktur)
- [Implementierte Konzepte](#implementierte-konzepte)
- [Nachrichtentypen](#nachrichtentypen)
- [Fehlerbehandlung](#fehlerbehandlung)
- [Beispiele](#beispiele)
- [Technische Details](#technische-details)

## Überblick

RingChat ist ein verteiltes Echtzeit-Chat-System, das folgende Hauptkomponenten umfasst:

- **Server**: Mehrere Server organisiert in einer Ring-Topologie
- **Leader-Election**: Automatische Wahl eines Leaders mittels Chang-Roberts-Algorithmus
- **Clients**: Kommunizieren ausschließlich mit dem aktuellen Leader
- **Replikation**: Automatische Synchronisation der Chat-Historie über alle Server
- **Fehlertoleranz**: Automatische Erkennung und Behandlung von Server-Ausfällen

## Architektur

### System-Komponenten

```
┌─────────────────────────────────────────────────┐
│                   RingChat                       │
├─────────────────────────────────────────────────┤
│                                                  │
│   ┌──────────┐      ┌──────────┐      ┌──────┐│
│   │ Server 1 │─────▶│ Server 2 │─────▶│Server││
│   │ (Leader) │      │(Follower)│      │  3   ││
│   └────┬─────┘      └──────────┘      └──┬───┘│
│        │                                   │    │
│        └───────────────────────────────────┘    │
│                  Ring-Topologie                 │
│                                                  │
│   ┌────────┐  ┌────────┐  ┌────────┐          │
│   │Client 1│  │Client 2│  │Client 3│          │
│   └───┬────┘  └───┬────┘  └───┬────┘          │
│       │           │            │                │
│       └───────────┴────────────┘                │
│         Verbindung zum Leader                   │
└─────────────────────────────────────────────────┘
```

### Ring-Topologie

- Server sind in einem logischen Ring organisiert
- Jeder Server kennt seinen linken und rechten Nachbarn
- Nachrichten für Leader-Election und Heartbeats werden im Ring weitergeleitet
- Bei Server-Ausfall wird der Ring automatisch rekonfiguriert

### Leader-Follower-Modell

- **Leader**:
  - Empfängt Client-Nachrichten
  - Vergibt globale Sequenznummern
  - Repliziert Nachrichten an alle Follower
  - Broadcastet Nachrichten an alle Clients

- **Follower**:
  - Empfangen replizierte Nachrichten vom Leader
  - Überwachen Leader-Status via Heartbeat
  - Initiieren neue Election bei Leader-Ausfall

## Features

### Client-Features
- Echtzeit-Chat mit mehreren Benutzern
- Automatische Verbindung zum aktuellen Leader
- Transparenter Leaderwechsel
- Nachrichtenpufferung während Leaderwechsel
- Chat-Historie beim Verbinden
- Ping/Keepalive zur Verbindungsüberwachung

### Server-Features
- **Discovery**: Automatisches Finden anderer Server via UDP Multicast
- **Leader Election**: Chang-Roberts-Algorithmus für Ring-basierte Wahl
- **Heartbeat**: Kontinuierliche Überwachung der Nachbarn
- **Replikation**: RAM-basierte Chat-Historie-Synchronisation
- **Fault Tolerance**: Automatische Erkennung und Behandlung von Ausfällen
- **Ring Management**: Dynamisches Hinzufügen/Entfernen von Servern

## Installation

### Voraussetzungen

- Python 3.8 oder höher
- Keine externen Dependencies erforderlich (verwendet nur Python Standard Library)

### Setup

1. Repository klonen oder herunterladen:
```bash
git clone <repository-url>
cd Ringchat
```

2. Keine zusätzlichen Pakete erforderlich - das Projekt verwendet nur Python-Standardbibliotheken:
   - `asyncio` - Asynchrone I/O
   - `logging` - Logging-Framework
   - `json` - JSON Serialisierung
   - `socket` - Netzwerk-Kommunikation

## Verwendung

### Server starten

Starte mehrere Server mit unterschiedlichen IDs und Ports:

```bash
# Server 1 (wird wahrscheinlich Leader)
python -m server.server 1 --port 5001

# Server 2
python -m server.server 2 --port 5002

# Server 3
python -m server.server 3 --port 5003
```

**Parameter:**
- Erste Zahl (z.B. `1`): Server-ID (bestimmt Priorität bei Leader-Election)
- `--port` oder `-p`: Server-Port für Ring-Kommunikation
- `--address` oder `-a`: Server-Adresse (Standard: 127.0.0.1)

**Hinweis**: Der Server mit der höchsten ID wird in der Regel zum Leader gewählt.

### Client starten

Starte einen oder mehrere Clients:

```bash
# Mit Benutzernamen
python -m client.client --username Alice

# Ohne Benutzernamen (wird nach Username gefragt)
python -m client.client
```

### Client-Befehle

Im Client stehen folgende Befehle zur Verfügung:

- **Nachricht senden**: Einfach Text eingeben und Enter drücken
- `/history` - Zeigt die letzten 10 Nachrichten an
- `/quit` - Beendet den Client

### Beispiel-Sitzung

**Terminal 1 - Server 1:**
```bash
$ python -m server.server 1 --port 5001
==================================================
  RingChat Server startet...
  ID: Server 1 | Port: 5001
==================================================

[INFO] Server-ID: Server 1
[INFO] Server vollständig gestartet
[STATUS] Server 1 | Rolle: LEADER
```

**Terminal 2 - Server 2:**
```bash
$ python -m server.server 2 --port 5002
[INFO] Server vollständig gestartet
[STATUS] Server 2 | Rolle: FOLLOWER
```

**Terminal 3 - Client:**
```bash
$ python -m client.client --username Alice
=== RingChat - Verbunden als Alice ===
Befehle: /quit zum Beenden, /history für Historie
=============================================
Alice> Hallo zusammen!
[14:23:45] Alice: Hallo zusammen!
```

## Systemanforderungen

- **Python**: 3.8+
- **Betriebssystem**: Windows, Linux, macOS
- **Netzwerk**: UDP Multicast-Unterstützung für Discovery
- **Ports**:
  - Server-Ports: Frei wählbar (z.B. 5001-5010)
  - Client-Ports: Server-Port + 100 (automatisch)
  - Multicast-Port: 5000 (UDP)

## Projektstruktur

```
Ringchat/
├── client/
│   ├── __init__.py
│   └── client.py              # Client-Implementierung
├── server/
│   ├── __init__.py
│   ├── server.py              # Hauptserver-Logik
│   ├── discovery.py           # Server-Discovery via Multicast
│   ├── election.py            # Chang-Roberts Leader Election
│   ├── heartbeat.py           # Heartbeat-Service
│   ├── leader.py              # Leader-spezifische Funktionen
│   ├── replication.py         # Chat-Historie Replikation
│   └── state.py               # Server-State Management
├── common/
│   ├── __init__.py
│   ├── config.py              # Konfigurationsparameter
│   └── messages.py            # Nachrichtenformate
└── README.md
```

### Modulbeschreibungen

#### Client-Module

- **client.py** (`client/client.py`)
  - Client-Hauptklasse `RingChatClient`
  - Automatische Leader-Discovery
  - Verbindungsmanagement mit Reconnect-Logik
  - Nachrichtenpufferung während Leaderwechsel
  - Interaktive Kommandozeilen-Schnittstelle

#### Server-Module

- **server.py** (`server/server.py:40-620`)
  - Hauptklasse `RingChatServer`
  - Koordiniert alle Server-Services
  - Ring-Topologie Management
  - Server-zu-Server Kommunikation

- **discovery.py** (`server/discovery.py`)
  - UDP Multicast Discovery für automatisches Server-Finden
  - Client-Discovery für Leader-Suche
  - Periodische Announce-Broadcasts

- **election.py** (`server/election.py:17-344`)
  - Chang-Roberts Ring-basierte Leader Election
  - Election Message Handling
  - Leader-Announcement Propagation

- **heartbeat.py** (`server/heartbeat.py`)
  - Periodische Heartbeats zwischen Ring-Nachbarn
  - Timeout-basierte Ausfallserkennung
  - Leader-Überwachung

- **leader.py** (`server/leader.py:19-344`)
  - Client-Server für Nachrichten-Empfang
  - Globale Sequenznummern-Vergabe
  - Chat-Broadcast an alle Clients
  - Historie-Verwaltung

- **replication.py** (`server/replication.py:17-293`)
  - RAM-basierte Replikation der Chat-Historie
  - Full-Sync für neue Server
  - Incremental Updates für laufende Replikation
  - ACK-basierte Bestätigung

- **state.py** (`server/state.py`)
  - Server-State Management
  - Ring-Topologie Datenstrukturen
  - Chat-Historie Storage
  - Neighbor-Tracking

#### Common-Module

- **config.py** (`common/config.py:1-77`)
  - Netzwerk-Konfiguration
  - Timeouts und Intervals
  - Port-Berechnungen
  - Server-ID Utilities

- **messages.py** (`common/messages.py:1-186`)
  - Nachrichtentypen-Definitionen
  - Serialisierung/Deserialisierung
  - Message Factory Functions

## Implementierte Konzepte

### A1: Client-Server-Architektur
Clients kommunizieren ausschließlich mit dem Leader-Server über TCP-Verbindungen.

### A2: Mehrere Server mit Leader-Rolle
Das System unterstützt mehrere Server, von denen einer als Leader agiert und Client-Anfragen verarbeitet.

### A3: Leader-basierte Kommunikation
Nur der aktuelle Leader akzeptiert Client-Verbindungen (auf Port `server_port + 100`).

### A4: Ring-Topologie
Server sind in einer Ring-Struktur organisiert, wobei jeder Server genau zwei Nachbarn hat (links und rechts).

### A5: Nachbarschaftskommunikation
Server kommunizieren primär mit ihren direkten Ring-Nachbarn für Heartbeat und Election.

### A6-A8: Server-Discovery
Server finden sich automatisch über UDP Multicast (Gruppe: 239.255.255.250, Port: 5000).

### A9: Leader Election
Bei Systemstart oder Leader-Ausfall wird der Chang-Roberts-Algorithmus ausgeführt.

### A10: Election bei Leader-Ausfall
Wenn der Leader ausfällt, initiiert ein Follower automatisch eine neue Election.

### A11: Ring-Rekonfiguration
Bei Server-Ausfall werden Nachbarschaftsbeziehungen automatisch aktualisiert.

### A12: Globale Nachrichtenordnung
Der Leader vergibt sequentielle Nummern für alle Chat-Nachrichten.

### A13: Broadcast an Clients
Der Leader sendet jede neue Nachricht an alle verbundenen Clients.

### A14: Duplikatserkennung
Nachrichten werden anhand ihrer Message-ID dedupliziert.

### A15: Nachrichtenzuverlässigkeit
Replikation mit ACKs stellt sicher, dass Nachrichten nicht verloren gehen.

### A16-A19: RAM-basierte Replikation
Chat-Historie wird im RAM gehalten und zwischen Servern repliziert (keine Persistenz auf Disk).

### A20: Kein Split-Brain
Election-Algorithmus stellt sicher, dass nur ein Leader existiert.

### A21-A23: Heartbeat-Mechanismus
Server senden periodisch Heartbeats (Interval: 1s, Timeout: 3s) an ihre Nachbarn.

### A24-A26: Transparenter Leaderwechsel
Clients werden über Leader-Änderungen informiert und verbinden sich automatisch neu. Nachrichten werden während des Wechsels gepuffert.

### A27: Server-Join Synchronisation
Neue Server synchronisieren die vollständige Chat-Historie vom Leader oder einem Nachbarn.

### A28-A33: Fehlertoleranz
System funktioniert solange mindestens ein Server aktiv ist. Ausgefallene Server können jederzeit wieder beitreten.

### A34-A47: Chang-Roberts Election Algorithmus
- Election-Nachrichten propagieren im Ring (`server/election.py:80-344`)
- Server mit höchster ID wird Leader
- Neue Server mit höherer ID triggern Re-Election
- Clients nehmen nicht an Elections teil

## Nachrichtentypen

### Discovery Messages
- `DISCOVERY_ANNOUNCE` - Server kündigt Präsenz an
- `DISCOVERY_RESPONSE` - Antwort mit Server-Info

### Ring Management
- `RING_JOIN_REQUEST` - Server möchte dem Ring beitreten
- `RING_JOIN_ACCEPT` - Join wird akzeptiert
- `RING_UPDATE` - Ring-Topologie Update
- `RING_LEAVE` - Server verlässt Ring

### Heartbeat
- `HEARTBEAT` - Heartbeat an Nachbarn
- `HEARTBEAT_ACK` - Heartbeat-Bestätigung

### Election
- `ELECTION_START` - Election beginnen
- `ELECTION_VOTE` - Kandidat-ID weitergeben
- `ELECTION_LEADER` - Leader-Announcement

### Chat
- `CHAT_MESSAGE` - Client sendet Nachricht
- `CHAT_BROADCAST` - Server broadcastet an Clients
- `CHAT_ACK` - Nachricht bestätigt

### Replication
- `REPLICATION_SYNC` - Repliziere Nachrichten
- `REPLICATION_ACK` - Replikation bestätigt
- `SYNC_REQUEST` - Fordere Full-Sync an
- `SYNC_RESPONSE` - Sende Historie

### Client
- `CLIENT_CONNECT` - Client verbindet
- `CLIENT_DISCONNECT` - Client trennt
- `CLIENT_MESSAGE` - Client-Nachricht
- `CLIENT_PING` - Client-Ping
- `CLIENT_PING_ACK` - Ping-Antwort
- `LEADER_INFO` - Leader-Info Update
- `MESSAGE_DELIVERED` - Nachricht zugestellt
- `HISTORY_REQUEST` - Fordere Historie an
- `HISTORY_RESPONSE` - Sende Historie

## Fehlerbehandlung

### Server-Ausfall
1. Heartbeat-Timeout erkennt Ausfall
2. Ausgefallener Server wird aus Ring entfernt
3. Ring-Nachbarschaften werden aktualisiert
4. Bei Leader-Ausfall: Neue Election

### Leader-Ausfall
1. Follower erkennen ausbleibende Heartbeats
2. Leader-Status wird gelöscht
3. Election wird gestartet
4. Clients werden über neuen Leader informiert

### Client-Disconnect
1. Client erkennt Verbindungsverlust (Ping-Timeout)
2. Client startet automatischen Reconnect
3. Client sucht neuen Leader via Discovery
4. Gepufferte Nachrichten werden nach Reconnect gesendet

### Netzwerk-Partitionierung
- Server in getrennten Partitionen wählen separate Leaders
- Bei Wiedervereinigung wird Re-Election durchgeführt
- Server mit höchster ID wird globaler Leader

## Beispiele

### Beispiel 1: Einfacher Chat mit 3 Servern

```bash
# Terminal 1
python -m server.server 3 --port 5003

# Terminal 2
python -m server.server 2 --port 5002

# Terminal 3
python -m server.server 1 --port 5001

# Terminal 4 - Client Alice
python -m client.client --username Alice

# Terminal 5 - Client Bob
python -m client.client --username Bob
```

Server 3 wird Leader (höchste ID). Alice und Bob können chatten.

### Beispiel 2: Leader-Ausfall Simulation

1. Starte 3 Server (IDs: 1, 2, 3)
2. Server 3 wird Leader
3. Starte 2 Clients
4. Stoppe Server 3 (Ctrl+C)
5. Server 2 wird neuer Leader
6. Clients reconnecten automatisch zu Server 2

### Beispiel 3: Dynamisches Hinzufügen

1. Starte Server 1 und 2
2. Starte Clients
3. Starte Server 3 (höhere ID)
4. Election wird getriggert
5. Server 3 wird neuer Leader
6. Clients wechseln zu Server 3

## Technische Details

### Netzwerk-Protokoll

**TCP (Server-Server & Client-Server)**
- Format: `[4 Bytes Length][JSON Message]`
- Length: Big-Endian Integer
- Message: UTF-8 encoded JSON

**UDP (Discovery)**
- Multicast Group: 239.255.255.250
- Port: 5000
- Format: JSON-encoded Discovery Messages

### Konfigurierbare Parameter

In `common/config.py`:

```python
# Netzwerk
MULTICAST_GROUP = "239.255.255.250"
MULTICAST_PORT = 5000
DISCOVERY_INTERVAL = 2.0  # Sekunden

# Timeouts
HEARTBEAT_INTERVAL = 1.0  # Sekunden
HEARTBEAT_TIMEOUT = 3.0  # Sekunden
ELECTION_TIMEOUT = 5.0  # Sekunden
LEADER_DISCOVERY_TIMEOUT = 2.0  # Sekunden

# Replikation
REPLICATION_TIMEOUT = 2.0  # Sekunden
SYNC_CHUNK_SIZE = 100  # Nachrichten pro Chunk

# Buffer
MESSAGE_BUFFER_SIZE = 65536  # Bytes
MAX_PENDING_MESSAGES = 1000  # Gepufferte Nachrichten
```

### Ports

- **Server Ring Port**: Frei wählbar (z.B. 5001, 5002, ...)
- **Client Port**: Server-Port + 100 (z.B. 5101, 5102, ...)
- **Discovery Port**: 5000 (UDP Multicast)

### Logging

Logging-Level kann in den Modulen angepasst werden:

```python
logging.basicConfig(
    level=logging.INFO,  # DEBUG für mehr Details
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
```

### Performance-Überlegungen

- **RAM-Nutzung**: Chat-Historie wächst unbegrenzt im RAM
- **Netzwerk**: Broadcast an alle Clients bei jeder Nachricht
- **Election**: Bei vielen Servern kann Election mehrere Sekunden dauern
- **Replikation**: Synchron - Leader wartet auf ACKs

---

## Entwickelt mit

- **Sprache**: Python 3.8+
- **Async Framework**: asyncio
- **Serialisierung**: JSON
- **Netzwerk**: TCP/UDP Sockets

