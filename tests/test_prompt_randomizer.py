"""
prompt_randomizer 模块单元测试
"""

import sys
import os
import json
import tempfile

# 把项目根目录加入路径，以便 import app 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


class TestInlineRandom:
    """测试 {A|B|C} 内联随机语法"""

    def test_basic_inline(self):
        from app.services.grok.services.prompt_randomizer import _INLINE_RE, _replace_inline
        import re
        result = _INLINE_RE.sub(_replace_inline, "{standing|sitting|kneeling}")
        assert result in ["standing", "sitting", "kneeling"]

    def test_multiple_inline(self):
        from app.services.grok.services.prompt_randomizer import _INLINE_RE, _replace_inline
        prompt = "1girl, {standing|sitting}, {red|blue} dress"
        result = _INLINE_RE.sub(_replace_inline, prompt)
        assert "{" not in result
        assert "|" not in result

    def test_single_option(self):
        from app.services.grok.services.prompt_randomizer import _INLINE_RE, _replace_inline
        result = _INLINE_RE.sub(_replace_inline, "{only_one}")
        assert result == "only_one"

    def test_empty_braces(self):
        from app.services.grok.services.prompt_randomizer import _INLINE_RE, _replace_inline
        prompt = "test {}"
        result = _INLINE_RE.sub(_replace_inline, prompt)
        # 空大括号保持原样
        assert result == "test {}"


class TestWildcard:
    """测试 <category> 通配符语法"""

    def test_existing_wildcard(self):
        from app.services.grok.services.prompt_randomizer import randomize_prompt
        result = randomize_prompt("<pose>")
        # 应该被替换为词库中的一个词条
        assert result != "<pose>"
        assert len(result) > 0

    def test_unknown_wildcard_preserved(self):
        from app.services.grok.services.prompt_randomizer import randomize_prompt
        result = randomize_prompt("<nonexistent_category_xyz>")
        # 未知词库保持原文
        assert result == "<nonexistent_category_xyz>"

    def test_multiple_same_wildcard_independent(self):
        from app.services.grok.services.prompt_randomizer import randomize_prompt
        # 多次调用，应该能产生不同结果（概率性，跑 50 次）
        results = set()
        for _ in range(50):
            result = randomize_prompt("<pose>")
            results.add(result)
        # 至少应该有 2 种不同结果
        assert len(results) >= 2

    def test_mixed_syntax(self):
        from app.services.grok.services.prompt_randomizer import randomize_prompt
        prompt = "1girl, <pose>, wearing {red|blue} dress, <style>"
        result = randomize_prompt(prompt)
        # 不应包含任何未替换的语法
        assert "<pose>" not in result
        assert "<style>" not in result
        assert "{" not in result
        assert "|" not in result


class TestPassthrough:
    """测试无特殊语法的 prompt 保持不变"""

    def test_plain_text(self):
        from app.services.grok.services.prompt_randomizer import randomize_prompt
        prompt = "1girl, standing, red dress, realistic"
        result = randomize_prompt(prompt)
        assert result == prompt

    def test_empty_string(self):
        from app.services.grok.services.prompt_randomizer import randomize_prompt
        assert randomize_prompt("") == ""

    def test_none_input(self):
        from app.services.grok.services.prompt_randomizer import randomize_prompt
        assert randomize_prompt(None) is None


class TestGetAvailableWildcards:
    """测试获取可用词库列表"""

    def test_returns_list(self):
        from app.services.grok.services.prompt_randomizer import get_available_wildcards
        wildcards = get_available_wildcards()
        assert isinstance(wildcards, list)
        # 应至少包含我们创建的 5 个词库
        expected = {"pose", "camera", "outfit", "legwear", "style"}
        assert expected.issubset(set(wildcards))

    def test_sorted_order(self):
        from app.services.grok.services.prompt_randomizer import get_available_wildcards
        wildcards = get_available_wildcards()
        assert wildcards == sorted(wildcards)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
