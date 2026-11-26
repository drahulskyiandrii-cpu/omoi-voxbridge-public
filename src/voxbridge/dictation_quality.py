"""Reject clearly degenerate speech recognition results before typing them."""
import re
import unicodedata
from collections import Counter
from itertools import groupby


MAX_SEGMENT_TEXT = 3000
MAX_RECORD_TEXT = 24000


def inspect_transcript(text, seconds=None, *, max_chars=MAX_SEGMENT_TEXT):
    if not isinstance(text, str) or not text.strip():
        return "STT_TEXT_EMPTY"
    if (len(text) > max_chars
            or any(unicodedata.category(ch) in ("Cc", "Cs", "Cf")
                   and ch not in "\t\n\r" for ch in text)):
        return "STT_TEXT_INVALID"
    # Whisper protocol markers are not spoken text. Do not strip them and
    # silently insert a different result. Preserve ordinary angle brackets.
    if re.search(r"<\|[^<>\r\n]{1,80}\|>", text):
        return "STT_TEXT_INVALID"
    # Do not silently rewrite a user's words. Long letter runs are a failure,
    # not a reliable reconstruction of the speech. Normal repeated letters,
    # punctuation and digits remain untouched.
    if re.search(r"([^\W\d_])\1{6,}", text, flags=re.IGNORECASE):
        return "STT_REPETITION_DETECTED"
    # Main OMOI's conservative transcript-shape checks; preserve numeric lists.
    words = [word for word in re.findall(r"[^\W_]+", text.casefold()) if any(c.isalpha() for c in word)]
    n = len(words)
    if n >= 12:
        counts = Counter(words)
        if max(counts.values()) / n >= .72 or (n >= 18 and len(counts)/n <= .14):
            return "STT_WORD_REPETITION"
        if any(len(list(group)) >= 8 for _, group in groupby(words)):
            return "STT_WORD_REPETITION"
        if isinstance(seconds,(int,float)) and seconds > .25 and n/seconds > 7 and len("".join(words))/seconds > 36:
            return "STT_IMPLAUSIBLE_RATE"
    return "STT_TEXT_OK"
