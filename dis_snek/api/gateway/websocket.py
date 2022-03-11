import asyncio
import collections
import logging
import random
import time
import zlib
from abc import abstractmethod
from types import TracebackType
from aiohttp import WSMsgType
from typing import TypeVar, TYPE_CHECKING

from dis_snek.client.const import logger_name
from dis_snek.client.errors import WebSocketClosed
from dis_snek.models.snek.cooldowns import CooldownSystem

from dis_snek.client.utils.input_utils import OverriddenJson

if TYPE_CHECKING:
    from dis_snek.api.gateway.state import ConnectionState

log = logging.getLogger(logger_name)

SELF = TypeVar("SELF", bound="WebsocketClient")


class WebsocketRateLimit:
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


class WebsocketClient:
    def __init__(self, state: "ConnectionState"):
        self.state = state
        self.ws = None
        self.ws_url = None

        self.rl_manager = WebsocketRateLimit()

        self.heartbeat_interval = None
        self.latency = collections.deque(maxlen=10)

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

        # Sanity check, it is extremely important that an instance isn't reused.
        self._entered = False

    async def __aenter__(self) -> SELF:
        if self._entered:
            raise RuntimeError("An instance of 'WebsocketClient' cannot be re-used!")

        self._entered = True
        self._zlib = zlib.decompressobj()

        self.ws = await self.state.client.http.websocket_connect(self.ws_url)

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

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    def close(self) -> None:
        self._close_gateway.set()

    async def send(self, data: str, bypass=False) -> None:
        """
        Send data to the websocket.

        Args:
            data: The data to send
            bypass: Should the rate limit be ignored for this send (used for heartbeats)
        """
        log.debug(f"Sending data to websocket: {data}")

        async with self._race_lock:
            if self.ws is None:
                raise RuntimeError
            if not bypass:
                await self.rl_manager.rate_limit()

            await self.ws.send_str(data)

    async def send_json(self, data: dict, bypass=False) -> None:
        """
        Send JSON data to the websocket.

        Args:
            data: The data to send
            bypass: Should the rate limit be ignored for this send (used for heartbeats)
        """
        serialized = OverriddenJson.dumps(data)
        await self.send(serialized, bypass)

    async def receive(self, force: bool = False) -> str:
        """
        Receive a full event payload from the WebSocket.

        Parameters:
            force:
                Whether to force the receiving, ignoring safety measures such as the read-lock.
                This option also means that exceptions are raised when a reconnection would normally
                be tried.

        """
        buffer = bytearray()

        while True:
            if not force:
                # If we are currently reconnecting in another task, wait for it to complete.
                await self._closed.wait()

            resp = await self.ws.receive()

            if resp.type == WSMsgType.CLOSE:
                log.debug(f"Disconnecting from gateway! Reason: {resp.data}::{resp.extra}")
                if resp.data >= 4000:
                    # This should propagate to __aexit__() which will forcefully shut down everything
                    # and cleanup correctly.
                    raise WebSocketClosed(resp.data)

                if force:
                    raise RuntimeError("Discord unexpectedly wants to close the WebSocket during force receive!")

                await self.reconnect(code=resp.data, resume=resp.data != 1000)
                continue

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
                continue

            return msg

    async def reconnect(self, *, resume: bool = False, code: int = 1012) -> None:
        async with self._race_lock:
            self._closed.clear()

            if self.ws is not None:
                await self.ws.close(code=code)

            self.ws = None
            self._zlib = zlib.decompressobj()

            self.ws = await self.state.client.http.websocket_connect(self.ws_url)

            hello = await self.receive(force=True)
            self.heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000

            if not resume:
                await self._identify()
            else:
                await self._resume_connection()

            self._closed.set()
            self._acknowledged.set()

    @abstractmethod
    async def run(self) -> None:
        """Start receiving events from the websocket."""
        ...

    async def run_bee_gees(self) -> None:
        try:
            await self._start_bee_gees()
        except Exception:
            self.close()
            log.error("The heartbeater raised an exception!", exc_info=True)

    async def _start_bee_gees(self) -> None:
        if self.heartbeat_interval is None:
            raise RuntimeError

        # try:
        #     await asyncio.wait_for(self._kill_bee_gees.wait(), timeout=self.heartbeat_interval * random.uniform(0, 0.5))
        # except asyncio.TimeoutError:
        #     pass
        # else:
        #     return

        log.debug(f"Sending heartbeat every {self.heartbeat_interval} seconds")
        while not self._kill_bee_gees.is_set():
            if not self._acknowledged.is_set():
                log.warning(
                    f"Heartbeat has not been acknowledged for {self.heartbeat_interval} seconds,"
                    " likely zombied connection. Reconnect!"
                )

                await self.reconnect(resume=True)

            self._acknowledged.clear()
            await self.send_heartbeat()
            self._last_heartbeat = time.perf_counter()

            try:
                # wait for next iteration, accounting for latency
                await asyncio.wait_for(self._kill_bee_gees.wait(), timeout=self.heartbeat_interval)
            except asyncio.TimeoutError:
                continue
            else:
                return

    @abstractmethod
    async def _identify(self) -> None:
        ...

    @abstractmethod
    async def _resume_connection(self) -> None:
        ...

    @abstractmethod
    async def send_heartbeat(self) -> None:
        ...
