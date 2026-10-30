"""Torch-free self-test of docs/worker_stt/stt_logic.py: python docs/worker_stt/selftest.py   (exit 0 = all checks passed)."""
import base64
import importlib.util
import pathlib
import struct
import sys

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("stt_logic", HERE / "stt_logic.py")
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)

checks = 0


def check(cond, what):
    global checks
    checks += 1
    if not cond:
        print("FAIL:", what)
        sys.exit(1)


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


# language
check(L.parse_language(None, "") == "", "absent + auto default -> auto")
check(L.parse_language(None, "hi") == "hi", "absent -> the worker default")
check(L.parse_language("auto", "hi") == "", "explicit auto beats a hi default")
check(L.parse_language("EN ", "hi") == "en", "case and spaces are ignored")
check(L.parse_language("hinglish") == "hi", "hinglish maps to hi")
check(L.parse_language("", "en") == "", "an empty string is auto")
check(raises(L.parse_language, "fr"), "an unknown language is refused")
check(raises(L.parse_language, 5), "a non-language value is refused")
# prompt
check(L.parse_prompt(None) == "" and L.parse_prompt("   ") == "", "absent or blank prompt -> empty")
check(L.parse_prompt("  a   b \n c ") == "a b c", "whitespace is collapsed")
check(len(L.parse_prompt("x" * 200)) == 200, "200 characters are allowed")
check(raises(L.parse_prompt, "x" * 201), "201 characters are refused")
check(raises(L.parse_prompt, 7), "a non-string prompt is refused")
check(len(L.parse_prompt("क" * 200)) == 200, "Devanagari counts characters, not bytes")
# pcm16
pcm = struct.pack("<4h", 0, 100, -100, 32767)
check(L.decode_pcm16(base64.b64encode(pcm).decode()) == pcm, "PCM16 round trip")
check(raises(L.decode_pcm16, base64.b64encode(b"abc").decode()), "an odd byte count is refused")
check(raises(L.decode_pcm16, "not base64!!"), "invalid base64 is refused")
# audio seconds and timing
check(L.audio_seconds(24000, 8000) == 3.0 and L.audio_seconds(10, 0) == 0.0, "audio seconds")
t = L.timing(0.61234, 0.7, 3.0)
check(t == {"inference_s": 0.612, "total_s": 0.7, "audio_s": 3.0}, "timing carries audio_s")
# generate kwargs: a copy, language replaced or removed, prompt ids passed through
base = {"task": "transcribe", "language": "en", "num_beams": 1}
check(L.build_generate_kwargs(base, "hi")["language"] == "hi" and base["language"] == "en", "language replaced, base untouched")
check("language" not in L.build_generate_kwargs(base, ""), "auto removes the language key")
check(L.build_generate_kwargs(base, "hi", prompt_ids=[1, 2])["prompt_ids"] == [1, 2], "prompt ids passed through")
check("prompt_ids" not in L.build_generate_kwargs(base, "hi"), "no prompt ids by default")
# startup line: language and options, no secrets, no words
line = L.startup_line("org/model", "hi", True, False, True)
check("default_language=hi" in line and "request_language=on" in line and "prompt=off" in line, "startup line content")
check("default_language=auto" in L.startup_line("org/model", "", True, True, False), "startup line says auto")
print(f"worker_stt selftest: {checks} checks passed")
