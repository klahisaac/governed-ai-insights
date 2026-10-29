"""Launch analytics: turn weekly new-to-brand patients into facts worth talking about.

Owner (at Celldex): Senior Data Scientist. Deterministic statistics only; no LLM.
"""

from __future__ import annotations

import math
from typing import Any

from insight.core.facts import AnalysisContext, Fact, WeeklySeries


def _change(current: int, previous: int) -> dict[str, Any]:
    difference = current - previous
    percent = round(difference / previous * 100, 1) if previous else 0.0
    # Difference of two Poisson counts: treat |difference| within 2 standard deviations as normal variation.
    within_normal_variation = abs(difference) <= 2 * math.sqrt(current + previous)
    if within_normal_variation:
        direction, magnitude = "flat", "normal"
    else:
        direction = "up" if difference > 0 else "down"
        magnitude = "large" if abs(percent) >= 25 else "moderate"
    return {
        "percent": percent,
        "within_normal_variation": within_normal_variation,
        "direction": direction,
        "magnitude": magnitude,
    }


class LaunchAnalyzer:
    name = "pharmaceutical.launch"
    version = "0.2.0"

    def analyze(self, context: AnalysisContext) -> list[Fact]:
        metric = context.metric
        facts: list[Fact] = []

        def add(fact_id: str, kind: str, values: dict[str, float], attributes: dict[str, Any], **options: Any) -> None:
            facts.append(Fact(fact_id, kind, metric.name, metric.version, context.scope, values, attributes, **options))

        complete: WeeklySeries = [(week, value) for week, value in context.weekly if week not in context.provisional_weeks]
        if len(complete) < 2:
            return facts
        (previous_week, previous_value), (latest_week, latest_value) = complete[-2], complete[-1]

        change = _change(latest_value, previous_value)
        add(
            "week_over_week_change",
            "change",
            {"value": latest_value, "previous": previous_value, "change_percent": change["percent"]},
            {
                "week": latest_week,
                "previous_week": previous_week,
                "direction": change["direction"],
                "magnitude": change["magnitude"],
                "within_normal_variation": change["within_normal_variation"],
            },
            count_fields=("value", "previous"),
        )

        streak = 0
        for index in range(len(complete) - 1, 0, -1):
            if complete[index][1] <= complete[index - 1][1]:
                break
            streak += 1
        if streak >= 3:
            add("streak", "trend", {"weeks": streak}, {"direction": "up", "through_week": latest_week})

        window = complete[-4:]
        source_of_business = context.breakdowns.get("source_of_business", {})
        window_weeks = {week for week, _ in window}
        switches = sum(value for week, value in source_of_business.get("switch", []) if week in window_weeks)
        total = sum(value for _, value in window)
        if total:
            add(
                "switch_share",
                "mix",
                {
                    "share_percent": round(100 * switches / total),
                    "switch": switches,
                    # The complement is a count too: with switch and total shown, it is total - switch.
                    "naive": total - switches,
                    "total": total,
                    "window_weeks": len(window),
                },
                {"from_week": window[0][0], "to_week": window[-1][0]},
                count_fields=("switch", "naive", "total"),
            )

        movers = []
        for member, series in context.breakdowns.get("region", {}).items():
            member_complete = [(week, value) for week, value in series if week not in context.provisional_weeks]
            if len(member_complete) >= 2:
                member_change = _change(member_complete[-1][1], member_complete[-2][1])
                if not member_change["within_normal_variation"]:
                    movers.append(
                        (abs(member_change["percent"]), member, member_complete[-1][1], member_complete[-2][1], member_change)
                    )
        if movers:
            _, member, current, previous, member_change = max(movers)
            add(
                "top_mover",
                "comparison",
                {"value": current, "previous": previous, "change_percent": member_change["percent"]},
                {
                    "region": member,
                    "week": latest_week,
                    "direction": member_change["direction"],
                    "magnitude": member_change["magnitude"],
                },
                count_fields=("value", "previous"),
            )

        if context.provisional_weeks:
            weeks, first, last = len(context.provisional_weeks), min(context.provisional_weeks), max(context.provisional_weeks)
            add(
                "provisional",
                "caveat",
                {"weeks": weeks},
                {"from_week": first, "to_week": last},
                required=True,
                statement=(f"The latest {weeks} weeks ({first} to {last}) are provisional while claims are still "
                           "arriving, so the comparisons above leave them out."),
            )
        return facts


def _within_binomial_variation(share: float, baseline: float, patients: int) -> bool:
    """Is `share` of `patients` within 2 standard errors of a known `baseline` share?"""
    return abs(share - baseline) <= 2 * math.sqrt(baseline * (1 - baseline) / patients)


def _within_two_proportion_variation(first: tuple[int, int], second: tuple[int, int]) -> bool:
    """Two (persistent, patients) groups: is the difference within 2 standard errors?"""
    (first_persistent, first_patients), (second_persistent, second_patients) = first, second
    pooled = (first_persistent + second_persistent) / (first_patients + second_patients)
    standard_error = math.sqrt(pooled * (1 - pooled) * (1 / first_patients + 1 / second_patients))
    return abs(first_persistent / first_patients - second_persistent / second_patients) <= 2 * standard_error


def _percent(numerator: int, denominator: int) -> int:
    return round(100 * numerator / denominator)


class PersistenceAnalyzer:
    """Facts for a start-week persistence rate. Only start weeks followed long enough count."""

    name = "pharmaceutical.persistence"
    version = "0.2.0"

    def analyze(self, context: AnalysisContext) -> list[Fact]:
        metric = context.metric
        parameters = metric.parameters
        facts: list[Fact] = []

        def add(fact_id: str, kind: str, values: dict[str, float], attributes: dict[str, Any], **options: Any) -> None:
            facts.append(Fact(fact_id, kind, metric.name, metric.version, context.scope, values, attributes, **options))

        numerators = dict(context.weekly_numerator)
        measured = [(week, numerators[week], patients) for week, patients in context.weekly
                    if week not in context.provisional_weeks and patients > 0]
        if measured:
            persistent = sum(count for _, count, _ in measured)
            patients = sum(count for _, _, count in measured)
            add(
                "pooled_persistence",
                "level",
                {
                    "rate_percent": _percent(persistent, patients),
                    "persistent": persistent,
                    "not_persistent": patients - persistent,
                    "patients": patients,
                    "start_weeks": len(measured),
                    "window_start_day": parameters["persistence_window_start_day"],
                    "window_end_day": parameters["persistence_window_end_day"],
                },
                {"from_week": measured[0][0], "to_week": measured[-1][0]},
                count_fields=("persistent", "not_persistent", "patients"),
            )

        if len(measured) >= 2:
            latest_week, latest_persistent, latest_patients = measured[-1]
            earlier = measured[:-1]
            earlier_persistent = sum(count for _, count, _ in earlier)
            earlier_patients = sum(count for _, _, count in earlier)
            latest_share, earlier_share = latest_persistent / latest_patients, earlier_persistent / earlier_patients
            within = _within_binomial_variation(latest_share, earlier_share, latest_patients)
            difference_points = 100 * (latest_share - earlier_share)
            add(
                "latest_measured_start_week",
                "change",
                {
                    "rate_percent": _percent(latest_persistent, latest_patients),
                    "earlier_rate_percent": _percent(earlier_persistent, earlier_patients),
                    "persistent": latest_persistent,
                    "not_persistent": latest_patients - latest_persistent,
                    "patients": latest_patients,
                },
                {
                    "week": latest_week,
                    "earlier_from_week": earlier[0][0],
                    "earlier_to_week": earlier[-1][0],
                    "direction": "flat" if within else ("up" if difference_points > 0 else "down"),
                    "magnitude": "normal" if within else ("large" if abs(difference_points) >= 15 else "moderate"),
                    "within_normal_variation": within,
                },
                count_fields=("persistent", "not_persistent", "patients"),
            )

        measured_weeks = {week for week, _, _ in measured}
        sources = context.breakdowns.get("source_of_business", {})
        source_numerators = context.breakdown_numerators.get("source_of_business", {})
        groups = {}
        for source in ("switch", "naive"):
            patients = sum(count for week, count in sources.get(source, []) if week in measured_weeks)
            persistent = sum(count for week, count in source_numerators.get(source, []) if week in measured_weeks)
            groups[source] = (persistent, patients)
        if all(patients for _, patients in groups.values()):
            (switch_persistent, switch_patients), (naive_persistent, naive_patients) = groups["switch"], groups["naive"]
            within = _within_two_proportion_variation(groups["switch"], groups["naive"])
            switch_higher = switch_persistent / switch_patients > naive_persistent / naive_patients
            add(
                "switch_versus_naive",
                "comparison",
                {
                    "switch_rate_percent": _percent(switch_persistent, switch_patients),
                    "naive_rate_percent": _percent(naive_persistent, naive_patients),
                    "switch_persistent": switch_persistent,
                    "switch_not_persistent": switch_patients - switch_persistent,
                    "switch_patients": switch_patients,
                    "naive_persistent": naive_persistent,
                    "naive_not_persistent": naive_patients - naive_persistent,
                    "naive_patients": naive_patients,
                },
                {
                    "from_week": measured[0][0],
                    "to_week": measured[-1][0],
                    # Switch patients relative to naive patients.
                    "direction": "flat" if within else ("up" if switch_higher else "down"),
                    "within_normal_variation": within,
                },
                count_fields=("switch_persistent", "switch_not_persistent", "switch_patients",
                              "naive_persistent", "naive_not_persistent", "naive_patients"),
            )

        if context.provisional_weeks:
            weeks, first, last = len(context.provisional_weeks), min(context.provisional_weeks), max(context.provisional_weeks)
            window_end_day = parameters["persistence_window_end_day"]
            add(
                "unmeasured_start_weeks",
                "caveat",
                {"weeks": weeks, "window_end_day": window_end_day},
                {"from_week": first, "to_week": last, "reason": metric.provisional_reason},
                required=True,
                statement=(f"The newest {weeks} start weeks ({first} to {last}) are not measured yet: those patients "
                           f"have not been followed for {window_end_day:g} days."),
            )
        return facts
