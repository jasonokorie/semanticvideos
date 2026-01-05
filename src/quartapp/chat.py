import io
import json
import os
import tempfile
from pathlib import Path

from azure.identity.aio import AzureDeveloperCliCredential, ManagedIdentityCredential, get_bearer_token_provider
from openai import AsyncOpenAI
from quart import (
    Blueprint,
    Response,
    current_app,
    render_template,
    request,
    stream_with_context,
)
from werkzeug.utils import secure_filename

from .video_handler import AzureBlobStorageHandler, VideoProcessor

bp = Blueprint("chat", __name__, template_folder="templates", static_folder="static")

# Video processing configuration
MAX_VIDEO_SIZE_MB = 2048  # 2 GB
ALLOWED_VIDEO_EXTENSIONS = {'.mp4'}
ALLOWED_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg'}


@bp.before_app_serving
async def configure_openai():
    bp.model_name = os.getenv("OPENAI_MODEL", "gpt-4o")
    openai_host = os.getenv("OPENAI_HOST", "github")

    if openai_host == "local":
        bp.openai_client = AsyncOpenAI(api_key="no-key-required", base_url=os.getenv("LOCAL_OPENAI_ENDPOINT"))
        current_app.logger.info("Using local OpenAI-compatible API service with no key")
    elif openai_host == "github":
        bp.model_name = f"openai/{bp.model_name}"
        bp.openai_client = AsyncOpenAI(
            api_key=os.environ["GITHUB_TOKEN"],
            base_url="https://models.github.ai/inference",
        )
        current_app.logger.info("Using GitHub models with GITHUB_TOKEN as key")
    elif os.getenv("AZURE_OPENAI_KEY_FOR_CHATVISION"):
        # Authenticate using an Azure OpenAI API key
        # This is generally discouraged, but is provided for developers
        # that want to develop locally inside the Docker container.
        bp.openai_client = AsyncOpenAI(
            base_url=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.getenv("AZURE_OPENAI_KEY_FOR_CHATVISION"),
        )
        current_app.logger.info("Using Azure OpenAI with key")
    elif os.getenv("RUNNING_IN_PRODUCTION"):
        client_id = os.environ["AZURE_CLIENT_ID"]
        azure_credential = ManagedIdentityCredential(client_id=client_id)
        token_provider = get_bearer_token_provider(azure_credential, "https://cognitiveservices.azure.com/.default")
        bp.openai_client = AsyncOpenAI(
            base_url=os.environ["AZURE_OPENAI_ENDPOINT"] + "/openai/v1/",
            api_key=token_provider,
        )
        current_app.logger.info("Using Azure OpenAI with managed identity credential for client ID %s", client_id)
    else:
        tenant_id = os.environ["AZURE_TENANT_ID"]
        azure_credential = AzureDeveloperCliCredential(tenant_id=tenant_id)
        token_provider = get_bearer_token_provider(azure_credential, "https://cognitiveservices.azure.com/.default")
        bp.openai_client = AsyncOpenAI(
            base_url=os.environ["AZURE_OPENAI_ENDPOINT"] + "/openai/v1/",
            api_key=token_provider,
        )
        current_app.logger.info("Using Azure OpenAI with az CLI credential for tenant ID: %s", tenant_id)
    current_app.logger.info("Using model %s", bp.model_name)

    # Initialize video handler
    bp.video_processor = VideoProcessor(fps=float(os.getenv("VIDEO_EXTRACT_FPS", "1.0")))
    bp.blob_storage = AzureBlobStorageHandler()
    if os.getenv("AZURE_STORAGE_ACCOUNT_URL"):
        await bp.blob_storage.initialize()
        current_app.logger.info("Azure Blob Storage initialized")


@bp.after_app_serving
async def shutdown_openai():
    await bp.openai_client.close()
    if hasattr(bp, 'blob_storage'):
        await bp.blob_storage.close()


@bp.get("/")
async def index():
    return await render_template("index.html")


@bp.post("/chat/video/upload")
async def video_upload_handler():
    """
    Handle video upload, extract frames, and store in blob storage.

    Returns:
        JSON with extracted frames and blob URL
    """
    try:
        files = await request.files

        if 'video' not in files:
            return {"error": "No video file provided"}, 400

        video_file = files['video']
        filename = secure_filename(video_file.filename)

        # Validate file extension
        file_ext = Path(filename).suffix.lower()
        if file_ext not in ALLOWED_VIDEO_EXTENSIONS:
            return {"error": f"Invalid file type. Only {ALLOWED_VIDEO_EXTENSIONS} allowed"}, 400

        # Read file into memory (with size validation)
        file_bytes = io.BytesIO()
        chunk_size = 8192
        total_size = 0
        max_size_bytes = MAX_VIDEO_SIZE_MB * 1024 * 1024

        while True:
            chunk = await video_file.read(chunk_size)
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > max_size_bytes:
                return {"error": f"File too large. Max size: {MAX_VIDEO_SIZE_MB} MB"}, 413
            file_bytes.write(chunk)

        file_bytes.seek(0)
        current_app.logger.info(f"Received video: {filename}, size: {total_size / (1024*1024):.2f} MB")

        # Upload to blob storage (if configured)
        blob_url = None
        if os.getenv("AZURE_STORAGE_ACCOUNT_URL"):
            blob_url = await bp.blob_storage.upload_video(file_bytes, filename)
            file_bytes.seek(0)  # Reset for frame extraction

        # Save to temporary file for frame extraction
        with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as temp_file:
            temp_file.write(file_bytes.read())
            temp_path = temp_file.name

        try:
            # Extract frames
            frames_base64 = await bp.video_processor.extract_frames(temp_path)

            if not frames_base64:
                return {"error": "No frames could be extracted from video"}, 422

            return {
                "success": True,
                "frames": frames_base64,
                "frame_count": len(frames_base64),
                "blob_url": blob_url,
                "filename": filename
            }

        finally:
            # Clean up temp file
            try:
                Path(temp_path).unlink()
            except Exception as e:
                current_app.logger.warning(f"Failed to delete temp file: {e}")

    except Exception as e:
        current_app.logger.error(f"Video upload error: {e}", exc_info=True)
        return {"error": str(e)}, 500


@bp.post("/chat/stream")
async def chat_handler():
    request_json = await request.get_json()
    request_messages = request_json["messages"]
    context = request_json.get("context", {})

    # Support both single image and multiple frames
    image = context.get("file")
    frames = context.get("frames", [])

    @stream_with_context
    async def response_stream():
        # This sends all messages, so API request may exceed token limits
        all_messages = [
            {"role": "system", "content": "You are a helpful assistant."},
        ] + request_messages[0:-1]
        all_messages = request_messages[0:-1]

        if frames:
            # Handle video frames - send multiple images
            user_content = [{"text": request_messages[-1]["content"], "type": "text"}]

            # Limit frames to avoid token limits (max 10 frames)
            max_frames = int(os.getenv("MAX_FRAMES_PER_REQUEST", "10"))
            selected_frames = frames[:max_frames]

            for idx, frame in enumerate(selected_frames):
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": frame, "detail": "auto"}
                })

            all_messages.append({"role": "user", "content": user_content})
            current_app.logger.info(f"Processing {len(selected_frames)} video frames")

        elif image:
            # Handle single image (existing behavior)
            user_content = []
            user_content.append({"text": request_messages[-1]["content"], "type": "text"})
            user_content.append({"image_url": {"url": image, "detail": "auto"}, "type": "image_url"})
            all_messages.append({"role": "user", "content": user_content})
        else:
            # Text only
            all_messages.append(request_messages[-1])

        chat_coroutine = bp.openai_client.chat.completions.create(
            # Azure Open AI takes the deployment name as the model name
            model=bp.model_name,
            messages=all_messages,
            stream=True,
            temperature=request_json.get("temperature", 0.5),
        )
        try:
            async for event in await chat_coroutine:
                event_dict = event.model_dump()
                if event_dict["choices"]:
                    yield json.dumps(event_dict["choices"][0], ensure_ascii=False) + "\n"
        except Exception as e:
            current_app.logger.error(e)
            yield json.dumps({"error": str(e)}, ensure_ascii=False) + "\n"

    return Response(response_stream())
