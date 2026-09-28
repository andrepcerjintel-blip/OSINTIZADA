"""Entity Extractors: extração determinística de entidades em texto livre."""

from osintizada.extractors.base import BaseExtractor, Extraction
from osintizada.extractors.pipeline import ExtractionPipeline, default_extractors, extractions_to_results

__all__ = ["BaseExtractor", "Extraction", "ExtractionPipeline", "default_extractors", "extractions_to_results"]
