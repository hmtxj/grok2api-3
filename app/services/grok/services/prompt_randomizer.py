"""
Prompt 动态随机解析引擎

支持两种语法：
  1. {A|B|C} — 每次随机选取一个选项
  2. <category> — 从预设词库中随机取一个词条
"""

import json
import random
import re
from pathlib import Path
from typing import Dict, List, Optional

from app.core.logger import logger

# 词库文件目录
_WILDCARDS_DIR = Path(__file__).resolve().parents[4] / "data" / "wildcards"

# 内存缓存：词库名 → 词条列表（兼容字符串数组和 {cn, en} 对象数组）
_wildcard_cache: Dict[str, list] = {}

# 正则：匹配 {opt1|opt2|opt3}
_INLINE_RE = re.compile(r"\{([^{}]+)\}")

# 正则：匹配 <category_name>（仅字母、数字、下划线）
_WILDCARD_RE = re.compile(r"<(\w+)>")


def load_wildcard(category: str) -> Optional[List[str]]:
    """加载指定词库，带内存缓存"""
    if category in _wildcard_cache:
        return _wildcard_cache[category]

    filepath = _WILDCARDS_DIR / f"{category}.json"
    if not filepath.exists():
        logger.debug(f"词库文件不存在: {filepath}")
        return None

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            items = json.load(f)
        if not isinstance(items, list) or len(items) == 0:
            logger.warning(f"词库为空或格式错误: {filepath}")
            return None
        _wildcard_cache[category] = items
        logger.info(f"已加载词库 <{category}>，共 {len(items)} 个词条")
        return items
    except Exception as e:
        logger.error(f"加载词库 {filepath} 失败: {e}")
        return None


def _replace_inline(match: re.Match) -> str:
    """处理 {A|B|C} 语法：随机选取一个"""
    options = [opt.strip() for opt in match.group(1).split("|") if opt.strip()]
    if not options:
        return match.group(0)
    return random.choice(options)


def _replace_wildcard(match: re.Match) -> str:
    """处理 <category> 语法：从词库随机取一个（兼容 {cn, en} 对象格式）"""
    category = match.group(1).lower()
    items = load_wildcard(category)
    if items is None:
        return match.group(0)
    chosen = random.choice(items)
    # 如果是 {cn, en} 对象，取 en 字段
    if isinstance(chosen, dict):
        return chosen.get("en", str(chosen))
    return str(chosen)


def randomize_prompt(prompt: str) -> str:
    """
    主入口：解析 Prompt 中的动态语法并替换。

    处理顺序：先替换 {A|B|C}，再替换 <category>。
    每次调用都会产生不同的随机结果。
    """
    if not prompt:
        return prompt

    # 第一遍：替换所有 {A|B|C} 内联随机
    result = _INLINE_RE.sub(_replace_inline, prompt)

    # 第二遍：替换所有 <category> 通配符
    result = _WILDCARD_RE.sub(_replace_wildcard, result)

    return result


def get_available_wildcards() -> List[str]:
    """返回所有可用的词库名称列表"""
    if not _WILDCARDS_DIR.exists():
        return []
    return sorted(
        p.stem for p in _WILDCARDS_DIR.glob("*.json")
        if p.is_file()
    )
