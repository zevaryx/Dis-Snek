from typing import ClassVar, Any, Optional
from urllib.parse import quote as _uriquote

from discord_snakes.models.snowflake import Snowflake


class Route:
    BASE: ClassVar[str] = "https://discord.com/api/v9"

    def __init__(self, method: str, path: str, **parameters: Any):
        self.path: str = path
        self.method: str = method

        url = f"{self.BASE}{self.path}"
        if parameters:
            if parameters:
                url = url.format_map({k: _uriquote(v) if isinstance(v, str) else v for k, v in parameters.items()})
        self.url: str = url

        self.channel_id: Optional[Snowflake] = parameters.get("channel_id")
        self.guild_id: Optional[Snowflake] = parameters.get("guild_id")
        self.webhook_id: Optional[Snowflake] = parameters.get("webhook_id")
        self.webhook_token: Optional[str] = parameters.get("webhook_token")

    @property
    def rl_bucket(self):
        return f"{self.channel_id}:{self.guild_id}:{self.path}"