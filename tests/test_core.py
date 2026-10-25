# -*- coding: utf-8 -*-
"""Базовые тесты: парсер, чтение архивов, индексатор.

Запуск:
    cd D:\\firmware_manager
    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from archive_reader import ArchiveReader, is_archive, is_firmware  # noqa: E402
from indexer import FirmwareIndexer  # noqa: E402
from parser import FirmwareNameParser  # noqa: E402


class ParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = FirmwareNameParser()

    def test_brand_model_from_archive_path(self) -> None:
        """Марка и модель берутся из папок внутри архива, а не только из имени."""
        path = (
            r"D:\прошивки\Tazoff.7z::Tazoff 15.01.26/06.2025/ВАЗ/M86/"
            r"М86 Ларгус пакет FULL/1.6 16V/I765LA02/I765LA02v3 tun.bin"
        )
        meta = self.parser.parse(path, r"D:\прошивки")
        self.assertEqual(meta.brand, "ВАЗ (LADA)")
        self.assertIsNotNone(meta.model)
        self.assertIsNotNone(meta.ecu_name)
        self.assertTrue(meta.is_tune)

    def test_vaz_code_is_recognised(self) -> None:
        """Код блока вида I484GKB2 сохраняется, а марка берётся из пути."""
        meta = self.parser.parse(r"D:\x\ВАЗ\Granta\I484GKB2_Mat_Tun_Е2.bin", r"D:\x")
        self.assertEqual(meta.brand, "ВАЗ (LADA)")
        self.assertEqual(meta.ecu_id, "I484GKB2")
        self.assertEqual(meta.eco_standard, "E2")
        self.assertTrue(meta.is_tune)

    def test_shallow_path_still_works(self) -> None:
        meta = self.parser.parse(r"D:\x\Hyundai Getz 1.6.bin", r"D:\x")
        self.assertEqual(meta.brand, "Hyundai")
        self.assertEqual(meta.model, "Getz")


class ArchiveReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="fwtest_")
        self.inner = os.path.join(self.tmp, "inner.zip")
        with zipfile.ZipFile(self.inner, "w") as zf:
            zf.writestr("I484GF02.bin", b"\x00" * 64)
        self.outer = os.path.join(self.tmp, "outer.zip")
        with zipfile.ZipFile(self.outer, "w") as zf:
            zf.writestr("top.bin", b"\x00" * 32)
            zf.writestr("readme.txt", "ВАЗ Гранта M74")
            zf.write(self.inner, "nested/inner.zip")
        self.reader = ArchiveReader()

    def test_listing(self) -> None:
        entries = self.reader.list_files(self.outer)
        names = {entry.name for entry in entries}
        self.assertIn("top.bin", names)
        self.assertIn("nested/inner.zip", names)
        self.assertTrue(is_firmware("top.bin"))
        self.assertTrue(is_archive("nested/inner.zip"))

    def test_extract_single_and_chain(self) -> None:
        dest = os.path.join(self.tmp, "out")
        single = self.reader.extract_single(self.outer, "top.bin", dest)
        self.assertEqual(os.path.getsize(single), 32)

        chained = self.reader.extract_chain(self.outer, ["nested/inner.zip", "I484GF02.bin"], dest)
        self.assertEqual(os.path.getsize(chained), 64)

    def test_missing_member_raises(self) -> None:
        from archive_reader import ArchiveError

        with self.assertRaises(ArchiveError):
            self.reader.list_files(os.path.join(self.tmp, "nope.zip"))


class IndexerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="fwidx_")
        root = os.path.join(self.tmp, "root")
        os.makedirs(root)
        self.root = root
        inner = os.path.join(self.tmp, "inner.zip")
        with zipfile.ZipFile(inner, "w") as zf:
            zf.writestr("I484GF02.bin", b"\x00" * 128)
        with zipfile.ZipFile(os.path.join(root, "pack.zip"), "w") as zf:
            zf.write(inner, "nested/inner.zip")
        self.pwd = os.path.join(self.tmp, "passwords.json")
        with open(self.pwd, "w", encoding="utf-8") as handle:
            handle.write('{"archives": {}, "default_password": null, "notes": {}}')
        self.indexer = FirmwareIndexer(
            db_path=os.path.join(self.tmp, "test.db"), passwords_file=self.pwd
        )

    def test_nested_files_are_indexed(self) -> None:
        result = self.indexer.scan_and_index(self.root, max_workers=2)
        self.assertEqual(result["total"], 1)
        row = self.indexer.search()["items"][0]
        self.assertTrue(row["archive_path"].endswith("pack.zip"))
        self.assertIn("::nested/inner.zip::I484GF02.bin", row["file_path"])

    def test_second_scan_uses_cache(self) -> None:
        self.indexer.scan_and_index(self.root, max_workers=1)
        second = self.indexer.scan_and_index(self.root, max_workers=1)
        self.assertEqual(second["total"], 1)
        overview = self.indexer.archives_overview()
        self.assertEqual(overview["summary"]["archives"], 1)
        self.assertEqual(overview["summary"]["files"], 1)

    def test_reparse_does_not_change_manual_rows(self) -> None:
        self.indexer.scan_and_index(self.root, max_workers=1)
        row = self.indexer.search()["items"][0]
        self.indexer.update_firmware(row["id"], {"brand": "ВАЗ (LADA)", "model": "Granta"})
        summary = self.indexer.reparse_all()
        updated = self.indexer.get(row["id"])
        self.assertEqual(updated["brand"], "ВАЗ (LADA)")
        self.assertEqual(updated["model"], "Granta")
        self.assertEqual(summary["total"], 1)


if __name__ == "__main__":
    unittest.main()
