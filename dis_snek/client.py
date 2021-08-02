import asyncio
import logging
import time
import traceback
from random import randint
from typing import Any
from typing import Callable
from typing import Coroutine
from typing import Dict
from typing import List
from typing import Optional

import aiohttp

from dis_snek.const import logger_name
from dis_snek.errors import GatewayNotFound
from dis_snek.errors import SnakeException
from dis_snek.errors import WebSocketClosed
from dis_snek.errors import WebSocketRestart
from dis_snek.gateway import WebsocketClient
from dis_snek.http_client import HTTPClient
from dis_snek.models.discord_objects.context import InteractionContext
from dis_snek.models.discord_objects.guild import Guild
from dis_snek.models.discord_objects.interactions import SlashCommand
from dis_snek.models.discord_objects.user import SnakeBotUser
from dis_snek.models.discord_objects.user import User
from dis_snek.models.snowflake import Snowflake_Type

log = logging.getLogger(logger_name)


class Snake:
    def __init__(self, intents, loop=None, sync_slash=False):
        self.loop: asyncio.AbstractEventLoop = asyncio.get_event_loop() if loop is None else loop
        self.intents = intents

        # "Factories"
        self.http: HTTPClient = HTTPClient(loop=self.loop)
        self.ws: WebsocketClient = WebsocketClient

        self._connection = None
        self._closed = False
        self.sync_slash = sync_slash
        self._ready: asyncio.Event = asyncio.Event()

        # caches
        self.guilds_cache = {}
        self._user: SnakeBotUser = None
        self.slash_commands = {}
        self._slash_scopes = {}

        self._listeners: Dict[str, List] = {}

        self.add_listener(self.on_socket_raw, "raw_socket_receive")
        self.add_listener(self._on_websocket_ready, "websocket_ready")

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def latency(self) -> float:
        return self.ws.latency

    @property
    def user(self) -> SnakeBotUser:
        return self._user

    async def login(self, token):
        """
        Login to discord
        :param token: Your bots token
        """
        log.debug(f"Logging in with token: {token}")
        me = await self.http.login(token.strip())
        self._user = SnakeBotUser.from_dict(me, self)
        self.dispatch("login")
        await self._ws_connect()

    async def _ws_connect(self):
        params = {
            "http": self.http,
            "dispatch": self.dispatch,
            "intents": self.intents,
            "resume": False,
            "session_id": None,
            "sequence": None,
        }
        while not self.is_closed:
            log.info(f"Attempting to {'re' if params['resume'] else ''}connect to gateway...")

            try:
                self.ws = await self.ws.connect(**params)

                await self.ws.run()
            except WebSocketRestart as ex:
                # internally requested restart
                self.dispatch("disconnect")
                if ex.resume:
                    params.update(resume=True, session_id=self.ws.session_id, sequence=self.ws.sequence)
                    continue
                params.update(resume=False, session_id=None, sequence=None)

            except (OSError, GatewayNotFound, aiohttp.ClientError, asyncio.TimeoutError, WebSocketClosed) as ex:
                self.dispatch("disconnect")

                if isinstance(ex, WebSocketClosed):
                    if ex.code == 1000:
                        # clean close
                        return
                    elif ex.code == 4011:
                        raise SnakeException("Your bot is too large, you must use shards") from None
                    elif ex.code == 4013:
                        raise SnakeException("Invalid Intents have been passed") from None
                    elif ex.code == 4014:
                        raise SnakeException(
                            "You have requested privileged intents that have not been enabled or approved. Check the developer dashboard"
                        ) from None
                    raise

                if isinstance(ex, OSError) and ex.errno in (54, 10054):
                    print("should reconnect")
                    params.update(resume=True, session_id=self.ws.session_id, sequence=self.ws.sequence)
                    continue
                params.update(resume=False, session_id=None, sequence=None)

            except Exception as e:
                self.dispatch("disconnect")
                log.error("".join(traceback.format_exception(type(e), e, e.__traceback__)))
                params.update(resume=False, session_id=None, sequence=None)

            await asyncio.sleep(randint(1, 5))

    def start(self, token):
        self.loop.run_until_complete(self.login(token))

    def _queue_task(self, coro, event_name, *args, **kwargs):
        async def _async_wrap(_coro, _event_name, *_args, **_kwargs):
            try:
                await coro(*_args, **_kwargs)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.error("".join(traceback.format_exception(type(e), e, e.__traceback__)))

        wrapped = _async_wrap(coro, event_name, *args, **kwargs)

        return asyncio.create_task(wrapped, name=f"snake:: {event_name}")

    def dispatch(self, event: str, *args, **kwargs):
        log.debug(f"Dispatching event: {event}")

        listeners = self._listeners.get(event, [])
        for _listen in listeners:
            try:
                self._queue_task(_listen, event, *args, **kwargs)
            except Exception as e:
                log.error(f"Error running listener: {e}")

    def add_listener(self, coro: Callable[..., Coroutine[Any, Any, Any]], event: Optional[str] = None):
        """
        Add a listener for an event, if no event is passed, one is determined
        :param coro: the coroutine to run
        :param event: the event to listen for
        :return:
        """
        if not event:
            event = coro.__name__

        event = event.replace("on_", "")

        if event not in self._listeners:
            self._listeners[event] = []
        self._listeners[event].append(coro)

    async def _init_slash(self) -> None:
        """
        Initialise slash commands.

        If `sync_slash` this will submit all registered slash commands to discord.
        Otherwise, it will get the list of interactions and cache their scopes.
        """
        if self.sync_slash:
            await self.submit_slash_commands()
        else:
            await self._cache_slash()

    async def _cache_slash(self):
        """Get all interactions used by this bot and cache them."""
        scopes = [g.id for g in self.guilds_cache.values()] + [None]
        for scope in scopes:
            resp_data = await self.http.get_interaction_element(self.user.id, scope)

            for cmd_data in resp_data:
                self._slash_scopes[str(cmd_data["id"])] = scope if scope else "global"
                try:
                    self.slash_commands[scope][cmd_data["name"]].cmd_id = str(cmd_data["id"])
                except KeyError:
                    pass

    def add_slash_command(self, command: SlashCommand):
        """
        Add a slash command to the client.

        :param command: The command to add
        :return:
        """
        if command.scope not in self.slash_commands:
            self.slash_commands[command.scope] = {}
        self.slash_commands[command.scope][command.name] = command

    async def submit_slash_commands(self) -> None:
        """Submit registered slash commands to discord."""
        scopes = [k for k in self.slash_commands.keys()]

        for scope in scopes:
            data = [v.to_dict() for v in self.slash_commands[scope].values()]

            resp_data = await self.http.post_interaction_element(
                self.user.id, data, guild_id=scope if scope != "global" else None
            )

            # cache data
            for cmd_data in resp_data:
                self._slash_scopes[str(cmd_data["id"])] = scope
                self.slash_commands[scope][cmd_data["name"]].cmd_id = str(cmd_data["id"])

    async def dispatch_slash_command(self, interaction_data: dict) -> None:
        """
        Identify and dispatch slash commands.

        :param interaction_data:
        :return:
        """
        # Yes this is temporary, im just blocking out the basic logic

        cmd_id = interaction_data["data"]["id"]
        name = interaction_data["data"]["name"]
        scope = self._slash_scopes.get(str(cmd_id))

        if scope in self.slash_commands:
            command: SlashCommand = self.slash_commands[scope][name]
            print(f"{command.scope} :: {command.name} should be called")

            ctx = InteractionContext.from_dict(interaction_data, self)
            await command.call(ctx)
        else:
            log.error(f"Unknown cmd_id received:: {cmd_id} ({name})")

    async def _on_raw_guild_create(self, data: dict):
        """
        Automatically cache a guild upon GUILD_CREATE event from gateway
        :param data: raw guild data
        """
        self.guilds_cache[data["id"]] = Guild(data, self)

    async def _on_websocket_ready(self, data: dict) -> None:
        """
        Catches websocket ready and determines when to dispatch the client `READY` signal.

        :param data: the websocket ready packet
        """
        expected_guild_count = len(data["guilds"])
        last_count = 0
        current_count = -1

        last_rcv = time.perf_counter()
        while True:
            # wait a while to let guilds cache
            await asyncio.sleep(0.5)

            current_count = len(self.guilds_cache)
            if current_count != expected_guild_count:
                if current_count == last_count:
                    # count hasnt changed, check how long we've been waiting
                    if time.perf_counter() - last_rcv >= 3:
                        # timeout
                        log.warning("Timeout waiting for guilds cache: Not all guilds will be in cache")
                        break
                else:
                    last_rcv = time.perf_counter()
                    last_count = current_count

                continue
            break

        # cache slash commands
        await self._init_slash()

        self.dispatch("ready")

    async def on_socket_raw(self, raw: dict):
        """
        Processes socket events and dispatches non-raw events
        :param raw: raw socket data
        """
        event = raw.get("t")
        data = raw.get("d")

        if event == "GUILD_CREATE":
            guild = Guild(data, self)
            # cache guild
            self.guilds_cache[guild.id] = guild
            self.dispatch("guild_create", guild)

        if event == "INTERACTION_CREATE":
            await self.dispatch_slash_command(data)
        print(event, data)

    async def get_guild(self, guild_id: Snowflake_Type, with_counts: bool = False) -> Guild:
        g_data = await self.http.get_guild(guild_id, with_counts)
        return Guild(g_data, self)

    async def get_guilds(
        self, limit: int = 200, before: Optional[Snowflake_Type] = None, after: Optional[Snowflake_Type] = None
    ):
        g_data = await self.http.get_guilds(limit, before, after)
        to_return = []
        for g in g_data:
            to_return.append(Guild(g, self))

        return to_return

    async def send_message(self, channel: Snowflake_Type, content: str):
        await self.http.create_message(channel, content)

    def slash_command(self, name: str, description: str = "No description set", scope: Snowflake_Type = "global"):
        def wrapper(func):
            cmd = SlashCommand(name, description, scope, call=func)
            self.add_slash_command(cmd)

        return wrapper
