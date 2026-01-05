"""
Video processing module for extracting frames and uploading to Azure Blob Storage.
"""

import asyncio
import base64
import io
import logging
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import List

import cv2
from azure.identity.aio import AzureDeveloperCliCredential, ManagedIdentityCredential
from azure.storage.blob.aio import BlobServiceClient

logger = logging.getLogger(__name__)


class VideoProcessor:
    """Handles video frame extraction and processing."""

    def __init__(self, fps: float = 1.0):
        """
        Initialize video processor.

        Args:
            fps: Frames per second to extract (default: 1.0)
        """
        self.fps = fps

    async def extract_frames(self, video_path: str) -> List[str]:
        """
        Extract frames from video at specified FPS rate.

        Args:
            video_path: Path to video file

        Returns:
            List of base64-encoded frames as data URLs
        """
        # Run blocking CV2 operations in thread pool
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._extract_frames_sync, video_path)

    def _extract_frames_sync(self, video_path: str) -> List[str]:
        """Synchronous frame extraction (runs in thread pool)."""
        frames_base64 = []

        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                raise ValueError(f"Cannot open video file: {video_path}")

            video_fps = cap.get(cv2.CAP_PROP_FPS)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            duration = total_frames / video_fps if video_fps > 0 else 0

            # Calculate frame interval
            frame_interval = int(video_fps / self.fps) if video_fps > 0 else 1

            logger.info(f"Video: {duration:.2f}s, {video_fps} fps, extracting at {self.fps} fps")

            frame_count = 0
            extracted_count = 0

            while True:
                ret, frame = cap.read()
                if not ret:
                    break

                # Extract frame at specified interval
                if frame_count % frame_interval == 0:
                    # Encode frame to JPEG
                    _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    frame_base64 = base64.b64encode(buffer).decode('utf-8')
                    frames_base64.append(f"data:image/jpeg;base64,{frame_base64}")
                    extracted_count += 1

                frame_count += 1

            cap.release()
            logger.info(f"Extracted {extracted_count} frames from {frame_count} total frames")

        except Exception as e:
            logger.error(f"Frame extraction error: {e}")
            raise

        return frames_base64


class AzureBlobStorageHandler:
    """Handles Azure Blob Storage operations with managed identity."""

    def __init__(self):
        self.container_name = os.getenv("AZURE_STORAGE_CONTAINER_NAME", "videos")
        self.account_url = os.getenv("AZURE_STORAGE_ACCOUNT_URL")
        self.blob_service_client: BlobServiceClient | None = None

    async def initialize(self):
        """Initialize blob service client with authentication."""
        if not self.account_url:
            raise ValueError("AZURE_STORAGE_ACCOUNT_URL environment variable not set")

        # Use same authentication pattern as OpenAI
        if os.getenv("RUNNING_IN_PRODUCTION"):
            client_id = os.environ["AZURE_CLIENT_ID"]
            credential = ManagedIdentityCredential(client_id=client_id)
            logger.info(f"Using managed identity for Blob Storage: {client_id}")
        else:
            tenant_id = os.environ["AZURE_TENANT_ID"]
            credential = AzureDeveloperCliCredential(tenant_id=tenant_id)
            logger.info(f"Using az CLI credential for Blob Storage: {tenant_id}")

        self.blob_service_client = BlobServiceClient(
            account_url=self.account_url,
            credential=credential
        )

        # Ensure container exists
        try:
            container_client = self.blob_service_client.get_container_client(self.container_name)
            if not await container_client.exists():
                await container_client.create_container()
                logger.info(f"Created container: {self.container_name}")
        except Exception as e:
            logger.warning(f"Container creation check failed: {e}")

    async def upload_video(
        self,
        file_stream: io.BytesIO,
        filename: str,
        content_type: str = "video/mp4"
    ) -> str:
        """
        Upload video to blob storage.

        Args:
            file_stream: Video file bytes
            filename: Original filename
            content_type: MIME type

        Returns:
            Blob URL
        """
        if not self.blob_service_client:
            await self.initialize()

        # Generate unique blob name
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        blob_name = f"{timestamp}-{unique_id}-{filename}"

        blob_client = self.blob_service_client.get_blob_client(
            container=self.container_name,
            blob=blob_name
        )

        try:
            # Upload with streaming for large files
            await blob_client.upload_blob(
                file_stream,
                blob_type="BlockBlob",
                content_settings={
                    "content_type": content_type
                },
                overwrite=True
            )

            logger.info(f"Uploaded video to blob: {blob_name}")
            return blob_client.url

        except Exception as e:
            logger.error(f"Blob upload failed: {e}")
            raise

    async def close(self):
        """Close blob service client."""
        if self.blob_service_client:
            await self.blob_service_client.close()
