"""Parse phase implementation."""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Union

from nova.handlers.base import BaseHandler
from nova.handlers.registry import HandlerRegistry
from nova.models.document import DocumentMetadata
from nova.phases.base import Phase

logger = logging.getLogger(__name__)


class ParsePhase(Phase):
    """Parse phase that converts input files to markdown."""

    def __init__(self, config, pipeline):
        """Initialize parse phase.

        Args:
            config: Configuration manager
            pipeline: Pipeline instance
        """
        super().__init__("parse", config, pipeline)

        # Initialize handler registry
        self.registry = HandlerRegistry(config)

        # Initialize stats
        self.stats: Dict[str, Dict[str, Any]] = {}

    def _get_handler(self, file_path: Path) -> Optional[BaseHandler]:
        """Get appropriate handler for file type.

        Args:
            file_path: Path to file

        Returns:
            Handler instance or None if no handler available
        """
        return self.registry.get_handler(file_path)

    async def process_file(
        self, file_path: Union[str, Path], output_dir: Union[str, Path]
    ) -> Optional[DocumentMetadata]:
        """Process a single file.

        Args:
            file_path: Path to file to process
            output_dir: Directory to write output to

        Returns:
            DocumentMetadata if successful, None if skipped
        """
        # Convert to Path objects
        file_path = Path(file_path)
        output_dir = Path(output_dir)

        # Get handler for file type
        handler = self._get_handler(file_path)
        if not handler:
            logger.warning(f"No handler found for {file_path}")
            self.pipeline.state[self.name]["skipped_files"].add(file_path)
            self._update_stats(file_path.suffix.lower(), "skipped", None)
            return None

        try:
            # Process file with handler
            metadata = DocumentMetadata.from_file(
                file_path=file_path,
                handler_name=handler.name,
                handler_version=handler.version,
            )

            # Get relative path from input directory
            try:
                rel_path = file_path.relative_to(self.config.input_dir)
            except ValueError:
                # If not under input_dir, try to find a parent directory that matches
                # Look for common parent directories like "Format Test" or "Format Test 3"
                parts = file_path.parts
                for i in range(len(parts) - 1, -1, -1):
                    if "Format Test" in parts[i]:
                        rel_path = Path(*parts[i:])
                        break
                else:
                    # If no match found, use just the filename
                    rel_path = Path(file_path.name)

            # Get output path using output manager
            output_path = self.pipeline.output_manager.get_output_path_for_phase(
                rel_path, "parse", ".parsed.md"
            )

            # Process file and update metadata
            metadata = await handler.process_impl(file_path, output_path, metadata)
            if metadata and metadata.processed:
                logger.info(f"Successfully processed {file_path}")
                self._update_stats(
                    file_path.suffix.lower(), "successful", handler.__class__.__name__
                )

                # Save metadata file in parse phase output directory
                metadata_path = self.pipeline.output_manager.get_output_path_for_phase(
                    rel_path, "parse", ".metadata.json"
                )
                metadata_path.parent.mkdir(parents=True, exist_ok=True)
                metadata.save(metadata_path)

                return metadata
            else:
                logger.error(f"Failed to process {file_path}")
                self._update_stats(
                    file_path.suffix.lower(), "failed", handler.__class__.__name__
                )
                return None

        except Exception as e:
            logger.error(f"Failed to process {file_path}: {str(e)}")
            self._update_stats(
                file_path.suffix.lower(),
                "failed",
                handler.__class__.__name__ if handler else None,
            )
            return None

    def _update_stats(
        self, extension: str, status: str, handler: Optional[str]
    ) -> None:
        """Update statistics for file type.

        Args:
            extension: File extension
            status: Processing status (successful/failed/skipped)
            handler: Handler class name
        """
        if extension not in self.stats:
            self.stats[extension] = {
                "total": 0,
                "successful": 0,
                "failed": 0,
                "skipped": 0,
                "unchanged": 0,
                "handlers": set(),
            }

        self.stats[extension]["total"] += 1
        self.stats[extension][status] += 1
        if handler:
            self.stats[extension]["handlers"].add(handler)

    def finalize(self) -> None:
        """Print phase summary and update pipeline stats."""
        logger.info("=== Parse Phase Summary ===")

        for extension, stats in self.stats.items():
            logger.info(f"\nFile type: {extension}")
            logger.info(f"Total files: {stats['total']}")
            logger.info(f"Successful: {stats['successful']}")
            logger.info(f"Failed: {stats['failed']}")
            logger.info(f"Skipped: {stats['skipped']}")
            logger.info(f"Unchanged: {stats['unchanged']}")
            logger.info(f"Handlers: {', '.join(stats['handlers'])}")

        # Log skipped files
        skipped_files = self.pipeline.state[self.name]["skipped_files"]
        if skipped_files:
            logger.warning(f"\nSkipped {len(skipped_files)} files:")
            for file in skipped_files:
                logger.warning(f"  - {file}")

        # Update pipeline state with stats
        if "stats" not in self.pipeline.state[self.name]:
            self.pipeline.state[self.name]["stats"] = {}
        self.pipeline.state[self.name]["stats"].update(self.stats)
