"""
Replikation der Chat-Historie im RAM für RingChat.
Implementiert Replikation zwischen aktiven Servern (A16, A17, A18, A19).
"""

import asyncio
import logging
import time
from typing import Callable, Optional, Dict, List, Any

from common.config import REPLICATION_TIMEOUT, SYNC_CHUNK_SIZE
from common.messages import MessageType, create_message, serialize_message, deserialize_message

logger = logging.getLogger(__name__)


class ReplicationService:
    """
    Service für Replikation der Chat-Historie zwischen Servern.

    Replikation erfolgt:
    1. Bei neuer Nachricht: Leader repliziert an alle Follower
    2. Bei Server-Join: Neuer Server synchronisiert von bestehendem Server (A27)
    3. Replikation nur im RAM - keine Persistenz (A16, A19)
    """

    def __init__(
        self,
        server_id: str,
        get_history: Callable[[], List[Dict]],
        set_history: Callable[[List[Dict]], None],
        add_message: Callable[[Dict], bool],
        get_current_sequence: Callable[[], int],
        set_sequence: Callable[[int], None],
        get_all_servers: Callable[[], List],
        send_to_server: Callable[[str, bytes], asyncio.coroutine],
        is_leader: Callable[[], bool]
    ):
        self.server_id = server_id
        self.get_history = get_history
        self.set_history = set_history
        self.add_message = add_message
        self.get_current_sequence = get_current_sequence
        self.set_sequence = set_sequence
        self.get_all_servers = get_all_servers
        self.send_to_server = send_to_server
        self.is_leader = is_leader

        self._pending_acks: Dict[str, asyncio.Event] = {}
        self._sync_in_progress = False

    async def replicate_message(self, chat_message: Dict) -> bool:
        """
        Repliziert eine neue Chat-Nachricht über den Ring (A17).
        Die Replikation erfolgt über Ring-Nachbarn, die die Nachricht weiterleiten.
        Nur vom Leader aufgerufen.

        Returns True wenn Replikation erfolgreich.
        """
        if not self.is_leader():
            logger.warning("Replikation nur vom Leader erlaubt")
            return False

        servers = self.get_all_servers()
        if len(servers) <= 1:
            # Keine anderen Server zum Replizieren
            return True

        message = create_message(
            MessageType.REPLICATION_SYNC,
            self.server_id,
            messages=[chat_message],
            last_sequence=chat_message.get("sequence_number", 0),
            is_full_sync=False,
            origin_id=self.server_id,  # Tracking um Schleifen zu vermeiden
            hop_count=0
        )
        data = serialize_message(message)

        # Sende an alle anderen Server (Leader repliziert direkt für Konsistenz A18)
        success_count = 0
        tasks = []

        for server in servers:
            if server.server_id != self.server_id:
                tasks.append(self._send_with_ack(server.server_id, data))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            success_count = sum(1 for r in results if r is True)

        logger.debug(f"Nachricht repliziert an {success_count}/{len(tasks)} Server")

        # Erfolg wenn mindestens die Hälfte bestätigt hat (A15: Nachrichten nicht verloren)
        # Bei Fehler: Nachricht wird bei nächster Sync nachgeholt
        return success_count >= len(tasks) // 2 or len(tasks) == 0

    async def _send_with_ack(self, server_id: str, data: bytes) -> bool:
        """Sendet Daten und wartet auf ACK."""
        ack_event = asyncio.Event()
        self._pending_acks[server_id] = ack_event

        try:
            await self.send_to_server(server_id, data)

            # Warte auf ACK mit Timeout
            try:
                await asyncio.wait_for(ack_event.wait(), timeout=REPLICATION_TIMEOUT)
                return True
            except asyncio.TimeoutError:
                logger.debug(f"Replication ACK Timeout für {server_id}")
                return False

        except Exception as e:
            logger.error(f"Fehler bei Replikation an {server_id}: {e}")
            return False
        finally:
            if server_id in self._pending_acks:
                del self._pending_acks[server_id]

    def handle_replication_ack(self, message: Dict):
        """Verarbeitet eine Replikations-ACK Nachricht."""
        sender_id = message.get("sender_id")
        if sender_id in self._pending_acks:
            self._pending_acks[sender_id].set()
            logger.debug(f"Replication ACK empfangen von {sender_id}")

    async def handle_replication_sync(self, message: Dict) -> bytes:
        """
        Verarbeitet eine empfangene Replikations-Nachricht (A17, A18).
        Speichert die Nachrichten im lokalen RAM.
        """
        messages = message.get("messages", [])
        last_sequence = message.get("last_sequence", 0)
        is_full_sync = message.get("is_full_sync", False)
        sender_id = message.get("sender_id")

        if is_full_sync:
            # Vollständige Synchronisation (A27: Server-Join)
            logger.info(f"Vollständige Synchronisation von {sender_id}: {len(messages)} Nachrichten")
            self.set_history(messages)
        else:
            # Inkrementelle Replikation
            for msg in messages:
                self.add_message(msg)

        # Aktualisiere Sequenznummer
        self.set_sequence(last_sequence)

        # Sende ACK zurück
        ack = create_message(
            MessageType.REPLICATION_ACK,
            self.server_id,
            last_sequence=last_sequence
        )
        return serialize_message(ack)

    async def request_full_sync(self, target_server_id: str) -> bool:
        """
        Fordert eine vollständige Synchronisation von einem Server an (A27).
        Wird aufgerufen wenn ein neuer Server dem Ring beitritt.
        """
        if self._sync_in_progress:
            logger.debug("Sync bereits in Progress")
            return False

        self._sync_in_progress = True
        logger.info(f"Fordere vollständige Synchronisation von {target_server_id}")

        try:
            message = create_message(
                MessageType.SYNC_REQUEST,
                self.server_id,
                from_sequence=0
            )
            data = serialize_message(message)
            await self.send_to_server(target_server_id, data)
            return True

        except Exception as e:
            logger.error(f"Fehler bei Sync-Anfrage: {e}")
            return False
        finally:
            self._sync_in_progress = False

    async def handle_sync_request(self, message: Dict) -> bytes:
        """
        Verarbeitet eine Sync-Anfrage und sendet die Historie (A27).
        """
        from_sequence = message.get("from_sequence", 0)
        requester_id = message.get("sender_id")

        logger.info(f"Sync-Anfrage von {requester_id}, ab Sequenz {from_sequence}")

        # Hole alle Nachrichten ab der angeforderten Sequenz
        history = self.get_history()
        filtered_history = [
            msg for msg in history
            if msg.get("sequence_number", 0) >= from_sequence
        ]

        # Sende in Chunks wenn nötig
        response = create_message(
            MessageType.SYNC_RESPONSE,
            self.server_id,
            messages=filtered_history,
            last_sequence=self.get_current_sequence(),
            is_full_sync=True
        )

        return serialize_message(response)

    def handle_sync_response(self, message: Dict):
        """
        Verarbeitet eine Sync-Antwort und speichert die Historie (A27).
        """
        messages = message.get("messages", [])
        last_sequence = message.get("last_sequence", 0)
        sender_id = message.get("sender_id")

        logger.info(f"Sync-Antwort von {sender_id}: {len(messages)} Nachrichten")

        # Speichere die empfangene Historie
        self.set_history(messages)
        self.set_sequence(last_sequence)

        self._sync_in_progress = False

    def get_sync_status(self) -> Dict:
        """Gibt den aktuellen Sync-Status zurück."""
        return {
            "sync_in_progress": self._sync_in_progress,
            "current_sequence": self.get_current_sequence(),
            "message_count": len(self.get_history())
        }


class ReplicationManager:
    """
    Manager für die Koordination der Replikation.
    Stellt sicher, dass die Chat-Historie konsistent bleibt (A18).
    """

    def __init__(self, replication_service: ReplicationService):
        self.service = replication_service
        self._replication_queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None

    async def start(self):
        """Startet den Replikations-Worker."""
        if self._running:
            return

        self._running = True
        self._worker_task = asyncio.create_task(self._replication_worker())
        logger.info("Replication Manager gestartet")

    async def stop(self):
        """Stoppt den Replikations-Worker."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("Replication Manager gestoppt")

    async def queue_replication(self, message: Dict):
        """Fügt eine Nachricht zur Replikations-Queue hinzu."""
        await self._replication_queue.put(message)

    async def _replication_worker(self):
        """Worker der Replikations-Aufgaben abarbeitet."""
        while self._running:
            try:
                # Warte auf nächste Nachricht
                message = await asyncio.wait_for(
                    self._replication_queue.get(),
                    timeout=1.0
                )

                # Repliziere die Nachricht
                await self.service.replicate_message(message)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fehler im Replication Worker: {e}")
