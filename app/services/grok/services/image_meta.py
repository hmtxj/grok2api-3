"""
图片元数据索引服务

为缓存管理页面提供按任务分组的图片索引。
每次图片下载/生成时写入元数据，支持按任务分组查询。
"""

import json
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Optional
from threading import Lock

from app.core.logger import logger
from app.core.storage import DATA_DIR

# 任务上下文变量（异步安全）
_current_task_id: ContextVar[Optional[str]] = ContextVar("_current_task_id", default=None)
_current_prompt: ContextVar[Optional[str]] = ContextVar("_current_prompt", default=None)
_current_aspect_ratio: ContextVar[str] = ContextVar("_current_aspect_ratio", default="1:1")
_current_source: ContextVar[str] = ContextVar("_current_source", default="api")


def set_task_context(
    task_id: str,
    prompt: str,
    source: str = "api",
    aspect_ratio: str = "1:1",
):
    """设置当前异步任务的上下文（在 create_image 开始时调用）"""
    _current_task_id.set(task_id)
    _current_prompt.set(prompt)
    _current_source.set(source)
    _current_aspect_ratio.set(aspect_ratio)


def clear_task_context():
    """清除当前任务上下文"""
    _current_task_id.set(None)
    _current_prompt.set(None)
    _current_source.set("api")
    _current_aspect_ratio.set("1:1")


def auto_record_download(filename: str):
    """
    下载完成后自动记录到当前任务（由 process_url 调用）。
    如果没有活跃的任务上下文则忽略。
    """
    task_id = _current_task_id.get()
    prompt = _current_prompt.get()
    if not task_id or not prompt:
        return
    record_images(
        task_id=task_id,
        prompt=prompt,
        filenames=[filename],
        source=_current_source.get(),
        aspect_ratio=_current_aspect_ratio.get(),
    )

# 索引文件路径
META_FILE = DATA_DIR / "tmp" / "image_meta.json"
IMAGE_DIR = DATA_DIR / "tmp" / "image"

# 线程安全锁
_meta_lock = Lock()


def _load_meta() -> Dict[str, Any]:
    """加载元数据索引"""
    if not META_FILE.exists():
        return {"tasks": {}}
    try:
        with open(META_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "tasks" not in data:
            data["tasks"] = {}
        return data
    except Exception as e:
        logger.warning(f"元数据索引加载失败，重建: {e}")
        return {"tasks": {}}


def _save_meta(data: Dict[str, Any]):
    """持久化元数据索引"""
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(META_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"元数据索引保存失败: {e}")


def generate_task_id() -> str:
    """生成任务 ID"""
    ts = int(time.time())
    short_uuid = uuid.uuid4().hex[:8]
    return f"task_{ts}_{short_uuid}"


# 合并时间窗口：同 prompt 同 source 在此毫秒内视为同一分组
_MERGE_WINDOW_MS = 60_000


def record_images(
    task_id: str,
    prompt: str,
    filenames: List[str],
    source: str = "api",
    aspect_ratio: str = "1:1",
):
    """
    记录一次生图任务的元数据

    写入前先查找同 prompt + 同 source + 60 秒内的已有分组，
    找到则追加图片到该分组，避免并发连接产生大量重复分组。
    """
    if not filenames:
        return

    with _meta_lock:
        data = _load_meta()
        now_ms = int(time.time() * 1000)

        if task_id in data["tasks"]:
            # 精确匹配已有任务，直接追加
            existing = data["tasks"][task_id]
            existing["images"].extend(filenames)
            existing["images"] = list(dict.fromkeys(existing["images"]))
        else:
            # 查找可合并的已有分组
            merge_target = None
            for tid, tdata in data["tasks"].items():
                if (
                    tdata.get("prompt") == prompt
                    and tdata.get("source") == source
                    and abs(now_ms - tdata.get("created_at", 0)) < _MERGE_WINDOW_MS
                ):
                    merge_target = tid
                    break

            if merge_target:
                existing = data["tasks"][merge_target]
                existing["images"].extend(filenames)
                existing["images"] = list(dict.fromkeys(existing["images"]))
            else:
                data["tasks"][task_id] = {
                    "prompt": prompt,
                    "source": source,
                    "created_at": now_ms,
                    "aspect_ratio": aspect_ratio,
                    "images": filenames,
                }
        _save_meta(data)


def get_tasks(
    page: int = 1,
    page_size: int = 20,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """
    按任务分组返回图片列表

    Args:
        page: 页码
        page_size: 每页任务数
        source: 过滤来源（"api" | "imagine"，None 表示全部）

    Returns:
        包含 total/page/page_size/tasks 的字典
    """
    with _meta_lock:
        data = _load_meta()

    tasks = data.get("tasks", {})

    # 过滤来源
    if source:
        tasks = {k: v for k, v in tasks.items() if v.get("source") == source}

    # 按创建时间降序排列
    sorted_items = sorted(
        tasks.items(),
        key=lambda x: x[1].get("created_at", 0),
        reverse=True,
    )

    total = len(sorted_items)
    start = max(0, (page - 1) * page_size)
    paged = sorted_items[start: start + page_size]

    # 构建结果，校验文件是否仍存在
    result_tasks = []
    for task_id, task_data in paged:
        # 检查图片文件是否仍存在
        existing_images = []
        for img_name in task_data.get("images", []):
            img_path = IMAGE_DIR / img_name
            if img_path.exists():
                try:
                    fsize = img_path.stat().st_size
                except Exception:
                    fsize = 0
                existing_images.append({
                    "name": img_name,
                    "view_url": f"/v1/files/image/{img_name}",
                    "size": fsize,
                })

        result_tasks.append({
            "task_id": task_id,
            "prompt": task_data.get("prompt", ""),
            "source": task_data.get("source", "api"),
            "created_at": task_data.get("created_at", 0),
            "aspect_ratio": task_data.get("aspect_ratio", "1:1"),
            "image_count": len(existing_images),
            "images": existing_images,
        })

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "tasks": result_tasks,
    }


def get_uncategorized_images(
    page: int = 1,
    page_size: int = 50,
) -> Dict[str, Any]:
    """
    获取未分类的图片（不属于任何任务的图片）

    Returns:
        包含 total/page/page_size/items 的字典
    """
    with _meta_lock:
        data = _load_meta()

    # 收集所有已分类的文件名
    categorized = set()
    for task_data in data.get("tasks", {}).values():
        for img in task_data.get("images", []):
            categorized.add(img)

    # 扫描磁盘上的所有图片
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    if not IMAGE_DIR.exists():
        return {"total": 0, "page": page, "page_size": page_size, "items": []}

    uncategorized = []
    for f in IMAGE_DIR.glob("*"):
        if f.is_file() and f.suffix.lower() in image_exts and f.name not in categorized:
            try:
                stat = f.stat()
                uncategorized.append({
                    "name": f.name,
                    "size": stat.st_size,
                    "size_bytes": stat.st_size,
                    "mtime_ms": int(stat.st_mtime * 1000),
                    "view_url": f"/v1/files/image/{f.name}",
                })
            except Exception:
                continue

    # 按时间降序
    uncategorized.sort(key=lambda x: x["mtime_ms"], reverse=True)

    total = len(uncategorized)
    start = max(0, (page - 1) * page_size)
    paged = uncategorized[start: start + page_size]

    return {"total": total, "page": page, "page_size": page_size, "items": paged}


def cleanup_deleted(filename: str):
    """图片文件被删除时，从索引中移除"""
    with _meta_lock:
        data = _load_meta()
        changed = False
        empty_tasks = []

        for task_id, task_data in data.get("tasks", {}).items():
            images = task_data.get("images", [])
            if filename in images:
                images.remove(filename)
                changed = True
            if not images:
                empty_tasks.append(task_id)

        # 清理空任务
        for task_id in empty_tasks:
            del data["tasks"][task_id]
            changed = True

        if changed:
            _save_meta(data)


def delete_task(task_id: str) -> Dict[str, Any]:
    """删除整个任务分组及其所有图片文件"""
    with _meta_lock:
        data = _load_meta()
        task = data.get("tasks", {}).get(task_id)
        if not task:
            return {"deleted": 0}

        deleted = 0
        for img_name in task.get("images", []):
            img_path = IMAGE_DIR / img_name
            if img_path.exists():
                try:
                    img_path.unlink()
                    deleted += 1
                except Exception as e:
                    logger.warning(f"删除图片失败 {img_name}: {e}")

        del data["tasks"][task_id]
        _save_meta(data)

    return {"deleted": deleted}


def cleanup_all():
    """清除所有元数据（配合清空缓存使用）"""
    with _meta_lock:
        _save_meta({"tasks": {}})


def merge_similar_tasks(window_ms: int = _MERGE_WINDOW_MS) -> Dict[str, Any]:
    """一次性合并历史中同 prompt + 同 source + 时间窗口内的重复分组"""
    with _meta_lock:
        data = _load_meta()
        tasks = data.get("tasks", {})
        if not tasks:
            return {"merged": 0, "removed": 0}

        # 按 (prompt, source) 分桶，桶内按 created_at 排序
        buckets: Dict[tuple, list] = {}
        for tid, tdata in tasks.items():
            key = (tdata.get("prompt", ""), tdata.get("source", "api"))
            buckets.setdefault(key, []).append((tid, tdata))

        merged_count = 0
        removed_ids = []

        for key, items in buckets.items():
            if len(items) < 2:
                continue
            items.sort(key=lambda x: x[1].get("created_at", 0))

            # 贪心合并：遍历排序后的列表，相邻且时间差在窗口内则合并
            anchor_tid, anchor_data = items[0]
            for tid, tdata in items[1:]:
                if abs(tdata.get("created_at", 0) - anchor_data.get("created_at", 0)) < window_ms:
                    # 合并到 anchor
                    anchor_data["images"].extend(tdata.get("images", []))
                    anchor_data["images"] = list(dict.fromkeys(anchor_data["images"]))
                    removed_ids.append(tid)
                    merged_count += 1
                else:
                    # 开始新的 anchor
                    anchor_tid, anchor_data = tid, tdata

        for tid in removed_ids:
            del tasks[tid]

        if removed_ids:
            _save_meta(data)
            logger.info(f"合并历史分组完成: 合并 {merged_count} 个, 移除 {len(removed_ids)} 个冗余分组")

    return {"merged": merged_count, "removed": len(removed_ids)}
