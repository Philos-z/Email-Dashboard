"""Read Outlook through the signed-in Codex MCP client, without starting a model turn."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import time

RPC_TIMEOUT_SECONDS = 60


class OutlookError(RuntimeError):
    pass


# Outlook folder names are localized; prefer connector metadata when available.
FOLDER_NAMES = {
    'inbox': {'inbox', '收件箱', 'posteingang'},
    'archive': {'archive', 'archives', '存档', '归档', '封存', 'archiv'},
    'sentitems': {'sent items', 'sent', '已发送邮件', '已发送', '寄件备份', '寄件備份', 'gesendete elemente'},
}


def watched_folders(rows):
    roots = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            raise OutlookError('邮箱 MCP 返回的文件夹格式无效')
        name = str(row.get('display_name') or '').strip().casefold()
        known = str(row.get('well_known_name') or '').casefold()
        # A nested folder named Inbox/Sent must not be mistaken for the system root.
        root = '/' not in str(row.get('path') or name)
        kind = next((kind for kind, names in FOLDER_NAMES.items()
                     if known == kind or (root and name in names)), None)
        if kind:
            roots[row['id']] = kind
    if not {'inbox', 'sentitems'} <= set(roots.values()):
        raise OutlookError('未能识别 Outlook 收件箱或已发送邮件文件夹，本次未推进检查位置')
    selected = dict(roots)
    for _ in rows:
        children = {row['id']: selected[row['parent_folder_id']] for row in rows
                    if row.get('parent_folder_id') in selected and row['id'] not in selected}
        if not children:
            break
        selected.update(children)
    return [{'id': row['id'], 'path': row.get('path') or row.get('display_name') or '',
             'kind': selected[row['id']],
             'direction': 'sent' if selected[row['id']] == 'sentitems' else 'received'}
            for row in rows if row['id'] in selected]


def codex_binary():
    bundled = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
    binary = os.getenv('ATLAS_CODEX_BIN') or shutil.which('codex') or (str(bundled) if bundled.exists() else None)
    if not binary:
        raise OutlookError('未找到本地 邮箱 MCP 客户端，请安装或打开 ChatGPT / Codex')
    return binary


class OutlookMCP:
    ordered = True
    provider = 'outlook'
    server_name = 'codex_apps'

    def __init__(self, process):
        self.process = process
        self.sequence = 0
        self.thread_id = None
        self.own_address = None

    async def rpc(self, method, params):
        self.sequence += 1
        number = self.sequence
        self.process.stdin.write((json.dumps({'id': number, 'method': method, 'params': params}) + '\n').encode())
        await self.process.stdin.drain()
        started = time.monotonic()
        operation = params.get('tool', method)
        try:
            return await asyncio.wait_for(self.receive(number), RPC_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            # Log operation/timing only; never mailbox contents, IDs, or credentials.
            logging.warning('Outlook MCP timeout: operation=%s elapsed=%.1fs', operation, time.monotonic() - started)
            stage = {'initialize': '启动客户端', 'thread/start': '初始化工具连接',
                     'microsoft_outlook_email.get_profile': '验证邮箱连接',
                     'microsoft_outlook_email.list_mail_folders': '读取文件夹',
                     'microsoft_outlook_email.list_messages': '读取邮件'}.get(operation, '查询')
            raise OutlookError(f'Outlook MCP {stage}超时（{RPC_TIMEOUT_SECONDS} 秒），请稍后重试') from exc

    async def receive(self, number):
        while True:
            line = await self.process.stdout.readline()
            if not line:
                raise OutlookError('邮箱 MCP 连接中断，请确认本机 ChatGPT 登录仍有效')
            try:
                result = json.loads(line)
            except (ValueError, UnicodeError):
                raise OutlookError('邮箱 MCP 客户端响应格式无效') from None
            if result.get('id') == number and ('result' in result or 'error' in result):
                if 'error' in result:
                    raise OutlookError('邮箱 MCP 调用失败，请在 ChatGPT 中检查邮箱连接与授权')
                return result['result']
            if 'id' in result and 'method' in result:
                # An unattended refresh cannot consent to new scopes or approve write tools.
                raise OutlookError('邮箱 MCP 需要交互授权，请在 ChatGPT 中重新连接自己的邮箱')

    async def initialize(self):
        await self.rpc('initialize', {'clientInfo': {'name': 'progress_atlas', 'version': '2.0'},
                                     'capabilities': {'experimentalApi': True}})
        self.process.stdin.write(b'{"method":"initialized"}\n')
        await self.process.stdin.drain()
        # This ephemeral context only initializes tool connections. No turn/start, model call,
        # persistent chat, credential extraction, or human-chat writer is involved.
        result = await self.rpc('thread/start', {'ephemeral': True, 'cwd': str(Path(__file__).resolve().parent)})
        self.thread_id = result['thread']['id']

    async def profile(self):
        profile = await self.rpc('mcpServer/tool/call', {
            'threadId': self.thread_id, 'server': self.server_name,
            'tool': 'microsoft_outlook_email.get_profile', 'arguments': {},
        })
        self.own_address = (profile.get('structuredContent') or {}).get('email')
        if profile.get('isError') or not isinstance(self.own_address, str) or not self.own_address:
            raise OutlookError('无法确认 Outlook 邮箱身份，本次未推进检查位置')
        return self.own_address

    async def list_watched_folders(self):
        if not self.own_address:
            await self.profile()
        result = await self.rpc('mcpServer/tool/call', {
            'threadId': self.thread_id, 'server': self.server_name,
            'tool': 'microsoft_outlook_email.list_mail_folders',
            'arguments': {'include_hidden_folders': False, 'top': 500},
        })
        data = result.get('structuredContent')
        if result.get('isError') or not isinstance(data, dict) or not isinstance(data.get('value'), list):
            raise OutlookError('Outlook 文件夹读取失败，请检查邮箱连接与授权')
        if data.get('next_link') or data.get('has_more') or len(data['value']) >= 500:
            raise OutlookError('Outlook 文件夹列表不完整，本次未推进检查位置')
        return watched_folders(data['value'])

    async def list_messages(self, since, through, skip=0, top=50, *, folder_id, direction='received'):
        clock = 'sentDateTime' if direction == 'sent' else 'receivedDateTime'
        result = await self.rpc('mcpServer/tool/call', {
            'threadId': self.thread_id, 'server': self.server_name,
            'tool': 'microsoft_outlook_email.list_messages',
            'arguments': {'folder_id': folder_id,
                          'filter': f'{clock} ge {since} and {clock} le {through}',
                          'order_by': f'{clock} asc', 'skip': skip, 'top': top},
        })
        if result.get('isError'):
            raise OutlookError('Outlook 邮件读取失败，请在 ChatGPT 中检查邮箱连接与授权')
        data = result.get('structuredContent')
        if not isinstance(data, dict) or not isinstance(data.get('value'), list):
            raise OutlookError('邮箱 MCP 返回的邮件列表格式无效')
        return data


    async def _chat_read(self, tool, arguments):
        # Names are constants in the three wrappers below, never supplied by the model.
        result = await self.rpc('mcpServer/tool/call', {
            'threadId': self.thread_id, 'server': self.server_name,
            'tool': 'microsoft_outlook_email.' + tool, 'arguments': arguments,
        })
        data = result.get('structuredContent')
        if result.get('isError') or not isinstance(data, dict):
            raise OutlookError('Outlook 邮件查询失败，请在 ChatGPT 中检查 Outlook 连接与授权')
        return data

    async def search_mail(self, query, size=15, from_index=0):
        return await self._chat_read('search_messages', {
            'query': query, 'size': size, 'from_index': from_index})

    async def recent_mail(self, folder_id, *, top=15, skip=0, direction='received', unread=False):
        clock = 'sentDateTime' if direction == 'sent' else 'receivedDateTime'
        arguments = {'folder_id': folder_id, 'top': top, 'skip': skip, 'order_by': clock + ' desc'}
        if unread:
            arguments['filter'] = 'isRead eq false'
        return await self._chat_read('list_messages', arguments)

    async def read_mail(self, message_id):
        return await self._chat_read('fetch_message', {'message_id': message_id})


@asynccontextmanager
async def connect():
    try:
        process = await asyncio.create_subprocess_exec(
            codex_binary(), 'app-server', '--stdio', stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=16_000_000, start_new_session=os.name != 'nt',
        )
    except OSError:
        raise OutlookError('无法启动 Codex MCP 客户端，请检查安装和 ATLAS_CODEX_BIN 路径') from None
    try:
        client = OutlookMCP(process)
        await client.initialize()
        yield client
    except (asyncio.TimeoutError, BrokenPipeError, ConnectionError) as exc:
        raise OutlookError('邮箱 MCP 响应超时或连接中断，请稍后重试') from exc
    finally:
        if process.returncode is None:
            try:
                if os.name == 'nt':
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                await asyncio.wait_for(process.wait(), 5)
            except (ProcessLookupError, asyncio.TimeoutError):
                if process.returncode is None:
                    try:
                        if os.name == 'nt':
                            process.kill()
                        else:
                            os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
