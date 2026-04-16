# iOS Engineering Tasks — Nova Voice Agent

> Generated: 2026-04-13  
> Context: Nova voice agent now streams LLM responses and Perplexity web search citations via WebRTC data channel. Three areas need iOS-side work.

---

## 1. TTS Sentence Buffering (Speech Quality Fix)

**Owner:** Voice/Audio engineer  
**Priority:** High  
**Problem:** Nova speaks brokenly on long responses because `AVSpeechSynthesizer` receives tiny token-level fragments (5–30 chars each) and speaks each one with audible pauses between them. Short responses sound fluid because they arrive in fewer, larger chunks.

### Evidence from Logs

```
# Long response — many tiny fragments → broken speech
🔊 Bot transcript: Here's the latest as of today:\n\n**Negotiations D...
🔊 Bot transcript: eadlocked**\n\nUS envoys (VP JD Vance, Jared Kushn...
🔊 Bot transcript: er, Steve Witkoff) met with Iranian officials in...
🔊 Bot transcript:  Islamabad April 11-12 for 21 hours of talks — e...
# (20+ fragments for a 1195-char response)

# Short response — few chunks → fluid speech
🔊 Bot transcript: Currently 74°F with partly cloudy skies, light b...
🔊 Bot transcript: reeze from the north at 10 mph, and fairly humid...
# (4 fragments for a 210-char response)
```

### Fix

Buffer incoming `Bot transcript` chunks and only pass to `AVSpeechSynthesizer` at **sentence boundaries**:

```swift
// Pseudocode for VoiceAgentService / TTS layer
private var ttsBuffer = ""
private var flushTimer: Timer?

func onBotTranscript(_ chunk: String) {
    ttsBuffer += chunk
    flushTimer?.invalidate()

    // Flush at sentence boundaries
    if let range = ttsBuffer.range(of: #"[.!?]\s"#, options: .regularExpression) {
        let sentence = String(ttsBuffer[..<range.upperBound])
        ttsBuffer = String(ttsBuffer[range.upperBound...])
        speakSentence(sentence)
    }

    // Fallback: flush after 400ms of no new chunks
    flushTimer = Timer.scheduledTimer(withTimeInterval: 0.4, repeats: false) { _ in
        if !self.ttsBuffer.isEmpty {
            self.speakSentence(self.ttsBuffer)
            self.ttsBuffer = ""
        }
    }
}
```

**Key rules:**
- Split on `.` `!` `?` followed by whitespace, or on `\n\n` (paragraph break)
- Debounce timer of ~400ms to flush any trailing fragment when streaming stops
- Strip markdown formatting (`**bold**`, `- list`, `#` headings) before passing to TTS
- Do **not** speak citation footnotes like `(4 sources available...)`

---

## 2. Source Citation Chip Bar (New Feature)

**Owner:** UI/Chat engineer  
**Priority:** Medium  
**Problem:** Nova's Perplexity web search returns structured citations via the `type: sources` server message. The iOS app already receives them (`📚 Received 4 sources`) but does not render anything.

### Server Message Format

Arrives on the WebRTC data channel as a server message:

```json
{
  "type": "sources",
  "query": "Iran United States negotiations Strait of Hormuz 2025 2026",
  "citations": [
    {
      "index": 1,
      "url": "https://www.thedailybeast.com/donald-trump-makes-jaw-dropping-admission/",
      "title": "Trump Makes Jaw-Dropping Admission About His War",
      "domain": "www.thedailybeast.com",
      "date": "2026-04-12",
      "snippet": "First 120 characters of the article snippet..."
    },
    {
      "index": 2,
      "url": "https://www.youtube.com/watch?v=MZ2qPxXoj90",
      "title": "",
      "domain": "www.youtube.com",
      "date": "",
      "snippet": ""
    }
  ]
}
```

**Field availability:**
| Field     | Always present | Notes |
|-----------|---------------|-------|
| `index`   | Yes | 1-based position |
| `url`     | Yes | Full URL string |
| `title`   | Sometimes | Empty string if unavailable |
| `domain`  | Yes | Extracted from URL server-side |
| `date`    | Sometimes | ISO date string or empty |
| `snippet` | Sometimes | Max 120 chars or empty |

### Recommended UI: Collapsible Source Chip Bar

Appears **below the assistant message bubble** that triggered the search.

```
┌──────────────────────────────────────────────────┐
│  Here's the latest as of today:                  │
│  **Negotiations Deadlocked** ...                 │
│                                                  │
│  📎 Sources (4)                          [v]     │
│  ┌──────────────┐ ┌──────────────┐ ┌─────...     │
│  │ 🌐 dailybeast│ │ ▶️ youtube   │ │ 🌐 cr...     │
│  └──────────────┘ └──────────────┘ └─────...     │
└──────────────────────────────────────────────────┘
```

**States:**
- **Collapsed (default):** Single line: `📎 Sources (N)` with chevron. Tapping toggles expansion.
- **Expanded:** Horizontal `ScrollView` of chips. Each chip shows favicon + truncated domain.

**Interactions:**
- **Tap chip** → Open URL in `SFSafariViewController`
- **Long press chip** → `UIActivityViewController` (share sheet)
- **Tap "Sources (N)" label** → Toggle expand/collapse

**Chip content priority:**
1. If `title` is non-empty: Show `title` (truncated to ~25 chars)
2. If `title` is empty: Show `domain` (e.g., `youtube.com`)
3. Favicon: Use `https://www.google.com/s2/favicons?domain={domain}&sz=32`

**Association:** The `sources` message always arrives **before** the LLM response text for the same turn. Attach citations to the **next** assistant message bubble in the conversation.

### Data Model

```swift
struct SourceCitation: Identifiable {
    let id: Int        // index
    let url: URL
    let title: String
    let domain: String
    let date: String?
    let snippet: String?
}
```

Parse from server message when `msg["type"] == "sources"`:
```swift
if let citations = msg["citations"] as? [[String: Any]] {
    let sources = citations.compactMap { dict -> SourceCitation? in
        guard let index = dict["index"] as? Int,
              let urlStr = dict["url"] as? String,
              let url = URL(string: urlStr) else { return nil }
        return SourceCitation(
            id: index,
            url: url,
            title: dict["title"] as? String ?? "",
            domain: dict["domain"] as? String ?? url.host ?? "",
            date: dict["date"] as? String,
            snippet: dict["snippet"] as? String
        )
    }
    // Attach to pending/next assistant message
}
```

---

## 3. Thinking Phase Indicator (Enhancement)

**Owner:** UI/Chat engineer  
**Priority:** Low  
**Problem:** During tool calls (web search, weather, etc.), Nova sends `phase` and `thinking` messages. The app receives them but the display could be improved.

### Server Messages (already handled)

```json
{"phase": "thinking"}
{"type": "thinking", "text": "Searching for Iran United States negotiations..."}
{"type": "heartbeat", "text": "Still working on it..."}
{"phase": "responding"}
{"phase": "done"}
```

### Current Behavior

The app logs `💭 Thinking update: ...` and `💓 Heartbeat: ...` but the visual indicator is minimal. The user reported not seeing what Nova was doing during the search phase.

### Suggested Improvement

When `phase: thinking` is active, show a **thinking pill** below the user's message:

```
┌──────────────────────────────┐
│  🔍 Searching for Iran US... │   ← animated shimmer
└──────────────────────────────┘
```

- Use the `text` field from the `type: thinking` message as the label
- Animate with a subtle shimmer or pulsing dot
- Dismiss when `phase: responding` or `phase: done` arrives
- If multiple `type: thinking` messages arrive, update the label (don't stack)

---

## Server Message Reference

All messages arrive on the WebRTC data channel. Key types relevant to these tasks:

| `type` / `phase` | Purpose | Fields |
|---|---|---|
| `phase: thinking` | LLM started processing | — |
| `type: thinking` | Progress update during tool calls | `text` |
| `type: heartbeat` | Keep-alive during long operations | `text` |
| `phase: responding` | First text token streaming | — |
| `phase: done` | Tool call completed | — |
| `type: sources` | Perplexity search citations | `query`, `citations[]` |
| `type: ping` | Connection keepalive | — |

---

## Testing Checklist

- [ ] Long web search response (e.g., "Tell me the latest news on...") speaks fluidly without stuttering
- [ ] Source chips appear below the assistant bubble after a web search
- [ ] Tapping a source chip opens the URL
- [ ] Short responses (e.g., weather, "OK") still speak immediately without delay
- [ ] Thinking pill shows search query during Perplexity lookups
- [ ] Thinking pill disappears when response starts streaming
