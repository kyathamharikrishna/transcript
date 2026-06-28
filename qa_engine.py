import json
import os
import re
import urllib.error
import urllib.request


STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "who",
    "why",
    "with",
    "you",
}

WORD_RE = re.compile(r"\b[\w'-]+\b", re.UNICODE)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _tokens(text):
    return {
        token.lower()
        for token in WORD_RE.findall(text or "")
        if len(token) > 2 and token.lower() not in STOPWORDS
    }


def _chunks_from_segments(segments, transcript, max_chars=1800):
    chunks = []
    current = []
    current_start = ""
    current_end = ""
    current_length = 0

    for segment in segments or []:
        line = f"[{segment.get('timestamp', '00:00:00')}] {segment.get('speaker', 'Speaker 1')}: {segment.get('text', '')}"
        if not current_start:
            current_start = segment.get("timestamp", "00:00:00")
        current_end = segment.get("end_timestamp", segment.get("timestamp", "00:00:00"))

        if current and current_length + len(line) > max_chars:
            text = "\n".join(current)
            chunks.append({"text": text, "start": current_start, "end": current_end})
            current = []
            current_start = segment.get("timestamp", "00:00:00")
            current_length = 0

        current.append(line)
        current_length += len(line)

    if current:
        chunks.append({"text": "\n".join(current), "start": current_start, "end": current_end})

    if chunks:
        return chunks

    transcript = transcript or ""
    for index in range(0, len(transcript), max_chars):
        text = transcript[index : index + max_chars]
        chunks.append({"text": text, "start": "00:00:00", "end": "00:00:00"})
    return chunks


def _rank_chunks(question, segments, transcript):
    query_tokens = _tokens(question)
    chunks = _chunks_from_segments(segments, transcript)
    scored = []

    for chunk in chunks:
        chunk_tokens = _tokens(chunk["text"])
        overlap = len(query_tokens & chunk_tokens)
        density = overlap / max(len(chunk_tokens), 1)
        scored.append((overlap, density, chunk))

    ranked = [chunk for overlap, _, chunk in sorted(scored, key=lambda item: item[:2], reverse=True) if overlap]
    return ranked[:4] if ranked else chunks[:2]


def _fallback_answer(question, chunks):
    query_tokens = _tokens(question)
    sentences = []

    for chunk in chunks:
        for sentence in SENTENCE_RE.split(chunk["text"]):
            score = len(query_tokens & _tokens(sentence))
            if score:
                sentences.append((score, sentence.strip()))

    if not sentences:
        return "I could not find that topic in the transcript."

    best = [sentence for _, sentence in sorted(sentences, key=lambda item: item[0], reverse=True)[:3]]
    return " ".join(best)


def _anthropic_answer(question, context):
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    body = {
        "model": model,
        "max_tokens": 500,
        "system": (
            "You answer questions using only the provided transcript context. "
            "If the transcript does not contain the answer, say that it was not mentioned. "
            "Be concise, cite timestamps when helpful, and do not invent details."
        ),
        "messages": [
            {
                "role": "user",
                "content": f"Transcript context:\n{context}\n\nQuestion: {question}",
            }
        ],
    }
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Anthropic API error: {details or exc.reason}") from exc

    parts = []
    for item in payload.get("content", []):
        if item.get("type") == "text":
            parts.append(item.get("text", ""))

    answer = "\n".join(part.strip() for part in parts if part.strip()).strip()
    return answer or None


def answer_question(question, transcript, segments=None):
    ranked_chunks = _rank_chunks(question, segments or [], transcript or "")
    context = "\n\n".join(chunk["text"] for chunk in ranked_chunks)
    source_windows = [f"{chunk['start']} - {chunk['end']}" for chunk in ranked_chunks if chunk.get("start")]

    try:
        answer = _anthropic_answer(question, context)
        if answer:
            return {
                "answer": answer,
                "source": "Claude via Anthropic API",
                "context_windows": source_windows,
            }
    except RuntimeError as exc:
        return {
            "answer": _fallback_answer(question, ranked_chunks),
            "source": f"Local retrieval fallback ({exc})",
            "context_windows": source_windows,
        }

    return {
        "answer": _fallback_answer(question, ranked_chunks),
        "source": "Local transcript retrieval",
        "context_windows": source_windows,
    }
