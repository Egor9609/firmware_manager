"""archive_reader.py — чтение архивов ZIP / RAR / 7Z / TAR.

Главный принцип: НИКАКОЙ массовой распаковки. Архивы открываются
только на чтение, извлекается максимум один файл по требованию
(например, при копировании прошивки в workspace).

Поддерживается:
    * ZIP (в т.ч. запароленные)
    * RAR / RAR5 (нужен UnRAR.exe, bsdtar или 7z в PATH)
    * 7Z (в т.ч. с шифрованием имён файлов)
    * TAR, TAR.GZ, TAR.BZ2, TAR.XZ

Пароли берутся из passwords.json через CredentialManager.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from credentials import CredentialManager

try:  # необязательные зависимости
    import rarfile
except ImportError:  # pragma: no cover
    rarfile = None  # type: ignore[assignment]

try:
    import py7zr
except ImportError:  # pragma: no cover
    py7zr = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Внешние распаковщики (нужны для RAR)
# ---------------------------------------------------------------------------

_RAR_TOOLS: Optional[Dict[str, str]] = None

_UNRAR_CANDIDATES = (
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
    r"C:\Program Files\WinRAR\Rar.exe",
    "/usr/bin/unrar",
    "/usr/local/bin/unrar",
    "/opt/homebrew/bin/unrar",
)
_SEVENZIP_CANDIDATES = (
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    r"C:\Program Files\7-Zip\7zz.exe",
    "/usr/bin/7z",
    "/usr/bin/7zz",
    "/usr/local/bin/7z",
)


def _first_existing(candidates: Iterable[str]) -> str:
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return ""


def configure_rar_tools(force: bool = False) -> Dict[str, str]:
    """Находит UnRAR/7z/bsdtar и подключает их к rarfile.

    В PATH их обычно нет (WinRAR ставится в Program Files), поэтому
    проверяются типовые пути. Без этого RAR-архивы не читаются вообще.
    """
    global _RAR_TOOLS
    if _RAR_TOOLS is not None and not force:
        return _RAR_TOOLS

    found: Dict[str, str] = {}
    if rarfile is None:
        _RAR_TOOLS = found
        return found

    unrar = (
        _first_existing(_UNRAR_CANDIDATES)
        or shutil.which("unrar")
        or shutil.which("UnRAR")
        or ""
    )
    sevenzip = (
        _first_existing(_SEVENZIP_CANDIDATES)
        or shutil.which("7z")
        or shutil.which("7zz")
        or ""
    )
    bsdtar = _first_existing(("/usr/bin/bsdtar", "/usr/local/bin/bsdtar")) or (
        shutil.which("bsdtar") or ""
    )

    if unrar and hasattr(rarfile, "UNRAR_TOOL"):
        rarfile.UNRAR_TOOL = unrar  # type: ignore[attr-defined]
        found["unrar"] = unrar
    if sevenzip and hasattr(rarfile, "SEVENZIP_TOOL"):
        rarfile.SEVENZIP_TOOL = sevenzip  # type: ignore[attr-defined]
        found["7z"] = sevenzip
    if bsdtar and hasattr(rarfile, "BSDTAR_TOOL"):
        rarfile.BSDTAR_TOOL = bsdtar  # type: ignore[attr-defined]
        found["bsdtar"] = bsdtar

    _RAR_TOOLS = found
    return found


# ---------------------------------------------------------------------------
# Расширения
# ---------------------------------------------------------------------------

FIRMWARE_EXTENSIONS = {".bin", ".hex", ".frf", ".ori", ".mod", ".dat", ".fls"}
DESCRIPTION_EXTENSIONS = {".txt", ".readme", ".nfo", ".md"}

ARCHIVE_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".xz", ".txz",
}

MAX_TEXT_SIZE = 2 * 1024 * 1024        # 2 МБ на текстовое описание
MAX_READ_SIZE = 16 * 1024 * 1024       # предохранитель для чтения в память
MAX_7Z_MEMBER = 512 * 1024 * 1024      # максимум для извлечения одного файла

_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Подсказки пароля прямо в имени архива: "pass 4pda", "пароль 12345" и т.п.
_PASSWORD_TRIGGERS = {"пароль", "password", "pass", "pw"}
_PASSWORD_FILLERS = {"is", "это", "от", "для", "к", "архива", "archive", "код", "code"}
_PASSWORD_SPLIT_RE = re.compile(r"[\s_\-:=.,;()\[\]{}]+")


class ArchiveError(Exception):
    """Базовая ошибка работы с архивом (сообщение — для пользователя)."""


class ArchiveOpenError(ArchiveError):
    """Архив не открывается: повреждён, нет инструмента и т.п."""


class ArchivePasswordError(ArchiveError):
    """Архив зашифрован, подходящего пароля нет."""


@dataclass
class ArchiveEntry:
    """Файл внутри архива."""

    name: str
    size: int = 0
    is_dir: bool = False
    encrypted: bool = False
    mtime: float = 0.0


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _basename(name: str) -> str:
    return str(name).replace("\\", "/").rsplit("/", 1)[-1]


def _extension(name: str) -> str:
    base = _basename(name)
    index = base.rfind(".")
    return base[index:].lower() if index > 0 else ""


def is_firmware(name: str) -> bool:
    """Является ли файл прошивкой по расширению."""
    return _extension(name) in FIRMWARE_EXTENSIONS


def is_description(name: str) -> bool:
    """Является ли файл текстовым описанием."""
    return _extension(name) in DESCRIPTION_EXTENSIONS


def is_archive(name: str) -> bool:
    """Является ли файл поддерживаемым архивом."""
    return archive_kind(name) != ""


def archive_kind(name: str) -> str:
    """Определяет тип архива: zip / rar / 7z / tar / ''."""
    low = str(name).lower()
    if low.endswith((".zip", ".jar")):
        return "zip"
    if low.endswith(".rar"):
        return "rar"
    if low.endswith(".7z"):
        return "7z"
    if low.endswith((
        ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
        ".gz", ".bz2", ".xz",
    )):
        return "tar"
    return ""


def decode_bytes(data: bytes) -> str:
    """Декодирует текст из архива с учётом типичных кодировок."""
    if not data:
        return ""
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "cp866", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def sanitize_filename(name: str, max_length: int = 180) -> str:
    """Убирает недопустимые для Windows символы из имени файла."""
    base = _basename(name)
    base = _INVALID_FILENAME_RE.sub("_", base).strip(" .")
    if not base:
        base = "firmware"
    if len(base) > max_length:
        stem, dot, ext = base.rpartition(".")
        if dot and len(ext) <= 8:
            base = stem[: max_length - len(ext) - 1] + "." + ext
        else:
            base = base[:max_length]
    return base


def unique_path(directory: str, filename: str) -> str:
    """Возвращает свободный путь в каталоге (добавляет _1, _2 при совпадении)."""
    candidate = os.path.join(directory, filename)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(filename)
    index = 1
    while True:
        candidate = os.path.join(directory, f"{stem}_{index}{ext}")
        if not os.path.exists(candidate):
            return candidate
        index += 1


# ---------------------------------------------------------------------------
# Чтение архивов
# ---------------------------------------------------------------------------


class ArchiveReader:
    """Читает списки файлов и извлекает отдельные элементы архивов."""

    def __init__(
        self,
        credentials: Optional[CredentialManager] = None,
        passwords_file: str = "passwords.json",
    ) -> None:
        self.credentials = credentials or CredentialManager(passwords_file)
        self._password_cache: Dict[str, str] = {}
        # Без этого RAR-архивы не открываются, если UnRAR не в PATH.
        self.tools = configure_rar_tools()

    # ------------------------------------------------------------- passwords

    def _candidates(self, archive_path: str) -> List[str]:
        result: List[str] = []
        cached = self._password_cache.get(os.path.abspath(archive_path))
        if cached:
            result.append(cached)
        for password in self.credentials.get_passwords(archive_path):
            if password not in result:
                result.append(password)
        # Последний шанс — подсказка в имени файла ("... pass 4pda.rar").
        for hint in self._hint_passwords(archive_path):
            if hint not in result:
                result.append(hint)
        return result

    @staticmethod
    def _hint_passwords(archive_path: str) -> List[str]:
        stem = os.path.splitext(os.path.basename(archive_path))[0]
        tokens = [token for token in _PASSWORD_SPLIT_RE.split(stem) if token]
        hints: List[str] = []
        for index, token in enumerate(tokens):
            if token.lower() not in _PASSWORD_TRIGGERS:
                continue
            position = index + 1
            # "пароль pass 4pda" -> пропускаем слово-заполнитель "pass".
            while position < len(tokens) and (
                tokens[position].lower() in _PASSWORD_TRIGGERS
                or tokens[position].lower() in _PASSWORD_FILLERS
            ):
                position += 1
            if position >= len(tokens):
                continue
            value = tokens[position].strip(".,;:!?\"'")
            if not value:
                continue
            if value.lower() in ("от", "для", "к", "архива", "archive", "rar", "zip", "7z"):
                continue
            if value not in hints:
                hints.append(value)
        return hints

    def _remember_password(self, archive_path: str, password: Optional[str]) -> None:
        if password:
            self._password_cache[os.path.abspath(archive_path)] = password

    # ------------------------------------------------------------ public API

    def list_files(self, archive_path: str) -> List[ArchiveEntry]:
        """Возвращает список файлов архива без распаковки."""
        kind = archive_kind(archive_path)
        if not kind:
            raise ArchiveOpenError(f"Неподдерживаемый формат архива: {archive_path}")
        if not os.path.isfile(archive_path):
            raise ArchiveOpenError(f"Файл не найден: {archive_path}")

        if kind == "zip":
            return self._list_zip(archive_path)
        if kind == "rar":
            return self._list_rar(archive_path)
        if kind == "7z":
            return self._list_7z(archive_path)
        return self._list_tar(archive_path)

    def list_firmware_files(self, archive_path: str) -> List[ArchiveEntry]:
        """Только прошивки внутри архива."""
        return [
            entry
            for entry in self.list_files(archive_path)
            if not entry.is_dir and is_firmware(entry.name)
        ]

    def list_description_files(self, archive_path: str) -> List[ArchiveEntry]:
        """Только текстовые описания внутри архива."""
        return [
            entry
            for entry in self.list_files(archive_path)
            if not entry.is_dir and is_description(entry.name)
        ]

    def read_member(
        self, archive_path: str, inner_path: str, limit: int = MAX_TEXT_SIZE
    ) -> bytes:
        """Читает один файл из архива в память (не более limit байт)."""
        kind = archive_kind(archive_path)
        if kind == "zip":
            return self._read_zip(archive_path, inner_path, limit)
        if kind == "rar":
            return self._read_rar(archive_path, inner_path, limit)
        if kind == "7z":
            return self._read_7z(archive_path, inner_path, limit)
        if kind == "tar":
            return self._read_tar(archive_path, inner_path, limit)
        raise ArchiveOpenError(f"Неподдерживаемый формат архива: {archive_path}")

    def read_text(
        self, archive_path: str, inner_path: str, limit: int = MAX_TEXT_SIZE
    ) -> str:
        """Читает и декодирует текстовое описание из архива."""
        # 7z умеет отдавать файл в stdout почти мгновенно, тогда как py7zr
        # ради одного .txt распаковывает solid-архив целиком (до секунды).
        tool = self.tools.get("7z")
        if tool and archive_kind(archive_path) in ("7z", "rar"):
            data = self._read_via_7z(tool, archive_path, inner_path, limit)
            if data is not None:
                return decode_bytes(data)
        return decode_bytes(self.read_member(archive_path, inner_path, limit))

    def _read_via_7z(
        self, tool: str, archive_path: str, inner_path: str, limit: int
    ) -> Optional[bytes]:
        attempts: List[Optional[str]] = [None]
        for candidate in self._candidates(archive_path):
            if candidate and candidate not in attempts:
                attempts.append(candidate)
        creationflags = 0x08000000 if os.name == "nt" else 0
        for password in attempts:
            command = [tool, "e", archive_path, "-so", str(inner_path)]
            if password:
                command.append(f"-p{password}")
            try:
                completed = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=300,
                    creationflags=creationflags,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if completed.returncode == 0 and completed.stdout:
                self._remember_password(archive_path, password)
                return completed.stdout[:limit]
        return None

    def extract_single(
        self, archive_path: str, inner_path: str, dest_dir: str
    ) -> str:
        """Извлекает ОДИН файл из архива в dest_dir. Возвращает путь."""
        os.makedirs(dest_dir, exist_ok=True)
        filename = sanitize_filename(inner_path)
        destination = unique_path(dest_dir, filename)
        kind = archive_kind(archive_path)

        if kind == "zip":
            self._extract_zip(archive_path, inner_path, destination)
        elif kind == "rar":
            self._extract_rar(archive_path, inner_path, destination)
        elif kind == "7z":
            self._extract_7z(archive_path, inner_path, destination)
        elif kind == "tar":
            self._extract_tar(archive_path, inner_path, destination)
        else:
            raise ArchiveOpenError(f"Неподдерживаемый формат архива: {archive_path}")

        return destination

    def extract_many(
        self, archive_path: str, names: List[str], dest_dir: str
    ) -> Dict[str, str]:
        """Извлекает несколько файлов ОДНИМ вызовом 7z (в разы быстрее).

        Для solid-7z и RAR это ключевая оптимизация: 7z распаковывает
        архив один раз на всю пачку, а не по разу на каждый файл.
        Ключ результата — исходное имя в архиве, значение — путь на диске.
        """
        os.makedirs(dest_dir, exist_ok=True)
        if not names:
            return {}

        base_names = {
            str(name): _basename(str(name)).replace("\\", "/") for name in names
        }
        tool = self.tools.get("7z")
        if tool:
            attempts: List[Optional[str]] = [None]
            for candidate in self._candidates(archive_path):
                if candidate and candidate not in attempts:
                    attempts.append(candidate)
            for password in attempts:
                if not self._run_7z(tool, archive_path, list(base_names), dest_dir, password):
                    continue
                found = {
                    name: os.path.join(dest_dir, base)
                    for name, base in base_names.items()
                    if os.path.isfile(os.path.join(dest_dir, base))
                }
                if found:
                    self._remember_password(archive_path, password)
                    return found
            # 7z не справился (нет файла, битый архив) — пробуем поштучно.
            self._log_7z_fallback(archive_path)

        result: Dict[str, str] = {}
        for name in base_names:
            try:
                result[name] = self.extract_single(archive_path, name, dest_dir)
            except ArchiveError:
                continue
        return result

    @staticmethod
    def _log_7z_fallback(archive_path: str) -> None:
        # Тихий лог: не мешает, но видно в файле логов при разборе проблем.
        try:
            print(f"[архивы] 7z не справился, поштучно: {archive_path}", flush=True)
        except (UnicodeEncodeError, OSError):
            pass

    @staticmethod
    def _run_7z(
        tool: str,
        archive_path: str,
        names: List[str],
        dest_dir: str,
        password: Optional[str],
    ) -> bool:
        command = [tool, "e", archive_path, f"-o{dest_dir}", "-y", "-bso0", "-bsp0"]
        if password:
            command.append(f"-p{password}")
        command.extend(names)
        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3600,
                creationflags=creationflags,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode in (0, 1)

    def extract_chain(
        self, archive_path: str, chain: List[str], dest_dir: str
    ) -> str:
        """Извлекает файл, вложенный в несколько архивов, по цепочке имён."""
        if not chain:
            raise ArchiveOpenError("Пустая цепочка вложенных архивов")
        if len(chain) == 1:
            return self.extract_single(archive_path, chain[0], dest_dir)

        temp_dirs: List[str] = []
        current_archive = archive_path
        try:
            for member in chain[:-1]:
                temp_dir = tempfile.mkdtemp(prefix="fwnested_")
                temp_dirs.append(temp_dir)
                current_archive = self.extract_single(
                    current_archive, member, temp_dir
                )
            return self.extract_single(current_archive, chain[-1], dest_dir)
        finally:
            for temp_dir in reversed(temp_dirs):
                shutil.rmtree(temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------- ZIP

    def _list_zip(self, archive_path: str) -> List[ArchiveEntry]:
        try:
            with zipfile.ZipFile(archive_path) as zf:
                entries: List[ArchiveEntry] = []
                for info in zf.infolist():
                    try:
                        mtime = datetime(*info.date_time).timestamp()
                    except (ValueError, OverflowError):
                        mtime = 0.0
                    entries.append(
                        ArchiveEntry(
                            name=info.filename,
                            size=info.file_size,
                            is_dir=info.is_dir(),
                            encrypted=bool(info.flag_bits & 0x1),
                            mtime=mtime,
                        )
                    )
                return entries
        except zipfile.BadZipFile as exc:
            raise ArchiveOpenError(f"Повреждённый ZIP-архив: {exc}") from exc
        except OSError as exc:
            raise ArchiveOpenError(f"Не удалось открыть ZIP: {exc}") from exc

    def _zip_info(self, zf: zipfile.ZipFile, inner_path: str) -> zipfile.ZipInfo:
        try:
            return zf.getinfo(inner_path)
        except KeyError as exc:
            raise ArchiveOpenError(f"Файл не найден в архиве: {inner_path}") from exc

    def _zip_password(
        self, zf: zipfile.ZipFile, info: zipfile.ZipInfo, archive_path: str
    ) -> Optional[str]:
        if not (info.flag_bits & 0x1):
            return None
        for password in self._candidates(archive_path):
            try:
                with zf.open(info, pwd=password.encode("utf-8", errors="replace")) as stream:
                    stream.read(1)
                self._remember_password(archive_path, password)
                return password
            except (RuntimeError, zipfile.BadZipFile):
                continue
        raise ArchivePasswordError(
            f"Архив зашифрован, пароль не найден: {os.path.basename(archive_path)}"
        )

    def _read_zip(self, archive_path: str, inner_path: str, limit: int) -> bytes:
        try:
            with zipfile.ZipFile(archive_path) as zf:
                info = self._zip_info(zf, inner_path)
                if info.file_size > max(limit * 4, MAX_READ_SIZE):
                    raise ArchiveOpenError("Файл слишком большой для чтения в память")
                password = self._zip_password(zf, info, archive_path)
                pwd = password.encode("utf-8", errors="replace") if password else None
                with zf.open(info, pwd=pwd) as stream:
                    return stream.read(limit)
        except zipfile.BadZipFile as exc:
            raise ArchiveOpenError(f"Повреждённый ZIP-архив: {exc}") from exc

    def _extract_zip(
        self, archive_path: str, inner_path: str, destination: str
    ) -> None:
        try:
            with zipfile.ZipFile(archive_path) as zf:
                info = self._zip_info(zf, inner_path)
                password = self._zip_password(zf, info, archive_path)
                pwd = password.encode("utf-8", errors="replace") if password else None
                with zf.open(info, pwd=pwd) as source, open(destination, "wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        except zipfile.BadZipFile as exc:
            raise ArchiveOpenError(f"Повреждённый ZIP-архив: {exc}") from exc

    # ------------------------------------------------------------------- RAR

    def _require_rarfile(self) -> None:
        if rarfile is None:
            raise ArchiveOpenError(
                "Модуль rarfile не установлен. Выполните: pip install rarfile"
            )

    def _list_rar(self, archive_path: str) -> List[ArchiveEntry]:
        self._require_rarfile()
        try:
            rf = rarfile.RarFile(archive_path)  # type: ignore[union-attr]
        except Exception as exc:  # rarfile.RarCannotExec, BadRarFile, ...
            raise ArchiveOpenError(self._rar_error(exc)) from exc

        try:
            infos = rf.infolist()
        except Exception as exc:
            raise ArchiveOpenError(self._rar_error(exc)) from exc

        try:
            needs_password = rf.needs_password()
        except Exception:
            needs_password = False

        if needs_password:
            opened = False
            for password in self._candidates(archive_path):
                try:
                    rf.setpassword(password)
                    infos = rf.infolist()
                    self._remember_password(archive_path, password)
                    opened = True
                    break
                except Exception:
                    continue
            if not opened:
                raise ArchivePasswordError(
                    f"RAR-архив зашифрован, пароль не найден: "
                    f"{os.path.basename(archive_path)}"
                )

        entries: List[ArchiveEntry] = []
        for info in infos:
            mtime = 0.0
            try:
                if info.date_time:
                    mtime = datetime(*info.date_time).timestamp()
            except (ValueError, OverflowError, TypeError):
                mtime = 0.0
            try:
                encrypted = bool(info.needs_password())
            except Exception:
                encrypted = bool(needs_password)
            entries.append(
                ArchiveEntry(
                    name=info.filename,
                    size=info.file_size,
                    is_dir=info.is_dir(),
                    encrypted=encrypted,
                    mtime=mtime,
                )
            )
        return entries

    @staticmethod
    def _rar_error(exc: Exception) -> str:
        text = str(exc)
        if rarfile is not None and isinstance(exc, rarfile.RarCannotExec):
            return (
                "Для чтения RAR нужен UnRAR.exe, 7z или bsdtar. "
                "Установите WinRAR/7-Zip (ищется автоматически в Program Files) "
                "или пересохраните архив в ZIP. Найдено: "
                f"{', '.join(sorted(_RAR_TOOLS)) if _RAR_TOOLS else 'ничего'}"
            )
        return f"Ошибка чтения RAR: {text}"

    def _rar_info(self, rf, inner_path: str):  # type: ignore[no-untyped-def]
        try:
            return rf.getinfo(inner_path)
        except KeyError as exc:
            raise ArchiveOpenError(f"Файл не найден в архиве: {inner_path}") from exc

    def _rar_password(self, rf, info, archive_path: str) -> Optional[str]:  # type: ignore[no-untyped-def]
        try:
            needs = rf.needs_password() or info.needs_password()
        except Exception:
            needs = False
        if not needs:
            return self._password_cache.get(os.path.abspath(archive_path))
        for password in self._candidates(archive_path):
            try:
                with rf.open(info, pwd=password) as stream:
                    stream.read(1)
                self._remember_password(archive_path, password)
                return password
            except Exception:
                continue
        raise ArchivePasswordError(
            f"RAR-архив зашифрован, пароль не найден: "
            f"{os.path.basename(archive_path)}"
        )

    def _read_rar(self, archive_path: str, inner_path: str, limit: int) -> bytes:
        self._require_rarfile()
        try:
            rf = rarfile.RarFile(archive_path)  # type: ignore[union-attr]
        except Exception as exc:
            raise ArchiveOpenError(self._rar_error(exc)) from exc
        info = self._rar_info(rf, inner_path)
        password = self._rar_password(rf, info, archive_path)
        with rf.open(info, pwd=password) as stream:
            return stream.read(limit)

    def _extract_rar(
        self, archive_path: str, inner_path: str, destination: str
    ) -> None:
        self._require_rarfile()
        try:
            rf = rarfile.RarFile(archive_path)  # type: ignore[union-attr]
        except Exception as exc:
            raise ArchiveOpenError(self._rar_error(exc)) from exc
        info = self._rar_info(rf, inner_path)
        password = self._rar_password(rf, info, archive_path)
        with rf.open(info, pwd=password) as source, open(destination, "wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)

    # -------------------------------------------------------------------- 7Z

    def _require_py7zr(self) -> None:
        if py7zr is None:
            raise ArchiveOpenError(
                "Модуль py7zr не установлен. Выполните: pip install py7zr"
            )

    def _open_7z(self, archive_path: str):
        """Открывает 7z, перебирая пароли. Возвращает (архив, пароль)."""
        self._require_py7zr()
        candidates: List[Optional[str]] = [None] + list(self._candidates(archive_path))
        last_error: Optional[Exception] = None

        for password in candidates:
            try:
                zf = py7zr.SevenZipFile(archive_path, mode="r", password=password)
            except Exception as exc:  # неверный пароль / повреждение
                last_error = exc
                continue

            try:
                needs = zf.needs_password()
            except Exception:
                needs = False

            if needs and not password:
                zf.close()
                continue

            self._remember_password(archive_path, password)
            return zf, password

        if last_error is not None:
            text = str(last_error).lower()
            if "password" in text or "lzma" in text or "encrypt" in text:
                raise ArchivePasswordError(
                    f"7Z-архив зашифрован, пароль не найден: "
                    f"{os.path.basename(archive_path)}"
                )
            raise ArchiveOpenError(f"Ошибка открытия 7Z: {last_error}")
        raise ArchivePasswordError(
            f"7Z-архив зашифрован, пароль не найден: "
            f"{os.path.basename(archive_path)}"
        )

    def _list_7z(self, archive_path: str) -> List[ArchiveEntry]:
        zf, _password = self._open_7z(archive_path)
        try:
            names = zf.getnames()
            infos = {}
            try:
                for info in zf.list():
                    infos[info.filename] = info
            except Exception:
                infos = {}

            try:
                encrypted = bool(zf.needs_password())
            except Exception:
                encrypted = False

            entries: List[ArchiveEntry] = []
            for name in names:
                info = infos.get(name)
                size = int(getattr(info, "uncompressed", 0) or 0) if info else 0
                is_dir = name.endswith("/") or (
                    info is not None and bool(getattr(info, "is_directory", False))
                )
                mtime = 0.0
                created = getattr(info, "creationtime", None) if info else None
                if isinstance(created, datetime):
                    try:
                        mtime = created.timestamp()
                    except (ValueError, OverflowError):
                        mtime = 0.0
                entries.append(
                    ArchiveEntry(
                        name=name,
                        size=size,
                        is_dir=is_dir,
                        encrypted=encrypted,
                        mtime=mtime,
                    )
                )
            return entries
        finally:
            zf.close()

    def _read_7z(self, archive_path: str, inner_path: str, limit: int) -> bytes:
        zf, password = self._open_7z(archive_path)
        zf.close()

        entries = {entry.name: entry for entry in self._list_7z(archive_path)}
        entry = entries.get(inner_path)
        if entry is None:
            raise ArchiveOpenError(f"Файл не найден в архиве: {inner_path}")
        if entry.size and entry.size > MAX_7Z_MEMBER:
            raise ArchiveOpenError("Файл слишком большой для чтения в память")

        temp_dir = tempfile.mkdtemp(prefix="fw7z_")
        try:
            self._extract_7z_to_dir(archive_path, inner_path, temp_dir, password)
            extracted = self._locate_extracted(temp_dir, inner_path)
            with open(extracted, "rb") as handle:
                return handle.read(limit)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _extract_7z(
        self, archive_path: str, inner_path: str, destination: str
    ) -> None:
        zf, password = self._open_7z(archive_path)
        zf.close()

        temp_dir = tempfile.mkdtemp(prefix="fw7z_")
        try:
            self._extract_7z_to_dir(archive_path, inner_path, temp_dir, password)
            extracted = self._locate_extracted(temp_dir, inner_path)
            shutil.move(extracted, destination)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _extract_7z_to_dir(
        self,
        archive_path: str,
        inner_path: str,
        directory: str,
        password: Optional[str],
    ) -> None:
        self._require_py7zr()
        candidates: List[Optional[str]] = []
        if password:
            candidates.append(password)
        for item in [None] + list(self._candidates(archive_path)):
            if item not in candidates:
                candidates.append(item)

        last_error: Optional[Exception] = None
        for candidate in candidates:
            try:
                with py7zr.SevenZipFile(  # type: ignore[union-attr]
                    archive_path, mode="r", password=candidate
                ) as zf:
                    zf.extract(path=directory, targets=[inner_path])
                self._remember_password(archive_path, candidate)
                return
            except Exception as exc:  # неверный пароль и т.п.
                last_error = exc
                continue

        raise ArchivePasswordError(
            f"Не удалось извлечь файл из 7Z (проверьте пароль): {last_error}"
        )

    @staticmethod
    def _locate_extracted(directory: str, inner_path: str) -> str:
        candidate = os.path.join(directory, *inner_path.split("/"))
        if os.path.isfile(candidate):
            return candidate
        base = _basename(inner_path)
        for root, _dirs, files in os.walk(directory):
            if base in files:
                return os.path.join(root, base)
        raise ArchiveOpenError(f"Не удалось найти извлечённый файл: {inner_path}")

    # ------------------------------------------------------------------- TAR

    def _list_tar(self, archive_path: str) -> List[ArchiveEntry]:
        try:
            with tarfile.open(archive_path, "r:*") as tf:
                entries: List[ArchiveEntry] = []
                for member in tf.getmembers():
                    entries.append(
                        ArchiveEntry(
                            name=member.name,
                            size=member.size,
                            is_dir=member.isdir(),
                            encrypted=False,
                            mtime=float(member.mtime or 0),
                        )
                    )
                return entries
        except tarfile.TarError as exc:
            raise ArchiveOpenError(f"Повреждённый TAR-архив: {exc}") from exc
        except OSError as exc:
            raise ArchiveOpenError(f"Не удалось открыть TAR: {exc}") from exc

    def _tar_member(self, tf: tarfile.TarFile, inner_path: str) -> tarfile.TarInfo:
        try:
            return tf.getmember(inner_path)
        except KeyError as exc:
            raise ArchiveOpenError(f"Файл не найден в архиве: {inner_path}") from exc

    def _read_tar(self, archive_path: str, inner_path: str, limit: int) -> bytes:
        try:
            with tarfile.open(archive_path, "r:*") as tf:
                member = self._tar_member(tf, inner_path)
                stream = tf.extractfile(member)
                if stream is None:
                    raise ArchiveOpenError("Не удалось прочитать файл из TAR")
                with stream:
                    return stream.read(limit)
        except tarfile.TarError as exc:
            raise ArchiveOpenError(f"Повреждённый TAR-архив: {exc}") from exc

    def _extract_tar(
        self, archive_path: str, inner_path: str, destination: str
    ) -> None:
        try:
            with tarfile.open(archive_path, "r:*") as tf:
                member = self._tar_member(tf, inner_path)
                source = tf.extractfile(member)
                if source is None:
                    raise ArchiveOpenError("Не удалось извлечь файл из TAR")
                with source, open(destination, "wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        except tarfile.TarError as exc:
            raise ArchiveOpenError(f"Повреждённый TAR-архив: {exc}") from exc
