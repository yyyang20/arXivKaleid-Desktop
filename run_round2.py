from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import build_round2_inputs
import main
import model_usage
import round2_fulltext_state


@dataclass(frozen=True)
class Round2InputBundle:
    run_selection: build_round2_inputs.Round2RunSelection
    papers: list[dict[str, Any]]
    messages: list[dict[str, str]]
    request_summary: dict[str, Any]
    input_mode: str = "full_text"
    estimated_request_tokens: int = 0
    max_request_tokens: int = round2_fulltext_state.DEFAULT_MAX_REQUEST_TOKENS
    full_text_estimated_tokens: int | None = None
    long_reading_papers: tuple[dict[str, Any], ...] = ()
    fulltext_extraction_failed_papers: tuple[dict[str, Any], ...] = ()
    token_budget_excluded_papers: tuple[dict[str, Any], ...] = ()
    input_failure_reasons: tuple[str, ...] = ()
    uses_fulltext_state: bool = False

    @property
    def all_inputs_usable(self) -> bool:
        if self.uses_fulltext_state:
            if self.input_mode == "no_candidates":
                return not self.papers and not self.input_failure_reasons
            return (
                bool(self.papers)
                and not self.input_failure_reasons
                and 0 < self.estimated_request_tokens <= self.max_request_tokens
            )
        return False


def build_round2_result_tool(config: dict[str, Any]) -> dict[str, Any]:
    """构造唯一的 Round 2 命名结果出口；语义仍由现有提示词和校验器决定。"""
    return {
        "type": "function",
        "name": "submit_round2_results",
        "description": "提交本批次经过全文比较后的最终推荐 JSON。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_type": {
                    "type": "string",
                    "enum": [main.ROUND2_TASK_TYPE],
                },
                "selection_policy": {
                    "type": "string",
                    "enum": [main.ROUND2_SELECTION_POLICY],
                },
                "profile_version": {
                    "type": "string",
                    "enum": [config["versions"]["research_profile_version"]],
                },
                "prompt_version": {
                    "type": "string",
                    "enum": [config["versions"]["round2_prompt_version"]],
                },
                "final_recommendations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "arxiv_id": {"type": "string"},
                            "version": {"type": "string"},
                            "content_label": {
                                "type": "string",
                                "enum": ["成像", "新解", "成像｜新解", "其他"],
                            },
                            "reason": {"type": "string"},
                        },
                        "required": [
                            "arxiv_id",
                            "version",
                            "content_label",
                            "reason",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "task_type",
                "selection_policy",
                "profile_version",
                "prompt_version",
                "final_recommendations",
            ],
            "additionalProperties": False,
        },
    }


def request_round2_json(
    client: Any,
    bundle: Round2InputBundle,
    config: dict[str, Any],
) -> Any:
    """通过一次命名工具调用取得 Round 2 JSON，不提供自动重试。"""
    stage = main.deepseek_stage_config(config, "round2")
    if stage.get("output_transport") != main.ROUND2_OUTPUT_TRANSPORT:
        raise RuntimeError("round2_output_transport_invalid")
    return client.request_responses_json(
        bundle.messages,
        max_tokens=int(stage["max_output_tokens"]),
        forced_tool=build_round2_result_tool(config),
    )


def read_round2_context(
    project_root: Path,
) -> tuple[dict[str, Any], dict[str, Path], dict[str, Any], str]:
    config_path = project_root / "config.json"
    config = main.load_config(config_path)
    paths = main.resolve_configured_paths(project_root, config)
    profile = main.research_profile_identity(config)
    try:
        prompt = paths["round2_prompt"].read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise RuntimeError("cannot_read_round2_prompt") from exc
    if not prompt.strip():
        raise RuntimeError("empty_round2_prompt")
    return config, paths, profile, prompt


def parse_details_json(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_round2_candidate_papers(
    connection: sqlite3.Connection,
    config: dict[str, Any],
    *,
    run_id: int,
    prompt_version: str,
    profile_version: str,
    policy_version: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"""
        SELECT s.arxiv_id, s.version, s.result_rank, s.score,
               s.confidence, s.reason, s.details_json,
               p.title, p.authors, p.categories, p.primary_category, p.summary
        FROM screening_results AS s
        JOIN papers AS p
          ON p.arxiv_id = s.arxiv_id AND p.version = s.version
        WHERE {build_round2_inputs.round1_selected_exists_sql()}
          AND s.run_id = ?
          AND s.task_type = ?
          AND s.is_selected = 1
          AND s.prompt_version = ?
          AND s.research_profile_version = ?
        ORDER BY s.result_rank ASC, s.score DESC, s.arxiv_id
        """,
        [
            policy_version,
            run_id,
            build_round2_inputs.ROUND1_TASK_TYPE,
            prompt_version,
            profile_version,
        ],
    ).fetchall()

    papers: list[dict[str, Any]] = []
    for row in rows:
        details = parse_details_json(row["details_json"])
        papers.append(
            {
                "arxiv_id": str(row["arxiv_id"]),
                "version": int(row["version"]),
                "title": str(row["title"] or ""),
                "authors": str(row["authors"] or ""),
                "categories": str(row["categories"] or ""),
                "primary_category": str(row["primary_category"] or ""),
                "summary": str(row["summary"] or ""),
                "round1_rank": int(row["result_rank"] or 0),
                "score": int(row["score"]) if row["score"] is not None else None,
                "confidence": (
                    int(row["confidence"])
                    if row["confidence"] is not None
                    else None
                ),
                "round1_reason": str(row["reason"] or ""),
                "matched_reasons": details.get("matched_reasons", []),
                "negative_reasons": details.get("negative_reasons", []),
                "evidence_from_title_or_abstract": details.get(
                    "evidence_from_title_or_abstract", []
                ),
                **(
                    {"content_label": details["content_label"]}
                    if details.get("content_label")
                    in main.content_labels.CORE_CONTENT_LABEL_SET
                    else {}
                ),
            }
        )
    return papers


def exclude_confirmed_withdrawn_papers(
    connection: sqlite3.Connection,
    papers: list[dict[str, Any]],
    *,
    prompt_version: str,
) -> list[dict[str, Any]]:
    """v18 起撤稿只从全文候选中排除，不提升模型 Top K 外论文。"""
    if not main.content_labels.round1_prompt_uses_core_content_label(prompt_version):
        return papers
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pdf_downloads'"
    ).fetchone()
    if table is None:
        return papers
    withdrawn = {
        (str(row[0]), int(row[1]))
        for row in connection.execute(
            """
            SELECT arxiv_id, version
            FROM pdf_downloads
            WHERE failure_reason = 'withdrawn_no_pdf'
            """
        ).fetchall()
    }
    return [
        paper
        for paper in papers
        if (str(paper["arxiv_id"]), int(paper["version"])) not in withdrawn
    ]


def _positive_limit(config: dict[str, Any], field: str, default: int) -> int:
    limits = config.get("limits")
    value = limits.get(field, default) if isinstance(limits, dict) else default
    if not isinstance(value, int) or value <= 0:
        raise RuntimeError(f"invalid_round2_limit:{field}")
    return value


def _metadata_missing_fields(paper: dict[str, Any]) -> tuple[str, ...]:
    required = {
        "title": paper.get("title"),
        "authors": paper.get("authors"),
        "categories": paper.get("categories"),
        "primary_category": paper.get("primary_category"),
        "metadata_abstract": paper.get("summary"),
        "round1_reason": paper.get("round1_reason"),
    }
    return tuple(name for name, value in required.items() if not str(value or "").strip())


def _candidate_token_estimates(
    papers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "arxiv_id": str(paper["arxiv_id"]),
            "version": int(paper["version"]),
            "round1_rank": int(paper.get("round1_rank") or 0),
            "page_count": int(paper.get("pdf_page_count") or 0),
            "estimated_tokens": round2_fulltext_state.conservative_value_token_estimate(
                main.build_round2_candidate_payload(paper)
            ),
        }
        for paper in papers
    ]


def _input_exclusion(
    paper: dict[str, Any],
    *,
    reason: str,
    page_count: int,
    estimated_tokens: int | None = None,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    """保存可审计的单篇排除记录，不改变 Round 1 Top 10 身份。"""
    return {
        "arxiv_id": str(paper["arxiv_id"]),
        "version": int(paper["version"]),
        "title": str(paper.get("title") or ""),
        "round1_rank": int(paper.get("round1_rank") or 0),
        "score": paper.get("score"),
        "page_count": int(page_count),
        "exclusion_reason": reason,
        "estimated_tokens": estimated_tokens,
        "failure_reason": failure_reason,
    }


def _build_no_candidate_bundle(
    *,
    run_selection: build_round2_inputs.Round2RunSelection,
    selected_count: int,
    max_request_tokens: int,
    long_reading: list[dict[str, Any]],
    extraction_failed: list[dict[str, Any]],
    token_excluded: list[dict[str, Any]],
    candidate_token_estimates: list[dict[str, Any]] | None = None,
    full_text_estimated_tokens: int | None = None,
) -> Round2InputBundle:
    estimates = candidate_token_estimates or []
    return Round2InputBundle(
        run_selection=run_selection,
        papers=[],
        messages=[],
        request_summary={
            "input_mode": "no_candidates",
            "round1_selected_count": selected_count,
            "long_reading_count": len(long_reading),
            "fulltext_extraction_failed_count": len(extraction_failed),
            "token_budget_excluded_count": len(token_excluded),
            "estimated_request_tokens": 0,
            "max_request_tokens": max_request_tokens,
            "full_text_estimated_tokens": full_text_estimated_tokens,
            "candidate_token_estimates": estimates,
            "token_budget_excluded_papers": token_excluded,
        },
        input_mode="no_candidates",
        estimated_request_tokens=0,
        max_request_tokens=max_request_tokens,
        long_reading_papers=tuple(long_reading),
        fulltext_extraction_failed_papers=tuple(extraction_failed),
        token_budget_excluded_papers=tuple(token_excluded),
        full_text_estimated_tokens=full_text_estimated_tokens,
        uses_fulltext_state=True,
    )


def _build_fulltext_aware_bundle(
    *,
    run_selection: build_round2_inputs.Round2RunSelection,
    selected_papers: list[dict[str, Any]],
    fulltext_connection: sqlite3.Connection,
    config: dict[str, Any],
    profile: dict[str, Any],
    prompt: str,
) -> Round2InputBundle:
    round2_fulltext_state.validate_fulltext_schema(fulltext_connection)
    max_pdf_pages = _positive_limit(
        config,
        "max_round2_pdf_pages",
        round2_fulltext_state.DEFAULT_MAX_PDF_PAGES,
    )
    max_request_tokens = _positive_limit(
        config,
        "max_round2_request_tokens",
        round2_fulltext_state.DEFAULT_MAX_REQUEST_TOKENS,
    )

    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    long_reading: list[dict[str, Any]] = []
    fulltext_extraction_failed: list[dict[str, Any]] = []
    for paper in selected_papers:
        arxiv_id = str(paper["arxiv_id"])
        version = int(paper["version"])
        document = round2_fulltext_state.load_document(
            fulltext_connection, arxiv_id, version
        )
        identity = f"{arxiv_id}v{version}"
        if document is None:
            fulltext_extraction_failed.append(
                _input_exclusion(
                    paper,
                    reason="fulltext_extraction_failed",
                    page_count=0,
                    failure_reason="missing_fulltext_document_state",
                )
            )
            continue
        page_count = int(document["page_count"] or 0)
        gate_status = str(document["page_gate_status"] or "")
        if gate_status == round2_fulltext_state.PAGE_GATE_LONG_READING:
            if page_count <= max_pdf_pages:
                fulltext_extraction_failed.append(
                    _input_exclusion(
                        paper,
                        reason="fulltext_extraction_failed",
                        page_count=page_count,
                        failure_reason="invalid_long_reading_page_count",
                    )
                )
                continue
            long_reading.append(
                _input_exclusion(
                    paper,
                    reason="long_reading",
                    page_count=page_count,
                )
            )
            continue
        if gate_status != round2_fulltext_state.PAGE_GATE_ELIGIBLE:
            fulltext_extraction_failed.append(
                _input_exclusion(
                    paper,
                    reason="fulltext_extraction_failed",
                    page_count=page_count,
                    failure_reason=str(
                        document.get("failure_reason") or "unresolved_page_gate"
                    ),
                )
            )
            continue
        if page_count <= 0 or page_count > max_pdf_pages:
            fulltext_extraction_failed.append(
                _input_exclusion(
                    paper,
                    reason="fulltext_extraction_failed",
                    page_count=page_count,
                    failure_reason="invalid_eligible_page_count",
                )
            )
            continue
        missing_metadata = _metadata_missing_fields(paper)
        if missing_metadata:
            fulltext_extraction_failed.append(
                _input_exclusion(
                    paper,
                    reason="fulltext_extraction_failed",
                    page_count=page_count,
                    failure_reason=f"missing_{','.join(missing_metadata)}",
                )
            )
            continue
        eligible.append((paper, document))

    if not eligible:
        return _build_no_candidate_bundle(
            run_selection=run_selection,
            selected_count=len(selected_papers),
            max_request_tokens=max_request_tokens,
            long_reading=long_reading,
            extraction_failed=fulltext_extraction_failed,
            token_excluded=[],
        )

    fulltext_papers: list[dict[str, Any]] = []
    for paper, document in eligible:
        arxiv_id = str(paper["arxiv_id"])
        version = int(paper["version"])
        identity = f"{arxiv_id}v{version}"
        pages = round2_fulltext_state.load_document_pages(
            fulltext_connection, arxiv_id, version
        )
        page_count = int(document["page_count"])
        valid_page_numbers = [int(page["page_number"]) for page in pages] == list(
            range(1, page_count + 1)
        )
        if (
            document["extraction_status"]
            != round2_fulltext_state.EXTRACTION_EXTRACTED
            or int(document["extracted_page_count"] or 0) != page_count
            or len(pages) != page_count
            or not valid_page_numbers
            or not any(str(page["page_text"] or "").strip() for page in pages)
        ):
            fulltext_extraction_failed.append(
                _input_exclusion(
                    paper,
                    reason="fulltext_extraction_failed",
                    page_count=page_count,
                    failure_reason=str(
                        document.get("failure_reason") or "fulltext_unusable"
                    ),
                )
            )
            continue
        candidate = dict(paper)
        candidate.update(
            {
                "round2_input_mode": "full_text",
                "pdf_extraction_status": "extracted",
                "pdf_page_count": page_count,
                "pdf_pages": [dict(page) for page in pages],
            }
        )
        fulltext_papers.append(candidate)

    if not fulltext_papers:
        return _build_no_candidate_bundle(
            run_selection=run_selection,
            selected_count=len(selected_papers),
            max_request_tokens=max_request_tokens,
            long_reading=long_reading,
            extraction_failed=fulltext_extraction_failed,
            token_excluded=[],
        )

    initial_candidate_estimates = _candidate_token_estimates(fulltext_papers)
    estimate_by_key = {
        (str(item["arxiv_id"]), int(item["version"])): int(
            item["estimated_tokens"]
        )
        for item in initial_candidate_estimates
    }
    remaining = list(fulltext_papers)
    token_excluded: list[dict[str, Any]] = []
    messages = main.build_round2_messages(prompt, profile, remaining, config)
    initial_full_text_tokens = (
        round2_fulltext_state.conservative_request_token_estimate(messages)
    )
    estimated_tokens = initial_full_text_tokens

    # 超限时优先排除单篇载荷最大的论文；并列时优先排除
    # Round 1 排名更低者，再按 arXiv 身份固定顺序。每次都重新序列化整批请求。
    while remaining and estimated_tokens > max_request_tokens:
        excluded = sorted(
            remaining,
            key=lambda paper: (
                -estimate_by_key[(str(paper["arxiv_id"]), int(paper["version"]))],
                -int(paper.get("round1_rank") or 0),
                str(paper["arxiv_id"]),
                int(paper["version"]),
            ),
        )[0]
        excluded_key = (str(excluded["arxiv_id"]), int(excluded["version"]))
        token_excluded.append(
            _input_exclusion(
                excluded,
                reason="token_budget_excluded",
                page_count=int(excluded.get("pdf_page_count") or 0),
                estimated_tokens=estimate_by_key[excluded_key],
            )
        )
        remaining = [paper for paper in remaining if paper is not excluded]
        if remaining:
            messages = main.build_round2_messages(prompt, profile, remaining, config)
            estimated_tokens = (
                round2_fulltext_state.conservative_request_token_estimate(messages)
            )
        else:
            messages = []
            estimated_tokens = 0

    if not remaining:
        return _build_no_candidate_bundle(
            run_selection=run_selection,
            selected_count=len(selected_papers),
            max_request_tokens=max_request_tokens,
            long_reading=long_reading,
            extraction_failed=fulltext_extraction_failed,
            token_excluded=token_excluded,
            candidate_token_estimates=initial_candidate_estimates,
            full_text_estimated_tokens=initial_full_text_tokens,
        )

    summary = main.summarize_round2_request_messages(messages)
    summary.update(
        {
            "token_estimator_version": round2_fulltext_state.TOKEN_ESTIMATOR_VERSION,
            "estimated_request_tokens": estimated_tokens,
            "max_request_tokens": max_request_tokens,
            "full_text_estimated_tokens": initial_full_text_tokens,
            "round1_selected_count": len(selected_papers),
            "long_reading_count": len(long_reading),
            "fulltext_extraction_failed_count": len(fulltext_extraction_failed),
            "token_budget_excluded_count": len(token_excluded),
            "candidate_token_estimates": initial_candidate_estimates,
            "token_budget_excluded_papers": token_excluded,
        }
    )
    return Round2InputBundle(
        run_selection=run_selection,
        papers=remaining,
        messages=messages,
        request_summary=summary,
        input_mode="full_text",
        estimated_request_tokens=estimated_tokens,
        max_request_tokens=max_request_tokens,
        full_text_estimated_tokens=initial_full_text_tokens,
        long_reading_papers=tuple(long_reading),
        fulltext_extraction_failed_papers=tuple(fulltext_extraction_failed),
        token_budget_excluded_papers=tuple(token_excluded),
        uses_fulltext_state=True,
    )


def persist_round2_input_decisions(
    fulltext_connection: sqlite3.Connection, bundle: Round2InputBundle
) -> None:
    """将全文候选与三类单篇排除决定写入当前批次状态库。"""
    estimates = {
        (str(item["arxiv_id"]), int(item["version"])): int(
            item["estimated_tokens"]
        )
        for item in bundle.request_summary.get("candidate_token_estimates", [])
    }
    decisions: list[dict[str, Any]] = []
    for paper in bundle.papers:
        key = (str(paper["arxiv_id"]), int(paper["version"]))
        decisions.append(
            {
                "arxiv_id": key[0],
                "version": key[1],
                "decision": "full_text",
                "page_count": int(paper.get("pdf_page_count") or 0),
                "estimated_tokens": estimates.get(key),
            }
        )
    for items in (
        bundle.long_reading_papers,
        bundle.fulltext_extraction_failed_papers,
        bundle.token_budget_excluded_papers,
    ):
        for item in items:
            decisions.append(
                {
                    "arxiv_id": item["arxiv_id"],
                    "version": item["version"],
                    "decision": item["exclusion_reason"],
                    "page_count": item["page_count"],
                    "estimated_tokens": item.get("estimated_tokens"),
                    "failure_reason": item.get("failure_reason"),
                }
            )
    round2_fulltext_state.replace_input_decisions(fulltext_connection, decisions)


def build_round2_input_bundle(
    connection: sqlite3.Connection,
    config: dict[str, Any],
    profile: dict[str, Any],
    prompt: str,
    *,
    run_id: int,
    fulltext_connection: sqlite3.Connection,
    round1_identity: tuple[str, str, str] | None = None,
) -> Round2InputBundle:
    prompt_version, profile_version, policy_version = (
        round1_identity or build_round2_inputs.get_round1_identity(config)
    )
    build_round2_inputs.validate_required_schema(connection)
    run_selection = build_round2_inputs.select_round1_run(
        connection,
        prompt_version=prompt_version,
        profile_version=profile_version,
        policy_version=policy_version,
        run_id=run_id,
    )
    papers = load_round2_candidate_papers(
        connection,
        config,
        run_id=run_selection.run_id,
        prompt_version=prompt_version,
        profile_version=profile_version,
        policy_version=policy_version,
    )
    papers = exclude_confirmed_withdrawn_papers(
        connection,
        papers,
        prompt_version=prompt_version,
    )
    return _build_fulltext_aware_bundle(
        run_selection=run_selection,
        selected_papers=papers,
        fulltext_connection=fulltext_connection,
        config=config,
        profile=profile,
        prompt=prompt,
    )


def unusable_input_reasons(bundle: Round2InputBundle) -> list[str]:
    return list(bundle.input_failure_reasons) or ["fulltext_input_not_usable"]


def create_deepseek_client(
    *,
    project_root: Path,
    config: dict[str, Any],
    client_factory: Callable[..., Any] | None,
    api_key_override: str | None = None,
) -> Any:
    round2_deepseek = main.deepseek_stage_config(config, "round2")
    # Key 只由 Desktop 当前会话注入；不读取任何明文凭据文件。
    if not isinstance(api_key_override, str) or not api_key_override.strip():
        raise RuntimeError("deepseek_api_key_override_empty")
    api_key = api_key_override
    factory = client_factory or main.DeepSeekClient
    return factory(
        api_key=api_key,
        model=round2_deepseek["model"],
        endpoint_url=round2_deepseek["base_url"],
        timeout_seconds=float(round2_deepseek["timeout_seconds"]),
        max_retries=round2_deepseek["max_retries"],
        thinking_mode=round2_deepseek["thinking_mode"],
        reasoning_effort=round2_deepseek["reasoning_effort"],
    )


def build_round2_cache_identity(
    bundle: Round2InputBundle, config: dict[str, Any]
) -> dict[str, Any]:
    """构造包含 run_id 和完整请求哈希的第二轮复用身份。"""
    round2_deepseek = main.deepseek_stage_config(config, "round2")
    candidate_ids = [
        {"arxiv_id": paper["arxiv_id"], "version": int(paper["version"])}
        for paper in bundle.papers
    ]
    identity = {
        "run_id": bundle.run_selection.run_id,
        "task_type": main.ROUND2_TASK_TYPE,
        "model_name": round2_deepseek["model"],
        "prompt_version": config["versions"]["round2_prompt_version"],
        "research_profile_version": config["versions"][
            "research_profile_version"
        ],
        "selection_policy_version": main.ROUND2_SELECTION_POLICY,
        "input_hash": main.stable_json_hash(
            {
                "messages": bundle.messages,
                "thinking_mode": round2_deepseek["thinking_mode"],
                "reasoning_effort": round2_deepseek["reasoning_effort"],
                "output_transport": round2_deepseek["output_transport"],
                "named_tool": build_round2_result_tool(config),
            }
        ),
        "candidate_id_list_hash": main.stable_json_hash(candidate_ids),
        "feedback_sample_hash": main.stable_json_hash([]),
    }
    request_summary = {
        "run_id": identity["run_id"],
        "candidate_count": len(bundle.papers),
        "model_name": identity["model_name"],
        "prompt_version": identity["prompt_version"],
        "research_profile_version": identity["research_profile_version"],
        "selection_policy": main.ROUND2_SELECTION_POLICY,
        "output_transport": round2_deepseek["output_transport"],
        "input_hash": identity["input_hash"],
    }
    return {
        **identity,
        "cache_key": main.stable_json_hash(identity),
        "request_summary_json": json.dumps(
            request_summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def load_valid_round2_cache(
    connection: sqlite3.Connection,
    bundle: Round2InputBundle,
    config: dict[str, Any],
) -> tuple[str, dict[str, Any] | None, list[str]]:
    """读取并重新校验第二轮缓存；损坏或身份不一致统一视为未命中。"""
    identity = build_round2_cache_identity(bundle, config)
    row = connection.execute(
        """
        SELECT task_type, model_name, prompt_version,
               research_profile_version, selection_policy_version,
               input_hash, candidate_id_list_hash, feedback_sample_hash,
               request_summary, response_json
        FROM batch_cache
        WHERE cache_key = ?
        LIMIT 1
        """,
        (identity["cache_key"],),
    ).fetchone()
    if row is None:
        return "cache_miss", None, []

    for field in (
        "task_type",
        "model_name",
        "prompt_version",
        "research_profile_version",
        "selection_policy_version",
        "input_hash",
        "candidate_id_list_hash",
        "feedback_sample_hash",
    ):
        if row[field] != identity[field]:
            return "cache_miss", None, ["第二轮缓存身份不一致，已忽略缓存。"]
    if row["request_summary"] != identity["request_summary_json"]:
        return "cache_miss", None, ["第二轮缓存请求摘要不一致，已忽略缓存。"]
    try:
        raw_response = json.loads(row["response_json"])
    except (TypeError, json.JSONDecodeError):
        return "cache_miss", None, ["第二轮缓存 JSON 损坏，已忽略缓存。"]
    if not isinstance(raw_response, dict):
        return "cache_miss", None, ["第二轮缓存顶层不是对象，已忽略缓存。"]

    validated, warnings = main.validate_round2_result(
        raw_response,
        bundle.papers,
        max_recommendations=config["final_max_recommendations"],
        profile_version=config["versions"]["research_profile_version"],
        prompt_version=config["versions"]["round2_prompt_version"],
    )
    if not validated["batch_valid"]:
        return "cache_miss", None, warnings
    if config["versions"]["round2_prompt_version"] in main.ROUND2_PRECISION_PROMPT_VERSIONS:
        cached_audit = raw_response.get(main.ROUND2_V14_CACHE_AUDIT_KEY)
        recommendations = validated["final_recommendations"]
        cached_items = raw_response.get("final_recommendations")
        if not main.screening_stage_audit_valid(
            cached_audit, expected_accepted_count=len(recommendations)
        ) or not isinstance(cached_items, list):
            return "cache_miss", None, [
                "第二轮缓存缺少合法的逐篇选择审计，已忽略缓存。"
            ]
        positions = cached_audit["accepted_positions"]
        for index, (paper, cached_item, position) in enumerate(
            zip(recommendations, cached_items, positions, strict=True), start=1
        ):
            if not isinstance(cached_item, dict) or not isinstance(position, dict):
                return "cache_miss", None, ["第二轮缓存位置审计非法。"]
            model_position = cached_item.get("_arxivkaleid_model_position")
            if (
                isinstance(model_position, bool)
                or not isinstance(model_position, int)
                or model_position < 1
                or position.get("model_position") != model_position
                or position.get("final_rank") != index
                or position.get("arxiv_id") != paper["arxiv_id"]
                or position.get("version") != int(paper["version"])
            ):
                return "cache_miss", None, ["第二轮缓存安全字段非法。"]
            paper["model_position"] = model_position
        validated["selection_audit"] = cached_audit
        validated["validation_diagnostic"] = cached_audit
    return "cache_hit", validated, warnings


def save_round2_cache(
    connection: sqlite3.Connection,
    bundle: Round2InputBundle,
    config: dict[str, Any],
    raw_response: dict[str, Any],
) -> None:
    """保存已通过 validator 的第二轮原始 JSON 和安全身份摘要。"""
    identity = build_round2_cache_identity(bundle, config)
    with connection:
        connection.execute(
            """
            INSERT INTO batch_cache (
                cache_key, task_type, model_name, prompt_version,
                research_profile_version, selection_policy_version,
                input_hash, candidate_id_list_hash, feedback_sample_hash,
                request_summary, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                request_summary = excluded.request_summary,
                response_json = excluded.response_json,
                created_at = excluded.created_at
            """,
            (
                identity["cache_key"],
                identity["task_type"],
                identity["model_name"],
                identity["prompt_version"],
                identity["research_profile_version"],
                identity["selection_policy_version"],
                identity["input_hash"],
                identity["candidate_id_list_hash"],
                identity["feedback_sample_hash"],
                identity["request_summary_json"],
                json.dumps(raw_response, ensure_ascii=False, separators=(",", ":")),
                main.current_time_iso(),
            ),
        )


def run_round2_model_and_save(
    connection: sqlite3.Connection,
    project_root: Path,
    config: dict[str, Any],
    bundle: Round2InputBundle,
    *,
    client_factory: Callable[..., Any] | None = None,
    api_key_override: str | None = None,
    diagnostic_observer: Callable[[str, Any], None] | None = None,
) -> tuple[dict[str, Any], list[str], str]:
    def observe(kind: str, value: Any) -> None:
        if diagnostic_observer is not None:
            try:
                diagnostic_observer(kind, value)
            except Exception:
                pass

    if not bundle.all_inputs_usable:
        raise RuntimeError(
            "round2_inputs_not_usable:" + ";".join(unusable_input_reasons(bundle))
        )
    if bundle.input_mode == "no_candidates":
        empty_audit = main.empty_screening_stage_audit()
        main.save_round2_screening_results(
            connection,
            bundle.run_selection.run_id,
            [],
            config,
            selection_audit=empty_audit,
        )
        observe("outcome", {"code": "AKO-R2-NO_ELIGIBLE_FULLTEXT", "input_mode": bundle.input_mode})
        return (
            {
                "batch_valid": True,
                "actual_recommendation_count": 0,
                "final_recommendations": [],
                "selection_audit": empty_audit,
                "validation_diagnostic": empty_audit,
                "quality_note": "没有论文通过全文页数、解析质量与Token预算门控。",
            },
            [],
            "skipped_no_candidates",
        )

    cache_warnings: list[str] = []
    identity = build_round2_cache_identity(bundle, config)
    request_hash = str(identity["input_hash"])
    usage_identity = {
        "run_id": bundle.run_selection.run_id,
        "task_type": main.ROUND2_TASK_TYPE,
        "call_purpose": "screening",
        "model_requested": main.deepseek_stage_config(config, "round2")["model"],
        "prompt_version": config["versions"]["round2_prompt_version"],
        "research_profile_version": config["versions"][
            "research_profile_version"
        ],
        "selection_policy_version": main.ROUND2_SELECTION_POLICY,
        "request_hash": request_hash,
    }
    cache_status, cached, cache_warnings = load_valid_round2_cache(
        connection, bundle, config
    )
    if cache_status == "cache_hit" and cached is not None:
        call_id = model_usage.record_cache_reuse(
            connection, **usage_identity
        )
        try:
            main.save_round2_screening_results(
                connection,
                bundle.run_selection.run_id,
                cached["final_recommendations"],
                config,
                selection_audit=cached.get("selection_audit"),
            )
        except Exception as exc:
            model_usage.update_call_status(
                connection, call_id, "save_failed", type(exc).__name__
            )
            raise
        return cached, cache_warnings, cache_status

    local_cache_status = "miss"
    try:
        price_snapshot = model_usage.price_snapshot_from_config(
            config, stage="round2"
        )
        client = create_deepseek_client(
            project_root=project_root,
            config=config,
            client_factory=client_factory,
            **({"api_key_override": api_key_override} if api_key_override is not None else {}),
        )
    except Exception as exc:
        model_usage.record_no_api_event(
            connection,
            **usage_identity,
            local_cache_status=local_cache_status,
            call_status="configuration_failed",
            error_type=str(exc).split(":", 1)[0],
        )
        observe("configuration_failure", {"error_type": str(exc).split(":", 1)[0]})
        raise
    call_result = request_round2_json(client, bundle, config)
    observe("call_result", call_result)
    call_id = model_usage.record_api_call(
        connection,
        **usage_identity,
        local_cache_status=local_cache_status,
        call_result=call_result,
        price_snapshot=price_snapshot,
    )
    if not call_result.ok or call_result.data is None:
        raise RuntimeError(f"round2_deepseek_call_failed:{call_result.error_type}")

    try:
        validated, warnings = main.validate_round2_result(
            call_result.data,
            bundle.papers,
            max_recommendations=config["final_max_recommendations"],
            profile_version=config["versions"]["research_profile_version"],
            prompt_version=config["versions"]["round2_prompt_version"],
        )
    except Exception as exc:
        model_usage.update_call_status(
            connection, call_id, "validation_failed", type(exc).__name__
        )
        raise
    observe("validation_diagnostic", validated.get("validation_diagnostic"))
    if not validated["batch_valid"]:
        diagnostic = validated.get("validation_diagnostic")
        if not isinstance(diagnostic, dict):
            diagnostic = {
                "code": "round2_validation_diagnostic_unavailable",
                "stage": "internal",
            }
        validation_code = str(
            validated.get("validation_error_code")
            or diagnostic.get("code")
            or "round2_validation_failed"
        )
        observe("validation_diagnostic", diagnostic)
        model_usage.update_call_status(
            connection, call_id, "validation_failed", validation_code
        )
        # 只传递验证器生成的安全结构诊断，不传递模型正文或全文。
        raise RuntimeError(
            "round2_validation_failed:"
            + json.dumps(
                diagnostic,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    try:
        save_round2_cache(
            connection,
            bundle,
            config,
            validated.get("cacheable_response", call_result.data),
        )
        main.save_round2_screening_results(
            connection,
            bundle.run_selection.run_id,
            validated["final_recommendations"],
            config,
            selection_audit=validated.get("selection_audit"),
        )
    except Exception as exc:
        model_usage.update_call_status(
            connection, call_id, "save_failed", type(exc).__name__
        )
        raise
    model_usage.update_call_status(connection, call_id, "completed")
    return validated, cache_warnings + warnings, "cache_miss"
