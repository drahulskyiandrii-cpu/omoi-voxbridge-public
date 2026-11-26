"""Conservative OMOI-derived optional terminology and PCM preparation."""
import re
import unicodedata
import array
import math
import sys

def validate_hotwords(value):
    if not isinstance(value,str) or any(unicodedata.category(c).startswith("C") for c in value) or len(value.encode("utf-8"))>192:
        raise ValueError("INVALID_STT_HOTWORDS")
    return value.strip()

# Exact observed confusions with local context, no general rewriting/numbers.
_RULES=(
    ("whisper-whispel",r"\bWhispel\b","Whisper",r"\b(?:NPU|CPU|OpenVINO|Medium|STT)\b"),
    ("openvino-runtime",r"\bOpenWin(?:dow)?\s+(?:longtime|runtime)\b","OpenVINO runtime",r"\b(?:Vulkan|backend|NPU|CPU|Whisper)\b"),
    ("runtime-alongtime",r"\bAlongtime\b","runtime",r"\b(?:Node\.js|Electron|TypeScript|GitHub)\b"),
    ("einstellungen",r"\bанштеллинген\b","Einstellungen",r"голосов|модул|\bSpeech\b"),
    ("spracherkennung",r"\bшпаж\s+геннинг\b","Spracherkennung",r"автоматическ|голосов|\bSpeech\b"),
    ("speech-recognition",r"\bSpeech\s+Recording\b","Speech Recognition",r"распозна|автоматическ|\bSpracherkennung\b"),
)

def correct_terms(text):
    ids=[]
    for rule,pattern,replacement,context in _RULES:
        if re.search(context,text,re.I):
            text,n=re.subn(pattern,replacement,text,flags=re.I)
            if n: ids.append(rule)
    return text,tuple(ids)

def condition_pcm(pcm):
    """Optional DC removal + bounded gain, not denoising or echo cancellation.
    No trim, no time change; peak-limited, no boost of silence.
    """
    samples=array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder!="little":samples.byteswap()
    if not samples:return pcm
    mean=sum(samples)/len(samples)
    peak=max(abs(x-mean) for x in samples)
    rms=math.sqrt(sum((x-mean)**2 for x in samples)/len(samples))
    gain=min(3.0,max(1.0,2200/max(rms,1)),30000/max(peak,1)) if rms>=82 else 1.0
    for i,x in enumerate(samples):
        samples[i]=max(-32768,min(32767,round((x-mean)*gain)))
    if sys.byteorder!="little":samples.byteswap()
    return samples.tobytes()
