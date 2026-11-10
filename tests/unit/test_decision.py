"""Математическое ядро Decision Lab: TOPSIS, Парето, устойчивость, импорт."""

from __future__ import annotations

import json

import pytest

from backend.domain.decision.analysis import analyse
from backend.domain.decision.importers import parse_csv, parse_json
from backend.domain.decision.models import (
    Alternative,
    Constraint,
    Criterion,
    DecisionInputError,
    DecisionProblem,
    Measurement,
    SensitivitySettings,
    validate,
)
from backend.domain.decision.pareto import dominated_by, dominates
from backend.domain.decision.sensitivity import make_ranker, weight_intervals, weight_shares
from backend.domain.decision.topsis import competition_ranks, topsis

# ---------- классический TOPSIS ----------


def test_topsis_matches_hand_computed_example() -> None:
    #пример разобран по шагам в docs/research/MATHEMATICAL_MODEL.md, раздел 2.6
    result = topsis([[0.9, 10], [0.8, 5], [0.7, 20]], [0.6, 0.4], ["max", "min"])
    assert result.normalized[0] == pytest.approx([0.646162, 0.436436], abs=1e-6)
    assert result.ideal == pytest.approx([0.387697, 0.087287], abs=1e-6)
    assert result.anti_ideal == pytest.approx([0.301542, 0.349149], abs=1e-6)
    assert result.d_plus == pytest.approx([0.087287, 0.043077, 0.275670], abs=1e-6)
    assert result.d_minus == pytest.approx([0.194676, 0.265381, 0.0], abs=1e-6)
    assert result.closeness == pytest.approx([0.690431, 0.860346, 0.0], abs=1e-6)
    assert result.ranks == [2, 1, 3]


def test_weights_are_scale_invariant() -> None:
    matrix = [[0.9, 10], [0.8, 5], [0.7, 20]]
    a = topsis(matrix, [0.6, 0.4], ["max", "min"]).closeness
    b = topsis(matrix, [6, 4], ["max", "min"]).closeness
    assert a == pytest.approx(b)


def test_closeness_is_bounded() -> None:
    result = topsis([[1, 2, 3], [4, 0.5, 6], [7, 8, 0.1]], [1, 1, 1], ["max", "min", "max"])
    assert all(0 <= value <= 1 for value in result.closeness)


def test_constant_column_is_degenerate_and_ignored() -> None:
    with_constant = topsis([[0.9, 5], [0.8, 5]], [0.5, 0.5], ["max", "min"])
    assert with_constant.degenerate == [1]
    assert with_constant.ranks == [1, 2]


def test_zero_column_does_not_divide_by_zero() -> None:
    result = topsis([[0.0, 1.0], [0.0, 2.0]], [0.5, 0.5], ["min", "max"])
    assert result.degenerate == [0]
    assert result.ranks == [2, 1]


def test_identical_alternatives_share_first_place() -> None:
    result = topsis([[1.0, 2.0], [1.0, 2.0]], [0.5, 0.5], ["max", "min"])
    assert result.closeness == [0.5, 0.5]
    assert result.ranks == [1, 1]


def test_competition_ranks() -> None:
    assert competition_ranks([0.9, 0.5, 0.9, 0.1]) == [1, 3, 1, 4]


def test_minmax_normalisation_is_available() -> None:
    result = topsis([[0.9, 10], [0.8, 5], [0.7, 20]], [0.6, 0.4], ["max", "min"], "minmax")
    assert result.normalized[0] == pytest.approx([1.0, 1 / 3])


# ---------- Парето ----------


def test_dominance_respects_directions() -> None:
    assert dominates([0.9, 10], [0.8, 20], ["max", "min"])
    assert not dominates([0.9, 30], [0.8, 20], ["max", "min"])
    #равные не доминируют друг друга
    assert not dominates([0.9, 10], [0.9, 10], ["max", "min"])
    assert dominated_by([[0.9, 10], [0.8, 20], [0.95, 50]], ["max", "min"]) == [[], [0], []]


# ---------- режим C и его проверяемое свойство ----------

CRITERIA = (
    Criterion("auc", "ROC-AUC", "max", 0.5, group="quality"),
    Criterion("ms", "Инференс", "min", 0.3, unit="мс"),
    Criterion("mb", "Размер", "min", 0.2, unit="МБ"),
)
#найдено перебором: в классическом TOPSIS эта доминируемая альтернатива меняет лидера
REVERSAL_BASE = [[0.89, 103, 11], [0.90, 39, 127], [0.89, 18, 172]]
REVERSAL_DOMINATED = [0.89, 435, 565]


def _alt(key: str, row: list[float | None], std: float | None = None) -> Alternative:
    return Alternative(
        key,
        key,
        {c.key: Measurement(value, std if c.key == "auc" else None) for c, value in zip(CRITERIA, row, strict=True)},
    )


def _problem(rows: dict[str, list[float | None]], **kwargs: object) -> DecisionProblem:
    settings = kwargs.pop("sensitivity", SensitivitySettings(samples=300))
    return DecisionProblem(
        criteria=CRITERIA,
        alternatives=tuple(_alt(key, row) for key, row in rows.items()),
        sensitivity=settings,  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def test_classic_topsis_suffers_rank_reversal_from_dominated_alternative() -> None:
    base = {"A": REVERSAL_BASE[0], "B": REVERSAL_BASE[1], "C": REVERSAL_BASE[2]}
    before = analyse(_problem(base))["modes"]["topsis"]["winner"]
    after = analyse(_problem({**base, "D": REVERSAL_DOMINATED}))["modes"]["topsis"]["winner"]
    assert before == "C"
    assert after == "B"


def test_robust_mode_is_invariant_to_dominated_alternative() -> None:
    base = {"A": REVERSAL_BASE[0], "B": REVERSAL_BASE[1], "C": REVERSAL_BASE[2]}
    before = analyse(_problem(base))
    after = analyse(_problem({**base, "D": REVERSAL_DOMINATED}))
    assert before["modes"]["robust"]["ranking"] == after["modes"]["robust"]["ranking"]
    status = {item["key"]: item for item in after["alternatives"]}
    assert status["D"]["status"] == "dominated"
    assert status["D"]["dominated_by"] == ["A", "B", "C"]


def test_missing_value_is_not_zero() -> None:
    #без измерения инференса «0 мс» сделал бы X лучшей по скорости
    result = analyse(_problem({"A": [0.9, 40, 100], "B": [0.85, 10, 30], "X": [0.95, None, 10]}))
    status = {item["key"]: item for item in result["alternatives"]}
    assert status["X"]["status"] == "incomplete"
    assert status["X"]["missing"] == ["ms"]
    assert all(row["key"] != "X" for row in result["modes"]["topsis"]["ranking"])
    #в режиме по одной метрике X участвует: метрика у неё есть
    assert result["modes"]["primary"]["winner"] == "X"


def test_constraints_mark_infeasible_and_keep_them_visible() -> None:
    result = analyse(
        _problem(
            {"A": [0.94, 120, 450], "B": [0.92, 35, 110], "C": [0.89, 8, 15]},
            constraints=(Constraint("ms", "<=", 50), Constraint("mb", "<=", 100)),
        )
    )
    status = {item["key"]: item["status"] for item in result["alternatives"]}
    assert status == {"A": "infeasible", "B": "infeasible", "C": "pareto"}
    assert result["modes"]["robust"]["winner"] == "C"
    assert result["modes"]["primary"]["winner"] == "A"
    assert result["modes"]["primary"]["winner_violates"]


def test_constraint_on_unmeasured_value_is_unverified() -> None:
    criteria = (*CRITERIA, Criterion("mem", "Память", "min", 0.0, unit="МБ"))
    problem = DecisionProblem(
        criteria=criteria,
        alternatives=(_alt("A", [0.9, 10, 10]), _alt("B", [0.8, 5, 5])),
        constraints=(Constraint("mem", "<=", 100),),
        sensitivity=SensitivitySettings(samples=50),
    )
    result = analyse(problem)
    assert {item["status"] for item in result["alternatives"]} == {"unverified"}
    assert result["modes"]["robust"]["winner"] is None
    assert result["criteria"][-1]["role"] == "constraint_only"


def test_analysis_is_deterministic_for_seed() -> None:
    rows = {"A": [0.91, 45, 120], "B": [0.89, 12, 35], "C": [0.93, 110, 280], "D": [0.90, 30, 90]}
    first = analyse(_problem(rows))
    second = analyse(_problem(rows))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["method"]["input_sha256"] == second["method"]["input_sha256"]


def test_weight_shares_sum_to_one_and_intervals_contain_current() -> None:
    matrix = [[0.91, 45, 120], [0.89, 12, 35], [0.93, 110, 280]]
    ranker = make_ranker(["max", "min", "min"], "vector")
    shares = weight_shares(matrix, [0.5, 0.3, 0.2], ranker, samples=500, seed=1, concentration=None)
    assert sum(shares.win_share) == pytest.approx(1.0)
    for interval in weight_intervals(matrix, [0.5, 0.3, 0.2], ranker, 0.01):
        assert interval.low <= interval.current <= interval.high


def test_quality_dominant_weights_flip_winner() -> None:
    rows = {"A": [0.91, 45, 120], "B": [0.89, 12, 35], "C": [0.93, 110, 280]}
    balanced = analyse(_problem(rows))
    quality_only = DecisionProblem(
        criteria=(CRITERIA[0], *(Criterion(c.key, c.label, c.direction, 0.0) for c in CRITERIA[1:])),
        alternatives=tuple(_alt(k, v) for k, v in rows.items()),
        sensitivity=SensitivitySettings(samples=50),
    )
    assert balanced["modes"]["robust"]["winner"] == "B"
    assert analyse(quality_only)["modes"]["robust"]["winner"] == "C"


def test_double_counted_quality_is_warned() -> None:
    criteria = (*CRITERIA, Criterion("f1", "F1", "max", 0.2, group="quality"))
    alternatives = tuple(
        Alternative(
            k, k, {"auc": Measurement(a), "ms": Measurement(b), "mb": Measurement(c), "f1": Measurement(a - 0.1)}
        )
        for k, (a, b, c) in {"A": (0.9, 10, 10), "B": (0.8, 5, 50), "C": (0.85, 7, 30), "D": (0.7, 1, 90)}.items()
    )
    result = analyse(DecisionProblem(criteria, alternatives, sensitivity=SensitivitySettings(samples=20)))
    text = " ".join(result["warnings"])
    assert "Несколько метрик качества" in text
    assert "сильно коррелируют" in text


@pytest.mark.parametrize(
    "criteria",
    [
        (Criterion("a", "A", "max", 0.0), Criterion("b", "B", "min", 0.0)),
        (Criterion("a", "A", "max", -1.0),),
        (Criterion("a", "A", "max", float("nan")),),
        (Criterion("a", "A", "max", 1.0), Criterion("a", "A2", "min", 1.0)),
    ],
)
def test_invalid_weights_are_rejected(criteria: tuple[Criterion, ...]) -> None:
    alternatives = (
        Alternative("x", "x", {"a": Measurement(1.0), "b": Measurement(1.0)}),
        Alternative("y", "y", {"a": Measurement(2.0), "b": Measurement(2.0)}),
    )
    with pytest.raises(DecisionInputError):
        validate(DecisionProblem(criteria, alternatives))


def test_unknown_constraint_criterion_is_rejected() -> None:
    with pytest.raises(DecisionInputError):
        validate(_problem({"A": [1, 1, 1], "B": [2, 2, 2]}, constraints=(Constraint("nope", "<=", 1),)))


# ---------- импорт ----------


def test_csv_import_with_suffixes_std_and_empty_cells() -> None:
    text = "model,roc_auc,roc_auc_std,inference_ms,custom:max\nlogreg,0.91,0.01,4,1\ngbm,0.94,0.008,,2\n"
    matrix = parse_csv(text)
    keys = [item["key"] for item in matrix.criteria]
    assert keys == ["roc_auc", "inference_ms", "custom"]
    gbm = matrix.alternatives[1]
    assert gbm["label"] == "gbm"
    assert gbm["values"]["inference_ms"]["value"] is None
    assert gbm["values"]["roc_auc"]["std"] == pytest.approx(0.008)


def test_csv_without_direction_is_rejected_with_hint() -> None:
    with pytest.raises(DecisionInputError, match=":max"):
        parse_csv("model,mystery\na,1\nb,2\n")


def test_modelarena_leaderboard_import() -> None:
    payload = {
        "objective": {"metric": "roc_auc"},
        "champion": {"contender_key": "gbm"},
        "baseline_key": "dummy",
        "rows": [
            {
                "contender_key": "gbm",
                "label": "Gradient boosting",
                "family": "boosting",
                "std": 0.01,
                "elapsed_seconds": 3.2,
                "model_bytes": 2_500_000,
                "cross_validated": [
                    {"key": "roc_auc", "label": "ROC-AUC", "direction": "higher", "value": 0.95, "std": 0.01},
                    {"key": "log_loss", "label": "Log loss", "direction": "lower", "value": 0.2, "std": 0.02},
                ],
            },
            {"contender_key": "svm", "label": "SVM", "cross_validated": [], "reason": "FAILED"},
        ],
    }
    matrix = parse_json(payload)
    assert matrix.source == "modelarena"
    assert matrix.primary == "roc_auc"
    criteria = {item["key"]: item for item in matrix.criteria}
    assert criteria["log_loss"]["direction"] == "min"
    assert criteria["roc_auc"]["weight"] == 1.0
    gbm, svm = matrix.alternatives
    assert gbm["values"]["model_mb"]["value"] == pytest.approx(2.5)
    assert svm["values"]["roc_auc"]["value"] is None
    assert any("инференса" in note for note in matrix.notes)


def test_influence_reveals_compressed_quality_metric() -> None:
    #ROC-AUC 0.84–0.93 почти не различаются после векторной нормализации, а время и размер
    #различаются в десятки раз: заявленные 50% веса качества почти не влияют на выбор
    rows = {"A": [0.91, 45, 120], "B": [0.89, 12, 35], "C": [0.93, 110, 280], "E": [0.84, 3, 2]}
    result = analyse(_problem(rows))
    influence = result["modes"]["topsis"]["influence"]
    assert sum(influence.values()) == pytest.approx(1.0)
    assert influence["auc"] < 0.5 / 3
    assert any("фактическое влияние" in text for text in result["warnings"])
    #порог качества как ограничение возвращает качество в решение
    constrained = analyse(_problem(rows, constraints=(Constraint("auc", ">=", 0.88),)))
    assert constrained["modes"]["robust"]["winner"] != "E"


# ---------- режим D: фиксированные шкалы ----------

from backend.domain.decision.models import ScaleOverride  # noqa: E402
from backend.domain.decision.scales import Scale, fixed_closeness  # noqa: E402

FIXED_SCALES = (
    ScaleOverride("auc", "linear", 0.5, 1.0),
    ScaleOverride("ms", "log", 200.0, 2.0),
    ScaleOverride("mb", "log", 500.0, 5.0),
)


def test_scale_utilities_hand_computed() -> None:
    linear = Scale("auc", "linear", 0.5, 1.0, "natural")
    assert linear.utility(0.75) == pytest.approx(0.5)
    assert linear.utility(0.4) == 0.0
    log = Scale("ms", "log", 100.0, 1.0, "budget")
    #ln(10) лежит ровно посередине между ln(100) и ln(1)
    assert log.utility(10.0) == pytest.approx(0.5)
    assert log.utility(1000.0) == 0.0
    assert log.utility(0.0) == 1.0


def test_fixed_closeness_hand_computed() -> None:
    #u = (1, 0), w = (0.5, 0.5): D⁺ = 0.5, D⁻ = 0.5 → 0.5; u = (1, 1) → 1
    assert fixed_closeness([[1.0, 0.0], [1.0, 1.0], [0.0, 0.0]], [1, 1]) == pytest.approx([0.5, 1.0, 0.0])


def test_fixed_mode_is_invariant_to_any_added_alternative() -> None:
    base = {"A": REVERSAL_BASE[0], "B": REVERSAL_BASE[1], "C": REVERSAL_BASE[2]}
    before = analyse(_problem(base, scales=FIXED_SCALES))["modes"]["fixed"]["ranking"]
    #недоминируемая новая альтернатива: в TOPSIS она меняет нормы и идеалы, здесь — нет
    after = analyse(_problem({**base, "N": [0.97, 300, 1]}, scales=FIXED_SCALES))["modes"]["fixed"]["ranking"]
    order_before = [row["key"] for row in before]
    order_after = [row["key"] for row in after if row["key"] != "N"]
    assert order_before == order_after
    closeness_before = {row["key"]: row["closeness"] for row in before}
    for row in after:
        if row["key"] != "N":
            assert row["closeness"] == pytest.approx(closeness_before[row["key"]])


def test_fixed_mode_respects_dominance() -> None:
    result = analyse(_problem({"A": [0.9, 10, 10], "B": [0.85, 20, 30], "C": [0.95, 50, 5]}, scales=FIXED_SCALES))
    ranks = {row["key"]: row["rank"] for row in result["modes"]["fixed"]["ranking"]}
    assert ranks["A"] < ranks["B"]


def test_fixed_mode_uses_natural_and_budget_scales_by_default() -> None:
    criteria = (
        Criterion("roc_auc", "ROC-AUC", "max", 0.5, group="quality"),
        Criterion("inference_ms", "Инференс", "min", 0.5, unit="мс", group="time"),
    )
    alternatives = (
        Alternative("a", "a", {"roc_auc": Measurement(0.9), "inference_ms": Measurement(5)}),
        Alternative("b", "b", {"roc_auc": Measurement(0.8), "inference_ms": Measurement(1)}),
    )
    result = analyse(
        DecisionProblem(
            criteria,
            alternatives,
            constraints=(Constraint("inference_ms", "<=", 50),),
            sensitivity=SensitivitySettings(samples=20),
        )
    )
    scales = {item["criterion"]: item for item in result["modes"]["fixed"]["scales"]}
    assert (scales["roc_auc"]["source"], scales["roc_auc"]["worst"]) == ("natural", 0.5)
    assert (scales["inference_ms"]["source"], scales["inference_ms"]["kind"]) == ("budget", "log")
    assert scales["inference_ms"]["best"] == pytest.approx(0.5)


def test_observed_scale_is_warned() -> None:
    result = analyse(_problem({"A": [0.9, 10, 10], "B": [0.8, 5, 50]}))
    assert any("Режим D" in text for text in result["warnings"])


def test_scale_against_direction_is_rejected() -> None:
    with pytest.raises(DecisionInputError, match="противоречит"):
        analyse(_problem({"A": [0.9, 10, 10], "B": [0.8, 5, 50]}, scales=(ScaleOverride("auc", "linear", 1.0, 0.5),)))
