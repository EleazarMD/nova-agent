"""
Text utilities for Nova Agent - Natural Speech Pipeline.

Phase 2 of Zero-Wait Ground-Truth Architecture:
- Markdown stripping for TTS
- List-to-speech conversion
- Abbreviation expansion
- Domain-specific transformations
"""

import re
from enum import Enum
from typing import Optional


class QueryDomain(str, Enum):
    """Query domains for domain-specific speech transformation."""
    PRODUCTIVITY = "productivity"
    NEWS = "news"
    TASKS = "tasks"
    KNOWLEDGE = "knowledge"
    GENERAL = "general"


# Comprehensive abbreviation dictionary
ABBREVIATIONS = {
    # Medical/Location
    'FAM MED': 'Family Medicine',
    'MT BELV': 'Mount Bellevue',
    'MT': 'Mount',
    'BELV': 'Bellevue',
    'DR': 'Doctor',
    'DR.': 'Doctor',
    'ST': 'Street',
    'ST.': 'Street',
    'AVE': 'Avenue',
    'BLVD': 'Boulevard',
    'RD': 'Road',
    'LN': 'Lane',
    'CT': 'Court',
    'PL': 'Place',
    
    # Time
    'AM': 'A.M.',
    'PM': 'P.M.',
    'EST': 'Eastern Time',
    'CST': 'Central Time',
    'MST': 'Mountain Time',
    'PST': 'Pacific Time',
    
    # Business
    'INC': 'Incorporated',
    'LLC': 'L L C',
    'CORP': 'Corporation',
    'CO': 'Company',
    'DEPT': 'Department',
    'DEV': 'Development',
    'ENG': 'Engineering',
    'HR': 'Human Resources',
    'IT': 'I.T.',
    'OPS': 'Operations',
    'PM': 'Project Manager',
    'QA': 'Quality Assurance',
    'R&D': 'Research and Development',
    
    # Common
    'ASAP': 'as soon as possible',
    'ETA': 'estimated arrival',
    'FYI': 'for your information',
    'IMO': 'in my opinion',
    'TBH': 'to be honest',
    'AKA': 'also known as',
    'DIY': 'do it yourself',
    'EOD': 'end of day',
    'TBD': 'to be determined',
    'TBA': 'to be announced',
    
    # Tech
    'API': 'A P I',
    'CPU': 'C P U',
    'DNS': 'D N S',
    'GUI': 'G U I',
    'HTTP': 'H T T P',
    'HTTPS': 'H T T P S',
    'IP': 'I P',
    'JSON': 'J S O N',
    'LLM': 'L L M',
    'NLP': 'N L P',
    'OCR': 'O C R',
    'SDK': 'S D K',
    'SQL': 'S Q L',
    'SSH': 'S S H',
    'TTS': 'T T S',
    'URL': 'U R L',
    'VPN': 'V P N',
    'WiFi': 'Wi-Fi',
    'AI': 'A I',
    'ML': 'M L',
    'RAG': 'R A G',
}


def strip_markdown_for_speech(text: str) -> str:
    """
    Strip markdown formatting to make text sound natural when spoken.
    
    Converts:
    - **bold** → bold
    - *italic* → italic
    - - list items → list items (without dash)
    - ## headers → headers
    - [links](url) → links
    - Emojis → removed (iOS TTS handles poorly)
    
    Preserves line breaks for natural pauses.
    """
    if not text:
        return text
    
    # Remove bold/italic markers
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # **bold**
    text = re.sub(r'\*([^*]+)\*', r'\1', text)      # *italic*
    text = re.sub(r'__([^_]+)__', r'\1', text)      # __bold__
    text = re.sub(r'_([^_]+)_', r'\1', text)        # _italic_
    
    # Remove headers
    text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
    
    # Convert list items to natural speech
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    
    # Remove links but keep text
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    
    # Remove emojis (they sound weird in TTS)
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "]+",
        flags=re.UNICODE
    )
    text = emoji_pattern.sub('', text)
    
    # Clean up extra whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)  # Max 2 newlines
    text = re.sub(r' {2,}', ' ', text)      # Max 1 space
    
    return text.strip()


def convert_lists_to_speech(text: str) -> str:
    """
    Convert bullet lists to natural conversational flow.
    
    Example:
    "You have three tasks:
    - Call the doctor
    - Pick up groceries
    - Finish the report"
    
    Becomes:
    "You have three tasks: First, call the doctor. Second, pick up groceries. 
    And finally, finish the report."
    """
    lines = text.split('\n')
    result_lines = []
    list_items = []
    
    for line in lines:
        stripped = line.strip()
        # Check if this is a list item (starts with dash, bullet, or number)
        if re.match(r'^[-*•]\s+', stripped) or re.match(r'^\d+\.\s+', stripped):
            # Extract the item text
            item = re.sub(r'^[-*•]\s+', '', stripped)
            item = re.sub(r'^\d+\.\s+', '', item)
            list_items.append(item)
        else:
            # Not a list item - flush any accumulated list
            if list_items:
                result_lines.append(_format_list_items(list_items))
                list_items = []
            result_lines.append(line)
    
    # Flush any remaining list at end of text
    if list_items:
        result_lines.append(_format_list_items(list_items))
    
    return '\n'.join(result_lines)


def _format_list_items(items: list[str]) -> str:
    """Format a list of items into natural speech."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    
    # Multiple items - use first, middle, finally pattern
    intro = "You have several things:"
    if len(items) == 3:
        return f"{intro} First, {items[0]}. Second, {items[1]}. And finally, {items[2]}"
    
    # More than 3 items
    middle_items = items[1:-1]
    if middle_items:
        middle = ", ".join(middle_items)
        return f"{intro} First, {items[0]}. Then {middle}. And finally, {items[-1]}"
    else:
        return f"{intro} First, {items[0]}. And finally, {items[-1]}"


def expand_abbreviations(text: str) -> str:
    """
    Expand abbreviations to their full forms for natural speech.
    
    Example:
    "Meeting at 2PM at FAM MED" → "Meeting at 2 P.M. at Family Medicine"
    """
    result = text
    
    # Sort by length (longest first) to avoid partial replacements
    sorted_abbrs = sorted(ABBREVIATIONS.items(), key=lambda x: len(x[0]), reverse=True)
    
    for abbr, full in sorted_abbrs:
        # Use word boundaries to avoid partial matches
        pattern = r'\b' + re.escape(abbr) + r'\b'
        result = re.sub(pattern, full, result, flags=re.IGNORECASE)
    
    return result


def convert_time_ranges(text: str) -> str:
    """Convert time ranges to natural speech."""
    # 8:00–12:30 → 8 to 12:30
    text = re.sub(r'(\d{1,2}):(\d{2})\s*[–-]\s*(\d{1,2}):(\d{2})', r'\1:\2 to \3:\4', text)
    # 8-9 AM → 8 to 9 A.M.
    text = re.sub(r'(\d{1,2})\s*-\s*(\d{1,2})\s*(AM|PM)', r'\1 to \2 \3', text, flags=re.IGNORECASE)
    return text


def format_for_display(text: str) -> str:
    """
    Keep markdown for visual display in iOS UI.
    This is the text shown in the response card.
    """
    return text


def transform_for_speech(
    text: str,
    domain: Optional[QueryDomain] = None,
) -> str:
    """
    Complete natural speech transformation pipeline.
    
    Applies in order:
    1. Strip markdown
    2. Convert lists to speech
    3. Expand abbreviations
    4. Convert time ranges
    5. Clean up formatting
    
    Args:
        text: Input text (may contain markdown)
        domain: Optional domain for domain-specific transforms
        
    Returns:
        Natural speech text ready for TTS
    """
    if not text:
        return text
    
    # Step 1: Strip markdown
    text = strip_markdown_for_speech(text)
    
    # Step 2: Convert lists to conversational flow
    text = convert_lists_to_speech(text)
    
    # Step 3: Expand abbreviations
    text = expand_abbreviations(text)
    
    # Step 4: Convert time ranges
    text = convert_time_ranges(text)
    
    # Step 5: Domain-specific transformations
    if domain == QueryDomain.PRODUCTIVITY:
        text = _transform_productivity_speech(text)
    elif domain == QueryDomain.NEWS:
        text = _transform_news_speech(text)
    elif domain == QueryDomain.TASKS:
        text = _transform_tasks_speech(text)
    
    # Final cleanup
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()
    
    return text


def _transform_productivity_speech(text: str) -> str:
    """Transform productivity content for natural speech."""
    # Add natural transitions for schedule items
    if 'today' in text.lower():
        text = text.replace('Today', 'Today,')
    if 'tomorrow' in text.lower():
        text = text.replace('Tomorrow', 'Tomorrow,')
    
    # Clean up location formatting
    text = re.sub(r'\(([^)]+)\)', r'at \1', text)
    
    return text


def _transform_news_speech(text: str) -> str:
    """Transform news content for natural speech."""
    # Add "According to" for sources
    text = re.sub(r'^([^:]+):', r'According to \1,', text)
    
    # Clean up quoted text
    text = re.sub(r'"([^"]+)"', r'\1', text)
    
    return text


def _transform_tasks_speech(text: str) -> str:
    """Transform task content for natural speech."""
    # Count tasks and present naturally
    task_count = len(re.findall(r'\b(first|second|third|finally)\b', text.lower()))
    if task_count > 0:
        text = f"You have {task_count} tasks: {text}"
    
    return text


# Backwards compatibility
def format_schedule_for_speech(text: str) -> str:
    """Legacy function - now uses transform_for_speech."""
    return transform_for_speech(text, QueryDomain.PRODUCTIVITY)
