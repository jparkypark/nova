"""Image processor for handling image conversions and descriptions."""

import os
import shutil
import tempfile
import base64
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
import json
import time
import re
import logging
import subprocess

from PIL import Image
import pillow_heif
from openai import OpenAI
from rich.console import Console

from ..core.models import NovaConfig, ImageProcessingConfig
from ..core.errors import ProcessingError
from ..core.logging import get_logger

logger = get_logger(__name__)
console = Console()

@dataclass
class ImageMetadata:
    """Metadata for processed images."""
    original_path: str
    processed_path: str
    description: Optional[str]
    width: int
    height: int
    format: str
    size: int
    created_at: float
    processing_time: float
    error: Optional[str] = None

class ImageProcessor:
    """Handles image processing operations."""

    def __init__(self, config: NovaConfig, openai_client: Optional[OpenAI] = None, summary: Optional['ProcessingSummary'] = None):
        """Initialize processor.
        
        Args:
            config: Nova configuration
            openai_client: Optional OpenAI client instance
            summary: Optional processing summary instance
        """
        self.config = config
        self.image_config = config.image
        self.vision_api_available = False
        self.summary = summary
        
        # Initialize OpenAI client if key available
        if openai_client:
            self.openai_client = openai_client
            console.print("[success]Using provided OpenAI client[/]")
            self.vision_api_available = True
        else:
            openai_key = os.getenv('OPENAI_API_KEY')
            if openai_key:
                try:
                    self.openai_client = OpenAI(api_key=openai_key)
                    # Test the client with a minimal request
                    response = self.openai_client.chat.completions.create(
                        model="gpt-4-turbo-2024-04-09",
                        messages=[{
                            "role": "user",
                            "content": "Test connection"
                        }],
                        max_tokens=1
                    )
                    console.print("[success]OpenAI client initialized successfully[/]")
                    self.vision_api_available = True
                except Exception as e:
                    console.print(f"[error]Failed to initialize OpenAI client:[/] {str(e)}")
                    self.openai_client = None
            else:
                console.print("[warning]No OpenAI API key found - image descriptions will be limited[/]")
                self.openai_client = None
        
        # Initialize stats
        self.stats = {
            'total_processed': 0,
            'descriptions_generated': 0,
            'heic_conversions': 0,
            'cache_hits': 0,
            'api_calls': 0,
            'api_time_total': 0.0,
            'images_processed': 0,
            'errors': 0
        }
        
        # Setup directories
        self._setup_directories()

        # Store a record of files that changed extensions
        self.converted_files = {}  # { "old_filename.ext": "new_filename.ext", ... }

    def _setup_directories(self) -> None:
        """Create required directories."""
        # Create base directory first
        self.image_config.base_dir.mkdir(parents=True, exist_ok=True)
        
        # Create subdirectories
        self.image_config.original_dir.mkdir(parents=True, exist_ok=True)
        self.image_config.processed_dir.mkdir(parents=True, exist_ok=True)
        self.image_config.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.image_config.cache_dir.mkdir(parents=True, exist_ok=True)
        
        logger.debug("Created image processing directories", extra={
            "base_dir": str(self.image_config.base_dir),
            "original_dir": str(self.image_config.original_dir),
            "processed_dir": str(self.image_config.processed_dir),
            "metadata_dir": str(self.image_config.metadata_dir),
            "cache_dir": str(self.image_config.cache_dir)
        })

    def _get_cache_path(self, image_path: Path) -> Path:
        """Get cache file path for an image."""
        cache_name = f"{image_path.stem}_{image_path.stat().st_mtime}.json"
        return self.image_config.cache_dir / cache_name

    def _clear_cache(self) -> None:
        """Clear the cache directory."""
        try:
            removed_files = 0
            for cache_file in self.image_config.cache_dir.glob("*.json"):
                try:
                    # Validate cache entry
                    with open(cache_file, 'r') as f:
                        data = json.load(f)
                    if not data.get('description'):
                        logger.debug("Removing invalid cache file", extra={
                            "file": str(cache_file),
                            "reason": "no description"
                        })
                        cache_file.unlink()
                        removed_files += 1
                except Exception as e:
                    logger.debug("Removing corrupted cache file", extra={
                        "file": str(cache_file),
                        "error": str(e)
                    })
                    cache_file.unlink()
                    removed_files += 1
            
            if removed_files > 0:
                logger.info(f"Cleaned {removed_files} cache files")
        except Exception as e:
            logger.error("Failed to clear cache", extra={"error": str(e)})

    def _load_from_cache(self, image_path: Path) -> Optional[ImageMetadata]:
        """Load image metadata from cache if available and valid."""
        if not self.image_config.cache_enabled or not self.vision_api_available:
            logger.debug(f"Cache disabled or Vision API unavailable for {image_path.name}")
            return None

        cache_path = self._get_cache_path(image_path)
        if not cache_path.exists():
            logger.debug(f"No cache found for {image_path.name}")
            return None

        try:
            with open(cache_path, 'r') as f:
                data = json.load(f)
            
            # Check cache expiration
            if time.time() - data['created_at'] > self.image_config.cache_duration:
                logger.info(f"Cache expired for {image_path.name}")
                cache_path.unlink()
                return None

            # Ensure cache has description
            if not data.get('description'):
                logger.info(f"Invalid cache entry for {image_path.name} (no description)")
                cache_path.unlink()
                return None

            logger.info(f"Using cached data for {image_path.name}")
            return ImageMetadata(**data)
        except Exception as e:
            logger.warning(f"Failed to load cache for {image_path.name}: {e}")
            try:
                cache_path.unlink()
            except Exception:
                pass
            return None

    def _save_to_cache(self, image_path: Path, metadata: ImageMetadata) -> None:
        """Save image metadata to cache."""
        if not self.image_config.cache_enabled:
            return

        # Don't cache results without descriptions
        if metadata.description is None:
            logger.debug(f"Skipping cache for {image_path} - no description available")
            return

        cache_path = self._get_cache_path(image_path)
        try:
            with open(cache_path, 'w') as f:
                json.dump(metadata.__dict__, f)
        except Exception as e:
            logger.warning(f"Failed to save cache for {image_path}: {e}")

    def _validate_vision_api(self) -> None:
        """Validate access to OpenAI Vision API."""
        try:
            logger.debug("Testing OpenAI Vision API access")
            
            # Test with a tiny 1x1 transparent PNG
            test_image = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
            
            response = self.openai_client.chat.completions.create(
                model="gpt-4-turbo-2024-04-09",  # Use turbo model for testing
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Test image."
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{test_image}",
                                "detail": "low"
                            }
                        }
                    ]
                }],
                max_tokens=1
            )
            
            self.vision_api_available = True
            logger.info("OpenAI Vision API access validated")
            
        except Exception as e:
            self.vision_api_available = False
            logger.error("OpenAI Vision API validation failed", extra={
                "error": str(e),
                "error_type": type(e).__name__,
                "has_response": hasattr(e, 'response')
            })

    def _generate_image_description(self, image_path: Path) -> Optional[str]:
        """Generate a description for an image using OpenAI's vision model.
        
        Args:
            image_path: Path to the image file
            
        Returns:
            Generated description or None if generation failed
        """
        if not self.vision_api_available or not self.openai_client:
            return None
            
        try:
            # Read image and encode as base64
            with open(image_path, "rb") as image_file:
                base64_image = base64.b64encode(image_file.read()).decode('utf-8')
                
            response = self.openai_client.chat.completions.create(
                model="gpt-4-turbo-2024-04-09",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Please describe this image in detail, focusing on its key visual elements and composition."},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=500
            )
            
            self.stats['api_calls'] += 1
            description = response.choices[0].message.content
            if description:
                self.stats['descriptions_generated'] += 1
            return description
            
        except Exception as e:
            logger.error(f"Failed to generate image description: {str(e)}")
            self.stats['errors'] += 1
            return None

    def _format_markdown(self, metadata: ImageMetadata) -> str:
        """Format image metadata as markdown.
        
        Args:
            metadata: Image metadata to format
            
        Returns:
            str: Formatted markdown
        """
        lines = []
        
        # Add header with image name
        lines.extend([
            f"# Image: {Path(metadata.original_path).name}",
            ""
        ])
        
        # Add description in a collapsible section if available
        if metadata.description:
            lines.extend([
                "## Description",
                "",
                "<details>",
                "<summary>Click to expand description</summary>",
                "",
                metadata.description,
                "</details>",
                ""
            ])
        
        # Add technical metadata in a table
        lines.extend([
            "## Technical Details",
            "",
            "| Property | Value |",
            "|----------|-------|",
            f"| Original Format | {Path(metadata.original_path).suffix[1:].upper()} |",
            f"| Processed Format | {metadata.format} |",
            f"| Dimensions | {metadata.width}x{metadata.height} pixels |",
            f"| File Size | {metadata.size / 1024:.1f} KB |",
            f"| Processing Time | {metadata.processing_time:.2f} seconds |",
            ""
        ])
        
        # Add processing status
        if metadata.error:
            lines.extend([
                "## Processing Errors",
                "",
                f"⚠️ {metadata.error}",
                ""
            ])
        elif not metadata.description and self.openai_client:
            if self.vision_api_available:
                lines.extend([
                    "## Note",
                    "",
                    "*Image description generation failed. Please check the logs for details.*",
                    ""
                ])
            else:
                lines.extend([
                    "## Note",
                    "",
                    "*Image description not available - Vision API access required.*",
                    ""
                ])
        
        return "\n".join(lines)

    def process_image(self, input_path: Path, output_dir: Path) -> Optional[ImageMetadata]:
        """Process a single image file.
        
        Args:
            input_path: Path to input image
            output_dir: Directory for processed images
            
        Returns:
            ImageMetadata if successful, None if processing failed
        """
        start_time = time.time()
        self.stats['total_processed'] += 1
        
        try:
            # Check cache first
            cached_metadata = self._load_from_cache(input_path)
            if cached_metadata:
                self.stats['cache_hits'] += 1
                return cached_metadata
            
            # Convert HEIC to PNG if needed
            if input_path.suffix.lower() in ['.heic', '.heif']:
                try:
                    # Convert HEIC to PNG using sips
                    temp_path = self.image_config.original_dir / f"{input_path.stem}.png"
                    subprocess.run(['sips', '-s', 'format', 'png', str(input_path), '--out', str(temp_path)], check=True)
                    # Track the conversion
                    self.converted_files[input_path.name] = temp_path.name
                    input_path = temp_path  # Use the temp file for further processing
                    self.stats['heic_conversions'] += 1
                    
                except Exception as e:
                    console.print(f"[error]HEIC conversion failed:[/] {str(e)}")
                    raise ProcessingError(f"Failed to convert HEIC file: {str(e)}")
            
            # Open and process image
            with Image.open(input_path) as img:
                # Get original dimensions
                orig_width, orig_height = img.size
                
                # Calculate new dimensions
                max_width = self.image_config.max_width
                max_height = self.image_config.max_height
                
                if orig_width > max_width or orig_height > max_height:
                    # Calculate aspect ratio
                    ratio = min(max_width/orig_width, max_height/orig_height)
                    new_width = int(orig_width * ratio)
                    new_height = int(orig_height * ratio)
                    
                    # Resize image
                    img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                    console.print(f"Resizing image {orig_width}x{orig_height} → max {max_width}x{max_height}")
                
                # Save processed image
                output_path = output_dir / f"{input_path.stem}.png"
                img.save(output_path, format='PNG', optimize=True)
                
                # Track the conversion if the extension changed
                if input_path.suffix.lower() != '.png':
                    self.converted_files[input_path.name] = output_path.name
                
                # Get image description
                description = self._generate_image_description(output_path)
                if description:
                    self.stats['descriptions_generated'] += 1
                else:
                    console.print("No description generated")
                
                # Create metadata
                metadata = ImageMetadata(
                    original_path=str(input_path),
                    processed_path=str(output_path),
                    description=description,
                    width=img.width,
                    height=img.height,
                    format='PNG',
                    size=output_path.stat().st_size,
                    created_at=time.time(),
                    processing_time=time.time() - start_time
                )
                
                # Save metadata
                metadata_path = self.image_config.metadata_dir / f"{input_path.stem}.json"
                with open(metadata_path, 'w') as f:
                    json.dump(metadata.__dict__, f, indent=2)
                
                # Save to cache if we have a description
                if description:
                    self._save_to_cache(input_path, metadata)
                
                # Log processing stats
                orig_size = input_path.stat().st_size
                size_change = (metadata.size - orig_size) / orig_size * 100
                console.print(
                    f"Image processed: {orig_width}x{orig_height} → {img.width}x{img.height}, "
                    f"{orig_size/1024:.1f}KB → {metadata.size/1024:.1f}KB "
                    f"({size_change:.1f}% {'larger' if size_change > 0 else 'smaller'})"
                )
                
                return metadata
                
        except Exception as e:
            logger.error(f"Failed to process image {input_path}: {str(e)}")
            return None

    def _cleanup_temp_files(self, temp_files: List[Path]) -> None:
        """Clean up temporary files only."""
        for temp_file in temp_files:
            try:
                if temp_file.exists():
                    temp_file.unlink()
                    logger.debug(f"Cleaned up temporary file: {temp_file}")
            except Exception as e:
                logger.warning(f"Failed to clean up temporary file {temp_file}: {e}")

    def _process_image(self, input_path: Path, output_path: Path) -> Path:
        """Process an image file."""
        if input_path.suffix.lower() == '.heic':
            # Convert HEIC to PNG using sips
            png_output = output_path.with_suffix('.png')
            subprocess.run(['sips', '-s', 'format', 'png', str(input_path), '--out', str(png_output)], check=True)
            return png_output
        else:
            # Copy other images as is
            shutil.copy2(input_path, output_path)
            return output_path