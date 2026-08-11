from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from smog_ai import __version__
from smog_ai.artifacts.datasets import create_artifact_repository
from smog_ai.config import AppConfig
from smog_ai.domain import StageStats

logger = logging.getLogger(__name__)
_RESOURCE_DOCS_ROOT = Path(__file__).resolve().parents[1] / "resources" / "docs"


@dataclass(frozen=True, slots=True)
class DocumentationBundle:
    processing_markdown: str
    processing_latex: str
    mathematics_markdown: str
    model_plugin_markdown: str
    mathematics_latex: str
    hf20_markdown: str
    hf20_latex: str
    metadata: dict[str, Any]


def _resolve_documentation_path(path: Path, *, required: bool = True) -> tuple[Path, bool]:
    """Resolve a configured documentation path with a package-resource fallback.

    Runtime configuration may survive an upgrade and still contain an absolute path
    to an older checkout.  Documentation is also shipped inside ``smog_ai/resources``
    specifically so a stale checkout path must not invalidate hours of model work.
    """

    configured = Path(path).expanduser()
    if configured.exists() and configured.is_file():
        return configured.resolve(), False

    fallback = (_RESOURCE_DOCS_ROOT / configured.name).resolve()
    if fallback.exists() and fallback.is_file():
        logger.warning(
            "Configured documentation path is unavailable; using packaged resource: "
            "configured=%s fallback=%s",
            configured,
            fallback,
        )
        return fallback, True

    if required:
        raise FileNotFoundError(
            "Documentation file does not exist. "
            f"Configured path: {configured}; packaged fallback: {fallback}"
        )
    return configured, False


def _read(path: Path, *, required: bool = True) -> tuple[str, Path, bool]:
    resolved, fallback_used = _resolve_documentation_path(path, required=required)
    if not resolved.exists():
        return "", resolved, fallback_used
    return resolved.read_text(encoding="utf-8-sig"), resolved, fallback_used


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_documentation_bundle(config: AppConfig) -> DocumentationBundle:
    configured_paths = {
        "processing_markdown": config.documentation.processing_markdown,
        "processing_latex": config.documentation.processing_latex,
        "mathematics_markdown": config.documentation.mathematics_markdown,
        "model_plugin_markdown": config.documentation.model_plugin_markdown,
        "mathematics_latex": config.documentation.mathematics_latex,
        "hf20_markdown": config.documentation.hf20_markdown,
        "hf20_latex": config.documentation.hf20_latex,
    }
    documents: dict[str, str] = {}
    resolved_paths: dict[str, Path] = {}
    fallbacks: dict[str, bool] = {}
    # Documentation is a versioned bundle. If even one configured path points
    # to a stale/missing checkout, use the packaged resource set atomically
    # rather than mixing documents from different application generations.
    force_packaged_bundle = any(
        not Path(path).expanduser().is_file()
        for path in configured_paths.values()
    )
    for name, path in configured_paths.items():
        if force_packaged_bundle:
            resolved = (_RESOURCE_DOCS_ROOT / Path(path).name).resolve()
            if not resolved.is_file():
                raise FileNotFoundError(
                    f"Packaged documentation file does not exist: {resolved}"
                )
            text = resolved.read_text(encoding="utf-8-sig")
            fallback_used = True
        else:
            text, resolved, fallback_used = _read(path)
        documents[name] = text
        resolved_paths[name] = resolved
        fallbacks[name] = fallback_used

    metadata = {
        "schema_version": "1.2",
        "title": config.documentation.platform_title,
        "application_version": __version__,
        "generated_at": datetime.now(UTC).isoformat(),
        "packaged_resource_root": str(_RESOURCE_DOCS_ROOT),
        "fallback_used": any(fallbacks.values()),
        "documents": {
            name: {
                "configured_path": str(configured_paths[name]),
                "resolved_path": str(resolved_paths[name]),
                "packaged_fallback_used": fallbacks[name],
                "sha256": _sha256(text),
                "size": len(text.encode("utf-8")),
            }
            for name, text in documents.items()
        },
    }
    return DocumentationBundle(
        processing_markdown=documents["processing_markdown"],
        processing_latex=documents["processing_latex"],
        mathematics_markdown=documents["mathematics_markdown"],
        model_plugin_markdown=documents["model_plugin_markdown"],
        mathematics_latex=documents["mathematics_latex"],
        hf20_markdown=documents["hf20_markdown"],
        hf20_latex=documents["hf20_latex"],
        metadata=metadata,
    )


def _put_text(repository, key: str, text: str, *, document: str, content_type: str):  # type: ignore[no-untyped-def]
    return repository.store.put_bytes(
        key,
        text.encode("utf-8"),
        content_type=content_type,
        metadata={"document": document, "version": __version__},
        immutable=False,
    )


def publish_documentation(config: AppConfig) -> StageStats:
    if not config.documentation.enabled:
        return StageStats(skipped=1, details={"reason": "documentation_disabled"})
    bundle = load_documentation_bundle(config)
    if not config.documentation.publish_to_object_storage:
        return StageStats(
            downloaded=7,
            details={"status": "loaded_locally", "metadata": bundle.metadata},
        )
    if not config.object_storage.enabled:
        return StageStats(
            skipped=1,
            warnings=1,
            details={"reason": "object_storage_disabled"},
        )
    repository = create_artifact_repository(config)
    repository.ping()
    stored = {
        "processing_markdown": _put_text(
            repository,
            repository.layout.technical_processing_document,
            bundle.processing_markdown,
            document="technical-processing",
            content_type="text/markdown; charset=utf-8",
        ),
        "processing_latex": _put_text(
            repository,
            repository.layout.technical_processing_latex,
            bundle.processing_latex,
            document="technical-processing-latex",
            content_type="application/x-tex; charset=utf-8",
        ),
        "mathematics_markdown": _put_text(
            repository,
            repository.layout.mathematical_model_document,
            bundle.mathematics_markdown,
            document="mathematical-model",
            content_type="text/markdown; charset=utf-8",
        ),
        "model_plugin_markdown": _put_text(
            repository,
            repository.layout.model_plugin_guide,
            bundle.model_plugin_markdown,
            document="model-plugin-guide",
            content_type="text/markdown; charset=utf-8",
        ),
        "mathematics_latex": _put_text(
            repository,
            repository.layout.mathematical_model_latex,
            bundle.mathematics_latex,
            document="mathematical-model-latex",
            content_type="application/x-tex; charset=utf-8",
        ),
        "hf20_markdown": _put_text(
            repository,
            repository.layout.hf20_document,
            bundle.hf20_markdown,
            document="hf20-time-contract-mlops",
            content_type="text/markdown; charset=utf-8",
        ),
        "hf20_latex": _put_text(
            repository,
            repository.layout.hf20_latex,
            bundle.hf20_latex,
            document="hf20-time-contract-mlops-latex",
            content_type="application/x-tex; charset=utf-8",
        ),
    }
    manifest = {
        **bundle.metadata,
        "storage_backend": repository.store.backend_name,
        "objects": {
            name: {
                "key": item.key,
                "size": item.size,
                "etag": item.etag,
            }
            for name, item in stored.items()
        },
    }
    stored_manifest = repository.put_json(
        repository.layout.documentation_manifest,
        manifest,
        immutable=False,
    )
    return StageStats(
        inserted=8,
        details={
            "manifest_key": stored_manifest.key,
            "storage_backend": repository.store.backend_name,
            "documents": manifest["objects"],
            "metadata": bundle.metadata,
        },
    )
