"""生成可重复、脱敏的合成 Benchmark 种子数据。"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.schemas.ai import GradingResult, QuestionType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_DIR = PROJECT_ROOT / "benchmark" / "results"
DEFAULT_JSON_NAME = "synthetic_benchmark.json"
DEFAULT_CSV_NAME = "synthetic_benchmark.csv"

NonEmptyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class BenchmarkMetadata(BaseModel):
    """Benchmark 数据集的版本和生成元数据。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    dataset_version: NonEmptyText
    model_version: NonEmptyText
    prompt_version: NonEmptyText
    scoring_criteria: NonEmptyText
    seed: int
    record_count: int = Field(ge=10)


class SyntheticBenchmarkCase(BaseModel):
    """单条脱敏的合成阅卷样本。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    case_id: NonEmptyText
    question_type: QuestionType
    question: NonEmptyText
    scoring_criteria: NonEmptyText
    reference_answer: NonEmptyText
    student_answer: NonEmptyText
    score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    reason: NonEmptyText
    knowledge_points: list[NonEmptyText] = Field(min_length=1)
    validation_status: NonEmptyText

    @model_validator(mode="after")
    def _validate_score_range(self) -> SyntheticBenchmarkCase:
        """确保种子数据中的分数处于题目满分范围内。"""

        if self.score > self.max_score:
            raise ValueError("Benchmark 样本得分不能超过题目满分。")
        return self


class SyntheticBenchmarkDataset(BaseModel):
    """完整的合成 Benchmark 数据集。"""

    model_config = ConfigDict(extra="forbid")

    metadata: BenchmarkMetadata
    cases: list[SyntheticBenchmarkCase] = Field(min_length=10)

    @model_validator(mode="after")
    def _validate_record_count(self) -> SyntheticBenchmarkDataset:
        """确保元数据中的数量与实际样本数量一致。"""

        if self.metadata.record_count != len(self.cases):
            raise ValueError("Benchmark 元数据中的样本数量与实际记录不一致。")
        return self


_SEED_CASE_TEMPLATES: tuple[Mapping[str, Any], ...] = (
    {
        "case_id": "m0-math-addition",
        "question_type": QuestionType.SINGLE_CHOICE,
        "question": "2 加 2 等于多少？",
        "scoring_criteria": "答案完全正确得满分，否则不得分。",
        "reference_answer": "4",
        "student_answer": "4",
        "score": 5,
        "max_score": 5,
        "reason": "学生答案与参考答案一致。",
        "knowledge_points": ["整数加法"],
    },
    {
        "case_id": "m0-physics-force",
        "question_type": QuestionType.TRUE_FALSE,
        "question": "力可以改变物体的运动状态。",
        "scoring_criteria": "判断正确得满分，判断错误不得分。",
        "reference_answer": "正确",
        "student_answer": "正确",
        "score": 5,
        "max_score": 5,
        "reason": "学生正确识别了力对运动状态的影响。",
        "knowledge_points": ["力的作用效果"],
    },
    {
        "case_id": "m0-python-list",
        "question_type": QuestionType.SHORT_ANSWER,
        "question": "简述 Python 列表的两个主要特征。",
        "scoring_criteria": "答出有序和可变两个特征得满分，答出一个特征得一半分。",
        "reference_answer": "列表是有序且可变的集合。",
        "student_answer": "列表是有序且可变的集合。",
        "score": 10,
        "max_score": 10,
        "reason": "学生答案覆盖评分标准要求的两个特征。",
        "knowledge_points": ["Python 列表", "可变序列"],
    },
    {
        "case_id": "m0-database-index",
        "question_type": QuestionType.SHORT_ANSWER,
        "question": "数据库索引的主要作用是什么？",
        "scoring_criteria": "说明索引能够减少查询扫描范围并提升检索速度即可得分。",
        "reference_answer": "索引通过减少需要扫描的数据范围来提升查询速度。",
        "student_answer": "索引可以帮助数据库更快找到符合条件的记录。",
        "score": 8,
        "max_score": 10,
        "reason": "学生说明了索引提升查询定位速度的核心作用。",
        "knowledge_points": ["数据库索引", "查询优化"],
    },
    {
        "case_id": "m0-network-dns",
        "question_type": QuestionType.TRUE_FALSE,
        "question": "DNS 的作用之一是将域名解析为 IP 地址。",
        "scoring_criteria": "判断正确得满分，判断错误不得分。",
        "reference_answer": "正确",
        "student_answer": "错误",
        "score": 0,
        "max_score": 5,
        "reason": "学生判断与 DNS 的基本功能不一致。",
        "knowledge_points": ["DNS", "域名解析"],
    },
    {
        "case_id": "m0-history-source",
        "question_type": QuestionType.SHORT_ANSWER,
        "question": "分析历史资料时为什么需要核对资料来源？",
        "scoring_criteria": "说明来源核对有助于判断资料可靠性和避免片面结论即可得分。",
        "reference_answer": "核对来源可以评估可靠性、理解背景并减少片面解读。",
        "student_answer": "因为不同来源可能有不同观点，需要判断资料是否可靠。",
        "score": 9,
        "max_score": 10,
        "reason": "学生提到来源差异和可靠性判断，覆盖主要评分点。",
        "knowledge_points": ["史料分析", "证据意识"],
    },
    {
        "case_id": "m0-biology-cell",
        "question_type": QuestionType.SINGLE_CHOICE,
        "question": "细胞是生物体结构和功能的基本单位，这一说法是否正确？",
        "scoring_criteria": "选择正确答案得满分，否则不得分。",
        "reference_answer": "正确",
        "student_answer": "正确",
        "score": 5,
        "max_score": 5,
        "reason": "学生选择了符合细胞基本概念的答案。",
        "knowledge_points": ["细胞基本单位"],
    },
    {
        "case_id": "m0-literature-theme",
        "question_type": QuestionType.SHORT_ANSWER,
        "question": "概括文章主题时，应该重点关注哪些信息？",
        "scoring_criteria": "答出主要内容和作者核心意图即可得分。",
        "reference_answer": "应关注文章主要内容、关键事件和作者表达的核心意图。",
        "student_answer": "要看文章写了什么以及作者想表达什么。",
        "score": 8,
        "max_score": 10,
        "reason": "学生覆盖了主要内容和作者意图两个核心方面。",
        "knowledge_points": ["文章主题", "阅读概括"],
    },
    {
        "case_id": "m0-statistics-mean",
        "question_type": QuestionType.SINGLE_CHOICE,
        "question": "一组数据的平均数通常如何计算？",
        "scoring_criteria": "选择总和除以数据个数的答案得满分。",
        "reference_answer": "数据总和除以数据个数",
        "student_answer": "数据总和除以数据个数",
        "score": 5,
        "max_score": 5,
        "reason": "学生给出了平均数的正确计算方法。",
        "knowledge_points": ["平均数", "数据统计"],
    },
    {
        "case_id": "m0-ethics-privacy",
        "question_type": QuestionType.SHORT_ANSWER,
        "question": "设计教学系统时为什么要保护学生隐私？",
        "scoring_criteria": "说明保护个人信息、避免滥用并维护学习信任即可得分。",
        "reference_answer": "保护隐私可以避免个人信息滥用并维护学生的安全与信任。",
        "student_answer": "因为学生信息不能被随意公开或滥用。",
        "score": 9,
        "max_score": 10,
        "reason": "学生明确指出了信息公开和滥用风险。",
        "knowledge_points": ["隐私保护", "教育伦理"],
    },
)


def _select_seed_templates(count: int, seed: int) -> list[Mapping[str, Any]]:
    """按固定随机种子选择样本，保证同一参数生成结果一致。"""

    generator = random.Random(seed)
    indexes = list(range(len(_SEED_CASE_TEMPLATES)))
    generator.shuffle(indexes)
    selected: list[Mapping[str, Any]] = []
    for position in range(count):
        selected.append(
            _SEED_CASE_TEMPLATES[indexes[position % len(indexes)]],
        )
    return selected


def _build_case(
    template: Mapping[str, Any],
    *,
    occurrence: int,
) -> SyntheticBenchmarkCase:
    """使用共用评分 DTO 校验并构造一条脱敏 Benchmark 样本。"""

    grading_result = GradingResult(
        question_type=template["question_type"],
        score=template["score"],
        max_score=template["max_score"],
        reason=template["reason"],
        correct_points=template["knowledge_points"],
        missing_knowledge_points=[],
        knowledge_points=template["knowledge_points"],
        suggestions=["根据评分标准继续巩固相关知识点。"],
        confidence=1.0,
        validation_status="Validated",
        review_status="Not Required",
    )
    suffix = f"-{occurrence}" if occurrence else ""
    return SyntheticBenchmarkCase(
        case_id=f"{template['case_id']}{suffix}",
        question_type=grading_result.question_type,
        question=template["question"],
        scoring_criteria=template["scoring_criteria"],
        reference_answer=template["reference_answer"],
        student_answer=template["student_answer"],
        score=grading_result.score,
        max_score=grading_result.max_score,
        reason=grading_result.reason,
        knowledge_points=grading_result.knowledge_points,
        validation_status=grading_result.validation_status,
    )


def generate_synthetic_benchmark(
    *,
    count: int = 10,
    seed: int = 20260904,
    dataset_version: str = "m0-seed-v1",
    model_version: str = "synthetic-rules-v1",
    prompt_version: str = "synthetic-prompt-v1",
    scoring_criteria: str = "按参考答案、评分标准和知识点进行确定性评分。",
) -> dict[str, Any]:
    """生成确定性的合成 Benchmark 数据，不包含真实身份或密钥。"""

    if count < 10:
        raise ValueError("Benchmark 至少需要生成 10 条种子数据。")

    templates = _select_seed_templates(count, seed)
    occurrences: dict[str, int] = {}
    cases: list[SyntheticBenchmarkCase] = []
    for template in templates:
        base_case_id = str(template["case_id"])
        occurrence = occurrences.get(base_case_id, 0)
        occurrences[base_case_id] = occurrence + 1
        cases.append(_build_case(template, occurrence=occurrence))

    dataset = SyntheticBenchmarkDataset(
        metadata=BenchmarkMetadata(
            dataset_version=dataset_version,
            model_version=model_version,
            prompt_version=prompt_version,
            scoring_criteria=scoring_criteria,
            seed=seed,
            record_count=count,
        ),
        cases=cases,
    )
    return dataset.model_dump(mode="json")


def write_benchmark_results(
    dataset: Mapping[str, Any],
    *,
    output_dir: Path = DEFAULT_RESULTS_DIR,
) -> tuple[Path, Path]:
    """将 Benchmark 元数据和种子记录写入 JSON 与 CSV 文件。"""

    validated_dataset = SyntheticBenchmarkDataset.model_validate(dataset)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / DEFAULT_JSON_NAME
    csv_path = output_dir / DEFAULT_CSV_NAME

    json_path.write_text(
        json.dumps(
            validated_dataset.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    metadata = validated_dataset.metadata.model_dump(mode="json")
    fieldnames = [
        "case_id",
        "dataset_version",
        "model_version",
        "prompt_version",
        "scoring_criteria",
        "question_type",
        "question",
        "reference_answer",
        "student_answer",
        "score",
        "max_score",
        "reason",
        "knowledge_points",
        "validation_status",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for case in validated_dataset.cases:
            row = case.model_dump(mode="json")
            row.update(
                {
                    "dataset_version": metadata["dataset_version"],
                    "model_version": metadata["model_version"],
                    "prompt_version": metadata["prompt_version"],
                    "scoring_criteria": metadata["scoring_criteria"],
                    "knowledge_points": "|".join(row["knowledge_points"]),
                }
            )
            writer.writerow(row)

    return json_path, csv_path


def build_argument_parser() -> argparse.ArgumentParser:
    """创建中文命令行参数解析器。"""

    parser = argparse.ArgumentParser(
        description="生成 EduAgent 合成 Benchmark 种子数据。"
    )
    parser.add_argument(
        "--count", type=int, default=10, help="生成的种子数量，最少为 10。"
    )
    parser.add_argument("--seed", type=int, default=20260904, help="确定性随机种子。")
    parser.add_argument(
        "--dataset-version",
        default="m0-seed-v1",
        help="数据集版本标识。",
    )
    parser.add_argument(
        "--model-version",
        default="synthetic-rules-v1",
        help="模型或评分器版本标识。",
    )
    parser.add_argument(
        "--prompt-version",
        default="synthetic-prompt-v1",
        help="Prompt 版本标识。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIR,
        help="Benchmark 结果输出目录。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行命令行生成流程并输出中文结果提示。"""

    args = build_argument_parser().parse_args(argv)
    dataset = generate_synthetic_benchmark(
        count=args.count,
        seed=args.seed,
        dataset_version=args.dataset_version,
        model_version=args.model_version,
        prompt_version=args.prompt_version,
    )
    json_path, csv_path = write_benchmark_results(
        dataset,
        output_dir=args.output_dir,
    )
    print(f"已生成 {dataset['metadata']['record_count']} 条合成 Benchmark 种子数据。")
    print(f"JSON 结果：{json_path}")
    print(f"CSV 结果：{csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BenchmarkMetadata",
    "SyntheticBenchmarkCase",
    "SyntheticBenchmarkDataset",
    "build_argument_parser",
    "generate_synthetic_benchmark",
    "main",
    "write_benchmark_results",
]
