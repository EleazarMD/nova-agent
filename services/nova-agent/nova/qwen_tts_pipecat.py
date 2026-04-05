"""
Qwen TTS Service for Pipecat

Custom TTS service that connects to the local Qwen3-TTS server (port 4200).
"""

import os
import aiohttp
from typing import AsyncGenerator, Optional
from loguru import logger

from pipecat.frames.frames import (
    Frame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    ErrorFrame,
)
from pipecat.services.tts_service import TTSService

QWEN_TTS_URL = os.environ.get("QWEN_TTS_URL", "http://localhost:4200")


class QwenTTSService(TTSService):
    """Pipecat TTS service using local Qwen3-TTS server."""

    def __init__(
        self,
        *,
        voice: str = "american_female_warm",
        api_url: str = QWEN_TTS_URL,
        sample_rate: int = 24000,
        **kwargs,
    ):
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._voice = voice
        self._api_url = api_url
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self, frame: Frame):
        await super().start(frame)
        self._session = aiohttp.ClientSession()

    async def stop(self, frame: Frame):
        await super().stop(frame)
        if self._session:
            await self._session.close()
            self._session = None

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        """Generate speech from text using Qwen TTS."""
        logger.debug(f"Qwen TTS: Synthesizing with voice {self._voice} (context: {context_id})")

        yield TTSStartedFrame()

        try:
            if not self._session:
                self._session = aiohttp.ClientSession()

            payload = {
                "text": text,
                "voice_id": self._voice,
                "temperature": 0.3,
                "top_p": 0.8,
            }

            async with self._session.post(
                f"{self._api_url}/api/tts/synthesize",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Qwen TTS error: {resp.status} - {error_text}")
                    yield ErrorFrame(f"TTS failed: {resp.status}")
                    yield TTSStoppedFrame()
                    return

                audio_data = await resp.read()
                
                # Skip WAV header (44 bytes) to get raw PCM
                if len(audio_data) > 44:
                    pcm_data = audio_data[44:]
                    yield TTSAudioRawFrame(
                        audio=pcm_data,
                        sample_rate=self._sample_rate,
                        num_channels=1,
                    )

        except Exception as e:
            logger.error(f"Qwen TTS error: {e}")
            yield ErrorFrame(f"TTS error: {e}")

        yield TTSStoppedFrame()

    def can_generate_metrics(self) -> bool:
        return False
