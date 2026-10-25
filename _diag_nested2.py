import json
import os
import tempfile
import time

from archive_reader import ArchiveReader, is_archive, is_firmware
from credentials import CredentialManager

paths = [
    r"D:\прошивки\прошивки с rutracker\Складка\ADACT.7z",
    r"D:\прошивки\прошивки с rutracker\Складка\Paulus.7z",
]
reader = ArchiveReader(CredentialManager(os.path.join(os.path.dirname(__file__), "passwords.json")))
result = []
for path in paths:
    started = time.time()
    outer_entries = reader.list_files(path)
    nested = [e for e in outer_entries if not e.is_dir and is_archive(e.name)]
    item = {"outer": path, "nested": len(nested), "firmware": 0, "nested_inside": 0, "errors": 0, "items": []}
    with tempfile.TemporaryDirectory(prefix="fwnested2_") as temp_dir:
        for index, entry in enumerate(nested, 1):
            sub = {"name": entry.name, "firmware": 0, "nested": 0}
            try:
                inner_path = reader.extract_single(path, entry.name, temp_dir)
                inner_entries = reader.list_files(inner_path)
                sub["firmware"] = sum(1 for e in inner_entries if not e.is_dir and is_firmware(e.name))
                sub["nested"] = sum(1 for e in inner_entries if not e.is_dir and is_archive(e.name))
                item["firmware"] += sub["firmware"]
                item["nested_inside"] += sub["nested"]
                os.remove(inner_path)
            except Exception as exc:
                sub["error"] = f"{type(exc).__name__}: {exc}"
                item["errors"] += 1
            item["items"].append(sub)
            if index % 10 == 0 or index == len(nested):
                print(path.rsplit("\\", 1)[-1], index, len(nested), item["firmware"], item["nested_inside"], flush=True)
    item["seconds"] = round(time.time() - started, 1)
    result.append(item)
with open(r"D:\nested2_test.json", "w", encoding="utf-8") as handle:
    json.dump(result, handle, ensure_ascii=False, indent=2)
print("done")
