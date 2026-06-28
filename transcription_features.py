import math
import re


LANGUAGE_NAMES = {
    "en": "English",
    "te": "Telugu",
    "hi": "Hindi",
    "ta": "Tamil",
    "kn": "Kannada",
    "ml": "Malayalam",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "unknown": "Unknown",
}

ACTION_PATTERNS = [
    (
        "Deadline",
        re.compile(
            r"\b(deadline|due|by\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow|eod|next\s+week|\d{1,2}([/-]\d{1,2})?))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Action Item",
        re.compile(
            r"\b(action\s+item|todo|to-do|task|owner|assign|responsible|need\s+to|we\s+should|please\s+do)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Follow Up",
        re.compile(r"\b(follow[-\s]?up|circle\s+back|check\s+in|sync\s+again)\b", re.IGNORECASE),
    ),
    (
        "Decision",
        re.compile(r"\b(finalized|approved|decided|agreed|confirmed|signed\s+off)\b", re.IGNORECASE),
    ),
    (
        "Budget",
        re.compile(r"\b(budget|cost|pricing|invoice|payment|expense|revenue)\b", re.IGNORECASE),
    ),
]

SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
WORD_RE = re.compile(r"\b[\w'-]+\b", re.UNICODE)


def language_display(code):
    if not code:
        return "Unknown"
    return LANGUAGE_NAMES.get(str(code).lower(), str(code).title())


def format_timestamp(seconds):
    seconds = max(float(seconds or 0), 0)
    total = int(seconds)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_srt_timestamp(seconds):
    seconds = max(float(seconds or 0), 0)
    total_milliseconds = int(round(seconds * 1000))
    hours = total_milliseconds // 3_600_000
    minutes = (total_milliseconds % 3_600_000) // 60_000
    secs = (total_milliseconds % 60_000) // 1000
    milliseconds = total_milliseconds % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def format_duration(seconds):
    seconds = int(round(float(seconds or 0)))
    if seconds <= 0:
        return "0s"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def word_count(text):
    return len(WORD_RE.findall(text or ""))


def _confidence_from_segment(segment):
    avg_logprob = segment.get("avg_logprob")
    if avg_logprob is None:
        return None
    try:
        return max(0, min(1, math.exp(float(avg_logprob))))
    except (TypeError, ValueError, OverflowError):
        return None


def _normalize_word_token(token, index):
    token = token or ""
    if index and token and not token.startswith((" ", "\n", ".", ",", "!", "?", ":", ";", "'", '"')):
        return " " + token
    return token


def build_segments(whisper_segments):
    segments = []

    for index, segment in enumerate(whisper_segments or []):
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or start)
        words = []
        word_confidences = []

        for word_index, word in enumerate(segment.get("words") or []):
            probability = word.get("probability")
            confidence = None
            if probability is not None:
                try:
                    confidence = max(0, min(1, float(probability)))
                    word_confidences.append(confidence)
                except (TypeError, ValueError):
                    confidence = None

            words.append(
                {
                    "text": _normalize_word_token(word.get("word", ""), word_index),
                    "start": round(float(word.get("start") or start), 2),
                    "end": round(float(word.get("end") or end), 2),
                    "timestamp": format_timestamp(word.get("start") or start),
                    "confidence": confidence,
                    "confidence_percent": round(confidence * 100) if confidence is not None else None,
                    "low_confidence": confidence is not None and confidence < 0.65,
                }
            )

        confidence = None
        if word_confidences:
            confidence = sum(word_confidences) / len(word_confidences)
        else:
            confidence = _confidence_from_segment(segment)

        text = (segment.get("text") or "").strip()
        segments.append(
            {
                "index": index + 1,
                "speaker": "Speaker 1",
                "start": round(start, 2),
                "end": round(end, 2),
                "timestamp": format_timestamp(start),
                "end_timestamp": format_timestamp(end),
                "text": text,
                "words": words,
                "confidence": confidence,
                "confidence_percent": round(confidence * 100) if confidence is not None else None,
                "low_confidence": confidence is not None and confidence < 0.65,
            }
        )

    return segments


def create_timestamped_transcript(segments):
    lines = []
    for segment in segments or []:
        lines.append(f"[{segment['timestamp']}] {segment['speaker']}: {segment['text']}")
    return "\n".join(lines)


def create_srt(segments):
    blocks = []
    for index, segment in enumerate(segments or [], start=1):
        start = format_srt_timestamp(segment.get("start", 0))
        end = format_srt_timestamp(segment.get("end", segment.get("start", 0) + 1))
        blocks.append(f"{index}\n{start} --> {end}\n{segment.get('text', '').strip()}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _split_sentences(text):
    text = (text or "").strip()
    if not text:
        return []
    sentences = SENTENCE_RE.split(text)
    return [sentence.strip() for sentence in sentences if sentence.strip()]


def extract_action_items(segments):
    action_items = []
    seen = set()

    for segment in segments or []:
        for sentence in _split_sentences(segment.get("text", "")):
            labels = [label for label, pattern in ACTION_PATTERNS if pattern.search(sentence)]
            if not labels:
                continue

            normalized = re.sub(r"\s+", " ", sentence.lower()).strip()
            if normalized in seen:
                continue
            seen.add(normalized)

            action_items.append(
                {
                    "timestamp": segment.get("timestamp", "00:00:00"),
                    "speaker": segment.get("speaker", "Speaker 1"),
                    "text": sentence,
                    "labels": labels,
                }
            )

    return action_items
