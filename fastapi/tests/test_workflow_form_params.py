"""工作流节点表单参数解析回归测试（纯逻辑，不依赖 MySQL / 网络）

覆盖 params[name][] 多值字段（skill_ids / trigger_weekdays / action_wf_param_*）：
修复前它们会退化成 "name][" 垃圾键（如 "skill_ids]["），且值只剩最后一个。

运行：python -m pytest tests/test_workflow_form_params.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routes.workflows import _parse_params


class FakeForm:
    """最小 form 桩：只需 multi_items()"""

    def __init__(self, items):
        self._items = list(items)

    def multi_items(self):
        return list(self._items)


def test_multi_value_fields_collected_as_list():
    form = FakeForm(
        [
            ("params[agent_id]", "2"),
            ("params[prompt]", "hi"),
            ("params[skill_ids][]", "5"),
            ("params[skill_ids][]", "2"),
            ("params[action_wf_param_key][]", "text"),
            ("params[action_wf_param_key][]", ""),
            ("params[action_wf_param_val][]", "{{input.user_text}}"),
            ("params[action_wf_param_val][]", "ignored"),
        ]
    )
    p = _parse_params(form)
    assert p["agent_id"] == 2
    assert p["prompt"] == "hi"
    assert p["skill_ids"] == ["5", "2"]
    assert p["action_wf_param_key"] == ["text", ""]
    assert p["action_wf_param_val"] == ["{{input.user_text}}", "ignored"]
    # 无 "][" 之类的垃圾键残留
    assert not [k for k in p if "[" in k or "]" in k]


def test_nested_and_unknown_keys_ignored():
    form = FakeForm(
        [
            ("params[a][b]", "x"),  # 未知嵌套 → 丢弃，不产生 "a][b" 键
            ("other[key]", "y"),  # 非 params 前缀 → 丢弃
            ("params[limit]", "10"),
        ]
    )
    p = _parse_params(form)
    assert p == {"limit": 10}
