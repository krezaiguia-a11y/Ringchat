"""
Heartbeat und Failure Detection für RingChat.
Implementiert Server-zu-Server Heartbeats und Timeout-basierte Fehlererkennung (A21, A22, A23).
"""

import asyncio
import logging
import time
from typing import Callable, Optional, Dict, Any

from common.config import HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT
from common.messages import MessageType, create_message, serialize_message, deserialize_message

logger = logging.getLogger(__name__)


class HeartbeatService:
    """Service für Heartbeat-Austausch zwischen Ring-Nachbarn."""

    def __init__(
        self,
        server_id: str,
        get_neighbors: Callable[[], tuple],
        get_leader_info: Callable[[], tuple],
        send_to_neighbor: Callable[[str, bytes], asyncio.coroutine],
        on_neighbor_failed: Callable[[str], None],
        on_leader_failed: Callable[[], None]
    ):
        self.server_id = server_id
        self.get_neighbors = get_neighbors
        self.get_leader_info = get_leader_info
        self.send_to_neighbor = send_to_neighbor
        self.on_neighbor_failed = on_neighbor_failed
        self.on_leader_failed = on_leader_failed

        self._running = False
        self._sequence_number = 0
        self._last_heartbeat_received: Dict[str, float] = {}
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._check_task: Optional[asyncio.Task] = None

    async def start(self):
        """Startet den Heartbeat Service."""
        if self._running:
            return

        self._running = True
        logger.info("Heartbeat Service gestartet")

        # Starte Heartbeat-Sende-Loop
        self._heartbeat_task = asyncio.create_task(self._send_heartbeat_loop())

        # Starte Failure-Detection-Loop
        self._check_task = asyncio.create_task(self._check_failures_loop())

    async def stop(self):
        """Stoppt den Heartbeat Service."""
        self._running = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass

        logger.info("Heartbeat Service gestoppt")

    async def _send_heartbeat_loop(self):
        """Sendet periodisch Heartbeats an Ring-Nachbarn (A21)."""
        while self._running:
            try:
                await self._send_heartbeats()
                await asyncio.sleep(HEARTBEAT_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fehler im Heartbeat Loop: {e}")
                await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _send_heartbeats(self):
        """Sendet Heartbeats an beide Nachbarn (A5: nur Nachbarn)."""
        left, right = self.get_neighbors()
        leader_id, _, _ = self.get_leader_info()

        self._sequence_number += 1

        message = create_message(
            MessageType.HEARTBEAT,
            self.server_id,
            sequence_number=self._sequence_number,
            is_leader=(leader_id == self.server_id)
        )
        data = serialize_message(message)

        # Sende an linken Nachbarn
        if left and left.server_id != self.server_id:
            try:
                await self.send_to_neighbor(left.server_id, data)
            except Exception as e:
                logger.debug(f"Heartbeat an {left.server_id} fehlgeschlagen: {e}")

        # Sende an rechten Nachbarn
        if right and right.server_id != self.server_id and (not left or right.server_id != left.server_id):
            try:
                await self.send_to_neighbor(right.server_id, data)
            except Exception as e:
                logger.debug(f"Heartbeat an {right.server_id} fehlgeschlagen: {e}")

    async def _check_failures_loop(self):
        """Prüft periodisch auf ausgefallene Nachbarn (A22)."""
        while self._running:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                self._check_for_failures()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fehler im Failure Check Loop: {e}")

    def _check_for_failures(self):
        """Prüft ob Nachbarn ausgefallen sind (A22, A23)."""
        current_time = time.time()
        left, right = self.get_neighbors()
        leader_id, _, _ = self.get_leader_info()

        neighbors_to_check = []
        if left and left.server_id != self.server_id:
            neighbors_to_check.append(left)
        if right and right.server_id != self.server_id:
            if not left or right.server_id != left.server_id:
                neighbors_to_check.append(right)

        for neighbor in neighbors_to_check:
            last_hb = self._last_heartbeat_received.get(neighbor.server_id, 0)

            # Wenn noch nie empfangen, gib Zeit für initiale Verbindung
            if last_hb == 0:
                self._last_heartbeat_received[neighbor.server_id] = current_time
                continue

            time_since_last = current_time - last_hb

            if time_since_last > HEARTBEAT_TIMEOUT:
                logger.warning(
                    f"Server {neighbor.server_id} ausgefallen "
                    f"(kein Heartbeat seit {time_since_last:.1f}s)"
                )

                # Prüfe ob es der Leader war (A23)
                if neighbor.server_id == leader_id:
                    logger.warning("Leader ist ausgefallen!")
                    self.on_leader_failed()

                # Benachrichtige über Nachbar-Ausfall
                self.on_neighbor_failed(neighbor.server_id)

                # Entferne aus Tracking
                if neighbor.server_id in self._last_heartbeat_received:
                    del self._last_heartbeat_received[neighbor.server_id]

    def handle_heartbeat(self, message: Dict):
        """Verarbeitet einen empfangenen Heartbeat."""
        sender_id = message.get("sender_id")
        if sender_id:
            self._last_heartbeat_received[sender_id] = time.time()
            logger.debug(f"Heartbeat empfangen von {sender_id}")

            # Sende ACK zurück
            return self._create_heartbeat_ack(message)
        return None

    def _create_heartbeat_ack(self, original: Dict) -> bytes:
        """Erstellt eine Heartbeat-ACK Nachricht."""
        leader_id, _, _ = self.get_leader_info()
        message = create_message(
            MessageType.HEARTBEAT_ACK,
            self.server_id,
            sequence_number=original.get("sequence_number", 0),
            is_leader=(leader_id == self.server_id)
        )
        return serialize_message(message)

    def handle_heartbeat_ack(self, message: Dict):
        """Verarbeitet eine empfangene Heartbeat-ACK."""
        sender_id = message.get("sender_id")
        if sender_id:
            self._last_heartbeat_received[sender_id] = time.time()
            logger.debug(f"Heartbeat ACK empfangen von {sender_id}")

    def reset_neighbor_timeout(self, server_id: str):
        """Setzt den Timeout für einen Nachbarn zurück."""
        self._last_heartbeat_received[server_id] = time.time()

    def remove_server(self, server_id: str):
        """Entfernt einen Server aus dem Tracking."""
        if server_id in self._last_heartbeat_received:
            del self._last_heartbeat_received[server_id]

    def get_last_heartbeat(self, server_id: str) -> Optional[float]:
        """Gibt den letzten Heartbeat-Timestamp zurück."""
        return self._last_heartbeat_received.get(server_id)
