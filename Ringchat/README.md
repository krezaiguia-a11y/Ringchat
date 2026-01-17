# RingChat - Verteiltes Chat-System

Ein verteiltes Chat-System basierend auf einer Ring-Topologie mit automatischer Leader Election, Replikation und Fehlertoleranz.

## Architektur

```
RingChat
├── server/
│   ├── server.py          # Startet Server, Rollenlogik
│   ├── leader.py          # Leader-spezifisches Verhalten
│   ├── election.py        # Ring-basierte Leaderwahl (Chang-Roberts)
│   ├── discovery.py       # UDP-Multicast Discovery
│   ├── heartbeat.py       # Heartbeats & Failure Detection
│   ├── replication.py     # Replikation im RAM
│   └── state.py           # In-Memory Chat-Historie
│
├── client/
│   └── client.py          # Client → Leader Kommunikation
│
├── common/
│   ├── messages.py        # Nachrichtenformate
│   └── config.py          # Ports, Timeouts
│
└── README.md
```

## Anforderungen

- Python 3.8 oder höher
- Keine externen Abhängigkeiten (nur Standardbibliothek)

## Schnellstart

### Server starten

Öffnen Sie mehrere PowerShell-Fenster und starten Sie jeweils einen Server:

```powershell
# Terminal 1 - Server 1
cd C:\Users\kreza\Ringchat
python -m server.server 1 --port 6001

# Terminal 2 - Server 2
cd C:\Users\kreza\Ringchat
python -m server.server 2 --port 6002

# Terminal 3 - Server 3
cd C:\Users\kreza\Ringchat
python -m server.server 3 --port 6003
```

Die Server finden sich automatisch über UDP-Multicast und wählen einen Leader.

### Client starten

```powershell
cd C:\Users\kreza\Ringchat
python -m client.client
```

Oder mit Benutzername:

```powershell
python -m client.client -u MaxMustermann
```

## Client-Befehle

- Nachricht senden: Einfach Text eingeben und Enter drücken
- `/history` - Zeigt die letzten 10 Nachrichten
- `/quit` - Beendet den Client

## Funktionsweise

### 1. Dynamic Discovery (A6-A8)
Server finden sich automatisch über UDP-Multicast (239.255.255.250:5000).
Keine statische Konfiguration erforderlich.

### 2. Ring-Topologie (A4-A5)
Server werden logisch in einem Ring angeordnet.
Jeder Server kommuniziert nur mit seinen direkten Nachbarn.

### 3. Leader Election (A9, A34-A47)
- Chang-Roberts-Algorithmus für Ring-basierte Wahl
- Server mit der höchsten ID wird Leader
- Automatische Neuwahl bei Leader-Ausfall
- Präemptive Neuwahl wenn Server mit höherer ID beitritt

### 4. Nachrichtenordnung (A12-A15)
- Leader vergibt globale Sequenznummern
- Alle Clients sehen Nachrichten in gleicher Reihenfolge
- Keine Duplikate, keine Verluste im Normalbetrieb

### 5. Replikation (A16-A19)
- Chat-Historie wird ausschließlich im RAM gehalten
- Alle Server replizieren die Historie
- Bei Totalausfall gehen alle Daten verloren (A20)

### 6. Fehlertoleranz (A21-A23, A28-A30)
- Heartbeat-basierte Ausfallserkennung
- Automatische Ring-Rekonfiguration bei Server-Ausfall
- System funktioniert solange mindestens ein Server läuft

### 7. Transparenter Leaderwechsel (A24-A26)
- Clients finden neuen Leader automatisch
- Nachrichten während Leaderwechsel werden gepuffert
- Keine manuelle Aktion durch Clients erforderlich

## Konfiguration

Die Konfiguration befindet sich in `common/config.py`:

```python
MULTICAST_GROUP = "239.255.255.250"  # Multicast-Adresse
MULTICAST_PORT = 5000                 # Discovery-Port
HEARTBEAT_INTERVAL = 1.0              # Sekunden
HEARTBEAT_TIMEOUT = 3.0               # Ausfallserkennung
```

## Beispiel-Session

```
=== RingChat Server startet ===
Server-ID: Server 1
Adresse: 127.0.0.1:6001
Discovery Service startet...
Keine anderen Server gefunden - werde Leader
=== Ich bin jetzt der Leader ===
Client-Server gestartet auf 127.0.0.1:6101
```

```
=== RingChat - Verbunden als Alice ===
Befehle: /quit zum Beenden, /history für Historie
=============================================
Alice> Hallo zusammen!
[12:34:56] Alice: Hallo zusammen!
[12:35:01] Bob: Hi Alice!
```

## Testen der Fehlertoleranz

1. Starten Sie 3 Server
2. Verbinden Sie einen Client
3. Senden Sie einige Nachrichten
4. Beenden Sie den Leader-Server (Ctrl+C)
5. Beobachten Sie die automatische Neuwahl
6. Client verbindet automatisch mit neuem Leader

## Anforderungsabdeckung

| Anforderung | Beschreibung | Implementiert |
|-------------|--------------|---------------|
| A1-A5 | Architektur & Ring-Topologie | ✓ |
| A6-A8 | Dynamic Discovery | ✓ |
| A9-A11 | Leader Election | ✓ |
| A12-A15 | Nachrichtenordnung | ✓ |
| A16-A20 | Replikation (RAM-only) | ✓ |
| A21-A23 | Heartbeats & Failure Detection | ✓ |
| A24-A26 | Transparenter Leaderwechsel | ✓ |
| A27-A28 | Server Join/Leave | ✓ |
| A29-A30 | Robustheit | ✓ |
| A31-A33 | Nicht-funktionale Anforderungen | ✓ |
| A34-A47 | Erweiterte Election-Anforderungen | ✓ |

## Hinweise

- Alle Server müssen auf demselben Subnetz laufen (für Multicast)
- Windows Firewall muss UDP-Multicast erlauben
- Bei Problemen mit Multicast: Firewall-Regeln prüfen

## Lizenz

Dieses Projekt dient ausschließlich zu Bildungszwecken.
