import asyncio
import logging
import random
import socket
import struct
import time
from enum import IntEnum
from threading import Event
from typing import TYPE_CHECKING

from aiohttp import WSMsgType

from dis_snek.api.gateway.websocket import WebsocketClient
from dis_snek.api.voice.encryption import Encryption
from dis_snek.client.const import logger_name
from dis_snek.client.errors import VoiceWebSocketClosed
from dis_snek.client.utils.input_utils import OverriddenJson

log = logging.getLogger(logger_name)

if TYPE_CHECKING:
    pass


class OP(IntEnum):
    IDENTIFY = 0
    SELECT_PROTOCOL = 1
    READY = 2
    HEARTBEAT = 3
    SESSION_DESCRIPTION = 4
    SPEAKING = 5
    HEARTBEAT_ACK = 6
    RESUME = 7
    HELLO = 8
    RESUMED = 9
    CLIENT_DISCONNECT = 13


class VoiceGateway(WebsocketClient):
    guild_id: str
    heartbeat_interval: int
    session_id: str
    token: str
    encryptor: Encryption

    ssrc: int
    me_ip: str
    me_port: int
    voice_ip: str
    voice_port: int
    voice_modes: list[str]
    selected_mode: str
    socket: socket.socket
    ready: Event

    def __init__(self, state, voice_state: dict, voice_server: dict):
        super().__init__(state)

        self._voice_server_update = asyncio.Event()
        self.ws_url = f"wss://{voice_server['endpoint']}?v=4"
        self.session_id = voice_state["session_id"]
        self.token = voice_server["token"]
        self.guild_id = voice_server["guild_id"]

        self.sock_sequence = 0
        self.timestamp = 0
        self.ready = Event()

    async def wait_until_ready(self) -> None:
        await asyncio.to_thread(self.ready.wait)

    async def run(self) -> None:
        """Start receiving events from the websocket."""
        while True:
            stopping = asyncio.create_task(self._close_gateway.wait())
            receiving = asyncio.create_task(self.receive())
            done, _ = await asyncio.wait({stopping, receiving}, return_when=asyncio.FIRST_COMPLETED)

            if receiving in done:
                # Note that we check for a received message first, because if both completed at
                # the same time, we don't want to discard that message.
                msg = await receiving
                stopping.cancel()
            else:
                # This has to be the stopping task, which we join into the current task (even
                # though that doesn't give any meaningful value in the return).
                await stopping
                receiving.cancel()
                return

            op = msg.get("op")
            data = msg.get("d")
            seq = msg.get("s")

            if seq:
                self.sequence = seq

            # This may try to reconnect the connection so it is best to wait
            # for it to complete before receiving more - that way there's less
            # possible race conditions to consider.
            await self.dispatch_opcode(data, op)

    async def receive(self, force=True) -> str:
        buffer = bytearray()

        while True:
            if not force:
                await self._closed.wait()

            resp = await self.ws.receive()

            if resp.type == WSMsgType.CLOSE:
                log.debug(f"Disconnecting from gateway! Reason: {resp.data}::{resp.extra}")
                if resp.data == 4014:
                    self.ready.clear()
                    await self.reconnect()
                    continue
                raise VoiceWebSocketClosed(resp.data)

            elif resp.type is WSMsgType.CLOSED:
                if force:
                    raise RuntimeError("Discord unexpectedly closed the underlying socket during force receive!")

                if not self._closed.is_set():
                    # Because we are waiting for the even before we receive, this shouldn't be
                    # possible - the CLOSING message should be returned instead. Either way, if this
                    # is possible after all we can just wait for the event to be set.
                    await self._closed.wait()
                else:
                    # This is an odd corner-case where the underlying socket connection was closed
                    # unexpectedly without communicating the WebSocket closing handshake. We'll have
                    # to reconnect ourselves.
                    await self.reconnect(resume=True)

            elif resp.type is WSMsgType.CLOSING:
                if force:
                    raise RuntimeError("WebSocket is unexpectedly closing during force receive!")

                # This happens when the keep-alive handler is reconnecting the connection even
                # though we waited for the event before hand, because it got to run while we waited
                # for data to come in. We can just wait for the event again.
                await self._closed.wait()
                continue

            if resp.data is None:
                continue

            if isinstance(resp.data, bytes):
                buffer.extend(resp.data)

                if len(resp.data) < 4 or resp.data[-4:] != b"\x00\x00\xff\xff":
                    # message isn't complete yet, wait
                    continue

                msg = self._zlib.decompress(buffer)
                msg = msg.decode("utf-8")
            else:
                msg = resp.data

            try:
                msg = OverriddenJson.loads(msg)
            except Exception as e:
                log.error(e)

            return msg

    async def dispatch_opcode(self, data, op) -> None:
        match op:
            case OP.HEARTBEAT_ACK:
                self.latency.append(time.perf_counter() - self._last_heartbeat)

                if self._last_heartbeat != 0 and self.latency[-1] >= 15:
                    log.warning(f"High Latency! Voice heartbeat took {self.latency[-1]:.1f}s to be acknowledged!")
                else:
                    log.debug(f"❤ Heartbeat acknowledged after {self.latency[-1]:.5f} seconds")

                return self._acknowledged.set()

            case OP.READY:
                log.debug("Discord send VC Ready! Establishing a socket connection...")
                self.voice_ip = data["ip"]
                self.voice_port = data["port"]
                self.ssrc = data["ssrc"]
                self.voice_modes = [mode for mode in data["modes"] if mode in Encryption.SUPPORTED]

                if len(self.voice_modes) == 0:
                    log.critical("NO VOICE ENCRYPTION MODES SHARED WITH GATEWAY!")

                await self.establish_voice_socket()

            case OP.SESSION_DESCRIPTION:
                log.debug(f"Voice connection established; using {data['mode']}")
                self.encryptor = Encryption(data["secret_key"])
                self.ready.set()

            case _:
                return log.debug(f"Unhandled OPCODE: {op} = {data = }")

    async def reconnect(self, *, resume: bool = False, code: int = 1012) -> None:
        async with self._race_lock:
            self._closed.clear()

            if self.ws is not None:
                await self.ws.close(code=code)

            self.ws = None

            if not resume:
                log.debug("Waiting for updated server information...")
                try:
                    await asyncio.wait_for(self._voice_server_update.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._kill_bee_gees.set()
                    self.close()
                    log.debug("Terminating VoiceGateway due to disconnection")
                    return

                self._voice_server_update.clear()

            self.ws = await self.state.client.http.websocket_connect(self.ws_url)

            hello = await self.receive(force=True)
            self.heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000

            if not resume:
                await self._identify()
            else:
                await self._resume_connection()

            self._closed.set()
            self._acknowledged.set()

    async def _resume_connection(self) -> None:
        raise NotImplementedError

    async def establish_voice_socket(self) -> None:
        """Establish the socket connection to discord"""
        log.debug("IP Discovery in progress...")

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setblocking(False)

        packet = bytearray(70)
        struct.pack_into(">H", packet, 0, 1)  # 1 = Send
        struct.pack_into(">H", packet, 2, 70)  # 70 = Length
        struct.pack_into(">I", packet, 4, self.ssrc)

        self.socket.sendto(packet, (self.voice_ip, self.voice_port))
        resp = await self.loop.sock_recv(self.socket, 70)
        log.debug(f"Voice Initial Response Received: {resp}")

        ip_start = 4
        ip_end = resp.index(0, ip_start)
        self.me_ip = resp[ip_start:ip_end].decode("ascii")

        self.me_port = struct.unpack_from(">H", resp, len(resp) - 2)[0]
        log.debug(f"IP Discovered: {self.me_ip} #{self.me_port}")

        await self._select_protocol()

    def generate_packet(self, data: bytes) -> bytes:
        """Generate a packet to be sent to the voice socket."""
        header = bytearray(12)
        header[0] = 0x80
        header[1] = 0x78

        struct.pack_into(">H", header, 2, self.sock_sequence)
        struct.pack_into(">I", header, 4, self.timestamp)
        struct.pack_into(">I", header, 8, self.ssrc)

        return self.encryptor.encrypt(self.voice_modes[0], header, data)

    def send_packet(self, data: bytes, encoder, needs_encode=True) -> None:
        """Send a packet to the voice socket"""
        self.sock_sequence += 1
        if self.sock_sequence > 0xFFFF:
            self.sock_sequence = 0

        if self.timestamp > 0xFFFFFFFF:
            self.timestamp = 0

        if needs_encode:
            data = encoder.encode(data)
        packet = self.generate_packet(data)

        self.socket.sendto(packet, (self.voice_ip, self.voice_port))
        self.timestamp += encoder.samples_per_frame

    async def send_heartbeat(self) -> None:
        await self.send_json({"op": OP.HEARTBEAT, "d": random.uniform(0.0, 1.0)})
        log.debug("❤ Voice Connection is sending Heartbeat")

    async def _identify(self) -> None:
        """Send an identify payload to the voice gateway."""
        payload = {
            "op": OP.IDENTIFY,
            "d": {
                "server_id": self.guild_id,
                "user_id": self.state.client.user.id,
                "session_id": self.session_id,
                "token": self.token,
            },
        }
        serialized = OverriddenJson.dumps(payload)
        await self.ws.send_str(serialized)

        log.debug("Voice Connection has identified itself to Voice Gateway")

    async def _select_protocol(self) -> None:
        """Inform Discord of our chosen protocol."""
        payload = {
            "op": OP.SELECT_PROTOCOL,
            "d": {
                "protocol": "udp",
                "data": {"address": self.me_ip, "port": self.me_port, "mode": self.voice_modes[0]},
            },
        }
        await self.send_json(payload)

    async def speaking(self, is_speaking: bool = True) -> None:
        """
        Tell the gateway if we're sending audio or not.

        Args:
            is_speaking: If we're sending audio or not
        """
        payload = {
            "op": OP.SPEAKING,
            "d": {
                "speaking": 1 << 0 if is_speaking else 0,
                "delay": 0,
                "ssrc": self.ssrc,
            },
        }
        await self.ws.send_json(payload)

    def set_new_voice_server(self, payload: dict) -> None:
        self.ws_url = f"wss://{payload['endpoint']}?v=4"
        self.token = payload["token"]
        self.guild_id = payload["guild_id"]
        self._voice_server_update.set()
