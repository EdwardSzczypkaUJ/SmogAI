from __future__ import annotations

import logging
import random
import time
from collections.abc import Mapping
from typing import Any

import httpx

from smog_ai.config import APIConfig
from smog_ai.errors import ExternalAPIError, ExternalAPIStatusError

logger = logging.getLogger(__name__)

# IMGW returns ordinary JSON while the current GIOŚ v1 API returns JSON-LD.
# A client advertising only ``application/json`` receives HTTP 406 from GIOŚ.
DEFAULT_ACCEPT = "application/json, application/ld+json;q=0.9, */*;q=0.1"
TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


class ResilientHttpClient:
    def __init__(self, config: APIConfig, client: httpx.Client | None = None) -> None:
        self.config = config
        timeout = httpx.Timeout(
            connect=config.connect_timeout_seconds,
            read=config.read_timeout_seconds,
            write=config.read_timeout_seconds,
            pool=config.connect_timeout_seconds,
        )
        self.client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": config.user_agent, "Accept": DEFAULT_ACCEPT},
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "ResilientHttpClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Decode JSON/JSON-LD and retry only failures that can actually recover.

        Permanent client responses such as HTTP 400, 401, 403, 404 and 406 are
        returned immediately.  Retrying them only hides a bad contract/header and
        needlessly delays every scheduled run.
        """
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.client.get(url, params=params, headers=dict(headers or {}))
                if response.status_code in TRANSIENT_STATUS_CODES and attempt < self.config.max_retries:
                    delay = self._retry_delay(response, attempt)
                    logger.warning(
                        "Transient HTTP response",
                        extra={
                            "status": response.status_code,
                            "stage": "http_retry",
                            "url": str(response.request.url),
                            "attempt": attempt + 1,
                            "delay_seconds": delay,
                        },
                    )
                    time.sleep(delay)
                    continue

                response.raise_for_status()
                try:
                    return response.json()
                except ValueError as exc:
                    content_type = response.headers.get("Content-Type", "")
                    raise ExternalAPIError(
                        f"Non-JSON response from {response.url}; "
                        f"content-type={content_type!r}; body={self._body_excerpt(response)!r}"
                    ) from exc
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                time.sleep(self._delay(attempt))
            except httpx.HTTPStatusError as exc:
                last_error = self._status_error(exc)
                if exc.response.status_code not in TRANSIENT_STATUS_CODES:
                    break
                if attempt >= self.config.max_retries:
                    break
                time.sleep(self._delay(attempt))
            except ExternalAPIError as exc:
                last_error = exc
                break
        # Preserve structured status information for source-specific handling.
        # Wrapping every 4xx in a generic exception made an expected GIOŚ
        # "historical/inactive sensor has no current series" response look like
        # a retryable pipeline failure.
        if isinstance(last_error, ExternalAPIStatusError):
            raise last_error
        raise ExternalAPIError(f"GET failed for {url}: {last_error}") from last_error

    def get_bytes(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        response = self._get_response(url, params=params, headers=headers)
        return response.content

    def get_text(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        response = self._get_response(url, params=params, headers=headers)
        return response.text

    def _get_response(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.client.get(url, params=params, headers=dict(headers or {}))
                if (
                    response.status_code in TRANSIENT_STATUS_CODES
                    and attempt < self.config.max_retries
                ):
                    time.sleep(self._retry_delay(response, attempt))
                    continue
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
            except httpx.HTTPStatusError as exc:
                last_error = self._status_error(exc)
                if exc.response.status_code not in TRANSIENT_STATUS_CODES:
                    break
            if attempt < self.config.max_retries:
                time.sleep(self._delay(attempt))
        if isinstance(last_error, ExternalAPIStatusError):
            raise last_error
        raise ExternalAPIError(f"GET failed for {url}: {last_error}") from last_error

    def head(self, url: str) -> httpx.Response:
        try:
            response = self.client.head(url)
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise ExternalAPIError(f"HEAD failed for {url}: {exc}") from exc

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        return self._delay(attempt)

    @staticmethod
    def _body_excerpt(response: httpx.Response, limit: int = 500) -> str:
        try:
            return " ".join(response.text.split())[:limit]
        except Exception:  # pragma: no cover - defensive transport fallback
            return ""

    @classmethod
    def _status_error(cls, exc: httpx.HTTPStatusError) -> ExternalAPIStatusError:
        response = exc.response
        content_type = response.headers.get("Content-Type", "")
        body_excerpt = cls._body_excerpt(response)
        return ExternalAPIStatusError(
            "HTTP request failed: "
            f"status={response.status_code}, url={response.request.url}, "
            f"content-type={content_type!r}, body={body_excerpt!r}",
            status_code=response.status_code,
            url=str(response.request.url),
            content_type=content_type,
            body_excerpt=body_excerpt,
        )

    def _delay(self, attempt: int) -> float:
        base = self.config.backoff_base_seconds * (2**attempt)
        return base + random.uniform(0, max(0.05, base * 0.2))
