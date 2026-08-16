from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from smog_ai.artifacts.repository import ArtifactRepository
from smog_ai.storage.base import ObjectNotFoundError


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_instant(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def prepare_interaction_payload(response: dict[str, Any]) -> dict[str, Any]:
    """Keep the complete logical answer without duplicating bulky surface data."""

    payload = dict(response)
    omitted = []
    for key in ("map_points", "surface_options"):
        if key in payload:
            payload.pop(key, None)
            omitted.append(key)
    if omitted:
        payload["analytics_omitted_bulk_fields"] = omitted
    return payload


class OwnAnalyticsStore:
    """Private interaction history with configurable time-based retention.

    Raw events are stored only in the separate analytics repository. The public
    dashboard receives only the small aggregate pointer. Expired raw events are
    removed periodically while the aggregate remains available.
    """

    def __init__(
        self,
        repository: ArtifactRepository,
        *,
        private_prefix: str = "private/analytics",
        retention_days: int = 90,
    ) -> None:
        if not 1 <= int(retention_days) <= 3650:
            raise ValueError("analytics retention_days must be between 1 and 3650")
        self.repository = repository
        self.private_prefix = private_prefix.strip("/ ") or "private/analytics"
        self.retention_days = int(retention_days)
        self._lock = threading.Lock()
        self._retention_lock = threading.Lock()
        self._last_retention_cleanup: datetime | None = None

    def _event_key(self, kind: str, event_id: str, created: datetime) -> str:
        return (
            f"{self.private_prefix}/{kind}/year={created:%Y}/month={created:%m}/"
            f"day={created:%d}/{event_id}.json.gz"
        )

    @property
    def summary_key(self) -> str:
        return "analytics/query-quality/latest.json"

    def save_interaction(self, response: dict[str, Any]) -> dict[str, Any]:
        created = _now()
        event_id = str(response.get("request_id") or uuid.uuid4().hex)
        interaction = prepare_interaction_payload(response)
        event = {
            "schema_version": "1.1",
            "event_type": "forecast_interaction",
            "event_id": event_id,
            "created_at": created.isoformat(),
            "retention_days": self.retention_days,
            "trace_id": response.get("trace_id"),
            "response": interaction,
        }
        stored = self.repository.put_gzip_json(
            self._event_key("interactions", event_id, created),
            event,
            immutable=True,
        )
        self._update_interaction_summary(interaction, created)
        self._maybe_enforce_retention(created)
        return {"status": "ok", "key": stored.key, "size": stored.size}

    def save_feedback(
        self,
        *,
        feedback_id: str,
        trace_id: str,
        request_id: str | None,
        score: float,
        label: str | None,
        comment: str | None,
        question: str | None,
        components: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        created = _now()
        event = {
            "schema_version": "1.1",
            "event_type": "answer_feedback",
            "event_id": feedback_id,
            "created_at": created.isoformat(),
            "retention_days": self.retention_days,
            "trace_id": trace_id,
            "request_id": request_id,
            "score": float(score),
            "label": label,
            "comment": comment,
            "question": question,
            "components": {
                str(key): float(value)
                for key, value in (components or {}).items()
                if 0.0 <= float(value) <= 1.0
            },
        }
        stored = self.repository.put_gzip_json(
            self._event_key("feedback", feedback_id, created),
            event,
            immutable=True,
        )
        self._update_summary(
            float(score), created, label=label, components=event["components"]
        )
        self._maybe_enforce_retention(created)
        return {"status": "ok", "key": stored.key, "size": stored.size}

    def _maybe_enforce_retention(self, now: datetime) -> None:
        last = self._last_retention_cleanup
        if last is not None and now - last < timedelta(hours=6):
            return
        with self._retention_lock:
            last = self._last_retention_cleanup
            if last is not None and now - last < timedelta(hours=6):
                return
            self.enforce_retention(now=now)
            self._last_retention_cleanup = now

    def enforce_retention(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Delete only expired raw interaction/feedback objects.

        The aggregate summary is deliberately outside the private raw-event
        prefixes and is never deleted by this method.
        """

        current = (now or _now()).astimezone(UTC)
        cutoff = current - timedelta(days=self.retention_days)
        deleted = 0
        checked = 0
        for kind in ("interactions", "feedback"):
            for item in self.repository.store.list(f"{self.private_prefix}/{kind}/"):
                if not item.key.endswith(".json.gz"):
                    continue
                checked += 1
                created = item.last_modified
                if created is None:
                    try:
                        event = self.repository.get_gzip_json(item.key)
                        created = _parse_instant(event.get("created_at"))
                    except (ObjectNotFoundError, FileNotFoundError, ValueError, TypeError):
                        created = None
                if created is not None:
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=UTC)
                    created = created.astimezone(UTC)
                if created is not None and created < cutoff:
                    self.repository.store.delete(item.key)
                    deleted += 1
        return {
            "status": "ok",
            "retention_days": self.retention_days,
            "cutoff": cutoff.isoformat(),
            "checked": checked,
            "deleted": deleted,
            "summary_deleted": False,
        }

    def _update_summary(
        self, score: float, created: datetime, *, label: str | None = None,
        components: dict[str, float] | None = None,
    ) -> None:
        with self._lock:
            try:
                current = self.repository.get_json(self.summary_key)
            except (ObjectNotFoundError, FileNotFoundError):
                current = {}
            count = int(current.get("count") or 0) + 1
            total = float(current.get("score_sum") or 0.0) + score
            positive = int(current.get("positive_count") or 0) + int(score >= 0.5)
            daily = dict(current.get("daily") or {})
            day = created.strftime("%Y-%m-%d")
            day_row = dict(daily.get(day) or {})
            day_count = int(day_row.get("count") or 0) + 1
            day_sum = float(day_row.get("score_sum") or 0.0) + score
            daily[day] = {
                "count": day_count,
                "score_sum": day_sum,
                "average_score": day_sum / day_count,
            }
            buckets = dict(current.get("score_buckets") or {})
            bucket = (
                "0.75-1.00" if score >= 0.75 else
                "0.50-0.74" if score >= 0.50 else
                "0.25-0.49" if score >= 0.25 else "0.00-0.24"
            )
            buckets[bucket] = int(buckets.get(bucket) or 0) + 1
            labels = dict(current.get("feedback_labels") or {})
            if label:
                labels[str(label)] = int(labels.get(str(label)) or 0) + 1
            component_scores = dict(current.get("component_scores") or {})
            for name, value in (components or {}).items():
                row = dict(component_scores.get(name) or {})
                component_count = int(row.get("count") or 0) + 1
                component_sum = float(row.get("score_sum") or 0.0) + float(value)
                component_scores[name] = {
                    "count": component_count,
                    "score_sum": component_sum,
                    "average_score": component_sum / component_count,
                }
            payload = {
                "schema_version": "1.0",
                "source": "own_object_store",
                "updated_at": created.isoformat(),
                "count": count,
                "score_sum": total,
                "average_score": total / count,
                "positive_count": positive,
                "positive_fraction": positive / count,
                "daily": daily,
                "score_buckets": buckets,
                "feedback_labels": labels,
                "component_scores": component_scores,
                "interactions": current.get("interactions") or {},
            }
            self.repository.put_json(self.summary_key, payload)

    def _update_interaction_summary(
        self, response: dict[str, Any], created: datetime
    ) -> None:
        with self._lock:
            try:
                current = dict(self.repository.get_json(self.summary_key))
            except (ObjectNotFoundError, FileNotFoundError):
                current = {}
            stats = dict(current.get("interactions") or {})
            count = int(stats.get("count") or 0) + 1
            intent = dict(response.get("intent") or {})
            forecasts = list(response.get("forecasts") or [])
            warnings = list(response.get("warnings") or [])
            time_selection = dict(response.get("time_selection") or {})
            place = dict(response.get("place") or {})
            interpretation = dict(response.get("interpretation") or {})
            raw_usage = dict((interpretation.get("raw_response") or {}).get("usage") or {})
            prompt_tokens = int(
                interpretation.get("prompt_tokens")
                or raw_usage.get("prompt_tokens")
                or raw_usage.get("input_tokens")
                or 0
            )
            completion_tokens = int(
                interpretation.get("completion_tokens")
                or raw_usage.get("completion_tokens")
                or raw_usage.get("output_tokens")
                or 0
            )
            requested = {
                str(value) for value in list(intent.get("pollutants") or []) if value
            }
            returned = {
                str(item.get("parameter"))
                for item in forecasts if isinstance(item, dict) and item.get("parameter")
            }
            complete = bool(requested) and requested.issubset(returned)
            requested_time = _parse_instant(
                time_selection.get("requested_target_time")
                or intent.get("target_time")
            )
            returned_times = [
                _parse_instant(item.get("target_time"))
                for item in forecasts if isinstance(item, dict)
            ]
            # PCHIP may use surrounding source hours, but the resulting value is
            # still calculated for the exact requested instant.  Therefore this
            # check compares the answer contract, not exact source-row matching.
            exact_time = bool(
                requested_time
                and returned_times
                and all(value == requested_time for value in returned_times)
            )
            resolved_point = (
                place.get("latitude") is not None and place.get("longitude") is not None
            )
            stats["count"] = count
            for key, passed in (
                ("complete_parameter_answers", complete),
                ("exact_time_answers", exact_time),
                ("resolved_point_answers", resolved_point),
                ("answers_without_warnings", not warnings),
                ("answers_with_forecasts", bool(forecasts)),
            ):
                stats[key] = int(stats.get(key) or 0) + int(passed)
            stats["warning_total"] = int(stats.get("warning_total") or 0) + len(warnings)
            stats["forecast_value_total"] = int(stats.get("forecast_value_total") or 0) + len(forecasts)
            stats["prompt_tokens"] = int(stats.get("prompt_tokens") or 0) + prompt_tokens
            stats["completion_tokens"] = int(stats.get("completion_tokens") or 0) + completion_tokens
            stats["tokenized_interactions"] = int(stats.get("tokenized_interactions") or 0) + int(
                bool(prompt_tokens or completion_tokens)
            )
            providers = dict(stats.get("providers") or {})
            provider = str(interpretation.get("provider") or "unknown")
            providers[provider] = int(providers.get(provider) or 0) + 1
            stats["providers"] = providers
            models = dict(stats.get("models") or {})
            model = str(interpretation.get("model") or "unknown")
            models[model] = int(models.get(model) or 0) + 1
            stats["models"] = models
            parameters = dict(stats.get("requested_parameters") or {})
            for parameter in requested:
                parameters[parameter] = int(parameters.get(parameter) or 0) + 1
            stats["requested_parameters"] = parameters
            days = dict(stats.get("daily") or {})
            day = created.strftime("%Y-%m-%d")
            day_row = dict(days.get(day) or {})
            day_row["count"] = int(day_row.get("count") or 0) + 1
            day_row["complete"] = int(day_row.get("complete") or 0) + int(complete)
            day_row["exact_time"] = int(day_row.get("exact_time") or 0) + int(exact_time)
            day_row["prompt_tokens"] = int(day_row.get("prompt_tokens") or 0) + prompt_tokens
            day_row["completion_tokens"] = int(day_row.get("completion_tokens") or 0) + completion_tokens
            day_row["tokenized_interactions"] = int(
                day_row.get("tokenized_interactions") or 0
            ) + int(bool(prompt_tokens or completion_tokens))
            days[day] = day_row
            stats["daily"] = days
            current.update({
                "schema_version": "1.1",
                "source": "own_object_store",
                "updated_at": created.isoformat(),
                "interactions": stats,
            })
            current.setdefault("count", 0)
            current.setdefault("average_score", None)
            current.setdefault("positive_fraction", None)
            current.setdefault("daily", {})
            current.setdefault("score_buckets", {})
            current.setdefault("feedback_labels", {})
            current.setdefault("component_scores", {})
            self.repository.put_json(self.summary_key, current)

    def rebuild_summary(self) -> dict[str, Any]:
        """Recalculate the small public aggregate from private compressed events."""
        if self.repository.store.exists(self.summary_key):
            self.repository.store.delete(self.summary_key)
        feedback = sorted(
            [
                *self.repository.store.list(f"{self.private_prefix}/feedback/"),
            ],
            key=lambda item: item.key,
        )
        interactions = sorted(
            [
                *self.repository.store.list(f"{self.private_prefix}/interactions/"),
            ],
            key=lambda item: item.key,
        )
        for item in feedback:
            if not item.key.endswith(".json.gz"):
                continue
            event = dict(self.repository.get_gzip_json(item.key))
            created = _parse_instant(event.get("created_at")) or _now()
            self._update_summary(
                float(event.get("score") or 0.0), created,
                label=str(event.get("label")) if event.get("label") else None,
                components=dict(event.get("components") or {}),
            )
        for item in interactions:
            if not item.key.endswith(".json.gz"):
                continue
            event = dict(self.repository.get_gzip_json(item.key))
            created = _parse_instant(event.get("created_at")) or _now()
            response = event.get("response")
            if isinstance(response, dict):
                self._update_interaction_summary(response, created)
        result = self.summary()
        result["rebuilt_feedback_events"] = len(feedback)
        result["rebuilt_interaction_events"] = len(interactions)
        return result

    def summary(self) -> dict[str, Any]:
        try:
            value = self.repository.get_json(self.summary_key)
        except (ObjectNotFoundError, FileNotFoundError):
            value = {
                "schema_version": "1.0",
                "source": "own_object_store",
                "count": 0,
                "average_score": None,
                "positive_fraction": None,
                "daily": {},
                "score_buckets": {},
                "feedback_labels": {},
                "component_scores": {},
                "interactions": {},
            }
        result = dict(value)
        daily = result.get("daily") or {}
        if isinstance(daily, dict):
            result["daily"] = [
                {"date": day, **dict(row)} for day, row in sorted(daily.items())
            ]
        result["status"] = "ok"
        result["raw_event_retention_days"] = self.retention_days
        return result
