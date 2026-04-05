"""
Qwen3-TTS Integration for Nova Voice Agent

Provides voice cloning capabilities using Qwen3-TTS with Voice Studio cloned voices.
Integrates with the ecosystem dashboard's Voice Studio for voice management.
"""

import os
import aiohttp
import asyncio
from typing import Optional, Dict, List
from loguru import logger

QWEN_TTS_API = os.environ.get("QWEN_TTS_API", "http://localhost:4200")
QWEN_TTS_GATEWAY = os.environ.get("QWEN_TTS_GATEWAY", "http://localhost:8404/api/ai-gateway/qwen-tts")


class Qwen3TTSService:
    """Service for interacting with Qwen3-TTS voice cloning."""
    
    def __init__(self):
        self.api_url = QWEN_TTS_API
        self.gateway_url = QWEN_TTS_GATEWAY
        self._library_voices: Dict = {}
        self._custom_voices: List[Dict] = []
        
    async def initialize(self):
        """Initialize and fetch available voices from Voice Studio."""
        try:
            await self._fetch_library_voices()
            await self._fetch_custom_voices()
            logger.info(f"Qwen3-TTS initialized: {len(self._library_voices)} library voices, {len(self._custom_voices)} custom voices")
        except Exception as e:
            logger.error(f"Failed to initialize Qwen3-TTS: {e}")
    
    async def _fetch_library_voices(self):
        """Fetch library voices (Gemini-cloned voices from Voice Studio)."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.gateway_url}?action=library-voices") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self._library_voices = data.get("voices", {})
                        logger.debug(f"Loaded {len(self._library_voices)} library voices")
        except Exception as e:
            logger.warning(f"Failed to fetch library voices: {e}")
    
    async def _fetch_custom_voices(self):
        """Fetch custom user-created voices from Voice Studio."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.gateway_url}?action=voices") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self._custom_voices = data.get("voices", [])
                        logger.debug(f"Loaded {len(self._custom_voices)} custom voices")
        except Exception as e:
            logger.warning(f"Failed to fetch custom voices: {e}")
    
    async def synthesize(
        self,
        text: str,
        voice_id: Optional[str] = None,
        language: str = "Auto",
        temperature: float = 0.7,
        top_p: float = 0.9
    ) -> bytes:
        """
        Synthesize speech using Qwen3-TTS voice cloning.
        
        Args:
            text: Text to synthesize
            voice_id: Voice ID from Voice Studio (library or custom)
            language: Language for synthesis (Auto, English, Spanish, etc.)
            temperature: Sampling temperature (0.0-2.0)
            top_p: Top-p sampling (0.0-1.0)
            
        Returns:
            Audio bytes (WAV format)
        """
        try:
            # Use gateway for synthesis with voice cloning
            async with aiohttp.ClientSession() as session:
                payload = {
                    "text": text,
                    "mode": "custom-voice",
                    "speaker": voice_id or "american_female_warm",  # Default voice
                    "language": language,
                    "temperature": temperature,
                    "top_p": top_p,
                }
                
                async with session.post(
                    self.gateway_url,
                    json=payload,
                    headers={"Content-Type": "application/json"}
                ) as resp:
                    if resp.status == 200:
                        audio_data = await resp.read()
                        logger.debug(f"Synthesized {len(audio_data)} bytes for voice_id={voice_id}")
                        return audio_data
                    else:
                        error_text = await resp.text()
                        logger.error(f"TTS synthesis failed: {resp.status} - {error_text}")
                        raise Exception(f"TTS synthesis failed: {resp.status}")
        except Exception as e:
            logger.error(f"Qwen3-TTS synthesis error: {e}")
            raise
    
    async def get_available_voices(self) -> Dict:
        """
        Get all available voices (library + custom).
        
        Returns:
            Dict with 'library' and 'custom' voice lists
        """
        # Refresh voice lists
        await self._fetch_library_voices()
        await self._fetch_custom_voices()
        
        return {
            "library": self._library_voices,
            "custom": self._custom_voices,
        }
    
    async def clone_voice(
        self,
        text: str,
        reference_audio_path: str,
        ref_text: str = "",
        language: str = "Auto",
        temperature: float = 0.7,
        top_p: float = 0.9
    ) -> bytes:
        """
        Clone a voice from reference audio (for real-time voice cloning).
        
        Args:
            text: Text to synthesize
            reference_audio_path: Path to reference audio file
            ref_text: Transcript of reference audio (optional, improves quality)
            language: Language for synthesis
            temperature: Sampling temperature
            top_p: Top-p sampling
            
        Returns:
            Audio bytes (WAV format)
        """
        try:
            async with aiohttp.ClientSession() as session:
                # Create multipart form data
                data = aiohttp.FormData()
                data.add_field('text', text)
                data.add_field('language', language)
                data.add_field('ref_text', ref_text)
                data.add_field('temperature', str(temperature))
                data.add_field('top_p', str(top_p))
                
                # Add reference audio file
                with open(reference_audio_path, 'rb') as f:
                    data.add_field(
                        'reference_audio',
                        f,
                        filename='reference.wav',
                        content_type='audio/wav'
                    )
                
                async with session.post(
                    f"{self.api_url}/api/tts/voice-clone",
                    data=data
                ) as resp:
                    if resp.status == 200:
                        audio_data = await resp.read()
                        logger.debug(f"Cloned voice: {len(audio_data)} bytes")
                        return audio_data
                    else:
                        error_text = await resp.text()
                        logger.error(f"Voice cloning failed: {resp.status} - {error_text}")
                        raise Exception(f"Voice cloning failed: {resp.status}")
        except Exception as e:
            logger.error(f"Voice cloning error: {e}")
            raise


# Global instance
_qwen_tts_service: Optional[Qwen3TTSService] = None


async def get_qwen_tts_service() -> Qwen3TTSService:
    """Get or create the global Qwen3-TTS service instance."""
    global _qwen_tts_service
    if _qwen_tts_service is None:
        _qwen_tts_service = Qwen3TTSService()
        await _qwen_tts_service.initialize()
    return _qwen_tts_service


async def synthesize_speech(
    text: str,
    voice_id: Optional[str] = None,
    language: str = "Auto"
) -> bytes:
    """
    Convenience function for speech synthesis.
    
    Args:
        text: Text to synthesize
        voice_id: Voice ID from Voice Studio
        language: Language for synthesis
        
    Returns:
        Audio bytes (WAV format)
    """
    service = await get_qwen_tts_service()
    return await service.synthesize(text, voice_id, language)
