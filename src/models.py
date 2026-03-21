from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class TestCase(BaseModel):
    """
    结构化测试用例。

    字段与输出表格列一致：ID、优先级、模块、测试标题、摘要、前置条件、测试步骤、
    期望结果、实际结果（可为空）、类型、测试数据、备注。
    """

    id: str = Field(..., description="用例编号，例如 TC-001")
    priority: str = Field(..., description="优先级：P0/P1/P2/P3 或 High/Medium/Low")
    module: str = Field(..., description="所属模块/功能点")
    title: str = Field(..., description="测试标题")
    summary: str = Field("", description="摘要，一句话概括本用例验证点")
    preconditions: str = Field("", description="前置条件")
    steps: List[str] = Field(default_factory=list, description="测试步骤列表")
    expected: List[str] = Field(default_factory=list, description="期望结果列表")
    actual_result: str = Field("", description="实际结果，执行后填写，可为空")
    test_type: str = Field("", description="类型：功能/异常/边界/兼容/安全/性能 等")
    data: str = Field("", description="测试数据（可选）")
    remarks: str = Field("", description="备注（可选）")


class GenerationResult(BaseModel):
    """
    一次文档生成结果。

    - 对应单个源需求文档；
    - 包含思维导图、测试点列表、完整用例集以及元信息。
    """

    source_name: str = Field(..., description="来源文件名（不含路径）")
    language: str = Field(..., description="输出语言：zh/en")

    context_summary: str = Field(
        "",
        description="从文档提炼的可复用摘要（用于分批生成用例，减少 token 消耗）",
    )
    mindmap_mermaid: str = Field(..., description="Mermaid mindmap 文本（不带 ``` 包裹）")
    test_points: List[str] = Field(default_factory=list, description="测试点列表（便于索引/复用）")
    test_cases: List[TestCase] = Field(default_factory=list, description="测试用例集合")

    assumptions: List[str] = Field(default_factory=list, description="对需求不明确处的合理假设")
    risks: List[str] = Field(default_factory=list, description="需求/测试风险提示")
    out_of_scope: List[str] = Field(default_factory=list, description="不在范围内的内容（如果有）")

