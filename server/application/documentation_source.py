from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from smog_ai.artifacts.repository import ArtifactRepository
from smog_ai.storage.base import ObjectNotFoundError


@runtime_checkable
class DocumentationSource(Protocol):
    backend_name: str

    def ping(self) -> None: ...

    def manifest(self) -> dict[str, Any] | None: ...

    def processing_markdown(self) -> str: ...

    def processing_latex(self) -> str: ...

    def mathematics_markdown(self) -> str: ...

    def model_plugin_markdown(self) -> str: ...

    def mathematics_latex(self) -> str: ...

    def hf20_markdown(self) -> str: ...

    def hf20_latex(self) -> str: ...


@dataclass(slots=True)
class LocalDocumentationSource:
    processing_path: Path
    processing_latex_path: Path
    mathematics_path: Path
    plugin_path: Path
    latex_path: Path
    hf20_path: Path | None = None
    hf20_latex_path: Path | None = None
    backend_name: str = "local-documentation"

    @staticmethod
    def _read(path: Path) -> str:
        if not path.exists():
            raise FileNotFoundError(f"Documentation source is unavailable: {path}")
        return path.read_text(encoding="utf-8-sig")

    def ping(self) -> None:
        for path in (
            self.processing_path,
            self.processing_latex_path,
            self.mathematics_path,
            self.plugin_path,
            self.latex_path,
            self.hf20_path,
            self.hf20_latex_path,
        ):
            if path is None:
                continue
            if not path.exists():
                raise FileNotFoundError(path)

    def manifest(self) -> dict[str, Any] | None:
        return {
            "schema_version": "1.1",
            "application_version": "1.7.0",
            "backend": self.backend_name,
            "documents": {
                "processing_markdown": {"path": str(self.processing_path)},
                "processing_latex": {"path": str(self.processing_latex_path)},
                "mathematics_markdown": {"path": str(self.mathematics_path)},
                "model_plugin_markdown": {"path": str(self.plugin_path)},
                "mathematics_latex": {"path": str(self.latex_path)},
                "hf20_markdown": {"path": str(self.hf20_path) if self.hf20_path else None},
                "hf20_latex": {"path": str(self.hf20_latex_path) if self.hf20_latex_path else None},
            },
        }

    def processing_markdown(self) -> str:
        return self._read(self.processing_path)

    def processing_latex(self) -> str:
        return self._read(self.processing_latex_path)

    def mathematics_markdown(self) -> str:
        return self._read(self.mathematics_path)

    def model_plugin_markdown(self) -> str:
        return self._read(self.plugin_path)

    def mathematics_latex(self) -> str:
        return self._read(self.latex_path)

    def hf20_markdown(self) -> str:
        if self.hf20_path is None:
            raise FileNotFoundError("HF20 documentation path is not configured")
        return self._read(self.hf20_path)

    def hf20_latex(self) -> str:
        if self.hf20_latex_path is None:
            raise FileNotFoundError("HF20 LaTeX documentation path is not configured")
        return self._read(self.hf20_latex_path)


@dataclass(slots=True)
class ObjectStoreDocumentationSource:
    repository: ArtifactRepository
    fallback: DocumentationSource | None = None
    backend_name: str = "object-store-documentation"

    def ping(self) -> None:
        self.repository.ping()

    def _text(self, key: str, fallback_loader: Callable[[], str] | None) -> str:
        try:
            return self.repository.store.get_bytes(key).decode("utf-8-sig")
        except (ObjectNotFoundError, FileNotFoundError):
            if fallback_loader is None:
                raise FileNotFoundError(f"Documentation object is unavailable: {key}")
            return fallback_loader()

    def manifest(self) -> dict[str, Any] | None:
        try:
            payload = self.repository.get_json(
                self.repository.layout.documentation_manifest
            )
            if isinstance(payload, dict):
                payload.setdefault("backend", self.backend_name)
            return payload
        except (ObjectNotFoundError, FileNotFoundError):
            return self.fallback.manifest() if self.fallback else None

    def processing_markdown(self) -> str:
        return self._text(
            self.repository.layout.technical_processing_document,
            self.fallback.processing_markdown if self.fallback else None,
        )

    def processing_latex(self) -> str:
        return self._text(
            self.repository.layout.technical_processing_latex,
            self.fallback.processing_latex if self.fallback else None,
        )

    def mathematics_markdown(self) -> str:
        return self._text(
            self.repository.layout.mathematical_model_document,
            self.fallback.mathematics_markdown if self.fallback else None,
        )

    def model_plugin_markdown(self) -> str:
        return self._text(
            self.repository.layout.model_plugin_guide,
            self.fallback.model_plugin_markdown if self.fallback else None,
        )

    def mathematics_latex(self) -> str:
        return self._text(
            self.repository.layout.mathematical_model_latex,
            self.fallback.mathematics_latex if self.fallback else None,
        )

    def hf20_markdown(self) -> str:
        return self._text(
            self.repository.layout.hf20_document,
            self.fallback.hf20_markdown if self.fallback else None,
        )

    def hf20_latex(self) -> str:
        return self._text(
            self.repository.layout.hf20_latex,
            self.fallback.hf20_latex if self.fallback else None,
        )
