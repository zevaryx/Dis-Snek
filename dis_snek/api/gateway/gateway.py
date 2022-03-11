"""This file outlines the interaction between snek and Discord's Gateway API."""
import asyncio
import logging
import sys
import time
import zlib
from types import TracebackType
from typing import TypeVar, TYPE_CHECKING

from dis_snek.api import events
from dis_snek.client.const import logger_name
from dis_snek.client.utils.input_utils import OverriddenJson
from dis_snek.client.utils.serializer import dict_filter_none
from dis_snek.models.discord.enums import Status
from dis_snek.models.discord.enums import WebSocketOPCodes as OPCODE
from dis_snek.models.discord.snowflake import to_snowflake
from dis_snek.models.snek.cooldowns import CooldownSystem
from .websocket import WebsocketClient

if TYPE_CHECKING:
    from .state import ConnectionState
    from dis_snek.models.discord.snowflake import Snowflake_Type

__all__ = ["GatewayClient"]

log = logging.getLogger(logger_name)


SELF = TypeVar("SELF", bound="WebsocketClient")


class GatewayRateLimit:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        # docs state 120 calls per 60 seconds, this is set conservatively to 110 per 60 seconds.
        rate = 110
        interval = 60
        self.cooldown_system = CooldownSystem(1, interval / rate)
        # hacky way to throttle how frequently we send messages to the gateway

    async def rate_limit(self) -> None:
        async with self.lock:
            while not self.cooldown_system.acquire_token():
                await asyncio.sleep(self.cooldown_system.get_cooldown_time())


class GatewayClient(WebsocketClient):
    """
    Abstraction over one gateway connection.

    Multiple `WebsocketClient` instances can be used to implement same-process sharding.

    Attributes:
        buffer: A buffer to hold incoming data until its complete
        sequence: The sequence of this connection
        session_id: The session ID of this connection

    """

    def __init__(self, state: "ConnectionState", shard: tuple[int, int]) -> None:
        super().__init__(state)

        self.shard = shard

        self.chunk_cache = {}

        self._trace = []
        self.sequence = None
        self.session_id = None

        self.ws_url = state.gateway_url

        # This lock needs to be held to send something over the gateway, but is also held when
        # reconnecting. That way there's no race conditions between sending and reconnecting.
        self._race_lock = asyncio.Lock()
        # Then this event is used so that receive() can wait for the reconnecting to complete.
        self._closed = asyncio.Event()

        self._keep_alive = None
        self._kill_bee_gees = asyncio.Event()
        self._last_heartbeat = 0
        self._acknowledged = asyncio.Event()
        self._acknowledged.set()  # Initialize it as set

        self._close_gateway = asyncio.Event()

        # Santity check, it is extremely important that an instance isn't reused.
        self._entered = False

    async def __aenter__(self: SELF) -> SELF:
        if self._entered:
            raise RuntimeError("An instance of 'WebsocketClient' cannot be re-used!")

        self._entered = True
        self._zlib = zlib.decompressobj()

        self.ws = await self.state.client.http.websocket_connect(self.state.gateway_url)

        hello = await self.receive(force=True)
        self.heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000
        self._closed.set()

        self._keep_alive = asyncio.create_task(self.run_bee_gees())

        await self._identify()

        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, traceback: TracebackType | None
    ) -> None:
        # Technically should not be possible in any way, but might as well be safe worst-case.
        self._close_gateway.set()

        try:
            if self._keep_alive is not None:
                self._kill_bee_gees.set()
                try:
                    # Even if we get cancelled that is fine, because then the keep-alive
                    # handler will also be cancelled since we're waiting on it.
                    await self._keep_alive  # Wait for the keep-alive handler to finish
                finally:
                    self._keep_alive = None
        finally:
            if self.ws is not None:
                # We could be cancelled here, it is extremely important that we close the
                # WebSocket either way, hence the try/except.
                try:
                    await self.ws.close(code=1000)
                finally:
                    self.ws = None

    @property
    def average_latency(self) -> float:
        """Get the average latency of the connection."""
        if self.latency:
            return sum(self.latency) / len(self.latency)
        else:
            return float("inf")

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
            event = msg.get("t")

            if seq:
                self.sequence = seq

            if op == OPCODE.DISPATCH:
                asyncio.create_task(self.dispatch_event(data, seq, event))
                continue

            # This may try to reconnect the connection so it is best to wait
            # for it to complete before receiving more - that way there's less
            # possible race conditions to consider.
            await self.dispatch_opcode(data, op)

    async def dispatch_opcode(self, data, op) -> None:
        match op:

            case OPCODE.HEARTBEAT:
                return await self.send_heartbeat()

            case OPCODE.HEARTBEAT_ACK:
                self.latency.append(time.perf_counter() - self._last_heartbeat)

                if self._last_heartbeat != 0 and self.latency[-1] >= 15:
                    log.warning(
                        f"High Latency! shard ID {self.shard[0]} heartbeat took {self.latency[-1]:.1f}s to be acknowledged!"
                    )
                else:
                    log.debug(f"❤ Heartbeat acknowledged after {self.latency[-1]:.5f} seconds")

                return self._acknowledged.set()

            case OPCODE.RECONNECT:
                log.info("Gateway requested reconnect. Reconnecting...")
                return await self.reconnect(resume=True)

            case OPCODE.INVALIDATE_SESSION:
                log.warning("Gateway has invalidated session! Reconnecting...")
                return await self.reconnect(resume=data)

            case _:
                return log.debug(f"Unhandled OPCODE: {op} = {OPCODE(op).name}")

    async def dispatch_event(self, data, seq, event) -> None:
        match event:
            case "READY":
                self._trace = data.get("_trace", [])
                self.sequence = seq
                self.session_id = data["session_id"]
                log.info("Connected to gateway!")
                log.debug(f" Session ID: {self.session_id} Trace: {self._trace}")
                return self.state.client.dispatch(events.WebsocketReady(data))

            case "RESUMED":
                log.info(f"Successfully resumed connection! Session_ID: {self.session_id}")
                return self.state.client.dispatch(events.Resume())

            case "GUILD_MEMBERS_CHUNK":
                return asyncio.create_task(self._process_member_chunk(data))

            case _:
                # the above events are "special", and are handled by the gateway itself, the rest can be dispatched
                event_name = f"raw_{event.lower()}"
                processor = self.state.client.processors.get(event_name)
                if processor:
                    try:
                        asyncio.create_task(processor(events.RawGatewayEvent(data, override_name=event_name)))
                    except Exception as ex:
                        log.error(f"Failed to run event processor for {event_name}: {ex}")
                else:
                    log.debug(f"No processor for `{event_name}`")

        self.state.client.dispatch(events.RawGatewayEvent(data, override_name="raw_socket_receive"))
        self.state.client.dispatch(events.RawGatewayEvent(data, override_name=f"raw_{event.lower()}"))

    def close(self) -> None:
        self._close_gateway.set()

    async def _identify(self) -> None:
        """Send an identify payload to the gateway."""
        if self.ws is None:
            raise RuntimeError
        payload = {
            "op": OPCODE.IDENTIFY,
            "d": {
                "token": self.state.client.http.token,
                "intents": self.state.intents,
                "shard": self.shard,
                "large_threshold": 250,
                "properties": {"$os": sys.platform, "$browser": "dis.snek", "$device": "dis.snek"},
                "presence": self.state.presence,
            },
            "compress": True,
        }

        serialized = OverriddenJson.dumps(payload)
        await self.ws.send_str(serialized)

        log.debug(
            f"Shard ID {self.shard[0]} has identified itself to Gateway, requesting intents: {self.state.intents}!"
        )

    async def _resume_connection(self) -> None:
        """Send a resume payload to the gateway."""
        if self.ws is None:
            raise RuntimeError

        payload = {
            "op": OPCODE.RESUME,
            "d": {"token": self.state.client.http.token, "seq": self.sequence, "session_id": self.session_id},
        }

        serialized = OverriddenJson.dumps(payload)
        await self.ws.send_str(serialized)

        log.debug("Client is attempting to resume a connection")

    async def send_heartbeat(self) -> None:
        """Send a heartbeat to the gateway."""
        await self.send_json({"op": OPCODE.HEARTBEAT, "d": self.sequence}, True)
        log.debug(f"❤ Shard {self.shard[0]} is sending a Heartbeat")

    async def change_presence(self, activity=None, status: Status = Status.ONLINE, since=None) -> None:
        payload = dict_filter_none(
            {
                "since": int(since if since else time.time() * 1000),
                "activities": [activity] if activity else [],
                "status": status,
                "afk": False,
            }
        )
        await self.send_json({"op": OPCODE.PRESENCE, "d": payload})

    async def request_member_chunks(
        self,
        guild_id: "Snowflake_Type",
        query="",
        *,
        limit,
        user_ids=None,
        presences=False,
        nonce=None,
    ) -> None:
        payload = {
            "op": OPCODE.REQUEST_MEMBERS,
            "d": dict_filter_none(
                {
                    "guild_id": guild_id,
                    "presences": presences,
                    "limit": limit,
                    "nonce": nonce,
                    "user_ids": user_ids,
                    "query": query,
                }
            ),
        }
        await self.send_json(payload)

    async def _process_member_chunk(self, chunk: dict):

        guild = self.state.client.cache.guild_cache.get(to_snowflake(chunk.get("guild_id")))
        if guild:
            return asyncio.create_task(guild.process_member_chunk(chunk))
        raise ValueError(f"No guild exists for {chunk.get('guild_id')}")

    async def voice_state_update(
        self, guild_id: "Snowflake_Type", channel_id: "Snowflake_Type", muted: bool = False, deafened: bool = False
    ) -> None:
        payload = {
            "op": OPCODE.VOICE_STATE,
            "d": {"guild_id": guild_id, "channel_id": channel_id, "self_mute": False, "self_deaf": False},
        }
        await self.send_json(payload)
