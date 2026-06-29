import json
import os

import requests


TRANSLATION_LANGUAGE_OPTIONS = [
    ("none", "Do not translate"),
    ("en", "English"),
    ("hi", "Hindi"),
    ("te", "Telugu"),
    ("ta", "Tamil"),
    ("kn", "Kannada"),
    ("ml", "Malayalam"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("de", "German"),
]

TRANSLATION_LANGUAGE_NAMES = dict(TRANSLATION_LANGUAGE_OPTIONS)


def language_name(code):
    return TRANSLATION_LANGUAGE_NAMES.get(code or "none", code or "None")


def should_translate(source_language, target_language):
    target = (target_language or "none").lower()
    source = (source_language or "unknown").lower()
    return target not in {"", "none", "auto", source}


def translate_text(text, source_language, target_language):
    text = (text or "").strip()
    target_language = (target_language or "none").lower()
    if not text or not should_translate(source_language, target_language):
        return {
            "enabled": False,
            "target_language": language_name(target_language),
            "translated_text": "",
            "status": "skipped",
            "error": "",
        }

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return {
            "enabled": True,
            "target_language": language_name(target_language),
            "translated_text": "",
            "status": "missing_api_key",
            "error": "Set OPENAI_API_KEY to enable transcript translation.",
        }

    model = os.getenv("OPENAI_TRANSLATION_MODEL", "gpt-4o-mini")
    prompt = (
        "Translate this transcript accurately. Preserve timestamps like [00:01:23], "
        "speaker labels, names, numbers, and action-item wording. Return only the translation.\n\n"
        f"Source language: {language_name(source_language)}\n"
        f"Target language: {language_name(target_language)}\n\n"
        f"Transcript:\n{text[:12000]}"
    )
    response = requests.post(
        os.getenv("OPENAI_CHAT_COMPLETIONS_URL", "https://api.openai.com/v1/chat/completions"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        data=json.dumps(
            {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a precise multilingual transcript translator.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
            }
        ),
        timeout=120,
    )

    if response.status_code >= 400:
        return {
            "enabled": True,
            "target_language": language_name(target_language),
            "translated_text": "",
            "status": "failed",
            "error": response.text[:500],
        }

    payload = response.json()
    translated = (
        payload.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )
    return {
        "enabled": True,
        "target_language": language_name(target_language),
        "translated_text": translated,
        "status": "completed" if translated else "empty",
        "error": "",
    }
