"""credentials.py — менеджер паролей к архивам.

Файл passwords.json создаётся автоматически при первом запуске:

{
  "archives": {
    "vaz_2020.rar": "пароль",
    "D:/Firmware/BMW/msv70.7z": "bmw123"
  },
  "default_password": null,
  "notes": {
    "vaz_2020.rar": "пароль от Паулюса"
  }
}

Поиск пароля выполняется в порядке:
    1. полный путь;
    2. имя файла;
    3. частичное совпадение (ключ входит в путь или имя);
    4. default_password.

Пароли никогда не записываются в базу индекса и не отдаются
в веб-интерфейс.
"""

from __future__ import annotations

import fnmatch
import json
import os
import tempfile
from typing import Dict, List, Optional, Union

PasswordValue = Union[str, List[str], None]


class CredentialManager:
    """Хранит и подбирает пароли для архивов."""

    def __init__(self, passwords_file: str = "passwords.json") -> None:
        self.passwords_file = os.path.abspath(passwords_file)
        self.archives: Dict[str, PasswordValue] = {}
        self.default_password: Optional[str] = None
        self.notes: Dict[str, str] = {}
        self.last_error: Optional[str] = None
        self._signature = None
        self._load()

    # ------------------------------------------------------------------ load

    def _load(self) -> None:
        """Загружает passwords.json, при отсутствии — создаёт шаблон."""
        if not os.path.exists(self.passwords_file):
            self._save()
            self._signature = self._stat_signature()
            return

        try:
            with open(self.passwords_file, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            self.last_error = f"Не удалось прочитать passwords.json: {exc}"
            self._signature = self._stat_signature()
            return

        self._signature = self._stat_signature()

        if not isinstance(data, dict):
            self.last_error = "passwords.json должен содержать JSON-объект"
            return

        archives = data.get("archives", {})
        if isinstance(archives, dict):
            self.archives = {
                str(key): value
                for key, value in archives.items()
                if isinstance(value, (str, list)) or value is None
            }

        default = data.get("default_password")
        if isinstance(default, str) and default.strip():
            self.default_password = default.strip()
        else:
            self.default_password = None

        notes = data.get("notes", {})
        if isinstance(notes, dict):
            self.notes = {str(key): str(value) for key, value in notes.items()}

    def reload(self) -> None:
        """Перечитывает файл с диска (если его правили вручную)."""
        self.archives = {}
        self.default_password = None
        self.notes = {}
        self.last_error = None
        self._load()

    def _stat_signature(self):
        try:
            stat = os.stat(self.passwords_file)
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None

    def reload_if_changed(self) -> bool:
        """Перечитывает passwords.json, если он изменился на диске.

        Позволяет добавлять пароли без перезапуска сервера: достаточно
        сохранить файл и нажать «Переиндексировать».
        """
        signature = self._stat_signature()
        if signature == self._signature:
            return False
        self.reload()
        return True

    # ------------------------------------------------------------------ save

    def _save(self) -> None:
        """Атомарно сохраняет passwords.json."""
        payload = {
            "archives": self.archives,
            "default_password": self.default_password,
            "notes": self.notes,
        }
        directory = os.path.dirname(self.passwords_file) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".passwords_", suffix=".json", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_path, self.passwords_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # -------------------------------------------------------------- password

    @staticmethod
    def _normalize(path: str) -> str:
        return str(path).replace("\\", "/").strip().lower()

    @staticmethod
    def _first(value: PasswordValue) -> Optional[str]:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
        return None

    @staticmethod
    def _all_values(value: PasswordValue) -> List[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        result: List[str] = []
        for item in value:
            if isinstance(item, str) and item.strip() and item.strip() not in result:
                result.append(item.strip())
        return result

    def _entries(self) -> List[tuple[str, PasswordValue]]:
        return [(self._normalize(key), value) for key, value in self.archives.items()]

    def get_password(self, archive_path: str) -> Optional[str]:
        """Возвращает наиболее подходящий пароль для архива или None."""
        normalized = self._normalize(archive_path)
        base = normalized.rsplit("/", 1)[-1]
        entries = self._entries()

        # 1. Полный путь.
        for key, value in entries:
            if key == normalized:
                return self._first(value)

        # 2. Имя файла.
        for key, value in entries:
            if key == base:
                return self._first(value)

        # 3. Частичное совпадение.
        for key, value in entries:
            if not key:
                continue
            if key in normalized or (len(key) >= 3 and key in base):
                return self._first(value)

        # 4. Glob-маска (удобно для серий архивов).
        for key, value in entries:
            if any(char in key for char in "*?["):
                if fnmatch.fnmatch(normalized, key) or fnmatch.fnmatch(base, key):
                    return self._first(value)

        return self.default_password

    def get_passwords(self, archive_path: str) -> List[str]:
        """Возвращает все подходящие пароли (для перебора вариантов)."""
        normalized = self._normalize(archive_path)
        base = normalized.rsplit("/", 1)[-1]
        result: List[str] = []

        def add(value: Optional[str]) -> None:
            if value and value not in result:
                result.append(value)

        add(self._first(self.archives.get(archive_path)))

        for key, value in self._entries():
            matched = (
                key == normalized
                or key == base
                or (key and (key in normalized or (len(key) >= 3 and key in base)))
                or (
                    key
                    and any(char in key for char in "*?[")
                    and (fnmatch.fnmatch(normalized, key) or fnmatch.fnmatch(base, key))
                )
            )
            if matched:
                for item in self._all_values(value):
                    add(item)

        add(self.default_password)
        return result

    def add_password(
        self, archive_path: str, password: str, note: Optional[str] = None
    ) -> None:
        """Добавляет/обновляет пароль для архива (ключ — имя или путь)."""
        key = str(archive_path).strip()
        password = str(password).strip()
        if not key:
            raise ValueError("Укажите имя или путь архива")
        if not password:
            raise ValueError("Пароль не может быть пустым")

        existing = self.archives.get(key)
        values = self._all_values(existing)
        if password not in values:
            values.append(password)
        self.archives[key] = values[0] if len(values) == 1 else values

        if note is not None:
            if note.strip():
                self.notes[key] = note.strip()
            else:
                self.notes.pop(key, None)

        self._save()

    def set_default_password(self, password: Optional[str]) -> None:
        """Устанавливает пароль по умолчанию (None — отключить)."""
        value = (password or "").strip()
        self.default_password = value or None
        self._save()

    def remove_password(self, archive_path: str) -> bool:
        """Удаляет запись о пароле. Возвращает True, если что-то удалено."""
        key = str(archive_path).strip()
        removed = self.archives.pop(key, None) is not None
        self.notes.pop(key, None)
        if removed:
            self._save()
        return removed

    def as_dict(self) -> dict:
        """Представление для диагностики (без веб-выдачи паролей)."""
        return {
            "file": self.passwords_file,
            "archives_count": len(self.archives),
            "has_default": bool(self.default_password),
            "error": self.last_error,
        }
