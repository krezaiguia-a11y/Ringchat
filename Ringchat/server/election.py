"""
Ring-basierte Leader Election für RingChat.
Implementiert den Chang-Roberts-Algorithmus für Leaderwahl (A9, A34-A47).
"""

import asyncio
import logging
import time
from typing import Callable, Optional, Dict, List, Set

from common.config import ELECTION_TIMEOUT, get_server_number
from common.messages import MessageType, create_message, serialize_message, deserialize_message

logger = logging.getLogger(__name__)


class ElectionService:
    """
    Service für Ring-basierte Leader Election (Chang-Roberts Algorithmus).

    Der Algorithmus funktioniert wie folgt:
    1. Ein Server startet eine Election und sendet seine ID im Ring
    2. Jeder Server vergleicht die empfangene ID mit seiner eigenen
    3. Wenn die empfangene ID größer ist, wird sie weitergeleitet
    4. Wenn die eigene ID größer ist, wird die eigene ID gesendet
    5. Wenn ein Server seine eigene ID zurückerhält, ist er der Leader
    6. Der neue Leader sendet eine LEADER-Nachricht durch den Ring
    """

    def __init__(
        self,
        server_id: str,
        server_address: str,
        server_port: int,
        get_right_neighbor: Callable[[], Optional[object]],
        send_to_right: Callable[[bytes], asyncio.coroutine],
        on_leader_elected: Callable[[str, str, int], None],
        get_server_count: Callable[[], int]
    ):
        self.server_id = server_id
        self.server_address = server_address
        self.server_port = server_port
        self.get_right_neighbor = get_right_neighbor
        self.send_to_right = send_to_right
        self.on_leader_elected = on_leader_elected
        self.get_server_count = get_server_count

        self._election_in_progress = False
        self._participated_in_election = False
        self._current_candidate: Optional[str] = None
        self._election_start_time: float = 0
        self._election_task: Optional[asyncio.Task] = None
        self._visited_nodes: Set[str] = set()

    @property
    def election_in_progress(self) -> bool:
        return self._election_in_progress

    def _compare_ids(self, left: str, right: str) -> int:
        """
        Vergleicht zwei Server-IDs und bevorzugt numerischen Vergleich, falls mБglich.
        Gibt >0 zurグck wenn left > right, <0 wenn left < right, sonst 0.
        """
        left_num = get_server_number(left)
        right_num = get_server_number(right)

        if left_num and right_num:
            if left_num > right_num:
                return 1
            if left_num < right_num:
                return -1
            return 0

        if left > right:
            return 1
        if left < right:
            return -1
        return 0

    async def start_election(self):
        """
        Startet eine neue Leader Election (A36).
        Wird aufgerufen wenn:
        - Kein Leader bekannt ist
        - Leader ausgefallen ist
        - Server mit höherer ID beitritt (A47)
        """
        if self._election_in_progress:
            logger.debug("Election bereits aktiv, überspringe Start")
            return

        self._election_in_progress = True
        self._participated_in_election = True
        self._election_start_time = time.time()
        self._current_candidate = self.server_id
        self._visited_nodes = {self.server_id}

        logger.info(f"Starte Leader Election mit Kandidat {self.server_id}")

        # Erstelle Election-Nachricht mit eigener ID (A34, A35)
        message = create_message(
            MessageType.ELECTION_START,
            self.server_id,
            candidate_id=self.server_id,
            candidate_address=self.server_address,
            candidate_port=self.server_port,
            hop_count=1,
            visited_nodes=[self.server_id]
        )

        # Sende an rechten Nachbarn (A37, A38)
        await self._forward_to_right(message)

        # Starte Timeout-Überwachung
        self._election_task = asyncio.create_task(self._election_timeout_check())

    async def handle_election_message(self, message: Dict):
        """
        Verarbeitet eine empfangene Election-Nachricht (A38, A40).

        Regeln:
        - Wenn candidate_id > eigene ID: weiterleiten
        - Wenn candidate_id < eigene ID und nicht bereits teilgenommen: eigene ID senden
        - Wenn candidate_id == eigene ID: Ich bin Leader
        """
        msg_type = message.get("msg_type")
        candidate_id = message.get("candidate_id")
        candidate_address = message.get("candidate_address")
        candidate_port = message.get("candidate_port")
        hop_count = message.get("hop_count", 0)
        visited_nodes = message.get("visited_nodes", [])

        logger.debug(f"Election Nachricht empfangen: type={msg_type}, candidate={candidate_id}")

        if msg_type == MessageType.ELECTION_START.value:
            await self._process_election_vote(
                candidate_id, candidate_address, candidate_port,
                hop_count, visited_nodes
            )

        elif msg_type == MessageType.ELECTION_VOTE.value:
            await self._process_election_vote(
                candidate_id, candidate_address, candidate_port,
                hop_count, visited_nodes
            )

        elif msg_type == MessageType.ELECTION_LEADER.value:
            await self._process_leader_announcement(
                candidate_id, candidate_address, candidate_port,
                hop_count, visited_nodes
            )

    async def _process_election_vote(
        self,
        candidate_id: str,
        candidate_address: str,
        candidate_port: int,
        hop_count: int,
        visited_nodes: List[str]
    ):
        """Verarbeitet eine Election Vote Nachricht (A40, A44)."""

        # Prüfe auf Duplikate - wenn wir schon besucht wurden
        if self.server_id in visited_nodes:
            # Nachricht hat den Ring durchlaufen
            if candidate_id == self.server_id:
                # Ich habe die größte ID - ich bin Leader (A39, A42)
                logger.info(f"Ich bin der neue Leader! (ID: {self.server_id})")
                await self._announce_leadership()
            # Sonst ignorieren - ein anderer hat gewonnen
            return

        self._election_in_progress = True
        new_visited = visited_nodes + [self.server_id]

        # Vergleiche Kandidaten-ID mit eigener (A40)
        comparison = self._compare_ids(candidate_id, self.server_id)
        if comparison > 0:
            # Kandidat hat höhere ID - weiterleiten
            logger.debug(f"Kandidat {candidate_id} > {self.server_id}, leite weiter")
            self._current_candidate = candidate_id

            message = create_message(
                MessageType.ELECTION_VOTE,
                self.server_id,
                candidate_id=candidate_id,
                candidate_address=candidate_address,
                candidate_port=candidate_port,
                hop_count=hop_count + 1,
                visited_nodes=new_visited
            )
            await self._forward_to_right(message)

        elif comparison < 0:
            # Meine ID ist höher - sende mich als Kandidaten
            if not self._participated_in_election:
                logger.debug(f"Meine ID {self.server_id} > {candidate_id}, nominiere mich selbst")
                self._participated_in_election = True
                self._current_candidate = self.server_id

                message = create_message(
                    MessageType.ELECTION_VOTE,
                    self.server_id,
                    candidate_id=self.server_id,
                    candidate_address=self.server_address,
                    candidate_port=self.server_port,
                    hop_count=1,
                    visited_nodes=[self.server_id]
                )
                await self._forward_to_right(message)
            else:
                # Bereits teilgenommen, leite höheren Kandidaten weiter
                message = create_message(
                    MessageType.ELECTION_VOTE,
                    self.server_id,
                    candidate_id=candidate_id,
                    candidate_address=candidate_address,
                    candidate_port=candidate_port,
                    hop_count=hop_count + 1,
                    visited_nodes=new_visited
                )
                await self._forward_to_right(message)

        else:
            # candidate_id == self.server_id
            # Meine eigene Nachricht kam zurück - ich bin Leader (A39, A42)
            logger.info(f"Eigene Election-Nachricht zurück - Ich bin Leader!")
            await self._announce_leadership()

    async def _announce_leadership(self):
        """Kündigt die eigene Führung an (A41)."""
        self._election_in_progress = False
        self._participated_in_election = False

        # Informiere lokalen Server
        self.on_leader_elected(self.server_id, self.server_address, self.server_port)

        # Sende Leader-Announcement durch den Ring (A41)
        message = create_message(
            MessageType.ELECTION_LEADER,
            self.server_id,
            candidate_id=self.server_id,
            candidate_address=self.server_address,
            candidate_port=self.server_port,
            hop_count=1,
            visited_nodes=[self.server_id]
        )
        await self._forward_to_right(message)

    async def _process_leader_announcement(
        self,
        leader_id: str,
        leader_address: str,
        leader_port: int,
        hop_count: int,
        visited_nodes: List[str]
    ):
        """Verarbeitet eine Leader-Ankündigung (A41)."""

        # Prüfe ob wir diese Ankündigung schon gesehen haben
        if self.server_id in visited_nodes:
            # Ankündigung hat den Ring durchlaufen - fertig
            logger.debug("Leader-Announcement hat den Ring durchlaufen")
            return

        logger.info(f"Neuer Leader: {leader_id}")

        # Election beenden
        self._election_in_progress = False
        self._participated_in_election = False

        # Leader-Info speichern
        self.on_leader_elected(leader_id, leader_address, leader_port)

        # Weiterleiten an nächsten im Ring
        new_visited = visited_nodes + [self.server_id]
        message = create_message(
            MessageType.ELECTION_LEADER,
            self.server_id,
            candidate_id=leader_id,
            candidate_address=leader_address,
            candidate_port=leader_port,
            hop_count=hop_count + 1,
            visited_nodes=new_visited
        )
        await self._forward_to_right(message)

    async def _forward_to_right(self, message: Dict):
        """Sendet eine Nachricht an den rechten Nachbarn (A37)."""
        right = self.get_right_neighbor()
        if right:
            try:
                data = serialize_message(message)
                await self.send_to_right(data)
                logger.debug(f"Election-Nachricht an rechten Nachbarn gesendet")
            except Exception as e:
                logger.error(f"Fehler beim Senden an rechten Nachbarn: {e}")
                # Bei Fehler: Ring ist möglicherweise unterbrochen
                # Warte auf Rekonfiguration und neue Election
        else:
            # Kein rechter Nachbar - wir sind allein
            if self._election_in_progress:
                logger.info("Kein Nachbar - bin einziger Server, werde Leader")
                self._election_in_progress = False
                self._participated_in_election = False
                self.on_leader_elected(self.server_id, self.server_address, self.server_port)

    async def _election_timeout_check(self):
        """Überwacht den Election-Timeout (A43, A45)."""
        try:
            await asyncio.sleep(ELECTION_TIMEOUT)

            if self._election_in_progress:
                elapsed = time.time() - self._election_start_time
                logger.warning(f"Election Timeout nach {elapsed:.1f}s - starte neu")

                # Reset und Neustart
                self._election_in_progress = False
                self._participated_in_election = False
                await asyncio.sleep(0.5)  # Kurze Pause
                await self.start_election()

        except asyncio.CancelledError:
            pass

    def cancel_election(self):
        """Bricht eine laufende Election ab."""
        self._election_in_progress = False
        self._participated_in_election = False
        if self._election_task:
            self._election_task.cancel()

    def should_trigger_election_on_join(self, new_server_id: str, current_leader_id: Optional[str]) -> bool:
        """
        Prüft ob eine Election ausgelöst werden soll wenn ein neuer Server beitritt (A47).

        Returns True wenn:
        - Neuer Server hat höhere ID als aktueller Leader
        """
        if not current_leader_id:
            return True

        return self._compare_ids(new_server_id, current_leader_id) > 0
