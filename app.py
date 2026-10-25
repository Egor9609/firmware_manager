"""app.py — Flask-сервер библиотеки прошивок ECU.

Запуск:
    python app.py
    python app.py --root "D:\\Firmware" --workspace "C:\\FirmwareWorkspace"

По умолчанию сервер слушает 0.0.0.0:5000 (см. ТЗ). Для доступа только
с этого компьютера используйте: python app.py --host 127.0.0.1
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import subprocess
import sys
import threading
import webbrowser
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Dict, List, Optional

from flask import Flask, Response, jsonify, render_template, request

from indexer import FirmwareIndexer

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = str(BASE_DIR / "firmware_index.db")
PASSWORDS_FILE = str(BASE_DIR / "passwords.json")

# При запуске обычным `python app.py` из cmd в PATH может не быть WinRAR/7-Zip.
# Добавляем типовые каталоги, чтобы rarfile/7z находили UnRAR/7z.exe.
for _tool_dir in (
    r"C:\Program Files\WinRAR",
    r"C:\Program Files\7-Zip",
    r"C:\Program Files (x86)\WinRAR",
    r"C:\Program Files (x86)\7-Zip",
):
    if os.path.isdir(_tool_dir):
        _path_parts = os.environ.get("PATH", "").split(os.pathsep)
        if _tool_dir not in _path_parts:
            os.environ["PATH"] = _tool_dir + os.pathsep + os.environ.get("PATH", "")
LOG_FILE = BASE_DIR / "firmware_manager.log"


def _setup_logging(app: Flask) -> None:
    """Пишет ошибки и ход индексации в файл, чтобы их было видно потом."""
    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.setLevel(logging.INFO)
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)
    app.logger.info("=== старт сервера, база: %s", DB_PATH)


def _as_bool(value: Optional[str]) -> bool:
    return str(value).lower() in ("1", "true", "on", "yes")


def create_app(
    root_dir: Optional[str] = None, workspace: Optional[str] = None
) -> Flask:
    """Создаёт Flask-приложение и инициализирует индексатор."""
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
    _setup_logging(app)

    indexer = FirmwareIndexer(
        db_path=DB_PATH,
        passwords_file=PASSWORDS_FILE,
        workspace_path=workspace,
    )
    if root_dir:
        indexer.set_setting("root_path", os.path.abspath(root_dir))
    app.config["INDEXER"] = indexer

    state_lock = threading.Lock()
    scan_state: Dict[str, object] = {
        "running": False,
        "phase": 0,
        "message": "Индексация не запускалась",
        "done": 0,
        "total": 0,
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
        "root": indexer.get_setting("root_path") or "",
    }

    # ------------------------------------------------------------- helpers

    def api(func: Callable):
        """Оборачивает endpoint в единый JSON-формат и обработку ошибок."""

        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                result = func(*args, **kwargs)
                if isinstance(result, tuple):
                    return result
                payload = {"ok": True}
                if isinstance(result, dict):
                    payload.update(result)
                return jsonify(payload)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            except FileNotFoundError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 404
            except PermissionError as exc:
                return jsonify(
                    {"ok": False, "error": f"Нет доступа: {exc}"}
                ), 403
            except Exception as exc:  # noqa: BLE001
                app.logger.exception("Ошибка API")
                return jsonify(
                    {"ok": False, "error": f"Внутренняя ошибка: {exc}"}
                ), 500

        return wrapper

    # --------------------------------------------------------------- pages

    @app.get("/")
    def page_index():
        return render_template("index.html", active="index")

    @app.get("/unknown")
    def page_unknown():
        return render_template("unknown.html", active="unknown")

    @app.get("/workspace")
    def page_workspace():
        return render_template("workspace.html", active="workspace")

    @app.get("/archives")
    def page_archives():
        return render_template("archives.html", active="archives")

    @app.get("/passwords")
    def page_passwords():
        return render_template("passwords.html", active="passwords")

    @app.get("/duplicates")
    def page_duplicates():
        return render_template("duplicates.html", active="duplicates")

    @app.get("/similar")
    def page_similar():
        return render_template("similar.html", active="similar")

    # ------------------------------------------------------------- search

    @app.get("/api/search")
    @api
    def api_search():
        result = indexer.search(
            q=request.args.get("q"),
            brand=request.args.get("brand") or None,
            model=request.args.get("model") or None,
            engine=request.args.get("engine") or None,
            archive=request.args.get("archive") or None,
            ecu=request.args.get("ecu") or None,
            author=request.args.get("author") or None,
            gbo=_as_bool(request.args.get("gbo")),
            tune=_as_bool(request.args.get("tune")),
            stock=_as_bool(request.args.get("stock")),
            status=request.args.get("status") or None,
            favorites=_as_bool(request.args.get("favorites")),
            page=int(request.args.get("page", 1)),
            per_page=int(request.args.get("per_page", 50)),
            sort=request.args.get("sort", "brand"),
        )
        return result

    @app.get("/api/unknown")
    @api
    def api_unknown():
        result = indexer.unknown_list(
            limit=min(200, int(request.args.get("limit", 50))),
            offset=max(0, int(request.args.get("offset", 0))),
            count_only=_as_bool(request.args.get("count_only")),
        )
        return result

    @app.get("/api/filters")
    @api
    def api_filters():
        return {"filters": indexer.filters()}

    # ------------------------------------------------- избранное и подборки

    @app.post("/api/favorite")
    @api
    def api_favorite():
        data = request.get_json(silent=True) or {}
        firmware_id = data.get("id")
        if not firmware_id:
            raise ValueError("Не указан id прошивки")
        return indexer.toggle_favorite(int(firmware_id), bool(data.get("value", True)))

    @app.get("/api/filter_sets")
    @api
    def api_filter_sets():
        return {"items": indexer.list_filter_sets()}

    @app.post("/api/filter_sets")
    @api
    def api_filter_sets_save():
        data = request.get_json(silent=True) or {}
        name = str(data.get("name") or "").strip()
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("Некорректный набор фильтров")
        return indexer.save_filter_set(name, payload)

    @app.post("/api/filter_sets/delete")
    @api
    def api_filter_sets_delete():
        data = request.get_json(silent=True) or {}
        return indexer.delete_filter_set(str(data.get("name") or ""))

    # ------------------------------------------------------------ архивы

    @app.get("/api/archives")
    @api
    def api_archives():
        return indexer.archives_overview(
            q=request.args.get("q") or None,
            status=request.args.get("status") or "all",
            page=int(request.args.get("page", 1)),
            per_page=int(request.args.get("per_page", 50)),
            sort=request.args.get("sort", "files"),
        )

    @app.get("/api/archives/check")
    @api
    def api_archives_check():
        return indexer.archives_check_disk()

    # ------------------------------------------------------------- копии

    @app.get("/api/duplicates")
    @api
    def api_duplicates():
        return indexer.duplicates_overview(
            q=request.args.get("q") or None,
            page=int(request.args.get("page", 1)),
            per_page=int(request.args.get("per_page", 25)),
            sort=request.args.get("sort", "copies"),
        )

    @app.get("/api/firmware_duplicates")
    @api
    def api_firmware_duplicates():
        firmware_id = request.args.get("id")
        if not firmware_id:
            raise ValueError("Не указан id прошивки")
        return indexer.firmware_duplicates(int(firmware_id))

    @app.get("/api/firmware_siblings")
    @api
    def api_firmware_siblings():
        firmware_id = request.args.get("id")
        if not firmware_id:
            raise ValueError("Не указан id прошивки")
        return indexer.firmware_siblings(int(firmware_id))

    @app.get("/api/export_archives.csv")
    def api_export_archives_csv():
        """CSV со списком обработанных архивов."""
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
        writer.writerow(["Архив", "Файлов", "Вложенных", "Размер", "Обработан", "Кэш", "Ошибка", "Путь"])
        for row in indexer.archives_rows():
            writer.writerow([
                row.get("name") or "",
                row.get("files") or 0,
                row.get("nested") or 0,
                row.get("size") or 0,
                row.get("scanned_at") or "",
                "да" if row.get("from_cache") else "нет",
                (row.get("error") or "").replace("\r", " ").replace("\n", " ")[:500],
                row.get("path") or "",
            ])
        payload = "\ufeff" + buffer.getvalue()
        return Response(
            payload,
            mimetype="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="firmware-archives.csv"',
                "Content-Type": "text/csv; charset=utf-8",
            },
        )

    @app.post("/api/duplicates/recount")
    @api
    def api_duplicates_recount():
        return indexer.mark_duplicates()

    @app.post("/api/duplicates/verify")
    @api
    def api_duplicates_verify():
        data = request.get_json(silent=True) or {}
        group_key = str(data.get("group_key") or "").strip()
        if not group_key:
            raise ValueError("Не указана группа")
        return indexer.verify_group(
            group_key, force=bool(data.get("force"))
        )

    @app.post("/api/duplicates/export")
    @api
    def api_duplicates_export():
        data = request.get_json(silent=True) or {}
        group_key = str(data.get("group_key") or "").strip()
        if not group_key:
            raise ValueError("Не указана группа")
        return indexer.export_group(group_key)

    @app.get("/api/statistics")
    @api
    def api_statistics():
        return {"statistics": indexer.statistics()}

    # ------------------------------------------------------------ update

    @app.post("/api/update_firmware")
    @api
    def api_update_firmware():
        data = request.get_json(silent=True) or {}
        firmware_id = data.get("id")
        if not firmware_id:
            raise ValueError("Не указан id прошивки")
        updated = indexer.update_firmware(int(firmware_id), data)
        return {"firmware": updated}

    @app.post("/api/reparse_unknowns")
    @api
    def api_reparse_unknowns():
        """Повторно применяет правила парсера к «неизвестным» записям."""
        return indexer.reparse_unknowns()

    @app.post("/api/reparse_all")
    @api
    def api_reparse_all():
        """Пересобирает метаданные всех неручных записей текущим парсером."""
        return indexer.reparse_all()

    @app.post("/api/bulk_update")
    @api
    def api_bulk_update():
        """Массовая разметка выбранных записей."""
        data = request.get_json(silent=True) or {}
        ids = data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValueError("Не выбрано ни одной прошивки")
        values = {key: value for key, value in data.items() if key != "ids"}
        return indexer.bulk_update(ids, values)

    @app.post("/api/batch_copy")
    @api
    def api_batch_copy():
        """Копирует выбранные прошивки в рабочую папку."""
        data = request.get_json(silent=True) or {}
        ids = data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValueError("Не выбрано ни одной прошивки")
        return indexer.batch_copy(ids)

    @app.post("/api/batch_checksums")
    @api
    def api_batch_checksums():
        """Считает CRC32 для выбранных прошивок (кэш — в базе)."""
        data = request.get_json(silent=True) or {}
        ids = data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValueError("Не выбрано ни одной прошивки")
        return indexer.batch_checksums(ids, force=bool(data.get("force")))

    # ------------------------------------------------- копии по CRC

    crc_lock = threading.Lock()
    crc_state: Dict[str, object] = {
        "running": False,
        "processed": 0,
        "total": 0,
        "message": "Не запускалось",
        "error": None,
        "started_at": None,
        "finished_at": None,
    }
    crc_stop = {"flag": False}

    @app.get("/api/similar")
    @api
    def api_similar():
        return indexer.similar_overview(
            page=int(request.args.get("page", 1)),
            per_page=int(request.args.get("per_page", 25)),
        )

    @app.get("/api/crc/status")
    @api
    def api_crc_status():
        with crc_lock:
            snapshot = dict(crc_state)
        snapshot.update(indexer.crc_progress())
        return {"crc": snapshot}

    @app.post("/api/crc/start")
    @api
    def api_crc_start():
        with crc_lock:
            if crc_state["running"]:
                return jsonify({"ok": False, "error": "Расчёт уже идёт"}), 409
            progress = indexer.crc_progress()
            crc_state.update(
                {
                    "running": True,
                    "processed": 0,
                    "total": progress["remaining"],
                    "message": "Считаю контрольные суммы…",
                    "error": None,
                    "started_at": _now(),
                    "finished_at": None,
                }
            )
            crc_stop["flag"] = False

        def worker() -> None:
            failed: List[int] = []
            try:
                while not crc_stop["flag"]:
                    batch = indexer.crc_scan_batch(100, skip=failed)
                    if not batch["processed"]:
                        break
                    failed.extend(batch.get("failed") or [])
                    with crc_lock:
                        crc_state["processed"] = int(crc_state["processed"]) + int(
                            batch["crcs"]
                        )
                        crc_state["message"] = (
                            f"Посчитано {crc_state['processed']} из {crc_state['total']}"
                        )
                    if not batch["crcs"] and batch["errors"]:
                        break
            except Exception as exc:  # noqa: BLE001
                app.logger.exception("Ошибка расчёта CRC")
                with crc_lock:
                    crc_state["error"] = str(exc)
            finally:
                with crc_lock:
                    crc_state["running"] = False
                    crc_state["finished_at"] = _now()
                    crc_state["message"] = (
                        "Остановлено" if crc_stop["flag"] else "Готово"
                    )

        threading.Thread(target=worker, daemon=True, name="crc-scan").start()
        return {"started": True}

    @app.post("/api/crc/stop")
    @api
    def api_crc_stop():
        crc_stop["flag"] = True
        return {"stopping": True}

    CSV_COLUMNS = (
        ("id", "ID"),
        ("filename", "Файл"),
        ("brand", "Марка"),
        ("model", "Модель"),
        ("engine", "Двигатель"),
        ("ecu_name", "ЭБУ"),
        ("ecu_id", "ID блока"),
        ("author", "Автор"),
        ("eco_standard", "Евро"),
        ("year", "Год"),
        ("file_size", "Размер"),
        ("status", "Статус"),
        ("archive_path", "Архив"),
        ("file_path", "Путь"),
    )

    def _csv_response(rows: list, filename: str) -> Response:
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
        writer.writerow([title for _key, title in CSV_COLUMNS])
        for row in rows:
            writer.writerow(
                ["" if row.get(key) is None else row.get(key) for key, _title in CSV_COLUMNS]
            )
        # BOM — иначе Excel откроет кириллицу кракозябрами.
        payload = "\ufeff" + buffer.getvalue()
        return Response(
            payload,
            mimetype="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": "text/csv; charset=utf-8",
            },
        )

    @app.get("/api/export.csv")
    def api_export_csv():
        """Выгрузка текущих результатов поиска в CSV (все страницы)."""
        params = dict(
            q=request.args.get("q"),
            brand=request.args.get("brand") or None,
            model=request.args.get("model") or None,
            engine=request.args.get("engine") or None,
            archive=request.args.get("archive") or None,
            ecu=request.args.get("ecu") or None,
            author=request.args.get("author") or None,
            gbo=_as_bool(request.args.get("gbo")),
            tune=_as_bool(request.args.get("tune")),
            stock=_as_bool(request.args.get("stock")),
            status=request.args.get("status") or None,
            favorites=_as_bool(request.args.get("favorites")),
            sort=request.args.get("sort", "relevance"),
        )
        rows: list = []
        page = 1
        limit = 20000
        while len(rows) < limit:
            chunk = indexer.search(page=page, per_page=500, **params)
            rows.extend(chunk["items"])
            if not chunk["items"] or len(rows) >= chunk["total"]:
                break
            page += 1
        return _csv_response(rows[:limit], "firmware-search.csv")

    @app.post("/api/export.csv")
    def api_export_selected_csv():
        """Выгрузка выбранных прошивок в CSV."""
        data = request.get_json(silent=True) or {}
        ids = data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValueError("Не выбрано ни одной прошивки")
        return _csv_response(indexer.rows_by_ids(ids), "firmware-selected.csv")

    # ----------------------------------------------------------- workspace

    @app.get("/api/workspace")
    @api
    def api_workspace_get():
        return {"workspace": indexer.get_workspace_path()}

    @app.post("/api/workspace")
    @api
    def api_workspace_set():
        data = request.get_json(silent=True) or {}
        path = data.get("path") or data.get("workspace")
        if not path:
            raise ValueError("Не указан путь к рабочей папке")
        return {"workspace": indexer.set_workspace_path(str(path))}

    @app.get("/api/workspace_files")
    @api
    def api_workspace_files():
        files = indexer.list_workspace_files()
        return {
            "workspace": indexer.get_workspace_path(),
            "files": files,
            "count": len(files),
        }

    @app.post("/api/delete_workspace_file")
    @api
    def api_delete_workspace_file():
        data = request.get_json(silent=True) or {}
        name = data.get("name")
        if not name:
            raise ValueError("Не указано имя файла")
        indexer.delete_workspace_file(str(name))
        return {"deleted": name}

    @app.post("/api/copy_to_workspace")
    @api
    def api_copy_to_workspace():
        data = request.get_json(silent=True) or {}
        firmware_id = data.get("id")
        if not firmware_id:
            raise ValueError("Не указан id прошивки")
        result = indexer.copy_to_workspace(int(firmware_id))
        return result

    # ------------------------------------------------------- open location

    @app.get("/api/open_location")
    @api
    def api_open_location():
        path = request.args.get("path")
        archive = request.args.get("archive")
        firmware_id = request.args.get("id")

        if firmware_id:
            source = indexer.resolve_source(int(firmware_id))
            path = str(source["path"])
            archive = source["archive"]

        if archive:
            target = os.path.abspath(str(archive))
        elif path:
            target = os.path.abspath(str(path).split("::", 1)[0])
        else:
            raise ValueError("Не указан путь")

        if not os.path.exists(target):
            raise FileNotFoundError(f"Путь не найден: {target}")

        _open_in_explorer(target)
        return {"opened": target}

    # --------------------------------------------------------------- root

    @app.get("/api/root")
    @api
    def api_root_get():
        return {
            "root": indexer.get_setting("root_path") or "",
            "exists": os.path.isdir(indexer.get_setting("root_path") or ""),
        }

    @app.post("/api/root")
    @api
    def api_root_set():
        data = request.get_json(silent=True) or {}
        root = str(data.get("root") or "").strip()
        if not root:
            raise ValueError("Не указан путь к папке с прошивками")
        absolute = os.path.abspath(os.path.expandvars(root))
        if not os.path.isdir(absolute):
            raise ValueError(f"Папка не найдена: {absolute}")
        indexer.set_setting("root_path", absolute)
        return {"root": absolute}

    # ------------------------------------------------------------- reindex

    @app.post("/api/reparse")
    @api
    def api_reparse():
        """Переразбор имён/путей по базе — без чтения архивов."""
        return {"reparse": indexer.reparse_all()}

    @app.post("/api/reindex")
    @api
    def api_reindex():
        data = request.get_json(silent=True) or {}
        root = str(data.get("root") or indexer.get_setting("root_path") or "").strip()
        if not root:
            raise ValueError("Сначала укажите папку с прошивками")
        root = os.path.abspath(os.path.expandvars(root))
        if not os.path.isdir(root):
            raise ValueError(f"Папка не найдена: {root}")
        workers = int(data.get("max_workers") or 8)
        workers = min(32, max(1, workers))

        with state_lock:
            if scan_state["running"]:
                return jsonify(
                    {"ok": False, "error": "Индексация уже выполняется"}
                ), 409
            scan_state.update(
                {
                    "running": True,
                    "phase": 0,
                    "message": "Подготовка…",
                    "done": 0,
                    "total": 0,
                    "started_at": None,
                    "finished_at": None,
                    "result": None,
                    "error": None,
                    "root": root,
                }
            )

        indexer.set_setting("root_path", root)

        def worker() -> None:
            def callback(phase: int, message: str, done: int, total: int) -> None:
                with state_lock:
                    scan_state.update(
                        {
                            "phase": phase,
                            "message": message,
                            "done": done,
                            "total": total,
                        }
                    )

            with state_lock:
                scan_state["started_at"] = _now()
            app.logger.info(
                "индексация: старт, корень=%s, потоков=%s", root, workers
            )
            try:
                result = indexer.scan_and_index(
                    root, max_workers=workers, progress_cb=callback
                )
                app.logger.info(
                    "индексация: готово, файлов=%s, классифицировано=%s, "
                    "неизвестно=%s, ошибок=%s, время=%sс",
                    result.get("total"), result.get("classified"),
                    result.get("unknown"), len(result.get("errors") or []),
                    result.get("duration"),
                )
                with state_lock:
                    scan_state.update(
                        {
                            "running": False,
                            "result": result,
                            "finished_at": _now(),
                            "message": (
                                f"Готово: {result['total']} прошивок за "
                                f"{result['duration']} с"
                            ),
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                app.logger.exception("Ошибка индексации")
                with state_lock:
                    scan_state.update(
                        {
                            "running": False,
                            "error": str(exc),
                            "finished_at": _now(),
                            "message": f"Ошибка: {exc}",
                        }
                    )

        thread = threading.Thread(target=worker, daemon=True, name="indexer")
        thread.start()
        return {"started": True, "root": root}

    @app.get("/api/reindex/status")
    @api
    def api_reindex_status():
        with state_lock:
            snapshot = dict(scan_state)
        return {"scan": snapshot}

    # --------------------------------------------------------------- misc

    @app.get("/api/passwords")
    @api
    def api_passwords():
        # Пароли не отдаём: только диагностическая информация.
        return {"credentials": indexer.credentials.as_dict()}

    @app.get("/api/passwords/archives")
    @api
    def api_passwords_archives():
        return indexer.archives_for_passwords()

    @app.post("/api/passwords")
    @api
    def api_passwords_save():
        data = request.get_json(silent=True) or {}
        archive = str(data.get("archive") or "").strip()
        action = str(data.get("action") or "save")

        if action == "remove":
            if not archive:
                raise ValueError("Не указан архив")
            return {"removed": indexer.credentials.remove_password(archive)}

        if action == "default":
            indexer.credentials.set_default_password(data.get("password"))
            return {"has_default": bool(indexer.credentials.default_password)}

        password = str(data.get("password") or "")
        if not archive:
            raise ValueError("Не указан архив")
        indexer.credentials.add_password(archive, password, data.get("note"))
        return {"saved": archive, "total": len(indexer.credentials.archives)}

    @app.post("/api/passwords/check")
    @api
    def api_passwords_check():
        data = request.get_json(silent=True) or {}
        archive = str(data.get("archive") or "").strip()
        if not archive:
            raise ValueError("Не указан архив")
        return indexer.check_archive_password(archive)

    return app


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _open_in_explorer(target: str) -> None:
    """Открывает папку или выделяет файл в проводнике."""
    # Для файла внутри архива открываем сам архив: путь имеет вид
    # "D:\...\сборка.7z::внутри/файл.bin" — берём часть до "::".
    target = os.path.abspath(str(target).split("::", 1)[0])

    if sys.platform == "win32":
        if os.path.isfile(target):
            # Explorer понимает только вид:  /select,"C:\путь с пробелами\файл"
            # Если передать список аргументов, Python заключит в кавычки весь
            # токен "/select,C:\...", и проводник молча откроет «Документы».
            subprocess.Popen(f'explorer /select,"{target}"')
        elif os.path.isdir(target):
            subprocess.Popen(f'explorer "{target}"')
        else:
            parent = os.path.dirname(target)
            subprocess.Popen(f'explorer "{parent}"')
        return
    if sys.platform == "darwin":
        if os.path.isfile(target):
            subprocess.Popen(["open", "-R", target])
        else:
            subprocess.Popen(["open", target])
        return
    directory = target if os.path.isdir(target) else os.path.dirname(target)
    subprocess.Popen(["xdg-open", directory])


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(
        description="Библиотека прошивок ECU — веб-интерфейс"
    )
    parser.add_argument("--root", help="Корневая папка с прошивками")
    parser.add_argument(
        "--workspace",
        help=r"Рабочая папка (по умолчанию C:\FirmwareWorkspace)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Адрес сервера")
    parser.add_argument("--port", type=int, default=5000, help="Порт")
    parser.add_argument(
        "--scan", action="store_true", help="Проиндексировать папку при старте"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="Не открывать браузер"
    )
    args = parser.parse_args()

    app = create_app(root_dir=args.root, workspace=args.workspace)
    indexer: FirmwareIndexer = app.config["INDEXER"]

    if args.scan:
        root = indexer.get_setting("root_path")
        if root and os.path.isdir(root):
            indexer.scan_and_index(root)
        else:
            print("Индексация пропущена: не задана корректная --root")

    if not args.no_browser:
        url = f"http://127.0.0.1:{args.port}"
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
