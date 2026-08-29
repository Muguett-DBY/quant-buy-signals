from __future__ import annotations

import copy
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import requests

from data.as_of import shanghai_today
from data import patch4_evidence as patch4


CODE = "300059"
ART_CODE = "AN202607231234567890"
AS_OF = shanghai_today().isoformat()
TITLE = "测试公司:2026年限制性股票激励计划(草案)"


class _NoWait:
    def acquire(self) -> None:
        return None


class _Response:
    def __init__(
        self,
        payload: Any = None,
        *,
        raw: bytes | None = None,
        content_type: str = "text/plain; charset=UTF-8",
        status: int = 200,
        retry_after: str | None = None,
    ) -> None:
        self.raw = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode()
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(self.raw)),
        }
        self.status = status
        self.status_code = status
        if retry_after is not None:
            self.headers["Retry-After"] = retry_after
        self.url = ""
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise requests.HTTPError(f"HTTP {self.status}", response=self)

    def iter_content(self, chunk_size: int) -> list[bytes]:
        return [self.raw[index : index + chunk_size] for index in range(0, len(self.raw), chunk_size)]

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, responses: list[_Response], *, override_url: str | None = None) -> None:
        self.responses = list(responses)
        self.override_url = override_url
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def get(self, url: str, *, params: dict[str, Any], **kwargs: Any) -> _Response:
        del kwargs
        self.calls.append((url, dict(params)))
        if not self.responses:
            raise AssertionError("unexpected network request")
        response = self.responses.pop(0)
        response.url = self.override_url or requests.Request("GET", url, params=params).prepare().url
        return response

    def close(self) -> None:
        self.closed = True


def _metadata_payload(
    *,
    code: str = CODE,
    art_code: str = ART_CODE,
    title: str = TITLE,
    notice_date: str = AS_OF,
) -> dict[str, Any]:
    return {
        "success": 1,
        "error": None,
        "data": {
            "total_hits": 1,
            "page_index": 1,
            "page_size": patch4.PAGE_SIZE,
            "list": [
                {
                    "art_code": art_code,
                    "notice_date": f"{notice_date} 00:00:00",
                    "title": title,
                    "codes": [
                        {
                            "stock_code": code,
                            "market_code": "0",
                            "short_name": "测试公司",
                        }
                    ],
                    "columns": [
                        {
                            "column_code": "001003007",
                            "column_name": "股权激励",
                        }
                    ],
                }
            ],
        },
    }


def _content_payload(
    text: str,
    *,
    page_size: int = 2,
    code: str = CODE,
    art_code: str = ART_CODE,
    title: str = TITLE,
    notice_date: str = AS_OF,
    attach_url: str = "https://pdf.dfcfw.com/pdf/H2_AN202607231234567890_1.pdf",
) -> dict[str, Any]:
    return {
        "success": 1,
        "data": {
            "art_code": art_code,
            "notice_date": f"{notice_date} 00:00:00",
            "notice_title": title,
            "security": [{"stock": code, "short_name": "测试公司"}],
            "notice_content": text,
            "page_size": page_size,
            "attach_type": 0,
            "attach_url_web": attach_url,
            "attach_list": [],
        },
    }


def _complete_pages(*, short_term_sentence: str | None = None) -> tuple[str, str]:
    first = (
        "核心技术人员合计持有公司股份占公司总股本的6.2%。"
        "本激励计划覆盖的核心人才人数占公司核心人才总人数的35%。"
        "本激励计划归属业绩考核连续三年包含研发投入增长率指标。"
    )
    second = (
        "本股权激励计划的激励对象包括一线研发人员并授予限制性股票。"
        f"{short_term_sentence or '本激励计划归属业绩考核未设置股价考核指标。'}"
    )
    return first, second


def _session_for_pages(first: str, second: str, **content_overrides: Any) -> _Session:
    return _Session(
        [
            _Response(_metadata_payload()),
            _Response(_content_payload(first, **content_overrides)),
            _Response(_content_payload(second, **content_overrides)),
        ]
    )


def _fetch(session: _Session, tmp_path: Path, *, use_cache: bool = False) -> patch4.Patch4Evidence:
    return patch4.fetch_patch4_evidence(
        CODE,
        AS_OF,
        session=session,
        cache_dir=tmp_path,
        use_cache=use_cache,
        rate_limiter=_NoWait(),
    )


def test_complete_direct_facts_emit_validated_assessment_and_body_hashes(tmp_path: Path) -> None:
    first, second = _complete_pages()
    result = _fetch(_session_for_pages(first, second), tmp_path)

    assert result.available is True
    assert result.status == "complete"
    assert result.reason == ""
    assert result.assessment is not None
    assert set(result.assessment["criteria"]) == {
        "core_rd_ownership_pct",
        "esop_core_talent_coverage_pct",
        "long_term_rd_metrics",
        "frontline_rd_equity",
        "short_term_price_binding",
    }
    assert result.assessment["criteria"]["core_rd_ownership_pct"]["value"] == 6.2
    assert result.assessment["criteria"]["esop_core_talent_coverage_pct"]["value"] == 35.0
    assert result.assessment["criteria"]["long_term_rd_metrics"]["value"] is True
    assert result.assessment["criteria"]["frontline_rd_equity"]["value"] is True
    assert result.assessment["criteria"]["short_term_price_binding"]["value"] is False
    assert all(item["status"] == "known" for item in result.criteria.values())
    assert len(result.documents) == 1
    expected_hash = hashlib.sha256(f"{first}\n\f\n{second}".encode()).hexdigest()
    assert result.documents[0]["content_sha256"] == expected_hash
    assert len(result.documents[0]["page_sha256"]) == 2
    assert "notice_content" not in result.to_dict()


@pytest.mark.parametrize(
    ("ownership_sentence", "coverage_sentence", "criterion"),
    [
        (
            "核心技术人员持股数量占本次激励计划持股总量的6.2%。",
            "本员工持股计划覆盖的核心人才人数占公司核心人才总人数的35%。",
            "core_rd_ownership_pct",
        ),
        (
            "核心技术人员合计持有公司股份占公司总股本的6.2%。",
            "本员工持股计划核心人才覆盖比例为35%。",
            "esop_core_talent_coverage_pct",
        ),
        (
            "核心技术人员合计持有公司股份占公司总股本的6.2%。",
            "本员工持股计划覆盖的核心人才人数占全部激励对象总人数的35%。",
            "esop_core_talent_coverage_pct",
        ),
    ],
)
def test_percentage_facts_require_the_explicit_company_or_core_talent_denominator(
    tmp_path: Path,
    ownership_sentence: str,
    coverage_sentence: str,
    criterion: str,
) -> None:
    first = f"{ownership_sentence}{coverage_sentence}本激励计划归属业绩考核连续三年包含研发投入增长率指标。"
    _, second = _complete_pages()
    result = _fetch(_session_for_pages(first, second), tmp_path)

    assert result.available is False
    assert result.criteria[criterion]["status"] == "unknown"
    assert result.criteria[criterion]["value"] is None


def test_grant_price_and_market_price_do_not_become_short_term_binding(tmp_path: Path) -> None:
    first, second = _complete_pages(short_term_sentence="本计划授予价格为每股10元，不低于公告前一日市场股价的50%。")
    result = _fetch(_session_for_pages(first, second), tmp_path)

    assert result.available is False
    assert result.status == "incomplete"
    assert result.assessment is None
    assert result.criteria["short_term_price_binding"] == {
        "status": "unknown",
        "reason": "no_direct_explicit_statement",
        "value": None,
        "evidence_id": None,
        "documents_checked": 1,
    }
    assert all(
        result.criteria[key]["status"] == "known" for key in result.criteria if key != "short_term_price_binding"
    )


@pytest.mark.parametrize(
    ("replacement", "criterion"),
    [
        ("本计划介绍了研发投入和归属安排。", "long_term_rd_metrics"),
        ("本计划提及一线研发人员的职责。", "frontline_rd_equity"),
    ],
)
def test_absence_or_generic_mentions_never_become_false(
    tmp_path: Path,
    replacement: str,
    criterion: str,
) -> None:
    first, second = _complete_pages()
    if criterion == "long_term_rd_metrics":
        first = f"核心技术人员合计持股比例为6.2%。本员工持股计划核心人才覆盖比例为35%。{replacement}"
    else:
        second = f"{replacement}本激励计划归属业绩考核未设置股价考核指标。"
    result = _fetch(_session_for_pages(first, second), tmp_path)

    assert result.assessment is None
    assert result.criteria[criterion]["status"] == "unknown"
    assert result.criteria[criterion]["value"] is None


def test_frontline_requires_explicit_frontline_role_and_negation_stays_in_its_clause(tmp_path: Path) -> None:
    first, _ = _complete_pages()
    generic = "本股权激励计划的激励对象包括技术骨干并授予限制性股票。本激励计划归属业绩考核未设置股价考核指标。"
    generic_result = _fetch(_session_for_pages(first, generic), tmp_path)
    assert generic_result.criteria["frontline_rd_equity"]["status"] == "unknown"
    assert generic_result.criteria["frontline_rd_equity"]["value"] is None

    scoped = (
        "本股权激励计划不包括独立董事，激励对象包括一线研发人员并授予限制性股票。"
        "本激励计划归属业绩考核未设置股价考核指标。"
    )
    scoped_result = _fetch(_session_for_pages(first, scoped), tmp_path)
    assert scoped_result.criteria["frontline_rd_equity"]["status"] == "known"
    assert scoped_result.criteria["frontline_rd_equity"]["value"] is True


@pytest.mark.parametrize(
    "sentence",
    [
        "本股权激励计划已经结束，目前培训对象包括一线研发人员。",
        "公司未实施股权激励，本次会议参加人员包括一线研发人员。",
    ],
)
def test_frontline_inclusion_must_name_an_equity_plan_recipient(sentence: str) -> None:
    assert ("frontline_rd_equity", True) not in patch4._classify_sentence(sentence)


def test_long_term_rd_metric_requires_the_metric_itself_to_be_bound_to_multi_year_assessment() -> None:
    unrelated_role = "本计划激励对象包括研发项目负责人，考核期为三年，业绩指标为营业收入增长率。"
    unrelated_history = "公司连续三年研发投入持续增长并将股权激励考核指标设为营业收入增长率。"
    direct_metric = "本激励计划归属业绩考核连续三年包含研发投入增长率指标。"

    assert ("long_term_rd_metrics", True) not in patch4._classify_sentence(unrelated_role)
    assert ("long_term_rd_metrics", True) not in patch4._classify_sentence(unrelated_history)
    assert ("long_term_rd_metrics", True) in patch4._classify_sentence(direct_metric)


@pytest.mark.parametrize(
    "sentence",
    [
        "本激励计划连续三年将研发投入纳入年度预算并将营业收入增长率纳入业绩考核。",
        "本激励计划连续三年披露研发投入并将营业收入增长率纳入业绩考核。",
    ],
)
def test_rd_metric_near_an_unrelated_assessment_is_not_treated_as_bound(sentence: str) -> None:
    assert ("long_term_rd_metrics", True) not in patch4._classify_sentence(sentence)


def test_explicitly_excluding_a_short_term_share_price_target_is_a_negative_fact() -> None:
    sentence = "本计划不设置短期股价目标作为考核指标。"

    assert patch4._classify_sentence(sentence) == [("short_term_price_binding", False)]


def test_price_performance_must_be_bound_to_the_incentive_assessment() -> None:
    market_observation = "公司短期股价表现达到历史高位，激励计划考核指标为营业收入增长率。"
    direct_binding = "本激励计划将短期股价目标纳入归属考核条件。"
    explicit_negative = "本激励计划考核指标未与短期股价表现挂钩。"
    compact_negative = "本激励计划不与一年期股价表现挂钩。"
    unrelated_positive = "本激励计划一年期股价目标作为宣传参考并将营业收入增长率纳入业绩考核。"
    unrelated_negative = "本激励计划披露了短期股价表现，公司风险评估不设置股价考核指标。"

    assert ("short_term_price_binding", True) not in patch4._classify_sentence(market_observation)
    assert ("short_term_price_binding", True) not in patch4._classify_sentence(unrelated_positive)
    assert ("short_term_price_binding", False) not in patch4._classify_sentence(unrelated_negative)
    assert ("short_term_price_binding", True) in patch4._classify_sentence(direct_binding)
    assert patch4._classify_sentence(explicit_negative) == [("short_term_price_binding", False)]
    assert patch4._classify_sentence(compact_negative) == [("short_term_price_binding", False)]


@pytest.mark.parametrize(
    "sentence",
    [
        "本股权激励计划仅覆盖高级管理人员，一线研发人员无权益。",
        "本激励计划的激励对象不包括一线研发人员。",
        "一线研发人员未获授任何限制性股票。",
    ],
)
def test_explicit_frontline_rd_exclusion_is_a_negative_fact(sentence: str) -> None:
    assert patch4._classify_sentence(sentence) == [("frontline_rd_equity", False)]


def test_frontline_exclusion_in_unrelated_training_is_not_an_equity_fact() -> None:
    sentence = "本股权激励计划已经实施，公司年度培训不包括一线研发人员。"
    assert ("frontline_rd_equity", False) not in patch4._classify_sentence(sentence)


def test_mixed_group_ownership_is_not_attributed_only_to_core_rd() -> None:
    sentence = "核心技术人员及董事、高级管理人员合计持有公司股份占公司总股本的6.2%。"
    assert ("core_rd_ownership_pct", 6.2) not in patch4._classify_sentence(sentence)


@pytest.mark.parametrize(
    "body",
    [
        "本次终止的股权激励计划激励对象包括一线研发人员。",
        "已失效的员工持股计划参与对象包括一线研发人员。",
        "本股权激励计划的激励对象包括一线研发人员，本计划已经终止实施。",
    ],
)
def test_inactive_latest_plan_cannot_supply_patch4_facts(body: str) -> None:
    announcement = patch4._Announcement(ART_CODE, AS_OF, TITLE)
    document = {
        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "plan_id": "2026:限制性股票:未分期",
        "plan_status": patch4._plan_status(TITLE, body),
    }
    assert patch4._extract_facts(CODE, [announcement], [body], [document]) == []


@pytest.mark.parametrize(
    "body",
    [
        "本计划已取消部分激励对象的资格，其余安排继续实施。",
        "本激励计划已取消两名离职人员的授予资格。",
        "本计划已撤销原激励对象部分限制性股票。",
    ],
)
def test_partial_personnel_or_share_adjustments_do_not_terminate_the_plan(body: str) -> None:
    assert patch4._plan_status(TITLE, body) == "unrevoked"


@pytest.mark.parametrize(
    "body",
    [
        "公司出现下列情形之一时，本激励计划终止实施；公司未发生上述情形。",
        "若公司未满足法定条件时，本计划终止实施；截至公告日相关条件均已满足。",
        "如果发生以下任一情况，则本员工持股计划停止实施；目前未发生以下情况。",
        "若公司决定终止实施本激励计划，则应由股东大会审议并披露。",
        "公司在股东大会审议通过本激励计划之后终止实施本激励计划的，应当由股东大会审议决定。",
        "本激励计划终止实施时，公司应当及时披露。",
        "本激励计划停止实施的，应当由董事会审议并披露。",
        "本激励计划终止实施后，公司须及时披露。",
    ],
)
def test_conditional_termination_boilerplate_does_not_terminate_the_plan(body: str) -> None:
    assert patch4._plan_status(TITLE, body) == "unrevoked"


@pytest.mark.parametrize(
    "body",
    [
        "截至本公告日，本激励计划未终止实施，仍正常推进。",
        "本计划尚未终止。",
        "公司不存在终止实施本激励计划的情形。",
        "公司未决定终止实施本计划。",
        "本计划无需终止实施。",
        "公司不得随意终止实施本激励计划。",
        "任何人无权终止本计划。",
        "目前没有终止实施本计划。",
        "本计划不会终止实施。",
        "公司已于2024年终止实施2023年股权激励计划，本次计划正常实施。",
        "前期员工持股计划已终止，不影响本激励计划。",
    ],
)
def test_negated_or_different_plan_termination_does_not_terminate_current_plan(body: str) -> None:
    assert patch4._plan_status(TITLE, body) == "unrevoked"


@pytest.mark.parametrize(
    "body",
    [
        "公司决定不予终止本激励计划，继续推进。",
        "公司并无终止本激励计划的决定。",
        "关于本激励计划终止的市场传闻不实。",
        "董事会拟终止实施本激励计划，尚待股东大会审议。",
        "公司提出终止本激励计划的议案，尚未审议。",
        "关于终止实施本激励计划的议案未获股东大会通过。",
        "终止实施本激励计划事项尚待股东大会审议决定。",
        "本激励计划存在终止实施的风险，但目前正常实施。",
    ],
)
def test_proposal_rumour_or_unapproved_termination_is_not_final(body: str) -> None:
    assert patch4._plan_status(TITLE, body) == "unrevoked"


@pytest.mark.parametrize(
    "body",
    [
        "董事会决定不终止本激励计划。",
        "公司已撤回关于终止本激励计划的议案，本计划继续实施。",
        "关于终止本激励计划的议案被股东大会否决。",
        "关于终止本激励计划的议案已经取消。",
        "本激励计划的终止程序尚未启动。",
        "公司否认已经终止本激励计划。",
        "媒体关于公司终止本激励计划的报道不准确。",
    ],
)
def test_rejected_withdrawn_or_denied_termination_is_not_final(body: str) -> None:
    assert patch4._plan_status(TITLE, body) == "unrevoked"


def test_negated_termination_in_the_title_does_not_terminate_the_plan() -> None:
    title = "测试公司2026年限制性股票激励计划未终止实施的说明"
    assert patch4._plan_status(title, "本计划仍正常推进。") == "unrevoked"


@pytest.mark.parametrize(
    "body",
    [
        "本激励计划已经终止实施。",
        "本激励计划已终止。",
        "该股权激励计划已经终止。",
        "公司董事会决定本激励计划终止。",
        "董事会审议通过相关议案并决定终止实施本激励计划。",
        "鉴于公司已经出现下列情形之一，董事会决定终止实施本计划。",
    ],
)
def test_decisive_termination_keeps_the_plan_inactive(body: str) -> None:
    assert patch4._plan_status(TITLE, body) == "inactive"


@pytest.mark.parametrize(
    "body",
    [
        "公司已取消本激励计划。",
        "公司已撤销本激励计划。",
        "本激励计划正式废止。",
        "本激励计划宣布结束。",
        "董事会决定自2027年1月1日起终止本激励计划。",
        "公司于2027年1月1日决定终止本计划。",
        "公司于2025年12月31日决定终止本计划。",
    ],
)
def test_actual_action_or_effective_date_terminates_the_current_plan(body: str) -> None:
    assert patch4._plan_status(TITLE, body) == "inactive"


@pytest.mark.parametrize(
    "body",
    [
        "本计划并非未终止。",
        "本计划此前未终止，但董事会今日决定终止本计划。",
        "董事会此前拟终止本计划，但今日已正式决定终止本计划。",
        "公司此前决定不终止本计划，但今日股东大会决定终止本计划。",
        "公司不得不终止实施本激励计划。",
    ],
)
def test_double_negation_or_later_decisive_action_keeps_the_plan_inactive(body: str) -> None:
    assert patch4._plan_status(TITLE, body) == "inactive"


@pytest.mark.parametrize(
    "body",
    [
        "若公司发生重大违法行为，本计划终止实施，但上述情况现已发生，董事会决定终止本计划。",
        "本计划终止实施时公司应当披露，董事会今日已决定终止本计划。",
        "如果公司决定终止本计划，则应披露，公司董事会今日已决定终止本计划。",
    ],
)
def test_real_decision_after_boilerplate_in_the_same_sentence_is_inactive(body: str) -> None:
    assert patch4._plan_status(TITLE, body) == "inactive"


def test_termination_of_a_different_plan_kind_does_not_end_the_current_plan() -> None:
    body = "公司决定终止实施2026年股票期权激励计划，本限制性股票计划继续实施。"
    assert patch4._plan_status(TITLE, body) == "unrevoked"


def test_no_year_termination_notice_can_bind_to_the_selected_current_plan() -> None:
    title = "关于终止实施本次股权激励计划的公告"
    body = "董事会决定终止本次股权激励计划。"
    assert (
        patch4._plan_status(
            title,
            body,
            current_plan_id="2026:限制性股票:未分期",
        )
        == "inactive"
    )


def test_newer_no_year_termination_notice_is_attached_to_the_selected_plan() -> None:
    selected = patch4._Announcement(
        ART_CODE,
        "2026-07-22",
        "测试公司2026年限制性股票激励计划（草案）",
    )
    termination = patch4._Announcement(
        "AN202607231234567891",
        "2026-07-23",
        "关于终止实施本次股权激励计划的公告",
    )
    unrelated_old = patch4._Announcement(
        "AN202507231234567892",
        "2025-07-23",
        "关于终止实施本次股权激励计划的公告",
    )

    assert patch4._related_unidentified_termination_announcements(
        [selected, termination, unrelated_old],
        [selected],
        "2026:限制性股票:未分期",
    ) == [termination]


@pytest.mark.parametrize(
    ("sentence", "expected_key"),
    [
        (
            "本激励计划2026年、2027年及2028年归属业绩考核连续三年包含研发投入增长率指标。",
            "long_term_rd_metrics",
        ),
        (
            "截至2025年12月31日，核心技术人员合计持有公司股份占公司总股本的6.2%。",
            "core_rd_ownership_pct",
        ),
    ],
)
def test_scoring_years_and_reference_dates_are_not_mistaken_for_other_plans(
    sentence: str,
    expected_key: str,
) -> None:
    announcement = patch4._Announcement(ART_CODE, AS_OF, TITLE)
    document = {
        "content_sha256": hashlib.sha256(sentence.encode()).hexdigest(),
        "plan_id": "2026:限制性股票:未分期",
        "plan_status": "unrevoked",
    }

    facts = patch4._extract_facts(CODE, [announcement], [sentence], [document])

    assert [fact.key for fact in facts] == [expected_key]


@pytest.mark.parametrize(
    "sentence",
    [
        "2025年激励计划中核心技术人员合计持有公司股份占公司总股本的6.2%。",
        "2025年计划核心技术人员合计持有公司股份占公司总股本的6.2%。",
    ],
)
def test_generic_other_year_plan_cannot_supply_current_plan_facts(sentence: str) -> None:
    announcement = patch4._Announcement(ART_CODE, AS_OF, TITLE)
    document = {
        "content_sha256": hashlib.sha256(sentence.encode()).hexdigest(),
        "plan_id": "2026:限制性股票:未分期",
        "plan_status": "unrevoked",
    }

    assert patch4._extract_facts(CODE, [announcement], [sentence], [document]) == []


def test_patch4_facts_are_not_synthesized_across_different_plans() -> None:
    old_body = _complete_pages()[0]
    latest_body = _complete_pages()[1]
    old = patch4._Announcement("AN202607221234567890", "2026-07-22", "旧激励计划")
    latest = patch4._Announcement(ART_CODE, AS_OF, TITLE)
    documents = [
        {
            "content_sha256": hashlib.sha256(old_body.encode()).hexdigest(),
            "plan_id": "2025:限制性股票:未分期",
            "plan_status": "unrevoked",
        },
        {
            "content_sha256": hashlib.sha256(latest_body.encode()).hexdigest(),
            "plan_id": "2026:限制性股票:未分期",
            "plan_status": "unrevoked",
        },
    ]

    facts = patch4._extract_facts(CODE, [old, latest], [old_body, latest_body], documents)
    diagnostics, assessment, reason = patch4._build_atomic_result(
        CODE,
        date.fromisoformat(AS_OF),
        facts,
        [
            {
                "art_code": old.art_code,
                "code": CODE,
                "as_of": old.notice_date,
                "title": old.title,
                "url": patch4._detail_url(CODE, old.art_code),
                "page_size": 1,
                "page_sha256": [documents[0]["content_sha256"]],
                "content_sha256": documents[0]["content_sha256"],
                "content_length": len(old_body),
            },
            {
                "art_code": latest.art_code,
                "code": CODE,
                "as_of": latest.notice_date,
                "title": latest.title,
                "url": patch4._detail_url(CODE, latest.art_code),
                "page_size": 1,
                "page_sha256": [documents[1]["content_sha256"]],
                "content_sha256": documents[1]["content_sha256"],
                "content_length": len(latest_body),
            },
        ],
    )

    assert assessment is None
    assert "core_rd_ownership_pct" in reason
    assert diagnostics["core_rd_ownership_pct"]["status"] == "unknown"


def test_patch4_can_combine_current_facts_only_within_one_identified_plan() -> None:
    first, second = _complete_pages()
    older = patch4._Announcement("AN202607221234567890", "2026-07-22", TITLE)
    latest = patch4._Announcement(ART_CODE, AS_OF, TITLE)
    documents = [
        {
            "content_sha256": hashlib.sha256(first.encode()).hexdigest(),
            "plan_id": "2026:限制性股票:未分期",
            "plan_status": "unrevoked",
        },
        {
            "content_sha256": hashlib.sha256(second.encode()).hexdigest(),
            "plan_id": "2026:限制性股票:未分期",
            "plan_status": "unrevoked",
        },
    ]

    facts = patch4._extract_facts(CODE, [older, latest], [first, second], documents)
    diagnostics, assessment, reason = patch4._build_atomic_result(
        CODE,
        date.fromisoformat(AS_OF),
        facts,
        documents,
    )

    assert reason == ""
    assert assessment is not None
    assert all(item["status"] == "known" for item in diagnostics.values())


def test_same_day_different_plan_groups_are_ambiguous_not_ordered_by_art_code() -> None:
    announcements = [
        patch4._Announcement(
            "AN202607231234567891",
            AS_OF,
            "测试公司2026年限制性股票激励计划（草案）",
        ),
        patch4._Announcement(
            "AN202607231234567890",
            AS_OF,
            "测试公司2026年股票期权激励计划终止公告",
        ),
    ]

    assert patch4._select_plan_group(announcements) is None


def test_conflicting_direct_statements_fail_closed(tmp_path: Path) -> None:
    first, second = _complete_pages()
    second += "核心技术人员合计持有公司股份占公司总股本的8.0%。"
    result = _fetch(_session_for_pages(first, second), tmp_path)

    assert result.assessment is None
    assert result.criteria["core_rd_ownership_pct"]["status"] == "unknown"
    assert result.criteria["core_rd_ownership_pct"]["reason"] == "conflicting_direct_statements"


def test_each_body_page_must_keep_art_code_code_date_title_and_page_size(tmp_path: Path) -> None:
    first, second = _complete_pages()
    session = _Session(
        [
            _Response(_metadata_payload()),
            _Response(_content_payload(first)),
            _Response(_content_payload(second, art_code="AN202607231234567891")),
        ]
    )
    result = _fetch(session, tmp_path)

    assert result.available is False
    assert result.status == "source_unavailable"
    assert result.assessment is None
    assert all(item["status"] == "unknown" for item in result.criteria.values())
    assert "identity or page-size binding" in result.reason


def test_joint_announcement_security_bindings_are_rejected() -> None:
    metadata_codes = [
        {"stock_code": CODE, "market_code": "0", "short_name": "测试公司"},
        {"stock_code": "600519", "market_code": "1", "short_name": "另一公司"},
    ]
    body_codes = [
        {"stock": CODE, "short_name": "测试公司"},
        {"stock": "600519", "short_name": "另一公司"},
    ]

    with pytest.raises(patch4.Patch4EvidenceError, match="security-code bindings"):
        patch4._validate_code_rows(metadata_codes, CODE)
    with pytest.raises(patch4.Patch4EvidenceError, match="body security bindings"):
        patch4._validate_content_security(body_codes, CODE)


def test_http_redirect_and_http_attachment_are_rejected(tmp_path: Path) -> None:
    first, second = _complete_pages()
    redirected = _session_for_pages(first, second)
    redirected.override_url = "http://np-anotice-stock.eastmoney.com/api/security/ann"
    redirect_result = _fetch(redirected, tmp_path)
    assert redirect_result.status == "source_unavailable"
    assert "pinned HTTPS endpoint" in redirect_result.reason

    attachment = _session_for_pages(first, second, attach_url="http://pdf.dfcfw.com/a.pdf")
    attachment_result = _fetch(attachment, tmp_path)
    assert attachment_result.status == "source_unavailable"
    assert "attachment URL" in attachment_result.reason


def test_strict_json_rejects_duplicate_keys(tmp_path: Path) -> None:
    raw = (
        b'{"success":1,"success":1,"data":{"total_hits":0,"page_index":1,'
        + f'"page_size":{patch4.PAGE_SIZE},"list":[]}}}}'.encode()
    )
    result = _fetch(_Session([_Response(raw=raw)]), tmp_path)

    assert result.status == "source_unavailable"
    assert "duplicate key" in result.reason
    assert result.assessment is None


def test_declared_byte_and_body_page_limits_fail_closed(tmp_path: Path) -> None:
    too_large = _Response(_metadata_payload())
    too_large.headers["Content-Length"] = str(patch4.MAX_RESPONSE_BYTES + 1)
    byte_result = _fetch(_Session([too_large]), tmp_path)
    assert byte_result.status == "source_unavailable"
    assert "byte limit" in byte_result.reason

    first, _ = _complete_pages()
    page_result = _fetch(
        _Session(
            [
                _Response(_metadata_payload()),
                _Response(_content_payload(first, page_size=patch4.MAX_BODY_PAGES_PER_DOCUMENT + 1)),
            ]
        ),
        tmp_path,
    )
    assert page_result.status == "source_unavailable"
    assert "page-size binding" in page_result.reason


def test_safe_file_cache_replays_without_network(tmp_path: Path) -> None:
    first, second = _complete_pages()
    initial = _fetch(_session_for_pages(first, second), tmp_path, use_cache=True)
    assert initial.available is True
    assert initial.cache_hit is False
    assert initial.cache_diagnostic.endswith(";saved")

    replay_session = _Session([])
    replay = _fetch(replay_session, tmp_path, use_cache=True)
    assert replay.available is True
    assert replay.cache_hit is True
    assert replay.cache_diagnostic == "hit"
    assert replay.assessment == initial.assessment
    assert replay.documents == initial.documents
    assert replay_session.calls == []


def test_transient_source_failure_is_not_cached_and_the_next_call_can_recover(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(patch4.time, "sleep", lambda _seconds: None)
    failed = _fetch(
        _Session([_Response(status=503), _Response(status=503)]),
        tmp_path,
        use_cache=True,
    )
    assert failed.status == "source_unavailable"
    assert failed.cache_hit is False
    assert list(tmp_path.glob("*.json.gz")) == []

    first, second = _complete_pages()
    recovery_session = _session_for_pages(first, second)
    recovered = _fetch(recovery_session, tmp_path, use_cache=True)
    assert recovered.available is True
    assert recovered.cache_hit is False
    assert recovery_session.calls

    replay_session = _Session([])
    replay = _fetch(replay_session, tmp_path, use_cache=True)
    assert replay.available is True
    assert replay.cache_hit is True
    assert replay_session.calls == []


def test_request_retry_honours_retry_after_and_terminal_http_stops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(patch4.time, "sleep", waits.append)
    first, second = _complete_pages()
    retry_session = _Session(
        [
            _Response(status=429, retry_after="7"),
            _Response(_metadata_payload()),
            _Response(_content_payload(first)),
            _Response(_content_payload(second)),
        ]
    )

    recovered = _fetch(retry_session, tmp_path)

    assert recovered.available is True
    assert waits == [7.0]
    assert len(retry_session.calls) == 4

    waits.clear()
    terminal_session = _Session([_Response(status=404), _Response(_metadata_payload())])
    failed = _fetch(terminal_session, tmp_path)

    assert failed.status == "source_unavailable"
    assert "HTTP 404" in failed.reason
    assert waits == []
    assert len(terminal_session.calls) == 1


def test_public_record_validator_replays_bindings_and_rejects_tampering(tmp_path: Path) -> None:
    first, second = _complete_pages()
    record = _fetch(_session_for_pages(first, second), tmp_path).to_dict()

    assert patch4.validate_patch4_evidence_record(record, CODE, AS_OF) == record

    tampered_status = copy.deepcopy(record)
    tampered_status["status"] = "incomplete"
    with pytest.raises(patch4.Patch4EvidenceError, match="contradicts"):
        patch4.validate_patch4_evidence_record(tampered_status, CODE, AS_OF)

    tampered_identity = copy.deepcopy(record)
    tampered_identity["model_id"] = "forged-patch4-model"
    with pytest.raises(patch4.Patch4EvidenceError, match="identity"):
        patch4.validate_patch4_evidence_record(tampered_identity, CODE, AS_OF)

    tampered_hash = copy.deepcopy(record)
    tampered_hash["documents"][0]["content_sha256"] = "0" * 64
    with pytest.raises(patch4.Patch4EvidenceError, match="hash|binding"):
        patch4.validate_patch4_evidence_record(tampered_hash, CODE, AS_OF)

    tampered_diagnostic = copy.deepcopy(record)
    tampered_diagnostic["criteria"]["core_rd_ownership_pct"]["evidence_id"] = (
        f"eastmoney-notice:{CODE}:{ART_CODE}:sha256:{'0' * 16}"
    )
    with pytest.raises(patch4.Patch4EvidenceError, match="binding"):
        patch4.validate_patch4_evidence_record(tampered_diagnostic, CODE, AS_OF)

    tampered_assessment = copy.deepcopy(record)
    tampered_assessment["assessment"]["criteria"]["core_rd_ownership_pct"]["value"] = 99.0
    with pytest.raises(patch4.Patch4EvidenceError, match="bound|binding"):
        patch4.validate_patch4_evidence_record(tampered_assessment, CODE, AS_OF)

    tampered_summary = copy.deepcopy(record)
    summary = tampered_summary["assessment"]["criteria"]["core_rd_ownership_pct"]["evidence"]["summary"]
    tampered_summary["assessment"]["criteria"]["core_rd_ownership_pct"]["evidence"]["summary"] = summary.replace(
        "正文SHA-256前16位：",
        "无来源摘要：",
    )
    with pytest.raises(patch4.Patch4EvidenceError, match="hash|binding"):
        patch4.validate_patch4_evidence_record(tampered_summary, CODE, AS_OF)


def test_metadata_security_binding_and_duplicate_page_content_fail_closed(tmp_path: Path) -> None:
    first, _ = _complete_pages()
    code_result = _fetch(_Session([_Response(_metadata_payload(code="600519"))]), tmp_path)
    assert code_result.status == "source_unavailable"
    assert "requested security" in code_result.reason

    duplicate_result = _fetch(
        _Session(
            [
                _Response(_metadata_payload()),
                _Response(_content_payload(first)),
                _Response(_content_payload(first)),
            ]
        ),
        tmp_path,
    )
    assert duplicate_result.status == "source_unavailable"
    assert "duplicate content" in duplicate_result.reason


def test_batch_is_bounded_deduplicated_and_injects_one_session_per_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created: list[_Session] = []
    seen_sessions: list[_Session] = []

    def factory() -> _Session:
        session = _Session([])
        created.append(session)
        return session

    def fake_fetch(code: str, as_of: str, **kwargs: Any) -> patch4.Patch4Evidence:
        session = kwargs["session"]
        seen_sessions.append(session)
        reason = "incomplete_atomic_facts"
        return patch4._make_evidence(
            code,
            date.fromisoformat(as_of),
            assessment=None,
            diagnostics=patch4._unknown_diagnostics(reason),
            documents=[],
            cache_hit=False,
            cache_diagnostic="disabled",
            reason=reason,
        )

    monkeypatch.setattr(patch4, "fetch_patch4_evidence", fake_fetch)
    results = patch4.fetch_patch4_evidence_batch(
        [{"code": "600519", "as_of": AS_OF}, {"code": CODE, "as_of": AS_OF}],
        max_workers=2,
        session_factory=factory,
        cache_dir=tmp_path,
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert list(results) == [CODE, "600519"]
    assert len(created) == 2
    assert set(map(id, created)) == set(map(id, seen_sessions))
    assert all(session.closed for session in created)
    with pytest.raises(ValueError, match="duplicate"):
        patch4.fetch_patch4_evidence_batch(
            [{"code": CODE, "as_of": AS_OF}, {"code": CODE, "as_of": AS_OF}],
            session_factory=factory,
        )
    with pytest.raises(ValueError, match="company limit"):
        patch4.fetch_patch4_evidence_batch(
            [{"code": f"0{index:05d}", "as_of": AS_OF} for index in range(patch4.MAX_BATCH_COMPANIES + 1)],
            session_factory=factory,
        )


def test_batch_opens_a_circuit_after_repeated_source_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def fake_fetch(code: str, as_of: str, **kwargs: Any) -> patch4.Patch4Evidence:
        del kwargs
        calls.append(code)
        reason = "source_unavailable:ReadTimeout:upstream timed out"
        return patch4._make_evidence(
            code,
            date.fromisoformat(as_of),
            assessment=None,
            diagnostics=patch4._unknown_diagnostics(reason),
            documents=[],
            cache_hit=False,
            cache_diagnostic="disabled",
            reason=reason,
        )

    monkeypatch.setattr(patch4, "fetch_patch4_evidence", fake_fetch)
    requests_ = [{"code": f"00000{index}", "as_of": AS_OF} for index in range(1, 7)]
    results = patch4.fetch_patch4_evidence_batch(
        requests_,
        max_workers=2,
        session_factory=lambda: _Session([]),
        cache_dir=tmp_path,
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert len(calls) <= patch4.BATCH_SOURCE_FAILURE_LIMIT + patch4.MAX_WORKERS
    assert len(results) == len(requests_)
    assert any("batch_circuit_open" in item["reason"] for item in results.values())


@pytest.mark.parametrize(
    "reason",
    [
        "source_unavailable:Patch4EvidenceError:announcement request failed: HTTPError:403 Client Error",
        "source_unavailable:Patch4EvidenceError:announcement response is not JSON-compatible content",
        ("source_unavailable:Patch4EvidenceError:announcement response redirected outside the pinned HTTPS endpoint"),
    ],
)
def test_batch_circuit_covers_global_block_schema_and_redirect_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reason: str,
) -> None:
    calls: list[str] = []

    def fake_fetch(code: str, as_of: str, **kwargs: Any) -> patch4.Patch4Evidence:
        del kwargs
        calls.append(code)
        return patch4._make_evidence(
            code,
            date.fromisoformat(as_of),
            assessment=None,
            diagnostics=patch4._unknown_diagnostics(reason),
            documents=[],
            cache_hit=False,
            cache_diagnostic="disabled",
            reason=reason,
        )

    monkeypatch.setattr(patch4, "fetch_patch4_evidence", fake_fetch)
    results = patch4.fetch_patch4_evidence_batch(
        [{"code": f"00000{index}", "as_of": AS_OF} for index in range(1, 7)],
        max_workers=2,
        session_factory=lambda: _Session([]),
        cache_dir=tmp_path,
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert len(calls) == 2
    assert len(results) == 6
    assert sum("batch_circuit_open" in item["reason"] for item in results.values()) == 4


def test_batch_does_not_trip_for_company_specific_validation_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def fake_fetch(code: str, as_of: str, **kwargs: Any) -> patch4.Patch4Evidence:
        del kwargs
        calls.append(code)
        if len(calls) <= 2:
            reason = "source_unavailable:Patch4EvidenceError:announcement security-code bindings are invalid"
        else:
            reason = "incomplete_atomic_facts"
        return patch4._make_evidence(
            code,
            date.fromisoformat(as_of),
            assessment=None,
            diagnostics=patch4._unknown_diagnostics(reason),
            documents=[],
            cache_hit=False,
            cache_diagnostic="disabled",
            reason=reason,
        )

    monkeypatch.setattr(patch4, "fetch_patch4_evidence", fake_fetch)
    requests_ = [{"code": f"00000{index}", "as_of": AS_OF} for index in range(1, 4)]
    results = patch4.fetch_patch4_evidence_batch(
        requests_,
        max_workers=2,
        session_factory=lambda: _Session([]),
        cache_dir=tmp_path,
        use_cache=False,
        rate_limiter=_NoWait(),
    )

    assert sorted(calls) == ["000001", "000002", "000003"]
    assert all("batch_circuit_open" not in item["reason"] for item in results.values())
