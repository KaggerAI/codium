"""
budget_live.py — Live Union Budget Transcription & Analysis Module

This module handles real-time audio transcription and AI analysis of the
Union Budget speech using Google's Gemini API with chunk-based audio processing.

Features:
- Chunk-based audio transcription via Gemini 2.5 Flash
- AI-powered extraction of key budget announcements
- Sector-wise impact analysis
- Tax and fiscal policy summarization
"""

import os
import json
import base64
import tempfile
import time
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, field
from datetime import datetime
import traceback
from io import BytesIO

# Standard Gemini API (New v1.0 SDK)
from google import genai
from google.genai import types

# Configuration
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
TRANSCRIPTION_MODEL = "gemini-3.5-flash-lite"
ANALYSIS_MODEL = "gemini-3.5-flash-lite"
THINKING_LEVEL = "MEDIUM"

# Configure Gemini on module load
genai_client = None
if GOOGLE_API_KEY:
    try:
        genai_client = genai.Client(api_key=GOOGLE_API_KEY)
    except Exception as e:
        print(f"ERROR: Failed to initialize Gemini client in budget_live: {e}")

# Budget-specific system instruction for better context
BUDGET_SYSTEM_INSTRUCTION = """You are an expert Indian financial analyst specializing in conducting analysis of the 2026 Union Budget of India.
Your task is to transcribe and analyze the live Union Budget speech.

When transcribing:
- Accurately capture all numbers, percentages, and amounts mentioned (in crores, lakhs, etc.)
- Note sector names, scheme names, and policy announcements
- Capture Hindi terms with their English equivalents when relevant
- Focus on financial figures, tax rates, and budget allocations

When analyzing, focus on:
1. Tax changes (income tax slabs, corporate tax, GST, customs duty, excise)
2. Sector allocations and Capex (Defense, Railways, Healthcare, Education, Infrastructure, Agriculture)
3. New schemes (like PLI, PM-KISAN, etc.) and initiatives with their budgetary allocation
4. Fiscal targets (deficit targets, borrowing, GDP growth projections)
5. Market implications for different sectors (Banking, IT, Pharma, Auto, FMCG, etc.)
6. Changes in government policies and regulations
"""


@dataclass
class BudgetHighlight:
    """Represents a key budget announcement"""
    category: str  # 'tax', 'sector', 'capex', 'pli', 'financing', 'fiscal', 'regulation', 'other'
    title: str
    details: str
    impact: str  # 'positive', 'negative', 'neutral'
    sectors_affected: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass 
class SectorImpact:
    """Represents the budget's impact on a specific sector"""
    sector: str
    impact_type: str  # 'positive', 'negative', 'neutral', 'mixed'
    summary: str
    key_points: List[str] = field(default_factory=list)


class BudgetLiveSession:
    """
    Manages a live budget transcription session using chunk-based processing.
    
    Handles:
    - Audio chunk transcription via Gemini
    - Transcript accumulation
    - Periodic analysis and highlight extraction
    """
    
    def __init__(self, session_id: str):
        self.session_id = session_id
        
        self.transcript_buffer: List[str] = []
        self.full_transcript: str = ""
        self.highlights: List[BudgetHighlight] = []
        self.sector_impacts: Dict[str, SectorImpact] = {}
        
        self.is_active = False
        self.start_time = None
        
        # Analysis state
        self.last_analysis_length = 0
        self.analysis_threshold = 500  # Analyze every ~500 chars of new content
        self.chunk_count = 0
        
        # Gemini model instance
        self.model = None
        
    def start(self) -> bool:
        """Initialize the transcription session"""
        if not GOOGLE_API_KEY:
            raise RuntimeError("GOOGLE_API_KEY environment variable not set")
        
        try:
            # Note: client is now global to the module
            self.is_active = True
            self.start_time = datetime.now()
            print(f"INFO: Budget Live session {self.session_id} started at {self.start_time}")
            return True
            
        except Exception as e:
            print(f"ERROR: Failed to start Budget Live session: {e}")
            traceback.print_exc()
            raise
    
    def process_audio_chunk(self, audio_data: bytes, mime_type: str = "audio/webm") -> Dict[str, Any]:
        """
        Process an incoming audio chunk from the browser.
        
        Uses inline audio data with gemini-2.0-flash for audio transcription.
        
        Args:
            audio_data: Raw audio bytes from MediaRecorder
            mime_type: Audio format (default: audio/webm from browser)
        
        Returns:
            Dict with transcription and any new highlights
        """
        if not self.is_active:
            return {"error": "Session not active", "transcript": ""}
        
        result = {
            "transcript": "",
            "highlights": [],
            "sectors": {},
            "chunk_id": self.chunk_count
        }
        
        self.chunk_count += 1
        
        try:
            # Skip very small chunks (likely silence or noise)
            if len(audio_data) < 1000:  # Less than 1KB
                return result

            # Clean mime type (e.g., "audio/webm;codecs=opus" -> "audio/webm")
            clean_mime_type = mime_type.split(';')[0].strip()
            
            # Create inline audio data for Gemini
            # Format: audio first, then instruction (more reliable for audio processing)
            audio_part = {
                "inline_data": {
                    "mime_type": clean_mime_type,
                    "data": base64.b64encode(audio_data).decode('utf-8')
                }
            }
            
            # Use the new SDK for audio transcription
            global genai_client
            if not genai_client:
                raise ValueError("GenAI client not initialized")
                
            response = genai_client.models.generate_content(
                model=TRANSCRIPTION_MODEL,
                contents=[
                    types.Part.from_bytes(data=audio_data, mime_type=clean_mime_type),
                    types.Part.from_text(text=(
                        "Listen to this audio and transcribe the spoken words verbatim. "
                        "This is from an Indian government budget speech. "
                        "Output ONLY the transcription - no explanations or commentary. "
                        "If silent or unclear, say [SILENCE] or [UNCLEAR]."
                    ))
                ],
                config=types.GenerateContentConfig(
                    system_instruction=BUDGET_SYSTEM_INSTRUCTION,
                    temperature=0.0,
                    max_output_tokens=1500,
                    thinking_config=types.ThinkingConfig(thinking_level=THINKING_LEVEL)
                )
            )
            
            if response.text:
                transcript_text = response.text.strip()
                
                # Filter out error messages and refusals
                refusal_phrases = [
                    "I am sorry",
                    "I cannot",
                    "I'm unable",
                    "I am unable",
                    "text-based AI",
                    "cannot process audio",
                    "cannot transcribe",
                    "Please provide",
                    "I don't have",
                    "unable to process"
                ]
                
                is_refusal = any(phrase.lower() in transcript_text.lower() for phrase in refusal_phrases)
                
                if is_refusal:
                    print(f"DEBUG: Chunk {self.chunk_count} - Model refused, skipping")
                    return result
                
                # Valid transcription
                if transcript_text and transcript_text not in ["[UNCLEAR]", "[SILENCE]", ""]:
                    self.transcript_buffer.append(transcript_text)
                    self.full_transcript += " " + transcript_text
                    result["transcript"] = transcript_text
                    print(f"DEBUG: Chunk {self.chunk_count} transcribed: {transcript_text[:100]}...")
            
            # Trigger analysis if we have enough new content
            new_content_length = len(self.full_transcript) - self.last_analysis_length
            if new_content_length > self.analysis_threshold:
                analysis_result = self._analyze_content()
                if analysis_result:
                    result["highlights"] = [h.__dict__ for h in self.highlights[-5:]]
                    result["sectors"] = {k: v.__dict__ for k, v in self.sector_impacts.items()}
                self.last_analysis_length = len(self.full_transcript)
            
            return result
            
        except Exception as e:
            print(f"ERROR: Processing audio chunk {self.chunk_count}: {e}")
            traceback.print_exc()
            return {"error": str(e), "transcript": "", "chunk_id": self.chunk_count}
    
    def deep_scan(self) -> Dict[str, Any]:
        """Perform a comprehensive scan of the entire transcript (Admin only)"""
        print(f"INFO: Running Deep Scan for session {self.session_id} ({len(self.full_transcript)} chars)")
        # We pass full_scan=True to ensure the prompt considers the whole text
        self._analyze_content(full_scan=True, clear_existing=True)
        return self.get_state()

    def _analyze_content(self, full_scan: bool = False, clear_existing: bool = False) -> bool:
        """
        Internal method to analyze transcript and extract highlights/sectors.
        
        Args:
            full_scan: If True, analyzes the entire transcript. 
                       If False, analyzes only the most recent part.
            clear_existing: If True, clears existing highlights/sectors before adding new ones.
        """
        if not self.full_transcript or len(self.full_transcript) < 100:
            return False
        
        try:
            # Clear existing data if requested
            if clear_existing:
                self.highlights = []
                self.sector_impacts = {}

            # Use whole transcript for deep scan, otherwise last ~3000 chars
            analysis_text = self.full_transcript if full_scan else self.full_transcript[-3000:]
            
            prompt_type = "COMPREHENSIVE" if full_scan else "INCREMENTAL"
            analysis_prompt = f"""Analyze this Union Budget 2026 speech ({prompt_type} ANALYSIS) and extract detailed information.

TRANSCRIPT:
{analysis_text}

Extract and return as a single JSON object (Be extremely thorough, identify at least 20-25 items if the text allows):
- "highlights": Array of important announcements, each with:
    - "category": One of "tax", "sector", "capex", "pli", "financing", "fiscal", "regulation", "other"
    - "title": Brief title (max 10 words)
    - "details": Key details (max 50 words)
    - "impact": "positive", "negative", or "neutral" for markets
    - "sectors_affected": Array of affected sectors like ["Banking", "IT", "Pharma"]

- "sectors": Object mapping sector names to their impact (Identify all affected sectors, up to 25):
    - "impact_type": "positive", "negative", "neutral", or "mixed"
    - "summary": Brief summary (max 30 words)
    - "key_points": Array of 2-3 key points

{f"Analyze the entire text and extract ALL significant announcements (up to 25 key highlights)." if full_scan else "Only include NEW information not already covered. If nothing significant, return empty arrays/objects."}
"""
            
            global genai_client
            if not genai_client:
                return False

            response = genai_client.models.generate_content(
                model=ANALYSIS_MODEL,
                contents=analysis_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=BUDGET_SYSTEM_INSTRUCTION,
                    temperature=0.3,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_level=THINKING_LEVEL)
                )
            )
            
            if response.text:
                try:
                    # Parse the JSON response
                    analysis = json.loads(response.text)
                    
                    # Robustness check: Ensure response is a dictionary
                    if not isinstance(analysis, dict):
                        print(f"ERROR: AI analysis returned {type(analysis)} instead of dictionary. Value: {response.text[:500]}")
                        return False
                        
                    # Update highlights
                    highlights_data = analysis.get("highlights")
                    if isinstance(highlights_data, list):
                        for h in highlights_data:
                            if not isinstance(h, dict): continue
                            highlight = BudgetHighlight(
                                category=h.get("category", "other"),
                                title=h.get("title", ""),
                                details=h.get("details", ""),
                                impact=h.get("impact", "neutral"),
                                sectors_affected=h.get("sectors_affected", [])
                            )
                            # Avoid duplicates by checking title
                            if not any(existing.title == highlight.title for existing in self.highlights):
                                self.highlights.append(highlight)
                                print(f"DEBUG: New highlight: {highlight.title}")
                    
                    # Update sector impacts
                    sectors_data = analysis.get("sectors")
                    if isinstance(sectors_data, dict):
                        for sector, impact_data in sectors_data.items():
                            if not isinstance(impact_data, dict): continue
                            self.sector_impacts[sector] = SectorImpact(
                                sector=sector,
                                impact_type=impact_data.get("impact_type", "neutral"),
                                summary=impact_data.get("summary", ""),
                                key_points=impact_data.get("key_points", [])
                            )
                    return True
                except Exception as e:
                    print(f"ERROR: Failed to process AI analysis JSON: {e}")
                    traceback.print_exc()
                    return False
                            
        except json.JSONDecodeError as je:
            print(f"WARN: JSON parse error in analysis: {je}")
        except Exception as e:
            print(f"WARN: Analysis failed: {e}")
            traceback.print_exc()
        
        return False
    
    def stop(self) -> Dict[str, Any]:
        """Stop the session and return summary"""
        self.is_active = False
        
        duration = 0
        if self.start_time:
            duration = (datetime.now() - self.start_time).total_seconds()
        
        print(f"INFO: Budget Live session {self.session_id} stopped. "
              f"Duration: {duration:.1f}s, Chunks: {self.chunk_count}, "
              f"Transcript: {len(self.full_transcript)} chars")
        
        return {
            "session_id": self.session_id,
            "duration_seconds": duration,
            "chunks_processed": self.chunk_count,
            "transcript_length": len(self.full_transcript),
            "highlights_count": len(self.highlights),
            "sectors_analyzed": list(self.sector_impacts.keys())
        }
    
    def get_summary(self) -> Dict[str, Any]:
        """Get current session summary"""
        return {
            "session_id": self.session_id,
            "is_active": self.is_active,
            "chunks_processed": self.chunk_count,
            "transcript_length": len(self.full_transcript),
            "transcript_preview": self.full_transcript[-500:] if self.full_transcript else "",
            "highlights": [h.__dict__ for h in self.highlights],
            "sectors": {k: v.__dict__ for k, v in self.sector_impacts.items()}
        }
    
    def get_full_transcript(self) -> str:
        """Get the complete transcript"""
        return self.full_transcript

    def get_state(self) -> Dict[str, Any]:
        """Returns current state of the session for new joins/sync"""
        return {
            "full_transcript": self.full_transcript,
            "highlights": [h.__dict__ for h in self.highlights],
            "sectors": {k: v.__dict__ for k, v in self.sector_impacts.items()},
            "is_active": self.is_active,
            "chunk_count": self.chunk_count,
            "duration": (datetime.now() - self.start_time).total_seconds() if self.start_time else 0
        }

    def ingest_full_transcript(self, text: str) -> Dict[str, Any]:
        """Ingests a full transcript and re-analyzes everything (Admin only)"""
        # Save old length to trigger analysis properly
        self.full_transcript = text
        self.chunk_count += 1 # Increment for signaling
        
        # Trigger COMPREHENSIVE analysis and CLEAR old results
        self._analyze_content(full_scan=True, clear_existing=True)
        return self.get_state()

    def get_ai_context(self) -> str:
        """
        Consolidates the entire session context into a single string for AI chat.
        Includes full transcript, all extracted highlights, and sector impacts.
        """
        context_parts = []
        
        context_parts.append("=== BUDGET TRANSCRIPT (Full) ===")
        context_parts.append(self.full_transcript if self.full_transcript else "[No transcript yet]")
        
        if self.highlights:
            context_parts.append("\n=== EXTRACTED KEY HIGHLIGHTS ===")
            for h in self.highlights:
                context_parts.append(f"- [{h.category.upper()}] {h.title}: {h.details} (Impact: {h.impact})")
        
        if self.sector_impacts:
            context_parts.append("\n=== SECTOR IMPACTS ===")
            for sector, data in self.sector_impacts.items():
                context_parts.append(f"- {sector}: {data.impact_type.upper()} - {data.summary}")
        
        return "\n".join(context_parts)


def transcribe_audio_simple(audio_bytes: bytes, mime_type: str = "audio/webm") -> str:
    """
    Simple one-shot audio transcription using Gemini.
    Useful for testing or processing recorded audio.
    
    Args:
        audio_bytes: Raw audio data
        mime_type: Audio format
    
    Returns:
        Transcribed text
    """
    if not GOOGLE_API_KEY:
        return "[No API key configured]"
    
    try:
        global genai_client
        if not genai_client:
            return "[Client not initialized]"
            
        response = genai_client.models.generate_content(
            model=TRANSCRIPTION_MODEL,
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                types.Part.from_text(text="Transcribe this audio accurately. Output only the spoken words.")
            ],
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level=THINKING_LEVEL)
            )
        )
        
        return response.text if response.text else ""
        
    except Exception as e:
        print(f"ERROR: Simple transcription failed: {e}")
        return f"[Transcription error: {e}]"


def analyze_budget_text(text: str) -> Dict[str, Any]:
    """
    Analyze budget speech text for key announcements.
    Standalone function for on-demand analysis.
    
    Args:
        text: Budget speech text to analyze
    
    Returns:
        Dict with highlights, tax_changes, and sector_impacts
    """
    if not text or len(text) < 50:
        return {"error": "Insufficient text for analysis"}
    
    if not GOOGLE_API_KEY:
        return {"error": "No API key configured"}
    
    try:
        global genai_client
        if not genai_client:
            return {"error": "Client not initialized"}
            
        response = genai_client.models.generate_content(
            model=ANALYSIS_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(thinking_level=THINKING_LEVEL)
            )
        )
        
        if response.text:
            return json.loads(response.text)
        return {"error": "Empty response from API"}
        
    except json.JSONDecodeError as je:
        print(f"ERROR: JSON parse error: {je}")
        return {"error": "Failed to parse analysis response"}
    except Exception as e:
        print(f"ERROR: Budget analysis failed: {e}")
        return {"error": str(e)}


# Session storage (in-memory for single-worker, would need Redis for multi-worker)
_active_sessions: Dict[str, BudgetLiveSession] = {}


def get_session(session_id: str) -> Optional[BudgetLiveSession]:
    """Get an active session by ID"""
    return _active_sessions.get(session_id)


def create_session(session_id: str) -> BudgetLiveSession:
    """Create a new session"""
    session = BudgetLiveSession(session_id)
    _active_sessions[session_id] = session
    return session


def remove_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Remove a session and return its summary"""
    session = _active_sessions.pop(session_id, None)
    if session:
        return session.stop()
    return None
