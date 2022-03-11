from typing import Dict, Any, TYPE_CHECKING, Callable, Coroutine, List, Optional, Union

import aiohttp

from . import const

if TYPE_CHECKING:
    from dis_snek.models.snek.command import BaseCommand
    from dis_snek.models.snek.context import Context
    from dis_snek.models.snek.cooldowns import CooldownSystem, MaxConcurrency
    from dis_snek.models.discord.snowflake import Snowflake_Type

__all__ = [
    "SnakeException",
    "BotException",
    "GatewayNotFound",
    "LoginError",
    "HTTPException",
    "DiscordError",
    "BadRequest",
    "Forbidden",
    "NotFound",
    "RateLimited",
    "TooManyChanges",
    "WebSocketClosed",
    "VoiceWebSocketClosed",
    "WebSocketRestart",
    "ExtensionException",
    "ExtensionNotFound",
    "ExtensionLoadException",
    "ScaleLoadException",
    "CommandException",
    "CommandOnCooldown",
    "MaxConcurrencyReached",
    "CommandCheckFailure",
    "MessageException",
    "EmptyMessageException",
    "EphemeralEditException",
    "ThreadException",
    "ThreadOutsideOfGuild",
    "InteractionException",
    "InteractionMissingAccess",
    "AlreadyDeferred",
    "ForeignWebhookException",
    "EventLocationNotProvided",
]


class SnakeException(Exception):
    """Base Exception of discord-snakes."""


class BotException(SnakeException):
    """An issue occurred in the client, likely user error."""


class GatewayNotFound(SnakeException):
    """An exception that is raised when the gateway for Discord could not be found."""

    def __init__(self) -> None:
        super().__init__("Unable to find discord gateway!")


class LoginError(BotException):
    """The bot failed to login, check your token."""


class HTTPException(SnakeException):
    """
    A HTTP request resulted in an exception.

    Attributes:
        response aiohttp.ClientResponse: The response of the HTTP request
        text str: The text of the exception, could be None
        status int: The HTTP status code
        code int: The discord error code, if one is provided
        route Route: The HTTP route that was used

    """

    def __init__(self, response: aiohttp.ClientResponse, text=const.MISSING, discord_code=const.MISSING, **kwargs):
        self.response: aiohttp.ClientResponse = response
        self.status: int = response.status
        self.code: int = discord_code
        self.text: str = text
        self.errors: const.Absent[Any] = const.MISSING
        self.route = kwargs.get("route", const.MISSING)

        if data := kwargs.get("response_data"):
            if isinstance(data, dict):
                self.text = data.get("message", const.MISSING)
                self.code = data.get("code", const.MISSING)
                self.errors = data.get("errors", const.MISSING)
            else:
                self.text = data
        super().__init__(f"{self.status}|{self.response.reason}: {f'({self.code}) ' if self.code else ''}{self.text}")

    @staticmethod
    def search_for_message(errors: dict, lookup: Optional[dict] = None) -> list[str]:
        """
        Search the exceptions error dictionary for a message explaining the issue.

        Args:
            errors: The error dictionary of the http exception
            lookup: A lookup dictionary to use to convert indexes into named items

        Returns:
            A list of parsed error strings found

        """
        messages: List[str] = []
        errors = errors.get("errors", errors)

        def maybe_int(x) -> Union[int, Any]:
            """If something can be an integer, convert it to one, otherwise return its normal value"""
            try:
                return int(x)
            except ValueError:
                return x

        def _parse(_errors: dict, keys: Optional[List[str]] = None):
            """Search through the entire dictionary for any errors defined"""
            for key, val in _errors.items():
                if key == "_errors":
                    key_out = []
                    if keys:
                        if lookup:
                            # this code simply substitutes keys for attribute names
                            _lookup = lookup
                            for _key in keys:
                                _lookup = _lookup[maybe_int(_key)]

                                if isinstance(_lookup, dict):
                                    key_out.append(_lookup.get("name", _key))
                                else:
                                    key_out.append(_key)
                        else:
                            key_out = keys

                    for msg in val:
                        messages.append(f"{'->'.join(key_out)} {msg['code']}: {msg['message']}")
                else:
                    if keys:
                        keys.append(key)
                    else:
                        keys = [key]
                    _parse(val, keys)

        _parse(errors)

        return messages


class DiscordError(HTTPException):
    """A discord-side error."""


class BadRequest(HTTPException):
    """A bad request was made."""


class Forbidden(HTTPException):
    """You do not have access to this."""


class NotFound(HTTPException):
    """This resource could not be found."""


class RateLimited(HTTPException):
    """Discord is rate limiting this application."""


class TooManyChanges(SnakeException):
    """You have changed something too frequently."""


class WebSocketClosed(SnakeException):
    """The websocket was closed."""

    code: int = 0
    codes: Dict[int, str] = {
        1000: "Normal Closure",
        4000: "Unknown Error",
        4001: "Unknown OpCode",
        4002: "Decode Error",
        4003: "Not Authenticated",
        4004: "Authentication Failed",
        4005: "Already Authenticated",
        4007: "Invalid seq",
        4008: "Rate limited",
        4009: "Session Timed Out",
        4010: "Invalid Shard",
        4011: "Sharding Required",
        4012: "Invalid API Version",
        4013: "Invalid Intents",
        4014: "Disallowed Intents",
    }

    def __init__(self, code: int):
        self.code = code
        super().__init__(f"The Websocket closed with code: {code} - {self.codes.get(code, 'Unknown Error')}")


class VoiceWebSocketClosed(SnakeException):
    """The voice websocket was closed."""

    code: int = 0
    codes: Dict[int, str] = {
        1000: "Normal Closure",
        4000: "Unknown Error",
        4001: "Unknown OpCode",
        4002: "Decode Error",
        4003: "Not Authenticated",
        4004: "Authentication Failed",
        4005: "Already Authenticated",
        4006: "Sesssion no longer valid",
        4007: "Invalid seq",
        4009: "Session Timed Out",
        4011: "Server not found",
        4012: "Unknown protocol",
        4014: "Disconnected",
        4015: "Voice Server Crashed",
        4016: "Unknown encryption mode",
    }

    def __init__(self, code: int):
        self.code = code
        super().__init__(f"The Websocket closed with code: {code} - {self.codes.get(code, 'Unknown Error')}")


class WebSocketRestart(SnakeException):
    """The websocket closed, and is safe to restart."""

    resume: bool = False

    def __init__(self, resume: bool = False):
        self.resume = resume
        super().__init__("Websocket connection closed... reconnecting")


class ExtensionException(BotException):
    """An error occurred with an extension."""


class ExtensionNotFound(ExtensionException):
    """The desired extension was not found."""


class ExtensionLoadException(ExtensionException):
    """An error occurred loading an extension."""


class ScaleLoadException(ExtensionLoadException):
    """A scale failed to load."""


class CommandException(BotException):
    """An error occurred trying to execute a command."""


class CommandOnCooldown(CommandException):
    """
    A command is on cooldown, and was attempted to be executed.

    Attributes:
        command BaseCommand: The command that is on cooldown
        cooldown CooldownSystem: The cooldown system

    """

    def __init__(self, command: "BaseCommand", cooldown: "CooldownSystem"):
        self.command: "BaseCommand" = command
        self.cooldown: "CooldownSystem" = cooldown

        super().__init__(f"Command on cooldown... {cooldown.get_cooldown_time():.2f} seconds until reset")


class MaxConcurrencyReached(CommandException):
    """A command has exhausted the max concurrent requests."""

    def __init__(self, command: "BaseCommand", max_conc: "MaxConcurrency"):
        self.command: "BaseCommand" = command
        self.max_conc: "MaxConcurrency" = max_conc

        super().__init__(f"Command has exhausted the max concurrent requests. ({max_conc.concurrent} simultaneously)")


class CommandCheckFailure(CommandException):
    """
    A command check failed.

    Attributes:
        command BaseCommand: The command that's check failed
        check Callable[..., Coroutine]: The check that failed

    """

    def __init__(self, command: "BaseCommand", check: Callable[..., Coroutine], context: "Context"):
        self.command: "BaseCommand" = command
        self.check: Callable[..., Coroutine] = check
        self.context = context


class MessageException(BotException):
    """A message operation encountered an exception."""


class EmptyMessageException(MessageException):
    """You have attempted to send a message without any content or embeds"""


class EphemeralEditException(MessageException):
    """
    Your bot attempted to edit an ephemeral message. This is not possible.

    Its worth noting you can edit an ephemeral message with component's
    `edit_origin` method.

    """

    def __init__(self) -> None:
        super().__init__("Ephemeral messages cannot be edited.")


class ThreadException(BotException):
    """A thread operation encountered an exception."""


class ThreadOutsideOfGuild(ThreadException):
    """A thread was attempted to be created outside of a guild."""

    def __init__(self) -> None:
        super().__init__("Threads cannot be created outside of guilds")


class InteractionException(BotException):
    """An error occurred with an interaction."""


class InteractionMissingAccess(InteractionException):
    """The bot does not have access to the specified scope."""

    def __init__(self, scope: "Snowflake_Type"):
        self.scope: "Snowflake_Type" = scope

        if scope == const.GLOBAL_SCOPE:
            err_msg = "Unable to sync commands global commands"
        else:
            err_msg = (
                f"Unable to sync commands for guild `{scope}` -- Ensure the bot properly added to that guild "
                f"with `application.commands` scope. "
            )

        super().__init__(err_msg)


class AlreadyDeferred(BotException):
    """An interaction was already deferred, and you attempted to defer it again."""


class ForeignWebhookException(SnakeException):
    """Raised when you attempt to send using a webhook you did not create."""


class EventLocationNotProvided(BotException):
    """Raised when you have entity_type external and no location is provided."""
