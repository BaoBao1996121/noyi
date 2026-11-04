"""Portable managed-data backup; restores never overwrite or auto-run work."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sqlite3
import tempfile
import time
import zipfile
import uuid
from contextlib import closing

from .contracts import encode
from .model import ModelConfig
from dataclasses import asdict


MANAGED = {"tasks", "observations", "uploads", "inbox"}
MAX_BYTES = 512 * 1024 * 1024
MAX_FILES = 20000


def _checked(path, root):
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or not path.resolve().is_relative_to(root):
        raise ValueError("备份目录中存在重定向路径，请先检查")
    return path


def create_backup(store, data, destination):
    data, destination = Path(data).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError("备份文件已存在，请使用新文件名")
    if destination.is_relative_to(data):
        raise ValueError("请把备份保存到数据目录之外")
    if store.db.in_transaction:
        raise ValueError("当前资料正在提交，请稍后再备份")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pa-backup-", dir=destination.parent) as temporary:
        temp = Path(temporary)
        snapshot = temp / "state.sqlite"
        with closing(sqlite3.connect(snapshot)) as connection:
            store.db.backup(connection)
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("数据库完整性检查未通过，未生成备份")
        files = [("state.sqlite", snapshot)]
        for name in sorted(MANAGED):
            root = data / name
            if not root.exists():
                continue
            _checked(root, data)
            for directory, dirs, names in os.walk(root, followlinks=False):
                folder = _checked(Path(directory), data)
                for child in dirs:
                    _checked(folder / child, data)
                for filename in names:
                    path = _checked(folder / filename, data)
                    if not filename.endswith(".tmp"):
                        files.append((path.relative_to(data).as_posix(), path))
        if (data / "model.json").is_file():
            # Configuration is validated and rewritten; arbitrary extra fields
            # (including a mistakenly pasted key) never enter the archive.
            config = ModelConfig(**json.loads(_checked(data / "model.json", data).read_text(encoding="utf-8")))
            public_config = temp / "model.json"
            public_config.write_text(encode(asdict(config)), encoding="utf-8")
            files.append(("model.json", public_config))
        if len(files) > MAX_FILES:
            raise ValueError("备份文件数量过多，请先导出历史数据")
        manifest = {"format": "personal-agent-backup/v1", "created": time.time(), "original_data_directory": str(data), "credentials_included": False, "external_sources_included": False, "restore_auto_run": False, "files": {}}
        total = 0
        archive = temp / "backup.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for name, path in files:
                hasher, size = hashlib.sha256(), 0
                with path.open("rb") as source, z.open(name, "w") as target:
                    while chunk := source.read(1024 * 1024):
                        total += len(chunk)
                        size += len(chunk)
                        if total > MAX_BYTES:
                            raise ValueError("本版备份上限 512 MB，请先导出历史数据")
                        target.write(chunk)
                        hasher.update(chunk)
                manifest["files"][name] = {"sha256": hasher.hexdigest(), "bytes": size}
            z.writestr("manifest.json", encode(manifest).encode())
        # Exclusive creation never replaces an existing user backup. Hashes in
        # the manifest describe the exact bytes actually copied into the ZIP.
        with destination.open("xb") as output, archive.open("rb") as source:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
    return {"path": str(destination), "files": len(files), "uncompressed_bytes": total, "credentials_included": False, "restored_work_starts_paused": True}


def _archive_name(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or path.is_absolute() or any(p in {"..", ".", ""} for p in name.split("/"))
            or path.parts[0] not in MANAGED | {"state.sqlite", "model.json", "manifest.json"}
            or path.parts[0] not in MANAGED and len(path.parts) != 1):
        raise ValueError("备份包含无效或越界路径")
    if any(p.endswith((".", " ")) or p.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]} for p in path.parts):
        raise ValueError("备份包含平台保留路径")
    return path


def _relocate(spec, old_root, new_root):
    parser = PureWindowsPath if "\\" in old_root or len(old_root) > 1 and old_root[1] == ":" else PurePosixPath
    for source in spec.get("sources", []):
        if source.get("kind") != "file":
            continue
        try:
            relative = parser(source["location"]).relative_to(parser(old_root))
        except ValueError:
            continue
        if relative.parts and relative.parts[0] in MANAGED and ".." not in relative.parts:
            source["location"] = str(new_root.joinpath(*relative.parts))
    return spec


def restore_backup(archive, destination):
    from .store import Store

    archive, destination = Path(archive).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError("恢复必须使用不存在的新目录，不覆盖现有资料")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as z, tempfile.TemporaryDirectory(prefix="pa-restore-", dir=destination.parent) as temporary:
            temp = Path(temporary).resolve()
            entries = z.infolist()
            if len(entries) > MAX_FILES + 1 or len({e.filename.casefold() for e in entries}) != len(entries):
                raise ValueError("备份文件数量异常或包含重复路径")
            for entry in entries:
                _archive_name(entry.filename)
                if entry.file_size > MAX_BYTES or entry.is_dir() or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("备份包含无效文件类型或超大文件")
            manifest_info = z.getinfo("manifest.json")
            if manifest_info.file_size > 8 * 1024 * 1024:
                raise ValueError("备份清单过大")
            manifest = json.loads(z.read("manifest.json"))
            files = manifest.get("files")
            if manifest.get("format") != "personal-agent-backup/v1" or not isinstance(files, dict) or set(files) | {"manifest.json"} != {e.filename for e in entries} or "state.sqlite" not in files:
                raise ValueError("备份格式或文件清单不匹配")
            if not isinstance(manifest.get("original_data_directory"), str):
                raise ValueError("备份缺少原数据目录信息")
            total = 0
            for name, meta in files.items():
                path = temp.joinpath(*_archive_name(name).parts)
                if not isinstance(meta, dict) or type(meta.get("bytes")) is not int or not 0 <= meta["bytes"] <= MAX_BYTES:
                    raise ValueError("备份清单包含无效大小")
                path.parent.mkdir(parents=True, exist_ok=True)
                hasher, size = hashlib.sha256(), 0
                with z.open(name) as source, path.open("xb") as target:
                    while chunk := source.read(1024 * 1024):
                        total += len(chunk)
                        size += len(chunk)
                        if total > MAX_BYTES or size > meta["bytes"]:
                            raise ValueError("备份解压大小超出清单或上限")
                        hasher.update(chunk)
                        target.write(chunk)
                if size != meta["bytes"] or hasher.hexdigest() != meta.get("sha256"):
                    raise ValueError("备份文件内容与校验清单不一致")
            with closing(sqlite3.connect(temp / "state.sqlite")) as connection:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("备份数据库完整性检查失败")
                if connection.execute("PRAGMA user_version").fetchone()[0] > 7:
                    raise ValueError("此备份由较新版本创建，请先升级程序")
            restored = Store(temp / "state.sqlite")
            try:
                restored.recover()
                with restored.transaction():
                    restored.db.execute("CREATE TABLE IF NOT EXISTS local_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                    restored.db.execute("INSERT OR REPLACE INTO local_metadata VALUES('workspace_id',?)", (uuid.uuid4().hex,))
                    for table, field in [("tasks", "spec"), ("routine_policies", "template"), ("admission_rules", "template")]:
                        for row in restored.all(f"SELECT id,{field} FROM {table}"):
                            spec = _relocate(json.loads(row[field]), manifest["original_data_directory"], destination)
                            restored.db.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (encode(spec), row["id"]))
                    for goal in restored.all("SELECT id,revision,status FROM goals WHERE status IN ('active','needs_attention')"):
                        restored.set_goal_state(goal["id"], "paused", expected_revision=goal["revision"])
                    restored.db.execute("UPDATE tasks SET status='paused',auto_continue='paused',reason='restored_backup_requires_resume' WHERE status NOT IN ('succeeded','cancelled')")
                    restored.db.execute("UPDATE admission_rules SET mode='off',revision=revision+1 WHERE mode!='off'")
                    held_chats = restored.db.execute("UPDATE events SET state='held_after_restore' WHERE task_id IS NULL AND state='pending'").rowcount
                    restored.db.execute("UPDATE deliveries SET state='held_after_restore',last_error='restored_without_redelivery' WHERE state IN ('pending','failed')")
                    for room in restored.all("SELECT id FROM rooms"):
                        restored._notice(None, room["id"], "资料已从备份恢复。未完成任务与持续目标已暂停，自动接手已关闭；请检查来源和模型配置后明确恢复。原聊天请求不会自动重发。")
                restored.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                restored.close()
            # Both temporary and destination are verified absolute siblings.
            # rename fails if destination appeared; no replacement or deletion
            # of a pre-existing user directory is performed.
            if destination.exists() or temp.parent != destination.parent:
                raise ValueError("恢复目录已存在或位置改变，未覆盖")
            temp.rename(destination)
            return {"path": str(destination), "files": len(files), "tasks_paused": True, "admission_off": True, "held_chat_requests": held_chats, "credentials_restored": False}
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
        raise ValueError("备份内容无效，未恢复到目标目录") from exc
