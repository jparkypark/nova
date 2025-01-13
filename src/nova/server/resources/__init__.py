"""Resource handlers for Nova MCP server."""

from nova.server.types import ResourceHandler

from .attachment import AttachmentHandler
from .note import NoteHandler
from .ocr import OCRHandler
from .vector_store import VectorStoreHandler

__all__ = [
    "AttachmentHandler",
    "NoteHandler",
    "OCRHandler",
    "ResourceHandler",
    "VectorStoreHandler",
]
