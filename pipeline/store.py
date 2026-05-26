"""
pipeline/store.py — 按来源（source）+ 日期组织输出文件，提供检索接口

目录结构:
    /crawler/hotwords/  （最终对外目录）
    ├── sogou_newwords/         搜狗网络流行新词
    │   ├── 20260318/
    │   │   └── sogou_20260318_0510.txt
    │   └── ...
    ├── douyin_hotwords/        抖音热词
    │   ├── 20260318/
    │   │   └── douyin_hotwords_20260318_0110.json
    │   └── ...
    ├── weibo_hotsearch/        微博热搜
    ├── douyin_hotlist/         抖音热榜
    ├── xhs_hot/                小红书热点
    └── manifest.json           ← 全局索引（可选，加速查询）

按来源获取示例:
    store = StoreManager()
    store.list_sources()                              → ["douyin_hotwords", "sogou_newwords", ...]
    store.list_dates("douyin_hotwords")               → ["20260318", "20260317"]
    store.list_files("douyin_hotwords", "20260318")   → [FileInfo(...), ...]
    store.get_latest_files("douyin_hotwords")         → [FileInfo(...), ...]
    store.get_file_path("douyin_hotwords", "20260318", "douyin_hotwords_????????_????.json")
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import Config  # noqa: E402
from logger import get_logger  # noqa: E402

logger = get_logger("pipeline.store")


@dataclass
class FileInfo:
    """单个输出文件的元信息"""
    source: str          # douyin_hotwords / sogou_newwords / ...
    date: str            # YYYYMMDD
    filename: str        # 文件名
    full_path: str       # 完整路径
    size: int            # bytes
    created: str         # ISO 8601


class StoreManager:
    """
    统一存储管理器 — 按来源（source）和日期组织输出文件。

    所有爬取/处理的输出都通过 StoreManager 保存和检索，
    方便后续按来源批量拉取。
    """

    KNOWN_SOURCES = ("sogou_newwords", "douyin_hotwords", "weibo_hotsearch", "douyin_hotlist", "xhs_hot")

    def __init__(self, output_dir: Optional[str] = None):
        # 默认读取最终目录（/crawler/hotwords），crawler 暂存目录 output/ 只用于中转
        self.output_dir = Path(output_dir or Config.HOTWORDS_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── 写入 ─────────────────────────────────────────────────

    def save_text(self, source: str, filename: str, content: str, date: Optional[str] = None) -> str:
        """保存文本文件到 output/{source}/{date}/"""
        target = self._resolve_path(source, filename, date)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        logger.info(f"[store] 已保存: {target}")
        return str(target)

    def save_json(self, source: str, filename: str, data: Any, date: Optional[str] = None) -> str:
        """保存 JSON 文件到 output/{source}/{date}/"""
        target = self._resolve_path(source, filename, date)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"[store] 已保存: {target}")
        return str(target)

    def save_bytes(self, source: str, filename: str, data: bytes, date: Optional[str] = None) -> str:
        """保存二进制文件"""
        target = self._resolve_path(source, filename, date)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        logger.info(f"[store] 已保存: {target}")
        return str(target)

    # ── 查询 ─────────────────────────────────────────────────

    def list_sources(self) -> List[str]:
        """列出所有已有数据的来源"""
        if not self.output_dir.exists():
            return []
        return sorted([
            d.name for d in self.output_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        ])

    def list_dates(self, source: str) -> List[str]:
        """列出某个来源下所有日期（降序 = 最新在前）"""
        src_dir = self.output_dir / source
        if not src_dir.exists():
            return []
        return sorted(
            [d.name for d in src_dir.iterdir() if d.is_dir() and d.name.isdigit()],
            reverse=True,
        )

    def list_files(self, source: str, date: Optional[str] = None) -> List[FileInfo]:
        """
        列出某来源（+可选日期）的所有文件。
        若不指定日期，返回所有日期下的文件。
        """
        results: List[FileInfo] = []
        dates = [date] if date else self.list_dates(source)
        for d in dates:
            day_dir = self.output_dir / source / d
            if not day_dir.exists():
                continue
            for f in sorted(day_dir.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    results.append(self._file_info(source, d, f))
        return results

    def get_latest_files(self, source: str) -> List[FileInfo]:
        """获取某来源最新一天的所有文件"""
        dates = self.list_dates(source)
        if not dates:
            return []
        return self.list_files(source, dates[0])

    def get_file_path(self, source: str, date: str, pattern: str) -> Optional[str]:
        """通过 glob 模式在 output/{source}/{date}/ 下查找文件"""
        day_dir = self.output_dir / source / date
        matches = list(day_dir.glob(pattern))
        if matches:
            return str(max(matches, key=lambda p: p.stat().st_mtime))
        return None

    # ── 索引 ─────────────────────────────────────────────────

    def build_manifest(self) -> Dict[str, Any]:
        """生成全局索引 manifest.json（按来源 → 日期 → 文件列表）"""
        manifest: Dict[str, Any] = {}
        for source in self.list_sources():
            manifest[source] = {}
            for date in self.list_dates(source):
                files = self.list_files(source, date)
                manifest[source][date] = [asdict(f) for f in files]

        manifest_path = self.output_dir / "manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        logger.info(f"[store] 索引已更新: {manifest_path}")
        return manifest

    def get_source_summary(self) -> List[Dict[str, Any]]:
        """按来源汇总：来源名 + 日期数 + 最新日期 + 总文件数"""
        summary = []
        for source in self.list_sources():
            dates = self.list_dates(source)
            total_files = sum(len(self.list_files(source, d)) for d in dates)
            summary.append({
                "source": source,
                "total_dates": len(dates),
                "latest_date": dates[0] if dates else None,
                "total_files": total_files,
            })
        return summary

    # ── 内部 ─────────────────────────────────────────────────

    def _resolve_path(self, source: str, filename: str, date: Optional[str] = None) -> Path:
        if date is None:
            date = datetime.now().strftime("%Y%m%d")
        return self.output_dir / source / date / filename

    @staticmethod
    def _file_info(source: str, date: str, path: Path) -> FileInfo:
        stat = path.stat()
        return FileInfo(
            source=source,
            date=date,
            filename=path.name,
            full_path=str(path),
            size=stat.st_size,
            created=datetime.fromtimestamp(stat.st_mtime).isoformat(),
        )
