"""Парсинг VLESS-ссылок и поиск клиентов в старых 3x-ui SQLite БД."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import structlog


logger = structlog.get_logger(__name__)

_UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


@dataclass(frozen=True)
class XUiClient:
    uuid: str
    email: str
    expiry_time_ms: int
    enable: bool
    total_gb: int
    source_db: str
    inbound_id: int

    @property
    def has_unlimited_duration(self) -> bool:
        return self.expiry_time_ms == 0


def get_old_db_dir() -> Path:
    """Возвращает каталог со старыми 3x-ui БД (настраивается через OLD_X_UI_DB_DIR)."""
    raw = os.getenv('OLD_X_UI_DB_DIR', '/app/old-x-ui-db')
    return Path(raw)


def extract_uuid_from_vless(url: str) -> str | None:
    """Достаёт UUID из VLESS-ссылки.

    Формат: vless://<uuid>@host:port?...#remark
    Также принимает «голый» UUID в качестве входа.
    """
    if not url:
        return None
    value = url.strip()
    if _UUID_RE.match(value):
        return value.lower()
    parsed = urlparse(value)
    if parsed.scheme.lower() != 'vless' or not parsed.username:
        return None
    candidate = parsed.username.strip()
    if not _UUID_RE.match(candidate):
        return None
    return candidate.lower()


def find_client_by_uuid(uuid: str, db_dir: Path | None = None) -> XUiClient | None:
    """Ищет клиента с указанным UUID во всех *.db файлах каталога.

    Клиенты в 3x-ui хранятся в поле `inbounds.settings` (JSON со списком clients).
    Возвращает первый найденный клиент или None.
    """
    if not uuid:
        return None
    uuid_lc = uuid.lower()
    directory = db_dir or get_old_db_dir()
    if not directory.exists():
        logger.warning('Каталог со старыми 3x-ui БД не найден', path=str(directory))
        return None

    for db_path in sorted(directory.glob('*.db')):
        try:
            found = _scan_db_for_uuid(db_path, uuid_lc)
        except sqlite3.Error as error:
            logger.warning('Не удалось прочитать 3x-ui БД', path=str(db_path), error=str(error))
            continue
        if found is not None:
            return found
    return None


def _scan_db_for_uuid(db_path: Path, uuid_lc: str) -> XUiClient | None:
    uri = f'file:{db_path}?mode=ro'
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute('SELECT id, settings FROM inbounds')
        for row in cursor.fetchall():
            settings_raw = row['settings']
            if not settings_raw:
                continue
            try:
                parsed = json.loads(settings_raw)
            except (TypeError, ValueError):
                continue
            clients = parsed.get('clients') if isinstance(parsed, dict) else None
            if not isinstance(clients, list):
                continue
            for client in clients:
                if not isinstance(client, dict):
                    continue
                client_id = str(client.get('id') or '').lower()
                if client_id != uuid_lc:
                    continue
                return XUiClient(
                    uuid=client_id,
                    email=str(client.get('email') or ''),
                    expiry_time_ms=int(client.get('expiryTime') or 0),
                    enable=bool(client.get('enable', True)),
                    total_gb=int(client.get('totalGB') or 0),
                    source_db=db_path.name,
                    inbound_id=int(row['id']),
                )
    return None
