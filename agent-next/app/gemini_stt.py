"""Gemini 3.5 Transcribe adapter for existing audio files."""

from __future__ import annotations

import mimetypes
from pathlib import Path

import requests


FILES_URL = "https://generativelanguage.googleapis.com/upload/v1beta/files"
INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
MODEL = "gemini-3.5-transcribe"


class GeminiSTTError(RuntimeError):
    """Safe, non-secret-bearing Gemini transcription error."""


def _mime_type(path: Path) -> str:
    known = {
        ".aac": "audio/aac",
        ".aiff": "audio/aiff",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".mp4": "audio/mp4",
        ".oga": "audio/ogg",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
    }
    return known.get(path.suffix.lower()) or (
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )


def _response_json(response, error_code: str) -> dict:
    if response.status_code < 200 or response.status_code >= 300:
        raise GeminiSTTError(f"{error_code}_HTTP_{response.status_code}")
    try:
        data = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        raise GeminiSTTError(f"{error_code}_INVALID_RESPONSE") from None
    if not isinstance(data, dict):
        raise GeminiSTTError(f"{error_code}_INVALID_RESPONSE")
    return data


def _extract_text(data: dict) -> str:
    text = data.get("output_text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    # Tolerate structured output variants without assuming a fixed nesting.
    output = data.get("output")
    if isinstance(output, list):
        parts = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        parts.append(block["text"])
            elif isinstance(item.get("text"), str):
                parts.append(item["text"])
        combined = "\n".join(part.strip() for part in parts if part.strip())
        if combined:
            return combined

    raise GeminiSTTError("GEMINI_STT_EMPTY")


def transcribe_gemini_audio(
    audio_path: str | Path,
    api_key: str,
    timeout: float = 60.0,
) -> str:
    """Transcribe an existing audio file; never records audio or logs credentials."""
    key = (api_key or "").strip()
    if not key:
        raise GeminiSTTError("GEMINI_STT_KEY_MISSING")

    path = Path(audio_path)
    if not path.is_file():
        raise GeminiSTTError("GEMINI_STT_AUDIO_MISSING")
    try:
        size = path.stat().st_size
    except OSError:
        raise GeminiSTTError("GEMINI_STT_AUDIO_UNREADABLE") from None
    if size <= 0:
        raise GeminiSTTError("GEMINI_STT_AUDIO_EMPTY")

    mime = _mime_type(path)
    uploaded_name = None
    session = requests.Session()
    headers = {"x-goog-api-key": key}

    try:
        # Start a resumable upload.
        start = session.post(
            FILES_URL,
            headers={
                **headers,
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": mime,
                "Content-Type": "application/json",
            },
            json={"file": {"display_name": path.name}},
            timeout=timeout,
        )
        if start.status_code < 200 or start.status_code >= 300:
            raise GeminiSTTError(f"GEMINI_STT_UPLOAD_START_HTTP_{start.status_code}")

        upload_url = start.headers.get("x-goog-upload-url")
        if not upload_url:
            raise GeminiSTTError("GEMINI_STT_UPLOAD_URL_MISSING")

        try:
            with path.open("rb") as stream:
                content = stream.read()
        except OSError:
            raise GeminiSTTError("GEMINI_STT_AUDIO_UNREADABLE") from None

        uploaded = session.post(
            upload_url,
            headers={
                **headers,
                "Content-Length": str(len(content)),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
                "Content-Type": mime,
            },
            data=content,
            timeout=timeout,
        )
        uploaded_data = _response_json(uploaded, "GEMINI_STT_UPLOAD")
        file_info = uploaded_data.get("file", uploaded_data)
        if not isinstance(file_info, dict):
            raise GeminiSTTError("GEMINI_STT_UPLOAD_INVALID_RESPONSE")

        file_uri = file_info.get("uri")
        uploaded_name = file_info.get("name")
        if not isinstance(file_uri, str) or not file_uri:
            raise GeminiSTTError("GEMINI_STT_FILE_URI_MISSING")

        result = session.post(
            INTERACTIONS_URL,
            headers={**headers, "Content-Type": "application/json"},
            json={
                "model": MODEL,
                "input": [
                    {
                        "type": "audio",
                        "uri": file_uri,
                        "mime_type": mime,
                    }
                ],
            },
            timeout=timeout,
        )
        return _extract_text(_response_json(result, "GEMINI_STT_TRANSCRIBE"))

    except GeminiSTTError:
        raise
    except requests.RequestException:
        raise GeminiSTTError("GEMINI_STT_NETWORK_ERROR") from None
    except Exception:
        # Do not leak request details, file contents, or credentials.
        raise GeminiSTTError("GEMINI_STT_FAILED") from None
    finally:
        # Best-effort removal of the temporary remote upload.
        if uploaded_name and isinstance(uploaded_name, str):
            try:
                session.delete(
                    f"https://generativelanguage.googleapis.com/v1beta/{uploaded_name}",
                    headers=headers,
                    timeout=min(timeout, 15.0),
                )
            except Exception:
                pass
        session.close()
