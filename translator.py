import json
import os
import re

import requests


TRANSLATION_LANGUAGE_OPTIONS = [
    ("none", "Do not translate"),
    ("en", "English"),
    ("hi", "Hindi"),
    ("te", "Telugu"),
    ("ta", "Tamil"),
    ("kn", "Kannada"),
    ("ml", "Malayalam"),
    ("mr", "Marathi"),
    ("gu", "Gujarati"),
    ("bn", "Bengali"),
    ("ur", "Urdu"),
    ("pa", "Punjabi"),
    ("or", "Odia"),
    ("as", "Assamese"),
    ("sa", "Sanskrit"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("de", "German"),
    ("it", "Italian"),
    ("pt", "Portuguese"),
    ("ar", "Arabic"),
    ("zh", "Chinese"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("ru", "Russian"),
]

TRANSLATION_LANGUAGE_NAMES = dict(TRANSLATION_LANGUAGE_OPTIONS)


def language_name(code):
    return TRANSLATION_LANGUAGE_NAMES.get(code or "none", code or "None")


def should_translate(source_language, target_language):
    target = (target_language or "none").lower()
    source = (source_language or "unknown").lower()
    return target not in {"", "none", "auto", source}


def translation_provider():
    groq_key = (os.getenv("GROQ_API_KEY") or "").strip()
    if groq_key:
        return {
            "name": "groq",
            "api_key": groq_key,
            "url": os.getenv("GROQ_CHAT_COMPLETIONS_URL", "https://api.groq.com/openai/v1/chat/completions"),
            "model": os.getenv("GROQ_TRANSLATION_MODEL", "llama-3.3-70b-versatile"),
        }

    openai_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if openai_key:
        return {
            "name": "openai",
            "api_key": openai_key,
            "url": os.getenv("OPENAI_CHAT_COMPLETIONS_URL", "https://api.openai.com/v1/chat/completions"),
            "model": os.getenv("OPENAI_TRANSLATION_MODEL", "gpt-4o-mini"),
        }

    return None


def empty_translation(target_language, status="skipped", error="", enabled=False):
    return {
        "enabled": enabled,
        "target_language": language_name(target_language),
        "translated_text": "",
        "translated_summary": "",
        "status": status,
        "provider": "",
        "error": error,
    }


def call_translation_model(prompt, provider):
    response = requests.post(
        provider["url"],
        headers={
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": "application/json",
        },
        data=json.dumps(
            {
                "model": provider["model"],
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a precise multilingual translator for transcripts and summaries.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
            }
        ),
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(response.text[:500])
    payload = response.json()
    return (
        payload.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )


def strip_code_fences(text):
    text = (text or "").strip()
    fence_match = re.fullmatch(r"```(?:json|JSON)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    return text


def extract_json_object(text):
    text = strip_code_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return None
    return None


def normalize_translation_payload(translation):
    translation = dict(translation or {})
    raw_text = translation.get("translated_text") or ""
    raw_summary = translation.get("translated_summary") or ""

    parsed = extract_json_object(raw_text)
    if parsed:
        raw_text = parsed.get("translated_text") or parsed.get("text") or ""
        raw_summary = raw_summary or parsed.get("translated_summary") or parsed.get("summary") or ""

    summary_parsed = extract_json_object(raw_summary)
    if summary_parsed:
        raw_summary = summary_parsed.get("translated_summary") or summary_parsed.get("summary") or ""

    translation["translated_text"] = strip_code_fences(raw_text)
    translation["translated_summary"] = strip_code_fences(raw_summary)
    return translation


def translate_transcript_bundle(transcript, summary, source_language, target_language):
    transcript = (transcript or "").strip()
    summary = (summary or "").strip()
    target_language = (target_language or "none").lower()

    if not (transcript or summary) or not should_translate(source_language, target_language):
        return empty_translation(target_language)

    provider = translation_provider()
    if not provider:
        return empty_translation(
            target_language,
            status="missing_api_key",
            error="Set GROQ_API_KEY or OPENAI_API_KEY to enable translation.",
            enabled=True,
        )

    target_name = language_name(target_language)
    source_name = language_name(source_language)
    prompt = (
        "Translate the transcript and summary accurately. Preserve timestamps like [00:01:23], "
        "speaker labels, names, numbers, and action-item wording. Return plain valid JSON only with keys "
        "`translated_text` and `translated_summary`. Do not wrap the JSON in markdown or code fences.\n\n"
        f"Source language: {source_name}\n"
        f"Target language: {target_name}\n\n"
        f"Transcript:\n{transcript[:12000]}\n\n"
        f"Summary:\n{summary[:3000]}"
    )

    try:
        content = call_translation_model(prompt, provider)
        parsed = extract_json_object(content)
        if not parsed:
            parsed = {"translated_text": strip_code_fences(content), "translated_summary": ""}

        return normalize_translation_payload({
            "enabled": True,
            "target_language": target_name,
            "translated_text": (parsed.get("translated_text") or "").strip(),
            "translated_summary": (parsed.get("translated_summary") or "").strip(),
            "status": "completed",
            "provider": provider["name"],
            "error": "",
        })
    except RuntimeError as exc:
        return {
            "enabled": True,
            "target_language": target_name,
            "translated_text": "",
            "translated_summary": "",
            "status": "failed",
            "provider": provider["name"],
            "error": str(exc),
        }


def translate_text(text, source_language, target_language):
    return translate_transcript_bundle(text, "", source_language, target_language)
