"""Mailbox adapter boundary shared by sync and setup. No write tools are called."""
from contextlib import asynccontextmanager
from typing import Protocol

import outlook_mcp
import runtime_settings
from gmail_mcp import GmailMCP


class Mailbox(Protocol):
    own_address: str | None
    ordered: bool

    async def profile(self) -> str: ...
    async def list_watched_folders(self) -> list[dict]: ...
    async def list_messages(self, since: str, through: str, skip: int | str = 0,
                            top: int = 50, *, folder_id: str,
                            direction: str = 'received') -> dict: ...


@asynccontextmanager
async def connect(config=None):
    config = config or runtime_settings.read()
    async with outlook_mcp.connect() as transport:
        transport.server_name = config['mcpServer']
        provider = GmailMCP(transport, config['mcpServer']) if config['provider'] == 'gmail' else transport
        address = await provider.profile()
        if not isinstance(address, str) or address.casefold() != config['email'].casefold():
            raise outlook_mcp.OutlookError('已连接邮箱与设置不一致；请在 MCP 客户端连接自己填写的邮箱。本次未读取邮件。')
        yield provider


async def verify(config=None):
    config = config or runtime_settings.read()
    async with connect(config) as provider:
        return {'provider': config['provider'], 'email': provider.own_address, 'verified': True}
