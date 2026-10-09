"""Read-only Gmail adapter. Search tokens remain opaque and survive restarts."""
from __future__ import annotations
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime, parseaddr
import math

from outlook_mcp import OutlookError


def content(result):
    if result.get('isError'):
        raise OutlookError('Gmail MCP 调用失败，请检查自己的 Gmail 连接')
    value = result.get('structuredContent')
    if not isinstance(value, (dict, list)):
        raise OutlookError('Gmail MCP 响应格式无效，本次未推进检查位置')
    return value


def body_text(part, depth=0):
    if not isinstance(part, dict) or depth > 20 or part.get('filename'):
        return ''  # Never read attachment bodies.
    children = part.get('parts', [])
    if children:
        plain = [p for p in children if p.get('mimeType', p.get('mime_type')) == 'text/plain']
        return '\n'.join(filter(None, (body_text(p, depth + 1) for p in (plain or children))))[:12000]
    mime = part.get('mimeType', part.get('mime_type', ''))
    if mime not in {'text/plain', 'text/html'}:
        return ''
    encoded = (part.get('body') or {}).get('data', '')
    try:
        text = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode('utf-8', errors='replace')
    except (ValueError, TypeError):
        raise OutlookError('Gmail 正文编码无效') from None
    if mime == 'text/html':
        # Reuse the safe HTML-to-text parser before joining mixed MIME alternatives.
        from mail_sync import PlainText
        parser = PlainText(); parser.feed(text)
        text = ' '.join(parser.parts)
    return text[:12000]


def normalize(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('id'), str):
        raise OutlookError('Gmail 邮件格式无效')
    payload = raw.get('payload', raw.get('mime_tree', {}))
    headers = {str(h.get('name', '')).lower(): h.get('value', '') for h in payload.get('headers', [])}
    received = raw.get('internalDate', raw.get('internal_date'))
    try:
        if received is not None:
            moment = datetime.fromtimestamp(int(received) / 1000, timezone.utc)
        else:
            moment = parsedate_to_datetime(headers.get('date') or raw.get('date', ''))
            if moment.tzinfo is None:
                raise ValueError()
        stamp = moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except (ValueError, TypeError, OverflowError):
        raise OutlookError('Gmail 邮件缺少有效时间，本次未推进检查位置') from None
    sender = headers.get('from') or raw.get('from', '')
    if isinstance(sender, dict):
        sender = sender.get('email', sender.get('address', ''))
    return {'id': raw['id'], 'subject': headers.get('subject') or raw.get('subject', ''),
            # Query and checkpoint use Gmail internalDate, including Sent.
            'receivedDateTime': stamp, 'sentDateTime': stamp,
            'from': {'emailAddress': {'address': parseaddr(sender)[1]}},
            'body': {'contentType': 'text', 'content': body_text(payload) or raw.get('body_text') or raw.get('snippet', '')}}


class GmailMCP:
    ordered = False  # Gmail search does not promise ascending chronology.
    provider = 'gmail'

    def __init__(self, client, server):
        self.client, self.server = client, server
        self.own_address = None

    async def call(self, tool, arguments):
        return content(await self.client.rpc('mcpServer/tool/call', {
            'threadId': self.client.thread_id, 'server': self.server,
            'tool': 'gmail.' + tool, 'arguments': arguments}))

    async def profile(self):
        data = await self.call('get_profile', {})
        if not isinstance(data, dict):
            raise OutlookError('Gmail 邮箱身份响应格式无效')
        self.own_address = data.get('emailAddress') or data.get('email') or data.get('email_address')
        return self.own_address

    async def list_watched_folders(self):
        return [{'id': k, 'path': label, 'kind': k, 'direction': 'sent' if k == 'sentitems' else 'received'}
                for k, label in [('inbox', 'Inbox'), ('archive', 'Archive'), ('sentitems', 'Sent')]]

    async def list_messages(self, since, through, skip=0, top=50, *, folder_id, direction='received'):
        start = datetime.fromisoformat(since.replace('Z', '+00:00')).timestamp()
        end = datetime.fromisoformat(through.replace('Z', '+00:00')).timestamp()
        # Widen by one second, then filter precisely; Google's date literals use PST.
        scope = {'inbox': 'in:inbox', 'archive': '-in:inbox -in:sent', 'sentitems': 'in:sent'}[folder_id]
        query = f'{scope} -in:spam -in:trash -in:drafts after:{math.floor(start)-1} before:{math.ceil(end)+1}'
        data = await self.call('search_email_ids', {'query': query, 'max_results': top,
                                                     'next_page_token': str(skip) if skip else ''})
        if not isinstance(data, dict):
            raise OutlookError('Gmail 搜索结果格式无效')
        rows = data.get('ids', data.get('message_ids', data.get('messages', data.get('emails'))))
        if not isinstance(rows, list) or len(rows) > top:
            raise OutlookError('Gmail 搜索结果格式无效')
        ids = [row if isinstance(row, str) else row.get('id') if isinstance(row, dict) else None for row in rows]
        if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            raise OutlookError('Gmail 邮件 ID 格式无效')
        token = data.get('nextPageToken', data.get('next_page_token', '')) or ''
        if not isinstance(token, str) or len(token) > 4096 or (token and (token == skip or not ids)):
            raise OutlookError('Gmail 分页游标无效')
        if data.get('has_more') and not token:
            raise OutlookError('Gmail 缺少下一页游标')
        value = []
        if ids:
            result = await self.call('batch_read_email', {'message_ids': ids})
            messages = result if isinstance(result, list) else result.get('messages', result.get('emails', result.get('value')))
            if not isinstance(messages, list) or {m.get('id') for m in messages if isinstance(m, dict)} != set(ids) or len(messages) != len(ids):
                raise OutlookError('Gmail 邮件批次不完整，本次未推进检查位置')
            value = [normalize(m) for m in messages]
            value = [m for m in value if start <= datetime.fromisoformat(m['receivedDateTime'].replace('Z', '+00:00')).timestamp() <= end]
        return {'value': value, 'has_more': bool(token), 'next_from_index': token,
                'scanned': len(ids)}
