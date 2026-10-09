"""Per-installation paths and validated, non-secret mailbox preferences."""
from __future__ import annotations
import json
import os
import re
import tempfile
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
# Optional .env only configures this installation; never overrides process variables.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=False)
except ImportError:
    pass
DATA = Path(os.getenv('ATLAS_DATA_DIR', str(ROOT / 'data'))).expanduser().resolve()
CONFIG_PATH = DATA / 'mail_account.json'


def defaults():
    return {'provider': 'outlook', 'email': '', 'timeZone': os.getenv('ATLAS_TIMEZONE', 'Europe/Berlin'),
            'historySince': (date.today() - timedelta(days=30)).isoformat(),
            'mcpServer': os.getenv('ATLAS_MCP_SERVER', 'codex_apps'), 'shareWithModel': False,
            'verified': False}


def validate(data):
    if not isinstance(data, dict):
        raise ValueError('邮箱配置必须是对象')
    value = {k: data.get(k, v) for k, v in defaults().items() if k != 'verified'}
    if not isinstance(value['provider'], str) or value['provider'] not in {'outlook', 'gmail'}:
        raise ValueError('邮箱类型请选择 Outlook 或 Gmail')
    email = value['email']
    if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', email):
        raise ValueError('请填写要连接的邮箱地址')
    value['email'] = email.strip().casefold()
    try:
        ZoneInfo(value['timeZone'])
        since = date.fromisoformat(value['historySince'])
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise ValueError('请选择有效时区和邮件起始日期') from None
    if since > date.today():
        raise ValueError('邮件起始日期不能晚于今天')
    if not isinstance(value['mcpServer'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value['mcpServer']):
        raise ValueError('MCP server 名称无效')
    if not isinstance(value['shareWithModel'], bool):
        raise ValueError('请选择是否允许向自己的模型发送邮件摘要')
    return value


def read():
    if not CONFIG_PATH.exists():
        return defaults()
    try:
        raw = json.loads(CONFIG_PATH.read_text())
        return {**validate(raw), 'verified': raw.get('verified') is True}
    except (OSError, ValueError, TypeError):
        raise ValueError('邮箱配置无法读取，请检查本地配置文件') from None


def write(value):
    CONFIG_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix='.mail-account-', dir=CONFIG_PATH.parent)
    try:
        with os.fdopen(fd, 'w') as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(path, CONFIG_PATH)
    finally:
        Path(path).unlink(missing_ok=True)
    return value


def zone():
    return ZoneInfo(read()['timeZone'])


def empty_model():
    today = date.today()
    return {'schemaVersion': 1, 'title': 'Email Dashboard', 'snapshotDate': today.isoformat(),
            'windowStart': (today - timedelta(days=180)).isoformat(), 'windowEnd': today.isoformat(),
            'timezone': str(zone()), 'methodology': '仅按邮件证据整理，手动编辑优先。',
            'categories': ['工作', '生活', '其他'], 'items': [], 'todayUpdates': [],
            'todayMailSummary': {'receivedCount': 0, 'groupedRelevantCount': 0, 'promotionalCount': 0},
            'lastInboxUpdate': None}
