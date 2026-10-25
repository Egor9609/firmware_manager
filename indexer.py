"""indexer.py — индексатор библиотеки прошивок.

Сканирует корневую папку, читает архивы БЕЗ распаковки, парсит имена
файлов и текстовые описания, складывает всё в SQLite.

Фазы:
    0. Сканирование архивов и чтение текстовых описаний (кэш метаданных).
    1. Сбор всех файлов прошивок (обычных и внутри архивов).
    2. Параллельный парсинг имён и слияние с текстовыми метаданными.
    3. Запись в SQLite.

Оригиналы прошивок не изменяются: архивы открываются только на чтение.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from archive_reader import (
    MAX_TEXT_SIZE,
    ArchiveEntry,
    ArchiveError,
    ArchivePasswordError,
    ArchiveReader,
    decode_bytes,
    is_archive,
    is_description,
    is_firmware,
    sanitize_filename,
    unique_path,
)
from credentials import CredentialManager
from parser import (
    BRAND_ALIASES,
    MODEL_ALIASES,
    FirmwareMeta,
    FirmwareNameParser,
    TextDescriptionParser,
)

DEFAULT_WORKSPACE = r"C:\FirmwareWorkspace"
DEFAULT_DB = "firmware_index.db"
DEFAULT_PASSWORDS = "passwords.json"

# Крупные архивы (>1 ГБ) не читаем ради описаний: у солидных RAR/7Z
# извлечение одного .txt может распаковывать гигабайты. Имена прошивок
# из них всё равно индексируются, а описания можно добавить вручную.
MAX_DESCRIPTION_ARCHIVE_SIZE = 1024 * 1024 * 1024
MAX_DESCRIPTION_FILES = 5

# Вложенные архивы (архив в архиве). Разбираем не глубже MAX_NESTED_DEPTH,
# крупные вложенные архивы пропускаем, чтобы не распаковывать гигабайты.
MAX_NESTED_DEPTH = 2
MAX_NESTED_ARCHIVE_SIZE = 2 * 1024 * 1024 * 1024
MAX_NESTED_PER_ARCHIVE = 200000

# Версия формата кэша: меняется, когда в кэше появляются новые поля
# (например, текст описаний) — иначе старый кэш не даст их перечитать.
CACHE_VERSION = "2"

ProgressCallback = Callable[[int, str, int, int], None]

_SKIP_DIRS = {
    "$recycle.bin",
    "system volume information",
    "__macosx",
    ".git",
    ".svn",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS firmware (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL UNIQUE,
    archive_path TEXT,
    file_size INTEGER DEFAULT 0,
    brand TEXT,
    model TEXT,
    engine TEXT,
    ecu_name TEXT,
    ecu_id TEXT,
    author TEXT,
    eco_standard TEXT,
    version_tag TEXT,
    extra_tags TEXT,
    has_gbo INTEGER DEFAULT 0,
    is_tune INTEGER DEFAULT 0,
    is_stock INTEGER DEFAULT 0,
    imm_off INTEGER DEFAULT 0,
    year INTEGER,
    status TEXT DEFAULT 'unknown',
    description_file_path TEXT,
    indexed_at TEXT,
    manual INTEGER DEFAULT 0,
    search_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_firmware_brand ON firmware(brand);
CREATE INDEX IF NOT EXISTS idx_firmware_model ON firmware(model);
CREATE INDEX IF NOT EXISTS idx_firmware_ecu ON firmware(ecu_name);
CREATE INDEX IF NOT EXISTS idx_firmware_ecu_id ON firmware(ecu_id);
CREATE INDEX IF NOT EXISTS idx_firmware_author ON firmware(author);
CREATE INDEX IF NOT EXISTS idx_firmware_status ON firmware(status);
CREATE INDEX IF NOT EXISTS idx_firmware_year ON firmware(year);
CREATE INDEX IF NOT EXISTS idx_firmware_archive ON firmware(archive_path);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS nested_cache (
    cache_key TEXT PRIMARY KEY,
    outer_path TEXT NOT NULL,
    signature TEXT NOT NULL,
    entries_json TEXT NOT NULL,
    scanned_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_nested_cache_outer ON nested_cache(outer_path);

CREATE TABLE IF NOT EXISTS archive_cache (
    path TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    scanned_at TEXT
);

CREATE TABLE IF NOT EXISTS archive_scan (
    path TEXT PRIMARY KEY,
    name TEXT,
    size INTEGER DEFAULT 0,
    mtime REAL DEFAULT 0,
    files INTEGER DEFAULT 0,
    nested INTEGER DEFAULT 0,
    error TEXT,
    scanned_at TEXT,
    from_cache INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS duplicate_groups (
    group_key TEXT PRIMARY KEY,
    name TEXT,
    size INTEGER DEFAULT 0,
    copies INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS duplicate_members (
    firmware_id INTEGER PRIMARY KEY,
    group_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_dup_members_group ON duplicate_members(group_key);

CREATE VIRTUAL TABLE IF NOT EXISTS firmware_fts USING fts5(
    text,
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TABLE IF NOT EXISTS favorites (
    firmware_id INTEGER PRIMARY KEY,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS filter_sets (
    name TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS workspace_copies (
    firmware_id INTEGER PRIMARY KEY,
    file_name TEXT,
    copied_at TEXT
);
"""

_META_FIELDS = (
    "brand", "model", "engine", "ecu_name", "ecu_id", "author",
    "eco_standard", "version_tag", "extra_tags", "has_gbo", "is_tune",
    "is_stock", "imm_off", "year",
)

_UPSERT_SQL = """
INSERT INTO firmware (
    filename, file_path, archive_path, file_size,
    brand, model, engine, ecu_name, ecu_id, author,
    eco_standard, version_tag, extra_tags,
    has_gbo, is_tune, is_stock, imm_off, year,
    status, description_file_path, indexed_at, manual, search_text, description_text
) VALUES (
    :filename, :file_path, :archive_path, :file_size,
    :brand, :model, :engine, :ecu_name, :ecu_id, :author,
    :eco_standard, :version_tag, :extra_tags,
    :has_gbo, :is_tune, :is_stock, :imm_off, :year,
    :status, :description_file_path, :indexed_at, 0, :search_text, :description_text
)
ON CONFLICT(file_path) DO UPDATE SET
    filename = excluded.filename,
    archive_path = excluded.archive_path,
    file_size = excluded.file_size,
    indexed_at = excluded.indexed_at,
    brand = CASE WHEN firmware.manual = 1 THEN firmware.brand ELSE excluded.brand END,
    model = CASE WHEN firmware.manual = 1 THEN firmware.model ELSE excluded.model END,
    engine = CASE WHEN firmware.manual = 1 THEN firmware.engine ELSE excluded.engine END,
    ecu_name = CASE WHEN firmware.manual = 1 THEN firmware.ecu_name ELSE excluded.ecu_name END,
    ecu_id = CASE WHEN firmware.manual = 1 THEN firmware.ecu_id ELSE excluded.ecu_id END,
    author = CASE WHEN firmware.manual = 1 THEN firmware.author ELSE excluded.author END,
    eco_standard = CASE WHEN firmware.manual = 1 THEN firmware.eco_standard ELSE excluded.eco_standard END,
    version_tag = CASE WHEN firmware.manual = 1 THEN firmware.version_tag ELSE excluded.version_tag END,
    extra_tags = CASE WHEN firmware.manual = 1 THEN firmware.extra_tags ELSE excluded.extra_tags END,
    has_gbo = CASE WHEN firmware.manual = 1 THEN firmware.has_gbo ELSE excluded.has_gbo END,
    is_tune = CASE WHEN firmware.manual = 1 THEN firmware.is_tune ELSE excluded.is_tune END,
    is_stock = CASE WHEN firmware.manual = 1 THEN firmware.is_stock ELSE excluded.is_stock END,
    imm_off = CASE WHEN firmware.manual = 1 THEN firmware.imm_off ELSE excluded.imm_off END,
    year = CASE WHEN firmware.manual = 1 THEN firmware.year ELSE excluded.year END,
    status = CASE WHEN firmware.manual = 1 THEN firmware.status ELSE excluded.status END,
    description_file_path = CASE WHEN firmware.manual = 1
        THEN firmware.description_file_path ELSE excluded.description_file_path END,
    description_text = CASE WHEN firmware.manual = 1
        THEN firmware.description_text ELSE excluded.description_text END,
    search_text = CASE WHEN firmware.manual = 1 THEN firmware.search_text ELSE excluded.search_text END
"""

_SORT_MAP = {
    "brand": "brand IS NULL, brand, model, filename",
    "filename": "filename COLLATE NOCASE",
    "size": "file_size DESC",
    "newest": "indexed_at DESC",
    "year": "year DESC",
}


class FirmwareIndexer:
    """Сканирует библиотеку и предоставляет доступ к индексу."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB,
        passwords_file: str = DEFAULT_PASSWORDS,
        workspace_path: Optional[str] = None,
    ) -> None:
        self.db_path = os.path.abspath(db_path)
        self.credentials = CredentialManager(passwords_file)
        self.reader = ArchiveReader(self.credentials)
        self.name_parser = FirmwareNameParser()
        self.text_parser = TextDescriptionParser(self.name_parser)
        # Общее состояние прогресса: под-сообщения не должны сбивать счётчик.
        self._progress_lock = threading.Lock()
        self._progress_phase = 0
        self._progress_done = 0
        self._progress_total = 0
        self._nested_seen = 0
        self._scan_started = 0.0
        self._init_db()
        self._ensure_fts()
        if workspace_path:
            self.set_setting("workspace_path", os.path.abspath(workspace_path))
        elif not self.get_setting("workspace_path"):
            self.set_setting("workspace_path", DEFAULT_WORKSPACE)

    # ------------------------------------------------------------------ БД

    def _connect(self) -> sqlite3.Connection:
        # PRAGMA journal_mode повторно здесь не ставим: смена режима журнала
        # требует блокировки и на каждом соединении подвешивает параллельный
        # скан (см. _init_db, режим WAL ставится один раз).
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            try:
                connection.execute("SELECT description_text FROM firmware LIMIT 1")
            except sqlite3.Error:
                # Миграция существующей базы: колонка для текста описаний.
                connection.execute(
                    "ALTER TABLE firmware ADD COLUMN description_text TEXT"
                )
            try:
                connection.execute("SELECT crc32 FROM firmware LIMIT 1")
            except sqlite3.Error:
                # Кэш контрольных сумм: считаем один раз, потом берём из базы.
                connection.execute("ALTER TABLE firmware ADD COLUMN crc32 TEXT")
            try:
                connection.execute("SELECT count(*) FROM firmware_fts").fetchone()
                self._fts_ready = True
            except sqlite3.Error:
                # SQLite без FTS5 — поиск продолжит работать через LIKE.
                self._fts_ready = False
        finally:
            connection.close()

    def _rebuild_fts(self) -> None:
        """Перестраивает полнотекстовый индекс (1–2 с на 142 тыс. записей)."""
        if not getattr(self, "_fts_ready", False):
            return
        started = time.time()
        connection = self._connect()
        try:
            connection.execute("DELETE FROM firmware_fts")
            connection.execute(
                "INSERT INTO firmware_fts(rowid, text) "
                "SELECT id, search_text || ' ' || COALESCE(description_text, '') "
                "FROM firmware WHERE search_text IS NOT NULL"
            )
            connection.commit()
        except sqlite3.Error as exc:
            self._fts_ready = False
            self._log(f"FTS недоступен, поиск через LIKE: {exc}")
        finally:
            connection.close()
        self._log(
            f"поисковый индекс перестроен за {round(time.time() - started, 1)} с"
        )

    def _ensure_fts(self) -> None:
        """Строит индекс при первом запуске после обновления базы."""
        if not getattr(self, "_fts_ready", False):
            return
        connection = self._connect()
        try:
            indexed = connection.execute(
                "SELECT count(*) FROM firmware_fts"
            ).fetchone()[0]
            rows = connection.execute("SELECT count(*) FROM firmware").fetchone()[0]
        finally:
            connection.close()
        if rows and not indexed:
            self._rebuild_fts()

    @staticmethod
    def _fts_query(text: str) -> Optional[str]:
        """Запрос FTS5: каждое слово — префиксом, все слова обязательны."""
        if not text:
            return None
        tokens = [
            token
            for token in re.split(r"[^\w]+", str(text).lower(), flags=re.UNICODE)
            if token
        ]
        if not tokens:
            return None
        return " ".join(f'"{token}"*' for token in tokens)

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default
        finally:
            connection.close()

    def set_setting(self, key: str, value: Optional[str]) -> None:
        connection = self._connect()
        try:
            if value is None:
                connection.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)),
                )
            connection.commit()
        finally:
            connection.close()

    # ------------------------------------------------------------- утилиты

    @staticmethod
    def _log(message: str) -> None:
        try:
            print(f"[индексация] {message}", flush=True)
        except (UnicodeEncodeError, OSError):
            pass

    def _emit(
        self,
        callback: Optional[ProgressCallback],
        phase: int,
        message: str,
        done: Optional[int] = None,
        total: Optional[int] = None,
    ) -> None:
        """Публикует прогресс.

        done/total=None означает «не менять счётчик»: так под-сообщения
        (разбор вложенного архива) не заставляют полосу прыгать назад.
        """
        with self._progress_lock:
            if phase is not None:
                self._progress_phase = int(phase)
            if total is not None:
                self._progress_total = int(total)
                if self._progress_done > self._progress_total:
                    self._progress_done = self._progress_total
            if done is not None:
                self._progress_done = max(0, int(done))
            snapshot = (
                self._progress_phase,
                self._progress_done,
                self._progress_total,
            )

        self._log(message)
        if callback:
            try:
                callback(snapshot[0], message, snapshot[1], snapshot[2])
            except Exception:  # callback не должен ломать индексацию
                pass

    @staticmethod
    def _eta(started: float, done: int, total: int) -> str:
        """Грубая оценка остатка времени по средней скорости."""
        if done <= 0 or total <= 0 or done >= total:
            return ""
        elapsed = time.time() - started
        if elapsed <= 0 or elapsed > 86400:
            return ""
        remaining = elapsed / done * (total - done)
        if remaining < 90:
            return f"~{int(remaining)} с"
        return f"~{max(1, int(round(remaining / 60)))} мин"

    @staticmethod
    def _read_text_file(path: str, limit: int = MAX_TEXT_SIZE) -> str:
        try:
            with open(path, "rb") as handle:
                return decode_bytes(handle.read(limit))
        except OSError:
            return ""

    def _backup_db(self, keep: int = 5) -> Optional[str]:
        """Копия базы перед переиндексацией (страховка от неудачного скана)."""
        if not os.path.isfile(self.db_path):
            return None
        backup_dir = os.path.join(os.path.dirname(self.db_path), "backups")
        try:
            os.makedirs(backup_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = os.path.join(backup_dir, f"firmware_index-{stamp}.db")
            connection = self._connect()
            try:
                with sqlite3.connect(target) as destination:
                    connection.backup(destination)
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            self._log(f"не удалось сделать бэкап базы: {exc}")
            return None

        try:
            backups = sorted(
                name for name in os.listdir(backup_dir)
                if name.startswith("firmware_index-") and name.endswith(".db")
            )
            for stale in backups[:-keep]:
                os.remove(os.path.join(backup_dir, stale))
        except OSError:
            pass
        return target

    # -------------------------------------------------------------- скан

    def scan_and_index(
        self,
        root_dir: str,
        max_workers: int = 8,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> Dict[str, object]:
        """Полный скан папки и запись индекса. Возвращает статистику."""
        started = time.time()
        self._scan_started = started
        self._nested_seen = 0
        root = os.path.abspath(root_dir)
        if not os.path.isdir(root):
            raise ValueError(f"Папка не найдена: {root}")

        # Подхватываем пароли, добавленные в passwords.json без перезапуска.
        if self.credentials.reload_if_changed():
            self._log("passwords.json перечитан")

        backup = self._backup_db()
        if backup:
            self._log(f"резервная копия базы: {os.path.basename(backup)}")

        self.set_setting("root_path", root)

        archives, plain_by_dir, texts_by_dir = self._walk(root)
        plain_count = sum(len(items) for items in plain_by_dir.values())
        self._seed_archives(archives)
        self._emit(
            progress_cb, 0,
            f"Найдено архивов: {len(archives)}, файлов прошивок: {plain_count}",
            0, len(archives),
        )

        archive_results: List[Dict[str, object]] = []
        errors: List[str] = []

        # ---- Фаза 0: архивы и текстовые описания -------------------------
        if archives:
            phase_started = time.time()
            cached_archives = 0
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(self._scan_archive, path, progress_cb): path
                    for path in archives
                }
                for index, future in enumerate(as_completed(futures), start=1):
                    result = future.result()
                    archive_results.append(result)
                    if result.get("cached"):
                        cached_archives += 1
                    self._record_archive(
                        str(result["archive"]), result, bool(result.get("cached"))
                    )
                    error = result.get("error")
                    if error:
                        errors.append(f"{result['archive']}: {error}")
                    for nested_error in result.get("nested_errors") or []:
                        errors.append(f"{result['archive']}: {nested_error}")
                    percent = int(index * 100 / len(archives))
                    eta = self._eta(phase_started, index, len(archives))
                    tail = f", осталось {eta}" if eta else ""
                    self._emit(
                        progress_cb, 0,
                        f"Архивы {index}/{len(archives)} ({percent}%){tail} — "
                        f"{os.path.basename(str(result['archive']))}",
                        index, len(archives),
                    )
            if cached_archives:
                self._log(
                    f"из кэша взято архивов: {cached_archives} из {len(archives)}"
                )
        self._prune_archives(
            {str(result["archive"]) for result in archive_results}
        )

        # Текстовые описания рядом с обычными файлами.
        plain_cache: Dict[str, Dict[str, object]] = {}
        for directory, text_paths in texts_by_dir.items():
            firmware_paths = plain_by_dir.get(directory, [])
            if not firmware_paths:
                continue
            names = [os.path.basename(path) for path in firmware_paths]
            for text_path in text_paths[:10]:
                text = self._read_text_file(text_path)
                if not text:
                    continue
                parsed = self.text_parser.parse(text, names)
                for filename, meta in parsed.items():
                    target = os.path.join(directory, filename)
                    entry = plain_cache.setdefault(
                        target, {"meta": {}, "description_file": text_path}
                    )
                    entry["meta"].update(meta)  # type: ignore[union-attr]
                    entry["description_file"] = text_path

        self._emit(progress_cb, 1, "Сбор списка прошивок…", 0, 0)

        # ---- Фаза 1: записи ----------------------------------------------
        records: List[Dict[str, object]] = []
        for directory, paths in plain_by_dir.items():
            for path in paths:
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                records.append(
                    {
                        "filename": os.path.basename(path),
                        "file_path": path,
                        "archive_path": None,
                        "file_size": size,
                        "cache": plain_cache.get(path),
                    }
                )

        for result in archive_results:
            archive_path = str(result["archive"])
            cache = result.get("cache") or {}
            for member in result["members"]:  # type: ignore[union-attr]
                records.append(
                    {
                        "filename": os.path.basename(member.name),
                        "file_path": f"{archive_path}::{member.name}",
                        "archive_path": archive_path,
                        "file_size": member.size,
                        "cache": cache.get(member.name),
                    }
                )
            for nested in result.get("nested") or []:  # type: ignore[union-attr]
                chain = list(nested["chain"]) + [nested["name"]]
                records.append(
                    {
                        "filename": os.path.basename(nested["name"]),
                        "file_path": f"{archive_path}::" + "::".join(chain),
                        "archive_path": archive_path,
                        "file_size": nested["size"],
                        "cache": nested.get("cache"),
                    }
                )

        self._emit(
            progress_cb, 1,
            f"Всего прошивок к обработке: {len(records)}", 0, len(records),
        )

        # ---- Фаза 2: параллельный парсинг --------------------------------
        rows: List[Dict[str, object]] = []
        if records:
            phase_started = time.time()
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(self._build_row, record, root): record
                    for record in records
                }
                for index, future in enumerate(as_completed(futures), start=1):
                    try:
                        rows.append(future.result())
                    except Exception as exc:  # одна запись не должна ронять скан
                        errors.append(f"{futures[future]['file_path']}: {exc}")
                    if index % 200 == 0 or index == len(records):
                        percent = int(index * 100 / len(records))
                        eta = self._eta(phase_started, index, len(records))
                        tail = f", осталось {eta}" if eta else ""
                        self._emit(
                            progress_cb, 2,
                            f"Обработка {index}/{len(records)} "
                            f"({percent}%){tail}",
                            index, len(records),
                        )

        # ---- Фаза 3: запись в SQLite -------------------------------------
        self._emit(progress_cb, 3, "Запись индекса в базу…", 0, len(rows))
        classified = sum(1 for row in rows if row["status"] == "classified")
        unknown = len(rows) - classified
        now = datetime.now().isoformat(timespec="seconds")

        connection = self._connect()
        try:
            connection.execute("BEGIN")
            connection.execute(
                "CREATE TEMP TABLE IF NOT EXISTS seen(path TEXT PRIMARY KEY)"
            )
            connection.execute("DELETE FROM seen")
            connection.executemany(
                "INSERT OR IGNORE INTO seen(path) VALUES (?)",
                [(row["file_path"],) for row in rows],
            )

            chunk_size = 500
            for start in range(0, len(rows), chunk_size):
                chunk = rows[start:start + chunk_size]
                connection.executemany(_UPSERT_SQL, chunk)

            connection.execute(
                "DELETE FROM firmware WHERE file_path NOT IN (SELECT path FROM seen)"
            )
            connection.execute(
                "INSERT INTO meta(key, value) VALUES('last_scan', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (now,),
            )
            connection.execute(
                "INSERT INTO meta(key, value) VALUES('last_scan_stats', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (
                    f"{len(rows)} файлов, классифицировано {classified}, "
                    f"неизвестно {unknown}, ошибок {len(errors)}",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        inherited_filled = self.inherit_from_neighbors()["filled"]
        duplicate_groups = self.mark_duplicates()["groups"]
        self._rebuild_fts()

        duration = round(time.time() - started, 1)
        summary = {
            "root": root,
            "total": len(rows),
            "classified": classified,
            "unknown": unknown,
            "archives": len(archives),
            "errors": errors,
            "duration": duration,
            "inherited": inherited_filled,
            "duplicates": duplicate_groups,
        }
        self._emit(
            progress_cb, 3,
            f"Готово: {len(rows)} прошивок за {duration} с "
            f"(ошибок: {len(errors)})",
            len(rows), len(rows),
        )
        return summary

    # ------------------------------------------------------------- проход

    def _walk(
        self, root: str
    ) -> Tuple[List[str], Dict[str, List[str]], Dict[str, List[str]]]:
        """Собирает архивы, прошивки и описания, сгруппированные по папкам."""
        archives: List[str] = []
        plain_by_dir: Dict[str, List[str]] = defaultdict(list)
        texts_by_dir: Dict[str, List[str]] = defaultdict(list)
        workspace = os.path.abspath(self.get_workspace_path())

        for directory, dirnames, filenames in os.walk(root):
            allowed: List[str] = []
            for dirname in dirnames:
                full = os.path.join(directory, dirname)
                if dirname.startswith("."):
                    continue
                if dirname.lower() in _SKIP_DIRS:
                    continue
                if os.path.islink(full):
                    continue
                if os.path.abspath(full) == workspace:
                    continue
                allowed.append(dirname)
            dirnames[:] = allowed

            for filename in filenames:
                path = os.path.join(directory, filename)
                if os.path.islink(path):
                    continue
                if is_archive(filename):
                    archives.append(path)
                elif is_firmware(filename):
                    plain_by_dir[directory].append(path)
                elif is_description(filename):
                    texts_by_dir[directory].append(path)

        return archives, dict(plain_by_dir), dict(texts_by_dir)

    def _scan_archive(
        self,
        archive_path: str,
        progress_cb: Optional[ProgressCallback] = None,
        position: int = 0,
        total: int = 0,
    ) -> Dict[str, object]:
        """Фаза 0 для одного архива: файлы, описания, вложенные архивы."""
        signature = self._signature(archive_path)
        cached = self._archive_cache_get(archive_path, signature)
        if cached is not None:
            return cached

        try:
            entries = self.reader.list_files(archive_path)
        except ArchiveError as exc:
            return {
                "archive": archive_path,
                "members": [],
                "cache": {},
                "nested": [],
                "nested_errors": [str(exc)],
                "error": str(exc),
            }

        members = [
            entry for entry in entries if not entry.is_dir and is_firmware(entry.name)
        ]
        descriptions = [
            entry for entry in entries if not entry.is_dir and is_description(entry.name)
        ]
        try:
            archive_size = os.path.getsize(archive_path)
        except OSError:
            archive_size = 0
        if archive_size > MAX_DESCRIPTION_ARCHIVE_SIZE:
            descriptions = []

        cache = self._parse_descriptions(
            archive_path, descriptions, [member.name for member in members]
        )

        nested: List[Dict[str, object]] = []
        nested_errors: List[str] = []
        self._collect_nested(
            outer_path=archive_path,
            signature=signature,
            archive_path=archive_path,
            entries=entries,
            chain=[],
            depth=0,
            progress_cb=progress_cb,
            position=position,
            total=total,
            result=nested,
            errors=nested_errors,
        )

        result: Dict[str, object] = {
            "archive": archive_path,
            "members": members,
            "cache": cache,
            "nested": nested,
            "nested_errors": nested_errors,
            "error": None,
        }
        self._archive_cache_set(archive_path, signature, result)
        return result

    # ------------------------------------------------------- вложенные архивы

    def _signature(self, path: str) -> str:
        """Отпечаток архива: размер, mtime и версия passwords.json.

        Правка паролей меняет отпечаток — иначе зашифрованный архив
        остался бы «пустым» в кэше после добавления пароля.
        """
        try:
            stat = os.stat(path)
            own = f"{stat.st_size}:{int(stat.st_mtime)}"
        except OSError:
            own = "0:0"
        try:
            pstat = os.stat(self.credentials.passwords_file)
            passwords = f"{int(pstat.st_mtime)}:{pstat.st_size}"
        except OSError:
            passwords = "0:0"
        return f"{own}:{passwords}:{CACHE_VERSION}"

    def _parse_descriptions(
        self,
        archive_path: str,
        descriptions: List[ArchiveEntry],
        firmware_names: List[str],
        limit: int = MAX_DESCRIPTION_FILES,
    ) -> Dict[str, Dict[str, object]]:
        """Читает описания из архива и раскладывает метаданные по файлам."""
        cache: Dict[str, Dict[str, object]] = {}
        for description in descriptions[:limit]:
            if description.size and description.size > MAX_TEXT_SIZE:
                continue
            try:
                text = self.reader.read_text(archive_path, description.name)
            except ArchiveError:
                continue
            if not text:
                continue
            parsed = self.text_parser.parse(text, firmware_names)
            for filename, meta in parsed.items():
                entry = cache.setdefault(
                    filename, {"meta": {}, "description_file": description.name}
                )
                entry["meta"].update(meta)  # type: ignore[union-attr]
                entry["description_file"] = description.name
                # Текст описания тоже идёт в поиск: в .txt часто есть марка,
                # модель и автор, которых нет в имени файла.
                entry.setdefault("text", text[:2000])
        return cache

    def _list_nested(self, archive_path: str) -> List[Dict[str, object]]:
        """Оглавление вложенного архива в виде простых словарей (для кэша)."""
        return [
            {
                "name": entry.name,
                "size": int(entry.size or 0),
                "is_dir": bool(entry.is_dir),
                "is_firmware": bool(not entry.is_dir and is_firmware(entry.name)),
                "is_archive": bool(not entry.is_dir and is_archive(entry.name)),
                "is_description": bool(
                    not entry.is_dir and is_description(entry.name)
                ),
            }
            for entry in self.reader.list_files(archive_path)
        ]

    def _collect_nested(
        self,
        outer_path: str,
        signature: str,
        archive_path: str,
        entries: List[ArchiveEntry],
        chain: List[str],
        depth: int,
        progress_cb: Optional[ProgressCallback],
        position: int,
        total: int,
        result: List[Dict[str, object]],
        errors: List[str],
    ) -> None:
        """Рекурсивно разбирает архивы внутри архива (до MAX_NESTED_DEPTH).

        Вложенные архивы извлекаются пачками через 7z: один процесс на
        пачку вместо одного процесса на каждый файл. Иначе большие сборки
        (сотни мелких .rar внутри .zip) обрабатываются десятки минут.
        """
        if depth >= MAX_NESTED_DEPTH:
            return

        children: List[Tuple[ArchiveEntry, List[str], str]] = []
        for entry in entries:
            if entry.is_dir or not is_archive(entry.name):
                continue
            member_chain = list(chain) + [entry.name]
            label = "::".join(member_chain)
            if entry.size and entry.size > MAX_NESTED_ARCHIVE_SIZE:
                errors.append(
                    f"{label}: {entry.size // (1024 ** 2)} МБ больше лимита "
                    f"{MAX_NESTED_ARCHIVE_SIZE // (1024 ** 2)} МБ — пропущен"
                )
                continue
            children.append((entry, member_chain, label))

        if not children:
            return

        for batch in self._batch_children(children):
            temp_dir = tempfile.mkdtemp(prefix="fwnested_")
            try:
                extracted = self.reader.extract_many(
                    archive_path, [entry.name for entry, _c, _l in batch], temp_dir
                )
                for entry, member_chain, label in batch:
                    nested_file = extracted.get(str(entry.name))
                    if not nested_file:
                        errors.append(f"{label}: не удалось извлечь")
                        continue
                    with self._progress_lock:
                        self._nested_seen += 1
                        seen = self._nested_seen
                    elapsed = int(
                        time.time() - (self._scan_started or time.time())
                    )
                    self._emit(
                        progress_cb, 0,
                        f"Разбор #{seen} ({elapsed // 60}:{elapsed % 60:02d}) "
                        f"{os.path.basename(outer_path)} → {'/'.join(member_chain)}",
                    )
                    self._process_nested_archive(
                        outer_path=outer_path,
                        signature=signature,
                        nested_file=nested_file,
                        member_chain=member_chain,
                        depth=depth,
                        progress_cb=progress_cb,
                        position=position,
                        total=total,
                        result=result,
                        errors=errors,
                    )
            except Exception as exc:  # noqa: BLE001 — один архив не должен ронять скан
                errors.append(f"{'::'.join(chain)}: {type(exc).__name__}: {exc}")
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

    @staticmethod
    def _batch_children(
        children: List[Tuple[ArchiveEntry, List[str], str]],
        max_files: int = 60,
        max_bytes: int = 3 * 1024 * 1024 * 1024,
    ) -> List[List[Tuple[ArchiveEntry, List[str], str]]]:
        """Делит вложенные архивы на пачки, чтобы не забить диск распаковкой."""
        batches: List[List[Tuple[ArchiveEntry, List[str], str]]] = []
        current: List[Tuple[ArchiveEntry, List[str], str]] = []
        size = 0
        for child in children:
            child_size = int(child[0].size or 0)
            if current and (
                len(current) >= max_files or size + child_size > max_bytes
            ):
                batches.append(current)
                current = []
                size = 0
            current.append(child)
            size += child_size
        if current:
            batches.append(current)
        return batches

    def _process_nested_archive(
        self,
        outer_path: str,
        signature: str,
        nested_file: str,
        member_chain: List[str],
        depth: int,
        progress_cb: Optional[ProgressCallback],
        position: int,
        total: int,
        result: List[Dict[str, object]],
        errors: List[str],
    ) -> None:
        """Собирает файлы из уже распакованного вложенного архива."""
        cache_key = outer_path + "::" + "::".join(member_chain)
        listing = self._nested_cache_get(cache_key, signature)
        if listing is None:
            try:
                listing = self._list_nested(nested_file)
            except ArchiveError as exc:
                errors.append(f"{'::'.join(member_chain)}: {exc}")
                return
            self._nested_cache_set(cache_key, outer_path, signature, listing)

        firmware_items = [item for item in listing if item.get("is_firmware")]
        archive_items = [item for item in listing if item.get("is_archive")]
        description_items = [
            ArchiveEntry(name=str(item["name"]), size=int(item.get("size") or 0))
            for item in listing
            if item.get("is_description")
        ]

        nested_cache: Dict[str, Dict[str, object]] = {}
        if description_items:
            nested_cache = self._parse_descriptions(
                nested_file,
                description_items,
                [str(item["name"]) for item in firmware_items],
                limit=1,
            )

        for item in firmware_items:
            name = str(item["name"])
            description = nested_cache.get(name)
            payload: Optional[Dict[str, object]] = None
            if description:
                description_file = str(description.get("description_file") or "")
                payload = {
                    "meta": dict(description.get("meta") or {}),  # type: ignore[arg-type]
                    "description_file": "::".join(
                        member_chain + [description_file]
                    ),
                    "text": description.get("text"),
                }
            result.append(
                {
                    "chain": list(member_chain),
                    "name": name,
                    "size": int(item.get("size") or 0),
                    "cache": payload,
                }
            )

        if archive_items and depth + 1 < MAX_NESTED_DEPTH:
            self._collect_nested(
                outer_path=outer_path,
                signature=signature,
                archive_path=nested_file,
                entries=[
                    ArchiveEntry(
                        name=str(item["name"]),
                        size=int(item.get("size") or 0),
                        is_dir=bool(item.get("is_dir")),
                    )
                    for item in listing
                ],
                chain=member_chain,
                depth=depth + 1,
                progress_cb=progress_cb,
                position=position,
                total=total,
                result=result,
                errors=errors,
            )

    def _nested_cache_get(
        self, cache_key: str, signature: str
    ) -> Optional[List[Dict[str, object]]]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT entries_json, signature FROM nested_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        finally:
            connection.close()
        if not row or row["signature"] != signature:
            return None
        try:
            value = json.loads(row["entries_json"])
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, list) else None

    def _nested_cache_set(
        self,
        cache_key: str,
        outer_path: str,
        signature: str,
        listing: List[Dict[str, object]],
    ) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO nested_cache"
                "(cache_key, outer_path, signature, entries_json, scanned_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
                "outer_path = excluded.outer_path, "
                "signature = excluded.signature, "
                "entries_json = excluded.entries_json, "
                "scanned_at = excluded.scanned_at",
                (
                    cache_key,
                    outer_path,
                    signature,
                    json.dumps(listing, ensure_ascii=False),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    # ------------------------------------------------- кэш целых архивов

    @staticmethod
    def _dump_members(members: Iterable[ArchiveEntry]) -> List[Dict[str, object]]:
        return [
            {
                "name": entry.name,
                "size": int(entry.size or 0),
                "is_dir": bool(entry.is_dir),
                "encrypted": bool(entry.encrypted),
                "mtime": float(entry.mtime or 0.0),
            }
            for entry in members
        ]

    @staticmethod
    def _load_members(items: object) -> List[ArchiveEntry]:
        if not isinstance(items, list):
            return []
        result: List[ArchiveEntry] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            result.append(
                ArchiveEntry(
                    name=str(item.get("name") or ""),
                    size=int(item.get("size") or 0),
                    is_dir=bool(item.get("is_dir")),
                    encrypted=bool(item.get("encrypted")),
                    mtime=float(item.get("mtime") or 0.0),
                )
            )
        return result

    def _archive_cache_get(
        self, path: str, signature: str
    ) -> Optional[Dict[str, object]]:
        """Готовый результат прошлого скана архива, если файл не менялся."""
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT payload_json, signature FROM archive_cache WHERE path = ?",
                (path,),
            ).fetchone()
        finally:
            connection.close()
        if not row or row["signature"] != signature:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return {
            "archive": path,
            "members": self._load_members(payload.get("members")),
            "cache": payload.get("cache") if isinstance(payload.get("cache"), dict) else {},
            "nested": payload.get("nested") if isinstance(payload.get("nested"), list) else [],
            "nested_errors": (
                payload.get("nested_errors")
                if isinstance(payload.get("nested_errors"), list)
                else []
            ),
            "error": None,
            "cached": True,
        }

    def _archive_cache_set(
        self, path: str, signature: str, result: Dict[str, object]
    ) -> None:
        payload = {
            "members": self._dump_members(result.get("members") or []),  # type: ignore[arg-type]
            "cache": result.get("cache") or {},
            "nested": result.get("nested") or [],
            "nested_errors": result.get("nested_errors") or [],
        }
        try:
            encoded = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO archive_cache(path, signature, payload_json, scanned_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                "signature = excluded.signature, "
                "payload_json = excluded.payload_json, "
                "scanned_at = excluded.scanned_at",
                (
                    path,
                    signature,
                    encoded,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _build_row(self, record: Dict[str, object], root: str) -> Dict[str, object]:
        """Фаза 2: парсинг имени + слияние с текстовыми метаданными."""
        file_path = str(record["file_path"])
        meta = self.name_parser.parse(file_path, root)

        cache = record.get("cache")
        description_file = None
        description_text = None
        if isinstance(cache, dict):
            text_meta = cache.get("meta") or {}
            if isinstance(text_meta, dict):
                self._merge_text_meta(meta, text_meta)
            description_file = cache.get("description_file")
            description_text = cache.get("text")

        classified = bool(meta.brand or meta.model or meta.ecu_name)
        status = "classified" if classified else "unknown"

        search_parts = [
            record["filename"],
            meta.brand,
            meta.model,
            meta.engine,
            meta.ecu_name,
            meta.ecu_id,
            meta.author,
            meta.eco_standard,
            meta.version_tag,
            ", ".join(meta.extra_tags) if meta.extra_tags else None,
            str(meta.year) if meta.year else None,
        ]
        # Архив и папки над ним тоже участвуют в поиске: люди ищут
        # по имени коллекции ("Tazoff", "ADACT", "Складка"), а не по файлу.
        archive_path = record.get("archive_path")
        if archive_path:
            parts = [
                part for part in str(archive_path).replace("/", "\\").split("\\")
                if part
            ]
            if parts:
                search_parts.extend(parts[-3:])
                search_parts.append(os.path.splitext(parts[-1])[0])

        # Добавляем синонимы (рус/англ) только для модели,
        # чтобы поиск работал на любом языке. Синонимы марки не
        # добавляем: каноническое имя марки уже содержит "ваз"/"lada".
        if meta.model:
            for model, aliases in MODEL_ALIASES.items():
                if model == meta.model:
                    search_parts.extend(aliases)
        search_text = " ".join(str(part) for part in search_parts if part).lower()

        return {
            "filename": record["filename"],
            "file_path": file_path,
            "archive_path": record["archive_path"],
            "file_size": int(record["file_size"] or 0),
            "brand": meta.brand,
            "model": meta.model,
            "engine": meta.engine,
            "ecu_name": meta.ecu_name,
            "ecu_id": meta.ecu_id,
            "author": meta.author,
            "eco_standard": meta.eco_standard,
            "version_tag": meta.version_tag,
            "extra_tags": ", ".join(meta.extra_tags) if meta.extra_tags else None,
            "has_gbo": int(meta.has_gbo),
            "is_tune": int(meta.is_tune),
            "is_stock": int(meta.is_stock),
            "imm_off": int(meta.imm_off),
            "year": meta.year,
            "status": status,
            "description_file_path": description_file,
            "description_text": description_text,
            "indexed_at": datetime.now().isoformat(timespec="seconds"),
            "search_text": search_text,
        }

    @staticmethod
    def _merge_text_meta(meta: FirmwareMeta, text_meta: Dict[str, object]) -> None:
        """Описание из .txt имеет приоритет над именем файла."""
        for field_name in (
            "brand", "model", "engine", "ecu_name", "ecu_id", "author",
            "eco_standard", "version_tag",
        ):
            value = text_meta.get(field_name)
            if value not in (None, ""):
                setattr(meta, field_name, str(value))

        year = text_meta.get("year")
        if year not in (None, "", 0):
            try:
                meta.year = int(year)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass

        for flag in ("has_gbo", "is_tune", "is_stock", "imm_off"):
            if text_meta.get(flag):
                setattr(meta, flag, True)

        extra = text_meta.get("extra_tags")
        if extra:
            for tag in str(extra).split(","):
                tag = tag.strip()
                if tag and tag not in meta.extra_tags:
                    meta.extra_tags.append(tag)

    @staticmethod
    def _parent_dir(file_path: str) -> str:
        """Папка внутри архива (или на диске), в которой лежит файл."""
        normalized = str(file_path).replace("/", "\\")
        parent, _sep, _name = normalized.rpartition("\\")
        return parent

    @staticmethod
    def _dominant(values: List[str], share: float = 0.6) -> Optional[str]:
        if not values:
            return None
        counts: Dict[str, int] = {}
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        value, count = max(counts.items(), key=lambda item: item[1])
        if count < 2 or count / len(values) < share:
            return None
        return value

    def inherit_from_neighbors(self) -> Dict[str, object]:
        """Дополняет марку/модель по соседям в той же папке.

        В папках вида ".../ПОПКОРН/b174dr04.bin" марки в имени нет, но
        рядом обычно лежат файлы с понятными именами.
        """
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id, file_path, filename, archive_path, brand, model, "
                "engine, ecu_name, ecu_id, author, eco_standard, version_tag, "
                "extra_tags, has_gbo, is_tune, is_stock, imm_off, year, "
                "status, search_text, manual FROM firmware"
            ).fetchall()
        finally:
            connection.close()

        groups: Dict[str, Dict[str, List]] = {}
        for row in rows:
            parent = self._parent_dir(str(row["file_path"]))
            bucket = groups.setdefault(
                parent, {"rows": [], "brand": [], "model": [], "ecu": []}
            )
            bucket["rows"].append(row)
            for key, column in (("brand", "brand"), ("model", "model"), ("ecu", "ecu_name")):
                if row[column]:
                    bucket[key].append(str(row[column]))

        updates: List[Tuple[object, ...]] = []
        for _parent, bucket in groups.items():
            unknown = [
                row for row in bucket["rows"]
                if not row["manual"] and not row["brand"] and not row["model"]
            ]
            if not unknown or len(bucket["rows"]) < 2:
                continue
            brand = self._dominant(bucket["brand"])
            model = self._dominant(bucket["model"])
            ecu = self._dominant(bucket["ecu"], 0.7)
            if not brand and not ecu:
                continue

            for row in unknown:
                values: Dict[str, object] = {
                    "brand": row["brand"] or brand,
                    "model": row["model"] or model,
                    "engine": row["engine"],
                    "ecu_name": row["ecu_name"] or ecu,
                    "ecu_id": row["ecu_id"],
                    "author": row["author"],
                    "eco_standard": row["eco_standard"],
                    "version_tag": row["version_tag"],
                    "extra_tags": row["extra_tags"],
                    "has_gbo": row["has_gbo"],
                    "is_tune": row["is_tune"],
                    "is_stock": row["is_stock"],
                    "imm_off": row["imm_off"],
                    "year": row["year"],
                }
                status = (
                    "classified"
                    if values["brand"] or values["model"] or values["ecu_name"]
                    else "unknown"
                )
                search_text = self._compose_search_text(
                    str(row["filename"]), row["archive_path"], values
                )
                updates.append(
                    (
                        values["brand"], values["model"], values["ecu_name"],
                        status, search_text, row["id"],
                    )
                )

        if updates:
            connection = self._connect()
            try:
                connection.execute("BEGIN")
                for start in range(0, len(updates), 500):
                    connection.executemany(
                        "UPDATE firmware SET brand = ?, model = ?, ecu_name = ?, "
                        "status = ?, search_text = ? WHERE id = ?",
                        updates[start:start + 500],
                    )
                connection.commit()
            finally:
                connection.close()

        return {"filled": len(updates)}

    # ------------------------------------------------- переразбор имён

    @staticmethod
    def _compose_search_text(
        filename: str, archive_path: Optional[str], values: Dict[str, object]
    ) -> str:
        parts: List[object] = [
            filename,
            values.get("brand"),
            values.get("model"),
            values.get("engine"),
            values.get("ecu_name"),
            values.get("ecu_id"),
            values.get("author"),
            values.get("eco_standard"),
            values.get("version_tag"),
            values.get("extra_tags"),
            str(values["year"]) if values.get("year") else None,
        ]
        if archive_path:
            segments = [
                segment
                for segment in str(archive_path).replace("/", "\\").split("\\")
                if segment
            ]
            if segments:
                parts.extend(segments[-3:])
                parts.append(os.path.splitext(segments[-1])[0])
        model = values.get("model")
        if model:
            for model_name, aliases in MODEL_ALIASES.items():
                if model_name == model:
                    parts.extend(aliases)
        # Латинские варианты ЭБУ: "М74М" → "M74M", "М74.91" → "M74.91".
        # Позволяет искать привычной латиницей, не переключая раскладку.
        ecu_name = values.get("ecu_name")
        if ecu_name:
            latin_ecu = str(ecu_name).replace("М", "M").replace("м", "m")
            if latin_ecu != ecu_name:
                parts.append(latin_ecu)
        return " ".join(str(part) for part in parts if part).lower()

    def reparse_all(
        self,
        root: Optional[str] = None,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> Dict[str, object]:
        """Переразбирает имена и пути прямо по базе, не открывая архивы.

        Нужен после улучшений парсера: пересканировать гигабайты архивов
        не требуется — берём file_path из базы. Заполняются только пустые
        поля, ручные правки (manual=1) не трогаются.
        """
        started = time.time()
        root_dir = str(root or self.get_setting("root_path") or "")

        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id, file_path, filename, archive_path, brand, model, engine, "
                "ecu_name, ecu_id, author, eco_standard, version_tag, extra_tags, "
                "has_gbo, is_tune, is_stock, imm_off, year, status, search_text, manual "
                "FROM firmware"
            ).fetchall()
        finally:
            connection.close()

        total = len(rows)
        updates: List[Tuple[object, ...]] = []
        gained = 0
        phase_started = time.time()

        for index, row in enumerate(rows, start=1):
            if index % 500 == 0 or index == total:
                percent = int(index * 100 / total) if total else 100
                eta = self._eta(phase_started, index, total)
                tail = f", осталось {eta}" if eta else ""
                self._emit(
                    progress_cb, 4,
                    f"Переразбор имён {index}/{total} ({percent}%){tail}",
                    index, total,
                )

            if row["manual"]:
                continue

            meta = self.name_parser.parse(str(row["file_path"]), root_dir or None)
            values: Dict[str, object] = {
                "brand": row["brand"] or meta.brand,
                "model": row["model"] or meta.model,
                "engine": row["engine"] or meta.engine,
                "ecu_name": row["ecu_name"] or meta.ecu_name,
                "ecu_id": row["ecu_id"] or meta.ecu_id,
                "author": row["author"] or meta.author,
                "eco_standard": row["eco_standard"] or meta.eco_standard,
                "version_tag": row["version_tag"] or meta.version_tag,
                "extra_tags": row["extra_tags"]
                or (", ".join(meta.extra_tags) if meta.extra_tags else None),
                "has_gbo": 1 if (row["has_gbo"] or meta.has_gbo) else 0,
                "is_tune": 1 if (row["is_tune"] or meta.is_tune) else 0,
                "is_stock": 1 if (row["is_stock"] or meta.is_stock) else 0,
                "imm_off": 1 if (row["imm_off"] or meta.imm_off) else 0,
                "year": row["year"] or meta.year,
            }
            status = (
                "classified"
                if values["brand"] or values["model"] or values["ecu_name"]
                else "unknown"
            )
            search_text = self._compose_search_text(
                str(row["filename"]), row["archive_path"], values
            )

            unchanged = (
                status == row["status"]
                and search_text == row["search_text"]
                and all(values[key] == row[key] for key in values)
            )
            if unchanged:
                continue
            if status == "classified" and row["status"] == "unknown":
                gained += 1

            updates.append(
                (
                    values["brand"], values["model"], values["engine"],
                    values["ecu_name"], values["ecu_id"], values["author"],
                    values["eco_standard"], values["version_tag"],
                    values["extra_tags"], values["has_gbo"], values["is_tune"],
                    values["is_stock"], values["imm_off"], values["year"],
                    status, search_text, row["id"],
                )
            )

        if updates:
            connection = self._connect()
            try:
                connection.execute("BEGIN")
                for start in range(0, len(updates), 500):
                    connection.executemany(
                        "UPDATE firmware SET brand = ?, model = ?, engine = ?, "
                        "ecu_name = ?, ecu_id = ?, author = ?, eco_standard = ?, "
                        "version_tag = ?, extra_tags = ?, has_gbo = ?, is_tune = ?, "
                        "is_stock = ?, imm_off = ?, year = ?, status = ?, "
                        "search_text = ? WHERE id = ?",
                        updates[start:start + 500],
                    )
                connection.commit()
            finally:
                connection.close()

        inherited = self.inherit_from_neighbors()
        duplicates = self.mark_duplicates()
        self._rebuild_fts()
        duration = round(time.time() - started, 1)
        summary = {
            "total": total,
            "changed": len(updates),
            "newly_classified": gained,
            "inherited": inherited["filled"],
            "duplicates": duplicates["groups"],
            "duration": duration,
        }
        self._emit(
            progress_cb, 4,
            f"Переразбор готов: обновлено {len(updates)}, новых "
            f"классифицированных {gained} за {duration} с",
            total, total,
        )
        return summary

    # ------------------------------------------------------------- поиск

    def search(
        self,
        q: Optional[str] = None,
        brand: Optional[str] = None,
        model: Optional[str] = None,
        engine: Optional[str] = None,
        archive: Optional[str] = None,
        ecu: Optional[str] = None,
        author: Optional[str] = None,
        gbo: Optional[bool] = None,
        tune: Optional[bool] = None,
        stock: Optional[bool] = None,
        status: Optional[str] = None,
        favorites: bool = False,
        page: int = 1,
        per_page: int = 50,
        sort: str = "brand",
    ) -> Dict[str, object]:
        """Поиск с фильтрами и пагинацией.

        При наличии FTS5 запрос идёт по полнотекстовому индексу: быстро и
        с сортировкой по релевантности. Если индекс не дал результатов
        (или FTS недоступен) — обычный LIKE, чтобы подстрочный поиск работал.
        """
        where: List[str] = []
        params: List[object] = []

        if brand:
            where.append("brand = ?")
            params.append(brand)
        if model:
            where.append("model LIKE ?")
            params.append(f"%{model}%")
        if engine:
            # "1.6" находит и "1.6", и "1.6 8V", и "1.6 16V".
            where.append("engine LIKE ?")
            params.append(f"{engine}%")
        if archive:
            where.append("archive_path LIKE ?")
            params.append(f"%{archive}%")
        if ecu:
            where.append("(ecu_name LIKE ? OR ecu_id LIKE ?)")
            params.append(f"%{ecu}%")
            params.append(f"%{ecu}%")
        if author:
            where.append("author LIKE ?")
            params.append(f"%{author}%")
        if gbo:
            where.append("has_gbo = 1")
        if tune:
            where.append("is_tune = 1")
        if stock:
            where.append("is_stock = 1")
        if status in ("classified", "unknown"):
            where.append("status = ?")
            params.append(status)
        if favorites:
            where.append(
                "firmware.id IN (SELECT firmware_id FROM favorites)"
            )

        page = max(1, int(page))
        per_page = min(500, max(1, int(per_page)))
        offset = (page - 1) * per_page
        filters_sql = " WHERE " + " AND ".join(where) if where else ""

        fts_query = (
            self._fts_query(q)
            if (q and getattr(self, "_fts_ready", False))
            else None
        )
        if fts_query:
            found = self._search_fts(
                fts_query, filters_sql, params, sort, per_page, offset, page
            )
            if found["total"]:
                return found

        # LIKE-поиск: и фолбэк, и режим без FTS.
        like_where = list(where)
        like_params = list(params)
        if q:
            for token in str(q).lower().split():
                like_where.append(
                    "(search_text LIKE ? OR description_text LIKE ?)"
                )
                like_params.append(f"%{token}%")
                like_params.append(f"%{token}%")
        like_sql = " WHERE " + " AND ".join(like_where) if like_where else ""
        order_sql = _SORT_MAP.get(sort, _SORT_MAP["brand"])

        connection = self._connect()
        try:
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM firmware{like_sql}", like_params
            ).fetchone()["count"]
            rows = connection.execute(
                f"SELECT firmware.*, g.copies AS dup_copies, "
                f"(fav.firmware_id IS NOT NULL) AS fav, "
                f"(wc.firmware_id IS NOT NULL) AS in_workspace "
                f"FROM firmware "
                f"LEFT JOIN duplicate_members m ON m.firmware_id = firmware.id "
                f"LEFT JOIN duplicate_groups g ON g.group_key = m.group_key "
                f"LEFT JOIN favorites fav ON fav.firmware_id = firmware.id "
                f"LEFT JOIN workspace_copies wc ON wc.firmware_id = firmware.id"
                f"{like_sql} ORDER BY {order_sql} "
                f"LIMIT ? OFFSET ?",
                like_params + [per_page, offset],
            ).fetchall()
        finally:
            connection.close()
        return self._pack_result(rows, total, page, per_page)

    def _search_fts(
        self,
        fts_query: str,
        filters_sql: str,
        params: List[object],
        sort: str,
        per_page: int,
        offset: int,
        page: int,
    ) -> Dict[str, object]:
        """Поиск по индексу FTS5; сортировка по релевантности — bm25."""
        source = (
            "JOIN (SELECT rowid AS fid, bm25(firmware_fts) AS rank "
            "FROM firmware_fts WHERE firmware_fts MATCH ?) fts "
            "ON fts.fid = firmware.id"
        )
        order_sql = (
            "fts.rank"
            if sort == "relevance"
            else _SORT_MAP.get(sort, _SORT_MAP["brand"])
        )
        connection = self._connect()
        try:
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM firmware {source}{filters_sql}",
                [fts_query] + params,
            ).fetchone()["count"]
            rows = connection.execute(
                f"SELECT firmware.*, g.copies AS dup_copies, "
                f"(fav.firmware_id IS NOT NULL) AS fav, "
                f"(wc.firmware_id IS NOT NULL) AS in_workspace FROM firmware {source} "
                f"LEFT JOIN duplicate_members m ON m.firmware_id = firmware.id "
                f"LEFT JOIN duplicate_groups g ON g.group_key = m.group_key "
                f"LEFT JOIN favorites fav ON fav.firmware_id = firmware.id "
                f"LEFT JOIN workspace_copies wc ON wc.firmware_id = firmware.id"
                f"{filters_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
                [fts_query] + params + [per_page, offset],
            ).fetchall()
        except sqlite3.Error as exc:
            self._log(f"FTS-запрос не удался, ухожу на LIKE: {exc}")
            return {"items": [], "total": 0, "page": page, "per_page": per_page, "pages": 0}
        finally:
            connection.close()
        return self._pack_result(rows, total, page, per_page)

    @staticmethod
    def _pack_result(
        rows: Iterable[sqlite3.Row], total: int, page: int, per_page: int
    ) -> Dict[str, object]:
        pages = (total + per_page - 1) // per_page if total else 0
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
        }

    def get(self, firmware_id: int) -> Optional[Dict[str, object]]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM firmware WHERE id = ?", (firmware_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def unknown_list(
        self, limit: int = 50, offset: int = 0, count_only: bool = False
    ) -> Dict[str, object]:
        connection = self._connect()
        try:
            total = connection.execute(
                "SELECT COUNT(*) AS count FROM firmware WHERE status = 'unknown'"
            ).fetchone()["count"]
            if count_only:
                return {"total": total, "items": []}
            rows = connection.execute(
                "SELECT * FROM firmware WHERE status = 'unknown' "
                "ORDER BY filename LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        finally:
            connection.close()
        return {"total": total, "items": [dict(row) for row in rows]}

    def update_firmware(self, firmware_id: int, data: Dict[str, object]) -> Dict[str, object]:
        """Ручное обновление метаданных (страница Unknown)."""
        row = self.get(firmware_id)
        if not row:
            raise ValueError("Прошивка не найдена")

        allowed_text = (
            "brand", "model", "engine", "ecu_name", "ecu_id", "author",
            "eco_standard", "version_tag", "extra_tags",
        )
        allowed_flags = ("has_gbo", "is_tune", "is_stock", "imm_off")

        updates: Dict[str, object] = {}
        for field_name in allowed_text:
            if field_name in data:
                value = data.get(field_name)
                value = str(value).strip() if value is not None else ""
                updates[field_name] = value or None
        for field_name in allowed_flags:
            if field_name in data:
                updates[field_name] = 1 if data.get(field_name) else 0
        if "year" in data:
            value = data.get("year")
            if value in (None, "", 0, "0"):
                updates["year"] = None
            else:
                try:
                    updates["year"] = int(value)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    updates["year"] = None

        merged = dict(row)
        merged.update(updates)

        if "status" in data and data.get("status") in ("classified", "unknown"):
            status = str(data["status"])
        else:
            status = (
                "classified"
                if merged.get("brand") or merged.get("model") or merged.get("ecu_name")
                else "unknown"
            )
        updates["status"] = status

        search_parts = [
            merged.get("filename"), merged.get("brand"), merged.get("model"),
            merged.get("engine"), merged.get("ecu_name"), merged.get("ecu_id"),
            merged.get("author"), merged.get("eco_standard"),
            merged.get("version_tag"), merged.get("extra_tags"), merged.get("year"),
        ]
        if merged.get("model"):
            for model, aliases in MODEL_ALIASES.items():
                if model == merged.get("model"):
                    search_parts.extend(aliases)
        search_text = " ".join(str(part) for part in search_parts if part).lower()
        updates["search_text"] = search_text
        updates["manual"] = 1

        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [firmware_id]

        connection = self._connect()
        try:
            connection.execute(
                f"UPDATE firmware SET {assignments} WHERE id = ?", values
            )
            if getattr(self, "_fts_ready", False):
                fts_text = (
                    f"{search_text} {merged.get('description_text') or ''}"
                ).strip()
                connection.execute(
                    "DELETE FROM firmware_fts WHERE rowid = ?", (firmware_id,)
                )
                connection.execute(
                    "INSERT INTO firmware_fts(rowid, text) VALUES (?, ?)",
                    (firmware_id, fts_text),
                )
            connection.commit()
        finally:
            connection.close()

        updated = self.get(firmware_id)
        if updated is None:
            raise ValueError("Прошивка не найдена после обновления")
        return updated

    def reparse_unknowns(self) -> Dict[str, object]:
        """Повторно разбирает только «неизвестные» текущими правилами парсера.

        Не трогает ручные правки и не пересканирует архивы: берёт уже
        сохранённые file_path и заново применяет к ним справочники/правила.
        """
        root = self.get_setting("root_path") or ""
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM firmware "
                "WHERE status='unknown' AND COALESCE(manual, 0)=0"
            ).fetchall()
        finally:
            connection.close()

        updates: List[Tuple[object, ...]] = []
        classified = 0
        for row in rows:
            meta = self.name_parser.parse(str(row["file_path"]), root or None)
            brand = row["brand"] or meta.brand
            model = row["model"] or meta.model
            ecu_name = row["ecu_name"] or meta.ecu_name
            ecu_id = row["ecu_id"] or meta.ecu_id
            status = "classified" if (brand or model or ecu_name) else "unknown"
            if status == "classified":
                classified += 1
            old = (
                row["brand"], row["model"], row["ecu_name"],
                row["ecu_id"], row["status"],
            )
            new = (brand, model, ecu_name, ecu_id, status)
            if old == new:
                continue

            search_parts = [
                row["filename"], brand, model, row["engine"], ecu_name, ecu_id,
                row["author"], row["eco_standard"], row["version_tag"],
                row["extra_tags"], row["year"],
            ]
            if model:
                for model_name, aliases in MODEL_ALIASES.items():
                    if model_name == model:
                        search_parts.extend(aliases)
                        break
            search_text = " ".join(
                str(part) for part in search_parts if part
            ).lower()
            updates.append(
                (brand, model, ecu_name, ecu_id, status, search_text, row["id"])
            )

        if updates:
            connection = self._connect()
            try:
                connection.executemany(
                    "UPDATE firmware SET brand = ?, model = ?, ecu_name = ?, "
                    "ecu_id = ?, status = ?, search_text = ? WHERE id = ?",
                    updates,
                )
                connection.commit()
            finally:
                connection.close()

        remaining = len(rows) - classified
        now = datetime.now().isoformat(timespec="seconds")
        self.set_setting(
            "last_reparse",
            f"{now} — проверено {len(rows)}, новых {classified}, осталось {remaining}",
        )
        return {
            "checked": len(rows),
            "classified": classified,
            "remaining": remaining,
            "updated": len(updates),
        }

    @staticmethod
    def _prefer_specific(old: Optional[str], new: Optional[str]) -> Optional[str]:
        """Оставляет более точное значение: M74.9 важнее M74."""
        if not old:
            return new
        if not new:
            return old
        old_text = str(old).strip().lower()
        new_text = str(new).strip().lower()
        if old_text == new_text:
            return old
        if new_text.startswith(old_text) or old_text in new_text:
            return new
        return old

    def reparse_all(self) -> Dict[str, object]:
        """Пересобирает метаданные всех неручных записей текущим парсером.

        Нужен после изменения правил: старые записи могли получить общий
        ЭБУ (M74) там, где в пути уже есть точный (M74.9).
        """
        root = self.get_setting("root_path") or ""
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM firmware WHERE COALESCE(manual, 0)=0"
            ).fetchall()
        finally:
            connection.close()

        updates: List[Tuple[object, ...]] = []
        classified = 0
        for row in rows:
            meta = self.name_parser.parse(str(row["file_path"]), root or None)
            brand = meta.brand or row["brand"]
            model = meta.model or row["model"]
            ecu_name = self._prefer_specific(row["ecu_name"], meta.ecu_name)
            ecu_id = meta.ecu_id or row["ecu_id"]
            status = "classified" if (brand or model or ecu_name) else "unknown"
            if status == "classified":
                classified += 1

            old = (
                row["brand"], row["model"], row["ecu_name"],
                row["ecu_id"], row["status"],
            )
            new = (brand, model, ecu_name, ecu_id, status)
            if old == new:
                continue

            search_parts = [
                row["filename"], brand, model, row["engine"], ecu_name, ecu_id,
                row["author"], row["eco_standard"], row["version_tag"],
                row["extra_tags"], row["year"],
            ]
            if model:
                for model_name, aliases in MODEL_ALIASES.items():
                    if model_name == model:
                        search_parts.extend(aliases)
                        break
            search_text = " ".join(
                str(part) for part in search_parts if part
            ).lower()
            updates.append(
                (brand, model, ecu_name, ecu_id, status, search_text, row["id"])
            )

        chunk_size = 1000
        if updates:
            connection = self._connect()
            try:
                connection.execute("BEGIN")
                for start in range(0, len(updates), chunk_size):
                    connection.executemany(
                        "UPDATE firmware SET brand = ?, model = ?, ecu_name = ?, "
                        "ecu_id = ?, status = ?, search_text = ? WHERE id = ?",
                        updates[start:start + chunk_size],
                    )
                connection.commit()
            finally:
                connection.close()

        remaining = len(rows) - classified
        now = datetime.now().isoformat(timespec="seconds")
        self.set_setting(
            "last_reparse",
            f"{now} — полная пересборка: проверено {len(rows)}, "
            f"классифицировано {classified}, осталось {remaining}",
        )
        return {
            "checked": len(rows),
            "classified": classified,
            "remaining": remaining,
            "updated": len(updates),
        }

    def bulk_update(
        self, ids: Iterable[int], data: Dict[str, object]
    ) -> Dict[str, object]:
        """Массовая разметка: проставляет поля сразу нескольким записям.

        Пустые поля в запросе не меняются — можно проставить только марку,
        не трогая модель и ЭБУ.
        """
        allowed_text = (
            "brand", "model", "engine", "ecu_name", "ecu_id",
            "author", "eco_standard", "version_tag", "extra_tags",
        )
        updates: Dict[str, object] = {}
        for field_name in allowed_text:
            if field_name in data:
                value = data.get(field_name)
                value = str(value).strip() if value is not None else ""
                if value:
                    updates[field_name] = value
        for flag in ("has_gbo", "is_tune", "is_stock", "imm_off"):
            if data.get(flag):
                updates[flag] = 1
        if data.get("year") not in (None, "", 0, "0"):
            try:
                updates["year"] = int(data["year"])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass

        if not updates:
            raise ValueError("Заполните хотя бы одно поле")

        id_list: List[int] = []
        for item in ids:
            try:
                id_list.append(int(item))
            except (TypeError, ValueError):
                continue
        id_list = id_list[:500]
        if not id_list:
            raise ValueError("Не выбрано ни одной прошивки")

        placeholders = ",".join("?" for _ in id_list)
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM firmware WHERE id IN ({placeholders})", id_list
            ).fetchall()

            payload: List[Tuple[object, ...]] = []
            fts_rows: List[Tuple[object, object]] = []
            for row in rows:
                values: Dict[str, object] = {
                    key: row[key]
                    for key in (
                        "brand", "model", "engine", "ecu_name", "ecu_id", "author",
                        "eco_standard", "version_tag", "extra_tags", "has_gbo",
                        "is_tune", "is_stock", "imm_off", "year",
                    )
                }
                values.update(updates)
                status = (
                    "classified"
                    if values["brand"] or values["model"] or values["ecu_name"]
                    else "unknown"
                )
                search_text = self._compose_search_text(
                    str(row["filename"]), row["archive_path"], values
                )
                payload.append(
                    (
                        values["brand"], values["model"], values["engine"],
                        values["ecu_name"], values["ecu_id"], values["author"],
                        values["eco_standard"], values["version_tag"],
                        values["extra_tags"], int(values["has_gbo"] or 0),
                        int(values["is_tune"] or 0), int(values["is_stock"] or 0),
                        int(values["imm_off"] or 0), values["year"],
                        status, search_text, row["id"],
                    )
                )
                fts_rows.append(
                    (
                        row["id"],
                        f"{search_text} {row['description_text'] or ''}".strip(),
                    )
                )

            connection.execute("BEGIN")
            connection.executemany(
                "UPDATE firmware SET brand = ?, model = ?, engine = ?, ecu_name = ?, "
                "ecu_id = ?, author = ?, eco_standard = ?, version_tag = ?, "
                "extra_tags = ?, has_gbo = ?, is_tune = ?, is_stock = ?, imm_off = ?, "
                "year = ?, status = ?, search_text = ?, manual = 1 WHERE id = ?",
                payload,
            )
            if getattr(self, "_fts_ready", False):
                connection.execute(
                    f"DELETE FROM firmware_fts WHERE rowid IN ({placeholders})", id_list
                )
                connection.executemany(
                    "INSERT INTO firmware_fts(rowid, text) VALUES (?, ?)", fts_rows
                )
            connection.commit()
        finally:
            connection.close()

        return {"updated": len(payload), "ids": id_list}

    # ------------------------------------------------- пакетные операции

    def batch_copy(self, ids: Iterable[int]) -> Dict[str, object]:
        """Копирует несколько прошивок в рабочую папку за один запрос."""
        copied: List[str] = []
        errors: List[str] = []
        for item in list(ids)[:200]:
            try:
                result = self.copy_to_workspace(int(item))
                copied.append(str(result["file"]))
            except (ValueError, ArchiveError, OSError) as exc:
                errors.append(f"id {item}: {exc}")
        return {
            "copied": copied,
            "errors": errors[:50],
            "total": len(copied),
            "workspace": self.get_workspace_path(),
        }

    def rows_by_ids(self, ids: Iterable[int]) -> List[Dict[str, object]]:
        """Полные записи по списку id (для экспорта)."""
        id_list: List[int] = []
        for item in ids:
            try:
                id_list.append(int(item))
            except (TypeError, ValueError):
                continue
        id_list = id_list[:5000]
        if not id_list:
            return []
        placeholders = ",".join("?" for _ in id_list)
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT * FROM firmware WHERE id IN ({placeholders}) "
                f"ORDER BY brand, model, filename",
                id_list,
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def batch_checksums(
        self, ids: Iterable[int], force: bool = False
    ) -> Dict[str, object]:
        """Считает CRC32 для выбранных прошивок (с кэшем в базе)."""
        temp_dir = tempfile.mkdtemp(prefix="fwcrc_")
        items: List[Dict[str, object]] = []
        to_store: List[Tuple[str, int]] = []
        try:
            for item in list(ids)[:200]:
                row = self.get(int(item))
                if not row:
                    continue
                entry: Dict[str, object] = {
                    "id": row["id"],
                    "filename": row["filename"],
                    "brand": row["brand"],
                    "model": row["model"],
                    "ecu_name": row["ecu_name"],
                    "crc32": None,
                    "size": row["file_size"],
                    "error": None,
                    "cached": False,
                }
                cached = row.get("crc32")
                if cached and not force:
                    entry["crc32"] = str(cached)
                    entry["cached"] = True
                    items.append(entry)
                    continue
                try:
                    if row["archive_path"]:
                        chain = str(row["file_path"]).split("::")[1:]
                        path = self.reader.extract_chain(
                            str(row["archive_path"]), chain, temp_dir
                        )
                        with open(path, "rb") as handle:
                            data = handle.read()
                        os.remove(path)
                    else:
                        with open(str(row["file_path"]), "rb") as handle:
                            data = handle.read()
                    entry["crc32"] = f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
                    entry["size"] = len(data)
                    to_store.append((str(entry["crc32"]), int(row["id"])))
                except (ArchiveError, OSError) as exc:
                    entry["error"] = str(exc)
                items.append(entry)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if to_store:
            connection = self._connect()
            try:
                connection.executemany(
                    "UPDATE firmware SET crc32 = ? WHERE id = ?", to_store
                )
                connection.commit()
            finally:
                connection.close()

        unique = sorted({str(i["crc32"]) for i in items if i["crc32"]})
        return {
            "items": items,
            "unique": len(unique),
            "identical": len(items) > 1 and len(unique) <= 1,
        }

    def _containers_dir(self) -> str:
        """Постоянный кэш распакованных контейнеров (архив в архиве)."""
        path = os.path.join(os.path.dirname(self.db_path), "cache", "containers")
        os.makedirs(path, exist_ok=True)
        return path

    def _container_cache_key(self, archive_path: str, prefix: List[str]) -> str:
        try:
            stat = os.stat(archive_path)
            signature = f"{stat.st_size}:{int(stat.st_mtime)}"
        except OSError:
            signature = "0:0"
        raw = "|".join([os.path.abspath(archive_path), signature] + prefix)
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:20]

    def _cached_container(self, archive_path: str, chain: List[str]) -> str:
        """Распаковывает контейнер один раз и запоминает на диске.

        Без этого файл из "4pda.rar::...::ADACT.7z::..." распаковывал бы
        весь архив заново при каждом обращении — это минуты на файл.
        """
        prefix = list(chain[:-1])
        if not prefix:
            return archive_path

        base = os.path.join(
            self._containers_dir(),
            self._container_cache_key(archive_path, prefix),
        )
        marker = os.path.join(base, ".ready")
        if os.path.isfile(marker):
            try:
                with open(marker, "r", encoding="utf-8") as handle:
                    stored = handle.read().strip()
                if stored and os.path.isfile(stored):
                    return stored
            except OSError:
                pass

        os.makedirs(base, exist_ok=True)
        current = archive_path
        for depth, name in enumerate(prefix):
            target_dir = os.path.join(base, f"step{depth}")
            os.makedirs(target_dir, exist_ok=True)
            current = self.reader.extract_single(current, name, target_dir)
        try:
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write(current)
        except OSError:
            pass
        self._log(f"контейнер распакован в кэш: {os.path.basename(archive_path)}")
        return current

    def clear_container_cache(self) -> Dict[str, object]:
        """Очищает кэш контейнеров (освобождает диск)."""
        directory = self._containers_dir()
        removed = 0
        freed = 0
        try:
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                freed += _dir_size(path)
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            pass
        return {"removed": removed, "freed": freed}

    def checksum_one(
        self, firmware_id: int, force: bool = False
    ) -> Dict[str, object]:
        """CRC32 одной прошивки. Берёт из базы, если уже считали."""
        row = self.get(firmware_id)
        if not row:
            raise ValueError("Прошивка не найдена")

        entry: Dict[str, object] = {
            "id": row["id"],
            "filename": row["filename"],
            "brand": row["brand"],
            "model": row["model"],
            "ecu_name": row["ecu_name"],
            "crc32": None,
            "size": row["file_size"],
            "error": None,
            "cached": False,
        }
        cached = row.get("crc32")
        if cached and not force:
            entry["crc32"] = str(cached)
            entry["cached"] = True
            return entry

        temp_dir = tempfile.mkdtemp(prefix="fwcrc1_")
        try:
            if row["archive_path"]:
                chain = str(row["file_path"]).split("::")[1:]
                container = self._cached_container(str(row["archive_path"]), chain)
                path = self.reader.extract_single(container, chain[-1], temp_dir)
                with open(path, "rb") as handle:
                    data = handle.read()
            else:
                with open(str(row["file_path"]), "rb") as handle:
                    data = handle.read()
            entry["crc32"] = f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
            entry["size"] = len(data)
        except (ArchiveError, OSError, MemoryError) as exc:
            entry["error"] = str(exc)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if entry["crc32"]:
            connection = self._connect()
            try:
                connection.execute(
                    "UPDATE firmware SET crc32 = ? WHERE id = ?",
                    (entry["crc32"], row["id"]),
                )
                connection.commit()
            finally:
                connection.close()
        return entry

    def batch_checksums(
        self, ids: Iterable[int], force: bool = False
    ) -> Dict[str, object]:
        """CRC32 для нескольких прошивок (последовательно, с кэшем)."""
        items: List[Dict[str, object]] = []
        for item in list(ids)[:200]:
            try:
                items.append(self.checksum_one(int(item), force=force))
            except ValueError:
                continue
        unique = sorted({str(i["crc32"]) for i in items if i["crc32"]})
        return {
            "items": items,
            "unique": len(unique),
            "identical": len(items) > 1 and len(unique) <= 1,
        }

    # ------------------------------------------------- избранное и подборки

    def toggle_favorite(self, firmware_id: int, value: bool = True) -> Dict[str, object]:
        """Ставит/снимает звёздочку «избранное»."""
        connection = self._connect()
        try:
            if value:
                connection.execute(
                    "INSERT INTO favorites(firmware_id, created_at) VALUES (?, ?) "
                    "ON CONFLICT(firmware_id) DO NOTHING",
                    (firmware_id, datetime.now().isoformat(timespec="seconds")),
                )
            else:
                connection.execute(
                    "DELETE FROM favorites WHERE firmware_id = ?", (firmware_id,)
                )
            connection.commit()
            total = connection.execute(
                "SELECT COUNT(*) FROM favorites"
            ).fetchone()[0]
        finally:
            connection.close()
        return {"id": firmware_id, "favorite": bool(value), "total": total}

    def save_filter_set(self, name: str, payload: Dict[str, object]) -> Dict[str, object]:
        """Сохраняет набор фильтров под именем (перезаписывает одноимённый)."""
        name = str(name).strip()
        if not name:
            raise ValueError("Укажите название подборки")
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO filter_sets(name, payload, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET payload = excluded.payload, "
                "created_at = excluded.created_at",
                (
                    name[:100],
                    json.dumps(payload, ensure_ascii=False),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return {"saved": name}

    def list_filter_sets(self) -> List[Dict[str, object]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT name, payload, created_at FROM filter_sets ORDER BY created_at DESC"
            ).fetchall()
        finally:
            connection.close()
        result: List[Dict[str, object]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                payload = {}
            result.append(
                {"name": row["name"], "payload": payload, "created_at": row["created_at"]}
            )
        return result

    def delete_filter_set(self, name: str) -> Dict[str, object]:
        connection = self._connect()
        try:
            cursor = connection.execute(
                "DELETE FROM filter_sets WHERE name = ?", (str(name).strip(),)
            )
            connection.commit()
            removed = cursor.rowcount
        finally:
            connection.close()
        return {"removed": bool(removed)}

    # -------------------------------------------------------- статистика

    def statistics(self) -> Dict[str, object]:
        connection = self._connect()
        try:
            totals = connection.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(file_size), 0) AS size, "
                "COALESCE(SUM(has_gbo), 0) AS gbo, COALESCE(SUM(is_tune), 0) AS tune, "
                "COALESCE(SUM(is_stock), 0) AS stock, COALESCE(SUM(imm_off), 0) AS immo, "
                "COALESCE(SUM(manual), 0) AS manual, "
                "COALESCE(SUM(status = 'classified'), 0) AS classified, "
                "COALESCE(SUM(status = 'unknown'), 0) AS unknown "
                "FROM firmware"
            ).fetchone()
            archives = connection.execute(
                "SELECT COUNT(DISTINCT archive_path) AS count FROM firmware "
                "WHERE archive_path IS NOT NULL"
            ).fetchone()["count"]
            brands = connection.execute(
                "SELECT brand, COUNT(*) AS count FROM firmware "
                "WHERE brand IS NOT NULL AND brand <> '' "
                "GROUP BY brand ORDER BY count DESC LIMIT 15"
            ).fetchall()
            ecus = connection.execute(
                "SELECT ecu_name, COUNT(*) AS count FROM firmware "
                "WHERE ecu_name IS NOT NULL AND ecu_name <> '' "
                "GROUP BY ecu_name ORDER BY count DESC LIMIT 15"
            ).fetchall()
            authors = connection.execute(
                "SELECT author, COUNT(*) AS count FROM firmware "
                "WHERE author IS NOT NULL AND author <> '' "
                "GROUP BY author ORDER BY count DESC LIMIT 15"
            ).fetchall()
            years = connection.execute(
                "SELECT year, COUNT(*) AS count FROM firmware "
                "WHERE year IS NOT NULL GROUP BY year ORDER BY year DESC LIMIT 12"
            ).fetchall()
            last_scan = connection.execute(
                "SELECT value FROM meta WHERE key = 'last_scan'"
            ).fetchone()
            last_stats = connection.execute(
                "SELECT value FROM meta WHERE key = 'last_scan_stats'"
            ).fetchone()
            last_reparse = connection.execute(
                "SELECT value FROM meta WHERE key = 'last_reparse'"
            ).fetchone()
        finally:
            connection.close()

        return {
            "total": totals["total"],
            "total_size": totals["size"],
            "classified": totals["classified"],
            "unknown": totals["unknown"],
            "gbo": totals["gbo"],
            "tune": totals["tune"],
            "stock": totals["stock"],
            "immo": totals["immo"],
            "manual": totals["manual"],
            "archives": archives,
            "brands": [dict(row) for row in brands],
            "ecus": [dict(row) for row in ecus],
            "authors": [dict(row) for row in authors],
            "years": [dict(row) for row in years],
            "last_scan": last_scan["value"] if last_scan else None,
            "last_scan_stats": last_stats["value"] if last_stats else None,
            "last_reparse": last_reparse["value"] if last_reparse else None,
            "workspace": self.get_workspace_path(),
            "root": self.get_setting("root_path") or "",
        }

    def filters(self) -> Dict[str, List[str]]:
        """Списки значений для комбобоксов фильтров."""
        connection = self._connect()
        try:
            def column_values(column: str, limit: int = 400) -> List[str]:
                rows = connection.execute(
                    f"SELECT DISTINCT {column} AS value FROM firmware "
                    f"WHERE {column} IS NOT NULL AND {column} <> '' "
                    f"ORDER BY {column} LIMIT ?",
                    (limit,),
                ).fetchall()
                return [row["value"] for row in rows]

            def column_values_by_count(column: str, limit: int = 200) -> List[str]:
                rows = connection.execute(
                    f"SELECT {column} AS value, COUNT(*) AS count FROM firmware "
                    f"WHERE {column} IS NOT NULL AND {column} <> '' "
                    f"GROUP BY {column} ORDER BY count DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [row["value"] for row in rows]

            def counts_by(column: str, limit: int = 400) -> List[Dict[str, object]]:
                rows = connection.execute(
                    f"SELECT {column} AS value, COUNT(*) AS count FROM firmware "
                    f"WHERE {column} IS NOT NULL AND {column} <> '' "
                    f"GROUP BY {column} ORDER BY count DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [{"value": row["value"], "count": row["count"]} for row in rows]

            def archive_list() -> List[Dict[str, object]]:
                rows = connection.execute(
                    "SELECT name, files FROM archive_scan WHERE files > 0 "
                    "ORDER BY files DESC"
                ).fetchall()
                return [{"value": row["name"], "count": row["files"]} for row in rows]

            models_by_brand: Dict[str, List[str]] = {}
            for row in connection.execute(
                "SELECT brand, model, COUNT(*) AS count FROM firmware "
                "WHERE brand IS NOT NULL AND model IS NOT NULL AND model <> '' "
                "GROUP BY brand, model ORDER BY count DESC"
            ):
                models_by_brand.setdefault(str(row["brand"]), []).append(str(row["model"]))

            return {
                "brands": column_values("brand"),
                "models": column_values("model"),
                "models_by_brand": models_by_brand,
                "engines": column_values_by_count("engine"),
                "ecus": column_values("ecu_name"),
                "authors": column_values("author"),
                "euros": column_values("eco_standard", 20),
                "brand_counts": counts_by("brand", 400),
                "model_counts": counts_by("model", 400),
                "engine_counts": counts_by("engine", 200),
                "ecu_counts": counts_by("ecu_name", 400),
                "author_counts": counts_by("author", 200),
                "archives": archive_list(),
                # Алиасы нужны для подсказок: "газ" → GAZ, "гранта" → Granta.
                "brand_aliases": BRAND_ALIASES,
                "model_aliases": MODEL_ALIASES,
            }
        finally:
            connection.close()

    # --------------------------------------------------------- workspace

    def get_workspace_path(self) -> str:
        value = self.get_setting("workspace_path") or DEFAULT_WORKSPACE
        return os.path.abspath(value)

    def set_workspace_path(self, path: str) -> str:
        if not path or not str(path).strip():
            raise ValueError("Путь к рабочей папке не может быть пустым")
        absolute = os.path.abspath(os.path.expandvars(str(path).strip()))
        os.makedirs(absolute, exist_ok=True)
        self.set_setting("workspace_path", absolute)
        return absolute

    def copy_to_workspace(self, firmware_id: int) -> Dict[str, object]:
        """Копирует прошивку и связанное описание в рабочую папку."""
        # Если пароль только что добавили в passwords.json — подхватываем его.
        self.credentials.reload_if_changed()

        row = self.get(firmware_id)
        if not row:
            raise ValueError("Прошивка не найдена")

        workspace = self.get_workspace_path()
        os.makedirs(workspace, exist_ok=True)

        archive_path = row.get("archive_path")
        file_path = str(row["file_path"])

        if archive_path:
            chain = file_path.split("::")[1:]
            destination = self.reader.extract_chain(
                str(archive_path), chain, workspace
            )
        else:
            destination = unique_path(
                workspace, sanitize_filename(str(row["filename"]))
            )
            shutil.copy2(file_path, destination)

        description_name: Optional[str] = None
        description_source = row.get("description_file_path")

        if description_source:
            stem = os.path.splitext(os.path.basename(destination))[0]
            extension = os.path.splitext(str(description_source))[1] or ".txt"
            target = unique_path(
                workspace, sanitize_filename(f"{stem}_description{extension}")
            )
            if archive_path:
                extracted = self.reader.extract_chain(
                    str(archive_path), str(description_source).split("::"), workspace
                )
                os.replace(extracted, target)
            else:
                shutil.copy2(str(description_source), target)
            description_name = os.path.basename(target)

        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO workspace_copies(firmware_id, file_name, copied_at) "
                "VALUES (?, ?, ?) ON CONFLICT(firmware_id) DO UPDATE SET "
                "file_name = excluded.file_name, copied_at = excluded.copied_at",
                (
                    firmware_id,
                    os.path.basename(destination),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        return {
            "file": os.path.basename(destination),
            "description": description_name,
            "workspace": workspace,
            "path": destination,
        }

    def list_workspace_files(self) -> List[Dict[str, object]]:
        workspace = self.get_workspace_path()
        if not os.path.isdir(workspace):
            return []

        result: List[Dict[str, object]] = []
        for entry in os.scandir(workspace):
            if not entry.is_file():
                continue
            name = entry.name
            if is_firmware(name):
                kind = "firmware"
            elif is_description(name):
                kind = "description"
            else:
                kind = "other"
            try:
                stat = entry.stat()
            except OSError:
                continue
            result.append(
                {
                    "name": name,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "type": kind,
                }
            )
        result.sort(key=lambda item: float(item["mtime"]), reverse=True)
        return result

    def delete_workspace_file(self, name: str) -> None:
        """Удаляет файл из рабочей папки (только внутри неё)."""
        if not name or any(char in name for char in ("/", "\\")) or name in (".", ".."):
            raise ValueError("Недопустимое имя файла")

        workspace = os.path.abspath(self.get_workspace_path())
        target = os.path.abspath(os.path.join(workspace, name))
        if os.path.dirname(target) != workspace:
            raise ValueError("Файл находится вне рабочей папки")
        if not os.path.isfile(target):
            raise ValueError("Файл не найден")
        os.remove(target)

        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM workspace_copies WHERE file_name = ?", (name,)
            )
            connection.commit()
        finally:
            connection.close()

    # ------------------------------------------------------- диагностика

    # ------------------------------------------------------------- пароли

    def archives_for_passwords(self) -> Dict[str, object]:
        """Архивы из базы + признак «пароль уже известен» (сами пароли не отдаём)."""
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT archive_path, COUNT(*) AS files FROM firmware "
                "WHERE archive_path IS NOT NULL GROUP BY archive_path "
                "ORDER BY archive_path COLLATE NOCASE"
            ).fetchall()
            scans = {
                str(row["path"]): dict(row)
                for row in connection.execute("SELECT * FROM archive_scan")
            }
        finally:
            connection.close()

        items: List[Dict[str, object]] = []
        for row in rows:
            path = str(row["archive_path"])
            scan = scans.get(path) or {}
            note = self.credentials.notes.get(path) or self.credentials.notes.get(
                os.path.basename(path)
            )
            items.append(
                {
                    "path": path,
                    "name": os.path.basename(path),
                    "files": int(row["files"]),
                    "error": scan.get("error"),
                    "has_password": bool(self.credentials.get_passwords(path)),
                    "note": note,
                    "scanned_at": scan.get("scanned_at"),
                }
            )
        return {"items": items, "credentials": self.credentials.as_dict()}

    def check_archive_password(self, archive_path: str) -> Dict[str, object]:
        """Пробует открыть архив текущими паролями и говорит, что с ним."""
        path = os.path.abspath(str(archive_path).strip())
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Архив не найден: {path}")
        try:
            entries = self.reader.list_files(path)
        except ArchivePasswordError as exc:
            # Пароль в hint есть, но не подошёл — сохраняем для перебора.
            return {"status": "locked", "error": str(exc), "password_used": False}
        except ArchiveError as exc:
            return {"status": "error", "error": str(exc)}
        firmware = [
            entry for entry in entries if not entry.is_dir and is_firmware(entry.name)
        ]
        nested = [
            entry for entry in entries if not entry.is_dir and is_archive(entry.name)
        ]
        return {
            "status": "ok",
            "entries": len(entries),
            "firmware": len(firmware),
            "nested": len(nested),
            "password_used": bool(self.credentials.get_passwords(path)),
        }

    # ------------------------------------------------------------ архивы

    def _seed_archives(self, archives: List[str]) -> None:
        """Заносит все найденные архивы в список сразу, до разбора.

        Иначе во время долгой индексации страница «Архивы» показывает
        только те, что уже успели обработаться (в начале — один).
        """
        rows = []
        for path in archives:
            try:
                stat = os.stat(path)
                size, mtime = int(stat.st_size), float(stat.st_mtime)
            except OSError:
                size, mtime = 0, 0.0
            rows.append((path, os.path.basename(path), size, mtime))
        if not rows:
            return
        connection = self._connect()
        try:
            connection.executemany(
                "INSERT INTO archive_scan"
                "(path, name, size, mtime, files, nested, error, scanned_at, from_cache) "
                "VALUES (?, ?, ?, ?, 0, 0, NULL, NULL, 0) "
                "ON CONFLICT(path) DO UPDATE SET "
                "name = excluded.name, size = excluded.size, mtime = excluded.mtime",
                rows,
            )
            connection.commit()
        finally:
            connection.close()

    def _record_archive(
        self, path: str, result: Dict[str, object], from_cache: bool
    ) -> None:
        """Запоминает, что архив обработан и что из него получилось."""
        try:
            stat = os.stat(path)
            size, mtime = int(stat.st_size), float(stat.st_mtime)
        except OSError:
            size, mtime = 0, 0.0

        members = result.get("members") or []
        nested = result.get("nested") or []
        error = result.get("error")
        if not error:
            nested_errors = result.get("nested_errors") or []
            if nested_errors:
                error = "; ".join(str(item) for item in nested_errors)[:2000]

        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO archive_scan"
                "(path, name, size, mtime, files, nested, error, scanned_at, from_cache) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                "name = excluded.name, size = excluded.size, mtime = excluded.mtime, "
                "files = excluded.files, nested = excluded.nested, "
                "error = excluded.error, scanned_at = excluded.scanned_at, "
                "from_cache = excluded.from_cache",
                (
                    path,
                    os.path.basename(path),
                    size,
                    mtime,
                    len(members) + len(nested),  # type: ignore[arg-type]
                    len(nested),  # type: ignore[arg-type]
                    str(error) if error else None,
                    datetime.now().isoformat(timespec="seconds"),
                    int(from_cache),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _prune_archives(self, paths: set) -> None:
        """Убирает из списка архивы, которых больше нет в корне."""
        connection = self._connect()
        try:
            connection.execute(
                "CREATE TEMP TABLE IF NOT EXISTS seen_archives(path TEXT PRIMARY KEY)"
            )
            connection.execute("DELETE FROM seen_archives")
            connection.executemany(
                "INSERT OR IGNORE INTO seen_archives(path) VALUES (?)",
                [(path,) for path in paths],
            )
            connection.execute(
                "DELETE FROM archive_scan "
                "WHERE path NOT IN (SELECT path FROM seen_archives)"
            )
            connection.commit()
        finally:
            connection.close()

    def archives_overview(
        self,
        q: Optional[str] = None,
        status: str = "all",
        page: int = 1,
        per_page: int = 50,
        sort: str = "files",
    ) -> Dict[str, object]:
        """Список обработанных архивов + сводка «что могло не попасть»."""
        where: List[str] = []
        params: List[object] = []
        if q:
            where.append("(name LIKE ? OR path LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        if status == "error":
            where.append("error IS NOT NULL AND error <> ''")
        elif status == "empty":
            where.append(
                "files = 0 AND (error IS NULL OR error = '') "
                "AND scanned_at IS NOT NULL"
            )
        elif status == "pending":
            where.append("scanned_at IS NULL")
        elif status == "ok":
            where.append("files > 0")

        where_sql = " WHERE " + " AND ".join(where) if where else ""
        order_sql = {
            "files": "files DESC, name COLLATE NOCASE",
            "name": "name COLLATE NOCASE",
            "recent": "scanned_at DESC, name COLLATE NOCASE",
            "size": "size DESC, name COLLATE NOCASE",
        }.get(sort, "files DESC, name COLLATE NOCASE")

        page = max(1, int(page))
        per_page = min(500, max(1, int(per_page)))
        offset = (page - 1) * per_page

        connection = self._connect()
        try:
            totals = connection.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(CASE WHEN scanned_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS done, "
                "COALESCE(SUM(CASE WHEN scanned_at IS NULL THEN 1 ELSE 0 END), 0) AS pending, "
                "COALESCE(SUM(CASE WHEN error IS NOT NULL AND error <> '' THEN 1 ELSE 0 END), 0) AS with_error, "
                "COALESCE(SUM(CASE WHEN files = 0 AND (error IS NULL OR error = '') "
                "AND scanned_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS empty, "
                "COALESCE(SUM(CASE WHEN files > 0 THEN files ELSE 0 END), 0) AS files, "
                "COALESCE(SUM(size), 0) AS size, "
                "COALESCE(SUM(from_cache), 0) AS cached "
                "FROM archive_scan"
            ).fetchone()
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM archive_scan{where_sql}", params
            ).fetchone()["count"]
            rows = connection.execute(
                f"SELECT * FROM archive_scan{where_sql} "
                f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
                params + [per_page, offset],
            ).fetchall()
        finally:
            connection.close()

        pages = (total + per_page - 1) // per_page if total else 0
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "summary": {
                "archives": totals["total"],
                "done": totals["done"],
                "pending": totals["pending"],
                "with_error": totals["with_error"],
                "empty": totals["empty"],
                "files": totals["files"],
                "size": totals["size"],
                "cached": totals["cached"],
                "root": self.get_setting("root_path") or "",
                "last_scan": self.get_setting("last_scan"),
            },
        }

    def archives_check_disk(self) -> Dict[str, object]:
        """Сверяет список обработанных архивов с тем, что реально лежит на диске."""
        root = self.get_setting("root_path") or ""
        if not root or not os.path.isdir(root):
            raise ValueError("Не задана корректная корневая папка")

        archives, _plain, _texts = self._walk(root)
        connection = self._connect()
        try:
            known = {
                str(row["path"])
                for row in connection.execute("SELECT path FROM archive_scan")
            }
        finally:
            connection.close()

        on_disk = set(archives)
        missing = [path for path in archives if path not in known]
        stale = sorted(known - on_disk)

        def describe(path: str) -> Dict[str, object]:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            return {"path": path, "name": os.path.basename(path), "size": size}

        return {
            "root": root,
            "on_disk": len(archives),
            "in_index": len(known),
            "missing": [describe(path) for path in missing[:300]],
            "missing_count": len(missing),
            "stale": [describe(path) for path in stale[:300]],
            "stale_count": len(stale),
        }

    # ------------------------------------------------------------- копии

    def mark_duplicates(self) -> Dict[str, object]:
        """Помечает «похож на копию»: одинаковое имя + размер.

        Сравнение по содержимому потребовало бы распаковки каждого файла
        (142 тыс. записей) — неприемлемо. Пара «имя + размер» даёт
        кандидатов, которые потом сравниваются руками.
        """
        started = time.time()
        connection = self._connect()
        try:
            connection.execute("DELETE FROM duplicate_members")
            connection.execute("DELETE FROM duplicate_groups")
            connection.execute(
                "INSERT INTO duplicate_groups(group_key, name, size, copies) "
                "SELECT lower(filename) || '|' || file_size AS key, "
                "MIN(filename), file_size, COUNT(*) "
                "FROM firmware WHERE file_size > 0 "
                "GROUP BY key HAVING COUNT(*) > 1"
            )
            connection.execute(
                "INSERT INTO duplicate_members(firmware_id, group_key) "
                "SELECT id, lower(filename) || '|' || file_size FROM firmware "
                "WHERE file_size > 0 AND lower(filename) || '|' || file_size "
                "IN (SELECT group_key FROM duplicate_groups)"
            )
            connection.commit()
            groups = connection.execute(
                "SELECT COUNT(*) FROM duplicate_groups"
            ).fetchone()[0]
            copies = connection.execute(
                "SELECT COUNT(*) FROM duplicate_members"
            ).fetchone()[0]
            extra = connection.execute(
                "SELECT COALESCE(SUM(copies - 1), 0) FROM duplicate_groups"
            ).fetchone()[0]
        finally:
            connection.close()
        return {
            "groups": groups,
            "copies": copies,
            "extra": extra,
            "duration": round(time.time() - started, 1),
        }

    def duplicates_overview(
        self,
        q: Optional[str] = None,
        page: int = 1,
        per_page: int = 25,
        sort: str = "copies",
    ) -> Dict[str, object]:
        """Группы копий с перечнем файлов для ручного сравнения."""
        where: List[str] = []
        params: List[object] = []
        if q:
            where.append("g.name LIKE ?")
            params.append(f"%{q}%")
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        order_sql = {
            "copies": "g.copies DESC, g.name COLLATE NOCASE",
            "name": "g.name COLLATE NOCASE",
            "size": "g.size DESC, g.name COLLATE NOCASE",
        }.get(sort, "g.copies DESC, g.name COLLATE NOCASE")

        page = max(1, int(page))
        per_page = min(200, max(1, int(per_page)))
        offset = (page - 1) * per_page

        connection = self._connect()
        try:
            totals = connection.execute(
                "SELECT (SELECT COUNT(*) FROM duplicate_groups) AS groups, "
                "(SELECT COUNT(*) FROM duplicate_members) AS copies, "
                "(SELECT COALESCE(SUM(copies - 1), 0) FROM duplicate_groups) AS extra, "
                "(SELECT COALESCE(SUM(size), 0) FROM duplicate_groups) AS size"
            ).fetchone()
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM duplicate_groups g{where_sql}", params
            ).fetchone()["count"]
            rows = connection.execute(
                f"SELECT g.* FROM duplicate_groups g{where_sql} "
                f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
                params + [per_page, offset],
            ).fetchall()

            items: List[Dict[str, object]] = []
            for row in rows:
                members = connection.execute(
                    "SELECT f.id, f.filename, f.file_path, f.archive_path, "
                    "f.brand, f.model, f.ecu_name, f.file_size "
                    "FROM duplicate_members m "
                    "JOIN firmware f ON f.id = m.firmware_id "
                    "WHERE m.group_key = ? ORDER BY f.file_path LIMIT 100",
                    (row["group_key"],),
                ).fetchall()
                item = dict(row)
                item["members"] = [dict(member) for member in members]
                items.append(item)
        finally:
            connection.close()

        pages = (total + per_page - 1) // per_page if total else 0
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "summary": dict(totals),
        }

    def firmware_duplicates(self, firmware_id: int) -> Dict[str, object]:
        """Все файлы из той же группы копий (для сравнения в карточке)."""
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT group_key FROM duplicate_members WHERE firmware_id = ?",
                (firmware_id,),
            ).fetchone()
            if not row:
                return {"group": None, "items": []}
            members = connection.execute(
                "SELECT f.id, f.filename, f.file_path, f.archive_path, "
                "f.brand, f.model, f.ecu_name "
                "FROM duplicate_members m "
                "JOIN firmware f ON f.id = m.firmware_id "
                "WHERE m.group_key = ? ORDER BY f.file_path",
                (row["group_key"],),
            ).fetchall()
        finally:
            connection.close()
        return {"group": row["group_key"], "items": [dict(m) for m in members]}

    def _stage_container(
        self,
        archive_path: str,
        chain: List[str],
        cache: Dict[tuple, str],
        temp_dir: str,
    ) -> str:
        """Распаковывает контейнер (все звенья, кроме последнего) один раз.

        Без этого один и тот же архив распаковывался бы заново на каждый
        файл группы — для 200 копий это минуты.
        """
        prefix = tuple(chain[:-1])
        if not prefix:
            return archive_path
        if prefix in cache:
            return cache[prefix]

        current = archive_path
        for depth, name in enumerate(prefix):
            target_dir = os.path.join(temp_dir, f"c{len(cache)}_{depth}")
            os.makedirs(target_dir, exist_ok=True)
            current = self.reader.extract_single(current, name, target_dir)
        cache[prefix] = current
        return current

    def verify_group(
        self, group_key: str, limit: int = 20, force: bool = False
    ) -> Dict[str, object]:
        """Считает CRC32 содержимого для файлов группы.

        Имя и размер ничего не доказывают: у одного .bin бывают десятки
        версий под разные ГБО и авто. Сравнивать надо байты. По умолчанию
        проверяются первые `limit` файлов — этого достаточно, чтобы понять,
        одинаковы они или это разные прошивки. Уже посчитанные суммы
        берутся из базы, если не указан force.
        """
        connection = self._connect()
        try:
            totals = connection.execute(
                "SELECT copies FROM duplicate_groups WHERE group_key = ?",
                (group_key,),
            ).fetchone()
            members = connection.execute(
                "SELECT f.id, f.filename, f.file_path, f.archive_path, f.file_size, "
                "f.crc32 "
                "FROM duplicate_members m "
                "JOIN firmware f ON f.id = m.firmware_id "
                "WHERE m.group_key = ? ORDER BY f.file_path LIMIT ?",
                (group_key, limit),
            ).fetchall()
        finally:
            connection.close()

        temp_dir = tempfile.mkdtemp(prefix="fwverify_")
        containers: Dict[tuple, str] = {}
        results: List[Dict[str, object]] = []
        crcs: List[str] = []
        to_store: List[Tuple[str, int]] = []
        try:
            for row in members:
                item: Dict[str, object] = {
                    "id": row["id"],
                    "file_path": row["file_path"],
                    "crc32": None,
                    "size": row["file_size"],
                    "error": None,
                    "cached": False,
                }
                if row["crc32"] and not force:
                    item["crc32"] = str(row["crc32"])
                    item["cached"] = True
                    crcs.append(str(row["crc32"]))
                    results.append(item)
                    continue
                try:
                    if row["archive_path"]:
                        chain = str(row["file_path"]).split("::")[1:]
                        container = self._stage_container(
                            str(row["archive_path"]), chain, containers, temp_dir
                        )
                        target_dir = os.path.join(temp_dir, f"m{row['id']}")
                        os.makedirs(target_dir, exist_ok=True)
                        path = self.reader.extract_single(
                            container, chain[-1], target_dir
                        )
                        with open(path, "rb") as handle:
                            data = handle.read()
                    else:
                        with open(str(row["file_path"]), "rb") as handle:
                            data = handle.read()
                    item["crc32"] = f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
                    item["size"] = len(data)
                    crcs.append(str(item["crc32"]))
                    to_store.append((str(item["crc32"]), int(row["id"])))
                except (ArchiveError, OSError) as exc:
                    item["error"] = str(exc)
                results.append(item)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if to_store:
            connection = self._connect()
            try:
                connection.executemany(
                    "UPDATE firmware SET crc32 = ? WHERE id = ?", to_store
                )
                connection.commit()
            finally:
                connection.close()

        unique = sorted({crc for crc in crcs})
        total_copies = int(totals["copies"]) if totals else len(members)
        return {
            "group": group_key,
            "items": results,
            "checked": len(crcs),
            "total": total_copies,
            "unique": len(unique),
            "identical": len(unique) <= 1,
            "crcs": unique,
            "truncated": total_copies > len(members),
        }

    def export_group(self, group_key: str) -> Dict[str, object]:
        """Выгружает все файлы группы в папку рабочего каталога.

        Дальше их можно сравнить любым своим инструментом.
        """
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT name, copies FROM duplicate_groups WHERE group_key = ?",
                (group_key,),
            ).fetchone()
            members = connection.execute(
                "SELECT f.id, f.filename, f.file_path, f.archive_path "
                "FROM duplicate_members m "
                "JOIN firmware f ON f.id = m.firmware_id "
                "WHERE m.group_key = ? ORDER BY f.file_path",
                (group_key,),
            ).fetchall()
        finally:
            connection.close()

        if not members:
            raise ValueError("Группа не найдена")

        base_name = sanitize_filename(str(row["name"]) if row else group_key)
        target_dir = os.path.join(
            self.get_workspace_path(), "Копии", base_name
        )
        os.makedirs(target_dir, exist_ok=True)

        exported: List[str] = []
        errors: List[str] = []
        for member in members:
            try:
                if member["archive_path"]:
                    chain = str(member["file_path"]).split("::")[1:]
                    path = self.reader.extract_chain(
                        str(member["archive_path"]), chain, target_dir
                    )
                else:
                    destination = unique_path(
                        target_dir, sanitize_filename(str(member["filename"]))
                    )
                    shutil.copy2(str(member["file_path"]), destination)
                    path = destination
                exported.append(os.path.basename(path))
            except (ArchiveError, OSError) as exc:
                errors.append(f"{member['file_path']}: {exc}")

        return {
            "folder": target_dir,
            "exported": len(exported),
            "errors": errors[:20],
        }

    def firmware_siblings(
        self, firmware_id: int, limit: int = 100
    ) -> Dict[str, object]:
        """Файлы из той же папки — обычно это пары MOD/STOK/ORI."""
        row = self.get(firmware_id)
        if not row:
            raise ValueError("Прошивка не найдена")
        parent = self._parent_dir(str(row["file_path"]))
        if not parent:
            return {"items": [], "parent": ""}

        prefix = parent + "\\"
        folder = parent.rsplit("\\", 1)[-1]
        connection = self._connect()
        try:
            # Внутри архивов путь хранится через "/", поэтому LIKE по полному
            # префиксу не сработает — ищем по имени папки, а точное совпадение
            # проверяем в Python после нормализации разделителей.
            candidates = connection.execute(
                "SELECT id, filename, file_path, brand, model, ecu_name, "
                "is_tune, is_stock, has_gbo, imm_off, status, file_size "
                "FROM firmware WHERE file_path LIKE ? ORDER BY filename LIMIT ?",
                (f"%{folder}%", limit * 5),
            ).fetchall()
        finally:
            connection.close()

        items = [
            dict(item)
            for item in candidates
            if str(item["file_path"]).replace("/", "\\").startswith(prefix)
        ][:limit]
        return {"items": items, "parent": parent}

    def archives_rows(self) -> List[Dict[str, object]]:
        """Все обработанные архивы (для CSV-выгрузки)."""
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT name, path, files, nested, size, scanned_at, error, from_cache "
                "FROM archive_scan ORDER BY files DESC, name COLLATE NOCASE"
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def crc_scan_batch(
        self, limit: int = 100, skip: Optional[List[int]] = None
    ) -> Dict[str, object]:
        """Считает CRC для очередной пачки ещё не посчитанных файлов.

        Сначала обычные файлы на диске (быстро), потом содержимое архивов.
        """
        query = "SELECT id FROM firmware WHERE (crc32 IS NULL OR crc32 = '')"
        params: List[object] = []
        if skip:
            placeholders = ",".join("?" for _ in skip[:500])
            query += f" AND id NOT IN ({placeholders})"
            params.extend(skip[:500])
        query += " ORDER BY (archive_path IS NULL) DESC, file_size ASC LIMIT ?"
        params.append(limit)

        connection = self._connect()
        try:
            rows = connection.execute(query, params).fetchall()
        finally:
            connection.close()

        ids = [row["id"] for row in rows]
        if not ids:
            return {"processed": 0, "crcs": 0, "errors": 0, "failed": []}

        result = self.batch_checksums(ids)
        computed = [item for item in result["items"] if item["crc32"]]
        failed = [
            int(item["id"]) for item in result["items"]
            if not item["crc32"] and item["error"]
        ]
        return {
            "processed": len(ids),
            "crcs": len(computed),
            "errors": len(failed),
            "failed": failed,
        }

    def crc_progress(self) -> Dict[str, object]:
        connection = self._connect()
        try:
            total = connection.execute("SELECT COUNT(*) FROM firmware").fetchone()[0]
            done = connection.execute(
                "SELECT COUNT(*) FROM firmware WHERE crc32 IS NOT NULL AND crc32 <> ''"
            ).fetchone()[0]
        finally:
            connection.close()
        return {"total": total, "done": done, "remaining": total - done}

    def similar_overview(
        self, q: Optional[str] = None, page: int = 1, per_page: int = 25
    ) -> Dict[str, object]:
        """Группы файлов с одинаковой контрольной суммой (точные копии)."""
        where = ["crc32 IS NOT NULL", "crc32 <> ''"]
        params: List[object] = []
        where_sql = " WHERE " + " AND ".join(where)

        connection = self._connect()
        try:
            inner = (
                f"SELECT crc32, COUNT(*) AS copies FROM firmware{where_sql} "
                f"GROUP BY crc32 HAVING COUNT(*) > 1"
            )
            total = connection.execute(
                f"SELECT COUNT(*) FROM ({inner})"
            ).fetchone()[0]
            page = max(1, int(page))
            per_page = min(200, max(1, int(per_page)))
            offset = (page - 1) * per_page
            groups = connection.execute(
                f"SELECT crc32, copies FROM ({inner}) ORDER BY copies DESC, crc32 "
                f"LIMIT ? OFFSET ?",
                params + [per_page, offset],
            ).fetchall()

            items: List[Dict[str, object]] = []
            for group in groups:
                members = connection.execute(
                    "SELECT id, filename, file_path, archive_path, brand, model, "
                    "ecu_name, file_size FROM firmware "
                    "WHERE crc32 = ? ORDER BY file_path LIMIT 100",
                    (group["crc32"],),
                ).fetchall()
                items.append(
                    {
                        "crc32": group["crc32"],
                        "copies": group["copies"],
                        "members": [dict(member) for member in members],
                    }
                )
            stats = connection.execute(
                "SELECT COUNT(*) AS files, COALESCE(SUM(file_size), 0) AS size "
                "FROM firmware WHERE crc32 IS NOT NULL AND crc32 <> ''"
            ).fetchone()
        finally:
            connection.close()

        pages = (total + per_page - 1) // per_page if total else 0
        progress = self.crc_progress()
        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "summary": {
                "groups": total,
                "checked_files": stats["files"],
                "checked_size": stats["size"],
                "total_files": progress["total"],
                "done": progress["done"],
            },
        }

    def resolve_source(self, firmware_id: int) -> Dict[str, object]:
        """Путь и архив для открытия в проводнике."""
        row = self.get(firmware_id)
        if not row:
            raise ValueError("Прошивка не найдена")
        return {
            "path": row["file_path"],
            "archive": row["archive_path"],
            "filename": row["filename"],
        }
