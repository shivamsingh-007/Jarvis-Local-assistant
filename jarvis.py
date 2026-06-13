#!/usr/bin/env python3
"""
Desktop clap listener: reads the default microphone and logs when two loud transients
(a double clap) are detected within a short time window.

Run:
  python -m pip install -r requirements.txt
  python clap_listen.py

Tuning (constants below):
  SAMPLE_RATE   — usually 44100 or 48000; match your device if needed.
  BLOCK_MS      — analysis window size; smaller = snappier, noisier.
  SPIKE_RATIO   — how many times louder than the noise floor counts as a clap;
                    raise if false triggers; lower if claps are missed.
  COOLDOWN_S    — minimum seconds between double-clap logs (debounce).
  MIN_DOUBLE_GAP_S / MAX_DOUBLE_GAP_S — allowed time between the two claps.
  RETRIGGER_RATIO — audio must fall below threshold * this before another hit counts.
  NOISE_FLOOR_ALPHA — closer to 1 = slower baseline adaptation to room noise.
  MIN_RMS       — ignore spikes below this absolute level (float audio ~ [-1, 1]).
  SONG_URI      — Spotify or YouTube URL/URI to open on each double clap (empty = log only).
  FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP — if True, launch Cursor without -n (reuse / focus existing instance).
  OPEN_NEW_CURSOR_ON_DOUBLE_CLAP — if True, also launch Cursor with -n (extra new window; runs after focus launch if both).
  CURSOR_OPEN_FULLSCREEN — Windows: after focus/launch, send F11 to enter Cursor/VS Code-style fullscreen (toggle off with F11).
  OPEN_CLAUDE_CODE_IN_CHROME — Claude in Chrome after Spotify (CLAUDE_CODE_URL).
  OPEN_BINANCE_BTC_IN_CHROME — Binance BTC trade page in Chrome (BINANCE_BTC_URL).
  CLAUDE_CHROME_MONITOR / BINANCE_CHROME_MONITOR — 1-based display index (Windows: sorted left-to-top).
  CHROME_SEPARATE_SITE_PROFILES — Windows: if True, uses temp --user-data-dir per site (not your normal profile).
    Default False so Claude/Binance use your usual Chrome profile and logins; enable only if both windows keep
    opening on the same monitor and you accept a separate profile for automation.
  OPEN_CHROME_FULLSCREEN — Fullscreen on the chosen monitor (Windows: new window is detected and snapped with SetWindowPos).
  JARVIS_WELCOME_* — TTS after the song (ElevenLabs). Configure via environment or a `.env`
    file next to this script (ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, etc.).
    With JARVIS_WELCOME_CACHE_ENABLED, audio is saved under `.cache/jarvis_welcome/` (WAV) and
    replayed when phrase + voice + model + format match—no repeat API call. Delete that folder
    or set JARVIS_WELCOME_CACHE_ENABLED=False to force a fresh fetch.
  The welcome sequence runs only once per process. The assistant speaks in the background so Cursor
    opens without waiting for playback to finish (restart the script to run again).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
import numpy as np
import requests
import sounddevice as sd

# --- tuning knobs -----------------------------------------------------------
SAMPLE_RATE = 44100
BLOCK_MS = 40
CHANNELS = 1

SPIKE_RATIO = 4.5
COOLDOWN_S = 1.0
MIN_DOUBLE_GAP_S = 0.05
MAX_DOUBLE_GAP_S = 0.35
RETRIGGER_RATIO = 0.55
NOISE_FLOOR_ALPHA = 0.992
MIN_RMS = 0.0005
QUIET_GATE_MULT = 2.2  # update noise floor only when below floor * this

# Spotify: "spotify:track:TRACK_ID" or https://open.spotify.com/track/...
# YouTube: https://www.youtube.com/watch?v=...
SONG_URI = "https://open.spotify.com/track/39shmbIHICJ2Wxnk1fPSdz?si=2900c75c2e2d4b82"

# Cursor: focus existing instance (no -n). Set OPEN_NEW_CURSOR_ON_DOUBLE_CLAP for a new window as well.
FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP = True
OPEN_NEW_CURSOR_ON_DOUBLE_CLAP = False
CURSOR_OPEN_FULLSCREEN = True

# Google Chrome (fallback: default browser). URLs overridable in .env.
OPEN_CLAUDE_CODE_IN_CHROME = True
OPEN_BINANCE_BTC_IN_CHROME = True
OPEN_CHROME_FULLSCREEN = True
# False = default Chrome profile (your normal user, extensions, cookies). True = temp dirs under %TEMP% per site.
CHROME_SEPARATE_SITE_PROFILES = False
# Which physical screen (1 = leftmost/top-first after sorting). Windows only; ignored elsewhere.
CLAUDE_CHROME_MONITOR = 1
BINANCE_CHROME_MONITOR = 3

JARVIS_WELCOME_ENABLED = True
JARVIS_WELCOME_PHRASE = (
    "Welcome home sir. "
    "Congratulations on the new client for your SaaS app—make sure to follow up. "
    "If it helps: a short, specific note while the deal is still fresh usually "
    "anchors trust better than a polished deck sent cold a few days later."
)
# Seconds after launching SONG_URI before speaking (gives Spotify/browser time to start).
JARVIS_AFTER_SONG_DELAY_S = 1.0
# Save ElevenLabs PCM as WAV under .cache/jarvis_welcome/; replay skips the API when the key matches.
JARVIS_WELCOME_CACHE_ENABLED = True

load_dotenv(Path(__file__).resolve().parent / ".env")

# --- session metrics ---------------------------------------------------------
class _Metrics:
    def __init__(self):
        self.commands = 0
        self.success = 0
        self.fail = 0
        self.latencies: list[float] = []

    def record(self, ok: bool, latency: float):
        self.commands += 1
        if ok:
            self.success += 1
        else:
            self.fail += 1
        self.latencies.append(latency)

    def summary(self) -> str:
        if self.commands == 0:
            return "No commands this session."
        avg = sum(self.latencies) / len(self.latencies) if self.latencies else 0
        rate = (self.success / self.commands * 100) if self.commands else 0
        return (
            f"Session: {self.commands} commands, "
            f"{self.success} ok / {self.fail} fail ({rate:.0f}% success), "
            f"avg latency {avg:.1f}s"
        )

metrics = _Metrics()

# --- command router (Llama 3.1 via Ollama) -----------------------------------
LLAMA_CMD_URL = (os.environ.get("OLLAMA_CMD_URL") or "http://localhost:11434/v1/chat/completions").strip()
LLAMA_CMD_MODEL = (os.environ.get("OLLAMA_CMD_MODEL") or "llama3.1").strip()
LLAMA_CMD_TIMEOUT = int(os.environ.get("OLLAMA_CMD_TIMEOUT") or "60")

LLAMA_SYSTEM_PROMPT = """You are Jarvis, my local desktop automation router.

My interaction flow is:
- I double-clap to wake you.
- The system records a short audio clip (around 3-5 seconds).
- A speech-to-text (STT) model like Whisper converts that audio into text.
- YOU receive that transcribed text and must decide what to do.

The transcription may contain filler words ("uh", "um", "please"), minor errors ("perplex city" instead of "perplexity"), and weird punctuation. You must map this noisy text to ONE ACTION.

You ONLY support these actions:

1) "open_url" -> open exactly ONE of:
   - "https://www.perplexity.ai"
   - "https://github.com"
   - "https://www.notion.so"
   - "https://github.com/Panniantong/Agent-Reach"
   - "https://github.com/supertone-inc/supertonic"
   - "https://github.com/pipecat-ai/pipecat"
   - "https://github.com/tinyhumansai/openhuman"

2) "open_app" -> open exactly ONE of:
   - "vscode" (VS Code editor)
   - "vscode_project" (VS Code opened to current folder)
   - "spotify"
   - "chrome"

3) "read_web" -> read a webpage and return its content.
   - url: the full URL to read.

4) "youtube_summary" -> get YouTube video content/subtitles.
   - url: the YouTube video URL.

5) "github_info" -> get GitHub repo details (stars, description, language).
   - owner: repo owner (e.g. "Panniantong")
   - repo: repo name (e.g. "Agent-Reach")

6) "system_message" -> speak a short answer, with NO desktop side effects.

Your output MUST be this exact JSON shape:

{
  "action_type": "open_url" | "open_app" | "read_web" | "youtube_summary" | "github_info" | "system_message",
  "app": "<app_name_or_null>",
  "url": "<url_or_null>",
  "owner": "<github_owner_or_null>",
  "repo": "<github_repo_or_null>",
  "music_query": "<music_description_or_null>",
  "message": "<short user-facing message>"
}

Mapping rules:
- Variants of "open/start/launch/bring up/show me" -> open.
- "perplexity", "perplex city", etc. -> open_url with "https://www.perplexity.ai".
- "github", "git hub", "get hub" -> open_url with "https://github.com".
- "notion" -> open_url with "https://www.notion.so".
- "agent reach", "agent-reach" -> open_url with "https://github.com/Panniantong/Agent-Reach".
- "supertonic" -> open_url with "https://github.com/supertone-inc/supertonic".
- "pipecat" -> open_url with "https://github.com/pipecat-ai/pipecat".
- "openhuman", "open human" -> open_url with "https://github.com/tinyhumansai/openhuman".
- "visual studio code", "vs code", "code", "my editor", "my IDE" -> open_app, app="vscode".
- "open my project folder", "open this project", "open current folder" -> open_app, app="vscode_project".
- "spotify", "music app" -> open_app, app="spotify"; if user describes music (lofi, chill beats, deep focus, focus music, coding music), put that into "music_query".
- "chrome", "browser", "google chrome" -> open_app, app="chrome".

Examples:
- "open agent reach" -> open_url, Agent-Reach repo.
- "open supertonic github" -> open_url, Supertonic repo.
- "open pipecat repo" -> open_url, Pipecat repo.
- "open openhuman" -> open_url, OpenHuman repo.
- "read this page https://example.com" -> read_web, the URL.
- "summarize this youtube video https://youtube.com/watch?v=..." -> youtube_summary, the URL.
- "what is the pipecat repo" -> github_info, owner="pipecat-ai", repo="pipecat".
- "tell me about agent-reach on github" -> github_info, owner="Panniantong", repo="Agent-Reach".

If the user asks a question ("what is RAG", "tell me a joke", "explain transformers"):
- Use "system_message" and answer briefly.

If user says "never mind", "cancel", "forget it", "nothing", "stop listening":
- "system_message" with message like "Okay, I will do nothing."

If user asks for anything unsupported or dangerous (open Slack/terminal, delete files, run scripts, search Google, etc.):
- Use "system_message" with "I am not configured to do that yet."

Always:
- Ignore filler words (uh, um, please, hey Jarvis, etc.).
- Be robust to minor STT spelling problems.
- Return ONE valid JSON object only, no markdown or extra text."""


LLAMA_BASE_URL = (os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434").strip()


def check_ollama() -> bool:
    try:
        r = requests.get(f"{LLAMA_BASE_URL}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m.get("name", "") for m in r.json().get("models", [])]
        if not any(LLAMA_CMD_MODEL in m for m in models):
            print(f"[JARVIS] {LLAMA_CMD_MODEL} not pulled. Run: ollama pull {LLAMA_CMD_MODEL}")
            return False
        return True
    except Exception as e:
        print(f"[JARVIS] Ollama not reachable: {e}")
        return False


def check_audio_device() -> bool:
    device_str = (os.environ.get("JARVIS_INPUT_DEVICE") or "").strip()
    if not device_str:
        print("[JARVIS] No JARVIS_INPUT_DEVICE set. Using system default mic.")
        return True
    if not device_str.isdigit():
        print(f"[JARVIS] JARVIS_INPUT_DEVICE must be a number, got '{device_str}'.")
        return False
    idx = int(device_str)
    try:
        devices = sd.query_devices()
        if idx < 0 or idx >= len(devices):
            print(f"[JARVIS] Device index {idx} not found. Available devices:")
            for i, d in enumerate(devices):
                if d.get("max_input_channels", 0) > 0:
                    print(f"  {i}: {d['name']}")
            return False
        dev = devices[idx]
        if dev.get("max_input_channels", 0) == 0:
            print(f"[JARVIS] Device {idx} ({dev['name']}) has no input channels. Pick a mic.")
            return False
        print(f"[JARVIS] Using input device {idx}: {dev['name']}")
        return True
    except Exception as e:
        print(f"[JARVIS] Could not validate audio device: {e}")
        return True


def check_tools() -> None:
    vscode = _vscode_executable()
    if vscode:
        print(f"[JARVIS] VS Code found: {vscode}")
    else:
        print("[JARVIS] VS Code not found. Set VSCODE_EXE_PATH in .env or install VS Code.")

    spotify_path = _spotify_executable()
    if spotify_path:
        print(f"[JARVIS] Spotify found: {spotify_path}")
    else:
        print("[JARVIS] Spotify exe not found. Will use spotify:search: URI or web fallback.")

    chrome = _chrome_executable()
    if chrome:
        print(f"[JARVIS] Chrome found: {chrome}")
    else:
        print("[JARVIS] Chrome not found. Will use default browser.")


def _spotify_executable() -> str | None:
    custom = (os.environ.get("SPOTIFY_EXE_PATH") or "").strip()
    if custom and os.path.isfile(custom):
        return custom
    if sys.platform == "win32":
        for candidate in (
            os.path.join(os.environ.get("APPDATA", ""), "Spotify", "Spotify.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Spotify", "Spotify.exe"),
        ):
            if os.path.isfile(candidate):
                return candidate
    return shutil.which("spotify")


def call_llama_for_action(user_text: str) -> dict:
    payload = {
        "model": LLAMA_CMD_MODEL,
        "messages": [
            {"role": "system", "content": LLAMA_SYSTEM_PROMPT},
            {"role": "user", "content": f'Heard from microphone: "{user_text}"'},
        ],
        "temperature": 0.0,
    }
    last_err = None
    for attempt in range(2):
        try:
            r = requests.post(LLAMA_CMD_URL, json=payload, timeout=LLAMA_CMD_TIMEOUT)
            if r.status_code >= 500 and attempt == 0:
                log.warning("Ollama 5xx (attempt 1), retrying...")
                time.sleep(1)
                continue
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            return json.loads(content)
        except json.JSONDecodeError:
            print("[JARVIS] I could not understand that command reliably. Try rephrasing.")
            return {
                "action_type": "system_message",
                "app": None,
                "url": None,
                "music_query": None,
                "message": "I could not understand that command reliably.",
            }
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(1)
                continue
    print(f"[JARVIS] Command router error: {last_err}")
    return {
        "action_type": "system_message",
        "app": None,
        "url": None,
        "music_query": None,
        "message": f"Command router error: {last_err}",
    }


def _run_curl(args: list[str], timeout: int = 30) -> tuple[bool, str]:
    """Run curl.exe with args, return (success, stdout_or_error)."""
    try:
        result = subprocess.run(
            ["curl.exe", "-s", "--max-time", str(timeout)] + args,
            capture_output=True, timeout=timeout + 5,
        )
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        if result.returncode == 0 and stdout:
            return True, stdout
        return False, stderr or "Empty response"
    except FileNotFoundError:
        return False, "curl.exe not found"
    except subprocess.TimeoutExpired:
        return False, "Request timed out"
    except Exception as e:
        return False, str(e)


def _read_webpage(url: str) -> str:
    """Read a webpage via Jina Reader (Agent Reach)."""
    ok, data = _run_curl(["https://r.jina.ai/" + url])
    if ok:
        return data[:3000]
    return f"Failed to read page: {data}"


def _youtube_summary(url: str) -> str:
    """Get YouTube video content via Jina Reader."""
    ok, data = _run_curl(["https://r.jina.ai/" + url])
    if ok:
        return data[:3000]
    return f"Failed to read YouTube video: {data}"


def _github_info(owner: str, repo: str) -> str:
    """Get GitHub repo info via API."""
    ok, data = _run_curl([f"https://api.github.com/repos/{owner}/{repo}"])
    if ok:
        try:
            info = json.loads(data)
            stars = info.get("stargazers_count", "?")
            lang = info.get("language", "?")
            desc = info.get("description", "No description")
            return f"{owner}/{repo}: {desc} | Language: {lang} | Stars: {stars}"
        except json.JSONDecodeError:
            return data[:2000]
    return f"Failed to get repo info: {data}"


def execute_action(action: dict) -> None:
    t = action.get("action_type")
    music_query = action.get("music_query")

    if t == "open_url":
        url = action.get("url")
        if url:
            print(f"[JARVIS] Opening {url}")
            webbrowser.open(url)
        else:
            print("[JARVIS] No URL provided.")

    elif t == "open_app":
        app = (action.get("app") or "").lower()
        if app == "spotify":
            _open_spotify(music_query=music_query)
        elif app == "chrome":
            chrome = _chrome_executable()
            if chrome:
                subprocess.Popen(
                    [chrome],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print("[JARVIS] Opening Chrome.")
            else:
                print("[JARVIS] Chrome not found. Install Chrome or check your PATH.")
        elif app in ("vscode", "vs_code", "code", "cursor"):
            exe = _vscode_executable()
            if exe:
                subprocess.Popen(
                    [exe],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print("[JARVIS] Opening VS Code.")
            else:
                print("[JARVIS] VS Code not found. Set VSCODE_EXE_PATH in .env or install VS Code.")
        elif app == "vscode_project":
            exe = _vscode_executable()
            if exe:
                subprocess.Popen(
                    [exe, "."],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print("[JARVIS] Opening project folder in VS Code.")
            else:
                print("[JARVIS] VS Code not found. Set VSCODE_EXE_PATH in .env or install VS Code.")
        else:
            print(f"[JARVIS] I am not configured to open '{app}'.")

    elif t == "read_web":
        url = action.get("url")
        if url:
            print(f"[JARVIS] Reading {url} ...")
            content = _read_webpage(url)
            print(f"[JARVIS] {content}")
        else:
            print("[JARVIS] No URL provided for read_web.")

    elif t == "youtube_summary":
        url = action.get("url")
        if url:
            print(f"[JARVIS] Reading YouTube video ...")
            content = _youtube_summary(url)
            print(f"[JARVIS] {content}")
        else:
            print("[JARVIS] No URL provided for youtube_summary.")

    elif t == "github_info":
        owner = action.get("owner")
        repo = action.get("repo")
        if owner and repo:
            print(f"[JARVIS] Fetching GitHub info for {owner}/{repo} ...")
            content = _github_info(owner, repo)
            print(f"[JARVIS] {content}")
        else:
            print("[JARVIS] Missing owner/repo for github_info.")

    elif t == "system_message":
        msg = action.get("message", "")
        if msg:
            print(f"[JARVIS] {msg}")

    else:
        print(f"[JARVIS] Unknown action type: {t}")


def _open_spotify(*, music_query: str | None = None) -> None:
    import urllib.parse

    if music_query and music_query.strip():
        search_uri = "spotify:search:" + urllib.parse.quote_plus(music_query.strip())
        if sys.platform == "win32":
            subprocess.Popen(
                ["cmd", "/c", "start", search_uri],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            webbrowser.open(search_uri)
        print(f"[JARVIS] Playing '{music_query}' on Spotify.")
        return

    exe = _spotify_executable()
    if exe:
        subprocess.Popen(
            [exe],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("[JARVIS] Opening Spotify.")
        return

    chrome = _chrome_executable()
    if chrome:
        subprocess.Popen(
            [chrome, "https://open.spotify.com"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        webbrowser.open("https://open.spotify.com")
    print("[JARVIS] Opening Spotify.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("clap_listen")

# --- speech-to-text (faster-whisper) -----------------------------------------
STT_MODEL_NAME = (os.environ.get("STT_MODEL") or "base").strip()
STT_DEVICE = (os.environ.get("STT_DEVICE") or "cpu").strip()
STT_COMPUTE_TYPE = (os.environ.get("STT_COMPUTE_TYPE") or "int8").strip()
STT_RECORD_SECONDS = float(os.environ.get("STT_RECORD_SECONDS") or "4")

_whisper_model = None


def _get_whisper():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    try:
        from faster_whisper import WhisperModel
        log.info("Loading Whisper model: %s (device=%s, compute=%s)", STT_MODEL_NAME, STT_DEVICE, STT_COMPUTE_TYPE)
        _whisper_model = WhisperModel(STT_MODEL_NAME, device=STT_DEVICE, compute_type=STT_COMPUTE_TYPE)
        return _whisper_model
    except ImportError:
        log.warning("faster-whisper not installed. Run: pip install faster-whisper")
        return None
    except Exception as e:
        log.warning("Could not load Whisper model: %s", e)
        return None


def record_audio(seconds: float | None = None) -> np.ndarray | None:
    dur = seconds or STT_RECORD_SECONDS
    try:
        audio = sd.rec(int(SAMPLE_RATE * dur), samplerate=SAMPLE_RATE, channels=1, dtype="float32")
        sd.wait()
        return audio.flatten()
    except Exception as e:
        log.warning("Audio recording failed: %s", e)
        return None


def transcribe(audio: np.ndarray) -> str:
    model = _get_whisper()
    if model is None:
        return ""
    try:
        segments, info = model.transcribe(audio, language="en", beam_size=1, vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text
    except Exception as e:
        log.warning("Transcription failed: %s", e)
        return ""


def _play_clap_beep() -> None:
    try:
        t = np.linspace(0, 0.15, int(SAMPLE_RATE * 0.15), False)
        tone = (0.3 * np.sin(2 * np.pi * 880 * t)).astype(np.float32)
        sd.play(tone, SAMPLE_RATE)
    except Exception:
        pass


def block_samples() -> int:
    n = int(SAMPLE_RATE * BLOCK_MS / 1000)
    return max(n, 1)


def rms_mono(block: np.ndarray) -> float:
    if block.ndim > 1:
        block = np.mean(block.astype(np.float64), axis=1)
    else:
        block = block.astype(np.float64)
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block**2)))


def _elevenlabs_pcm_sample_rate(output_format: str) -> int:
    override = (os.environ.get("ELEVENLABS_PCM_SAMPLE_RATE") or "").strip()
    if override.isdigit():
        return int(override)
    if output_format.startswith("pcm_"):
        try:
            return int(output_format.split("_", maxsplit=1)[1])
        except (ValueError, IndexError):
            pass
    return 24000


def elevenlabs_env_config() -> tuple[str, str, str, int]:
    """voice_id, model_id, output_format, pcm_sample_rate."""
    voice = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
    model = (os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()
    fmt = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()
    rate = _elevenlabs_pcm_sample_rate(fmt)
    return voice, model, fmt, rate


def _jarvis_welcome_cache_dir() -> Path:
    base = Path(__file__).resolve().parent
    override = (os.environ.get("JARVIS_WELCOME_CACHE_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return base / ".cache" / "jarvis_welcome"


def _jarvis_welcome_cache_path(
    text: str, voice_id: str, model_id: str, output_format: str
) -> Path:
    key = f"{text}|{voice_id}|{model_id}|{output_format}".encode()
    digest = hashlib.sha256(key).hexdigest()[:24]
    return _jarvis_welcome_cache_dir() / f"{digest}.wav"


def _play_pcm_wav_file(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            rate = wf.getframerate()
            if ch != 1 or sw != 2:
                log.warning("Unsupported cached WAV (channels=%s, width=%s).", ch, sw)
                return False
            raw = wf.readframes(wf.getnframes())
    except (OSError, wave.Error) as e:
        log.warning("Could not read cached welcome audio: %s", e)
        return False
    if not raw:
        return False
    pcm_i16 = np.frombuffer(raw, dtype=np.int16)
    pcm_f = pcm_i16.astype(np.float32) / 32768.0
    try:
        sd.play(pcm_f, rate)
        sd.wait()
    except Exception as e:
        log.warning("Could not play cached welcome audio: %s", e)
        return False
    return True


def _speak(text: str) -> None:
    """Speak text via ElevenLabs TTS. Non-blocking (returns after playback starts)."""
    vid, model_id, output_format, pcm_rate = elevenlabs_env_config()
    if not vid:
        return
    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        return
    cache_dir = _jarvis_welcome_cache_dir()
    cache_path = _jarvis_welcome_cache_dir() / f"say_{hashlib.sha256(text.encode()).hexdigest()[:16]}.wav"
    if cache_path.is_file():
        try:
            with wave.open(str(cache_path), "rb") as wf:
                raw = wf.readframes(wf.getnframes())
            pcm_i16 = np.frombuffer(raw, dtype=np.int16)
            sd.play(pcm_i16.astype(np.float32) / 32768.0, pcm_rate)
            return
        except Exception:
            pass
    try:
        from elevenlabs.client import ElevenLabs
        client = ElevenLabs(api_key=api_key)
        chunks = client.text_to_speech.convert(
            voice_id=vid, text=text, model_id=model_id, output_format=output_format,
        )
        raw = b"".join(chunks)
    except Exception:
        return
    if not raw:
        return
    pcm_i16 = np.frombuffer(raw, dtype=np.int16)
    sd.play(pcm_i16.astype(np.float32) / 32768.0, pcm_rate)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with wave.open(str(cache_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(pcm_rate)
            wf.writeframes(pcm_i16.tobytes())
    except Exception:
        pass


def _save_pcm_wav_file(path: Path, pcm_bytes: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with wave.open(str(tmp), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        tmp.replace(path)
    except OSError:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        raise


def say_jarvis_welcome() -> None:
    if not JARVIS_WELCOME_ENABLED or not JARVIS_WELCOME_PHRASE.strip():
        return
    text = JARVIS_WELCOME_PHRASE.strip()
    vid, model_id, output_format, pcm_rate = elevenlabs_env_config()
    if not vid:
        log.warning("Set ELEVENLABS_VOICE_ID in the environment for ElevenLabs TTS.")
        return

    cache_path = _jarvis_welcome_cache_path(text, vid, model_id, output_format)
    if JARVIS_WELCOME_CACHE_ENABLED and cache_path.is_file():
        log.info("Playing welcome from cache: %s", cache_path)
        if _play_pcm_wav_file(cache_path):
            return
        log.warning("Cache miss after read failure; fetching from ElevenLabs.")

    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        log.warning("Set ELEVENLABS_API_KEY in the environment for ElevenLabs TTS.")
        return
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        log.warning("Install dependencies: pip install -r requirements.txt")
        return
    try:
        client = ElevenLabs(api_key=api_key)
        chunks = client.text_to_speech.convert(
            voice_id=vid,
            text=text,
            model_id=model_id,
            output_format=output_format,
        )
        raw = b"".join(chunks)
    except Exception as e:
        log.warning("ElevenLabs TTS failed: %s", e)
        return
    if not raw:
        log.warning("ElevenLabs returned empty audio.")
        return
    if JARVIS_WELCOME_CACHE_ENABLED:
        try:
            _save_pcm_wav_file(cache_path, raw, pcm_rate)
            log.info("Saved welcome audio to cache: %s", cache_path)
        except OSError as e:
            log.warning("Could not save welcome cache: %s", e)
    pcm_i16 = np.frombuffer(raw, dtype=np.int16)
    pcm_f = pcm_i16.astype(np.float32) / 32768.0
    try:
        sd.play(pcm_f, pcm_rate)
        sd.wait()
    except Exception as e:
        log.warning("Could not play ElevenLabs audio: %s", e)


def play_song(uri: str) -> None:
    u = uri.strip()
    if not u:
        return
    try:
        if sys.platform == "win32":
            os.startfile(u)
        else:
            webbrowser.open(u)
    except OSError as e:
        log.warning("Could not open SONG_URI: %s", e)


def _chrome_executable() -> str | None:
    if sys.platform == "win32":
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            if not base:
                continue
            p = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.isfile(p):
                return p
    return shutil.which("google-chrome") or shutil.which("chrome")


def _win32_sorted_monitor_rects() -> list[tuple[int, int, int, int]]:
    """Each monitor as (left, top, right, bottom), sorted left-to-right then top-to-bottom."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    collected: list[tuple[int, int, int, int]] = []

    @ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )
    def _cb(_hm, _hdc, lprc, _lp):
        r = lprc.contents
        collected.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, _cb, 0)
    collected.sort(key=lambda t: (t[0], t[1]))
    return collected


def _chrome_monitor_top_left(one_based_index: int) -> tuple[int, int]:
    """Top-left corner on virtual desktop for monitor N (1-based)."""
    l, t, _, _ = _chrome_monitor_bounds(one_based_index)
    return (l, t)


def _chrome_monitor_bounds(one_based_index: int) -> tuple[int, int, int, int]:
    """Monitor N as (left, top, right, bottom), 1-based index (sorted like other Chrome helpers)."""
    rects = _win32_sorted_monitor_rects()
    if not rects:
        return (0, 0, 1920, 1080)
    idx = one_based_index - 1
    if idx < 0:
        idx = 0
    if idx >= len(rects):
        log.warning(
            "Monitor %d requested but only %d found; using last monitor.",
            one_based_index,
            len(rects),
        )
        idx = len(rects) - 1
    return rects[idx]


def _chrome_monitor_pixel_size(one_based_index: int) -> tuple[int, int]:
    l, t, r, b = _chrome_monitor_bounds(one_based_index)
    return (max(320, r - l), max(240, b - t))


def _chrome_window_size() -> tuple[int, int]:
    w = (os.environ.get("CHROME_WINDOW_WIDTH") or "1400").strip()
    h = (os.environ.get("CHROME_WINDOW_HEIGHT") or "900").strip()
    try:
        return (max(400, int(w)), max(300, int(h)))
    except ValueError:
        return (1400, 900)


def _chrome_site_user_data_dir(site_key: str) -> str:
    p = Path(tempfile.gettempdir()) / "clap-trigger-chrome" / site_key
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _chrome_new_window_wait_timeout_s() -> float:
    try:
        return max(3.0, float((os.environ.get("CHROME_NEW_WINDOW_WAIT_S") or "25").strip()))
    except ValueError:
        return 25.0


def _chrome_top_level_browser_hwnds_win32() -> set[int]:
    """HWND ints for visible-or-minimized top-level Chrome browser windows."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GW_OWNER = 4
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    found: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd: wintypes.HWND, _lp: wintypes.LPARAM) -> bool:
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return True
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return True
        try:
            buf = ctypes.create_unicode_buffer(4096)
            sz = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                return True
            exe_path = buf.value
        finally:
            kernel32.CloseHandle(hproc)
        if os.path.basename(exe_path).lower() != "chrome.exe":
            return True
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        w, h = r.right - r.left, r.bottom - r.top
        if w < 80 or h < 80:
            return True
        found.add(int(hwnd))
        return True

    user32.EnumWindows(_enum, 0)
    return found


def _wait_new_chrome_hwnd_win32(before: set[int], timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.12)
        now = _chrome_top_level_browser_hwnds_win32()
        new = now - before
        if not new:
            continue
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        best: int | None = None
        best_area = 0
        for h in new:
            r = wintypes.RECT()
            if user32.GetWindowRect(h, ctypes.byref(r)):
                a = max(0, r.right - r.left) * max(0, r.bottom - r.top)
                if a > best_area:
                    best_area = a
                    best = h
        if best is not None:
            return best
    return None


def _chrome_snap_window_to_monitor_win32(
    hwnd: int,
    one_based_monitor: int,
    *,
    fullscreen: bool,
    windowed_size: tuple[int, int] | None,
) -> None:
    import ctypes
    from ctypes import wintypes

    ml, mt, mr, mb = _chrome_monitor_bounds(one_based_monitor)
    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    SW_SHOWMAXIMIZED = 3
    HWND_TOP = 0
    SWP_SHOWWINDOW = 0x0040
    SWP_FRAMECHANGED = 0x0020
    flags = SWP_SHOWWINDOW | SWP_FRAMECHANGED

    user32.ShowWindow(hwnd, SW_RESTORE)
    if fullscreen:
        w, h = mr - ml, mb - mt
        x, y = ml, mt
    else:
        ww, wh = windowed_size or _chrome_window_size()
        w, h = ww, wh
        x = ml + max(0, (mr - ml - w) // 2)
        y = mt + max(0, (mb - mt - h) // 2)
    user32.SetWindowPos(hwnd, HWND_TOP, x, y, w, h, flags)

    if fullscreen:
        user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)
        KEYEVENTF_KEYUP = 0x0002
        VK_F11 = 0x7A
        fg = user32.GetForegroundWindow()
        tid_tgt = user32.GetWindowThreadProcessId(hwnd, None)
        tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if tid_fg and tid_tgt:
            user32.AttachThreadInput(tid_fg, tid_tgt, True)
        user32.SetForegroundWindow(hwnd)
        if tid_fg and tid_tgt:
            user32.AttachThreadInput(tid_fg, tid_tgt, False)
        user32.keybd_event(VK_F11, 0, 0, 0)
        user32.keybd_event(VK_F11, 0, KEYEVENTF_KEYUP, 0)


def _open_url_in_chrome(
    url: str,
    *,
    new_window: bool = True,
    label: str = "URL",
    window_position: tuple[int, int] | None = None,
    window_size: tuple[int, int] | None = None,
    fullscreen: bool = False,
    win32_post_fullscreen_monitor: int | None = None,
    user_data_dir: str | None = None,
) -> None:
    u = url.strip()
    if not u:
        return
    chrome = _chrome_executable()
    try:
        if chrome:
            args = [chrome]
            if user_data_dir:
                args.append(f"--user-data-dir={user_data_dir}")
                args.append("--no-first-run")
            if new_window:
                args.append("--new-window")
            if window_position is not None:
                x, y = window_position
                args.append(f"--window-position={x},{y}")
            if window_size:
                args.append(f"--window-size={window_size[0]},{window_size[1]}")
            if fullscreen and not (
                sys.platform == "win32" and win32_post_fullscreen_monitor is not None
            ):
                args.append("--start-fullscreen")
            args.append(u)
            popen_kw: dict = {
                "args": args,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            before: set[int] | None = None
            if sys.platform == "win32" and win32_post_fullscreen_monitor is not None:
                before = _chrome_top_level_browser_hwnds_win32()
            subprocess.Popen(**popen_kw)
            if sys.platform == "win32" and win32_post_fullscreen_monitor is not None:
                mon = win32_post_fullscreen_monitor
                hwnd = _wait_new_chrome_hwnd_win32(before, _chrome_new_window_wait_timeout_s())
                if hwnd is not None:
                    _chrome_snap_window_to_monitor_win32(
                        hwnd,
                        mon,
                        fullscreen=fullscreen,
                        windowed_size=window_size if not fullscreen else None,
                    )
                else:
                    log.warning(
                        "Chrome: timed out waiting for new window (%s); check "
                        "CHROME_NEW_WINDOW_WAIT_S or close extra Chrome instances.",
                        label,
                    )
        else:
            log.warning("Chrome not found; opening %s in default browser.", label)
            webbrowser.open(u)
    except OSError as e:
        log.warning("Could not open %s in Chrome: %s", label, e)


def open_claude_in_chrome() -> None:
    if not OPEN_CLAUDE_CODE_IN_CHROME:
        return
    url = (os.environ.get("CLAUDE_CODE_URL") or "https://claude.ai/new").strip()
    pos: tuple[int, int] | None = None
    size: tuple[int, int] | None = None
    fs = OPEN_CHROME_FULLSCREEN
    post_mon: int | None = None
    user_data: str | None = None
    if sys.platform == "win32":
        post_mon = CLAUDE_CHROME_MONITOR
        pos = _chrome_monitor_top_left(CLAUDE_CHROME_MONITOR)
        if fs:
            size = _chrome_monitor_pixel_size(CLAUDE_CHROME_MONITOR)
        else:
            size = _chrome_window_size()
        if CHROME_SEPARATE_SITE_PROFILES:
            user_data = _chrome_site_user_data_dir("claude")
    elif not fs:
        size = _chrome_window_size()
    else:
        size = None
    _open_url_in_chrome(
        url,
        new_window=True,
        label="Claude",
        window_position=pos,
        window_size=size,
        fullscreen=fs,
        win32_post_fullscreen_monitor=post_mon,
        user_data_dir=user_data,
    )


def open_binance_btc_in_chrome() -> None:
    if not OPEN_BINANCE_BTC_IN_CHROME:
        return
    url = (
        os.environ.get("BINANCE_BTC_URL")
        or "https://www.binance.com/en/trade/BTC_USDT"
    ).strip()
    pos: tuple[int, int] | None = None
    size: tuple[int, int] | None = None
    fs = OPEN_CHROME_FULLSCREEN
    post_mon: int | None = None
    user_data: str | None = None
    if sys.platform == "win32":
        post_mon = BINANCE_CHROME_MONITOR
        pos = _chrome_monitor_top_left(BINANCE_CHROME_MONITOR)
        if fs:
            size = _chrome_monitor_pixel_size(BINANCE_CHROME_MONITOR)
        else:
            size = _chrome_window_size()
        if CHROME_SEPARATE_SITE_PROFILES:
            user_data = _chrome_site_user_data_dir("binance")
    elif not fs:
        size = _chrome_window_size()
    else:
        size = None
    _open_url_in_chrome(
        url,
        new_window=True,
        label="Binance BTC",
        window_position=pos,
        window_size=size,
        fullscreen=fs,
        win32_post_fullscreen_monitor=post_mon,
        user_data_dir=user_data,
    )


def _vscode_executable() -> str | None:
    custom = (os.environ.get("VSCODE_EXE_PATH") or "").strip()
    if custom and os.path.isfile(custom):
        return custom
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        for sub in (
            "Programs\\Microsoft VS Code\\Code.exe",
            "Programs\\cursor\\Cursor.exe",
            "Programs\\Cursor\\Cursor.exe",
        ):
            if local:
                p = os.path.join(local, *sub.split("\\"))
                if os.path.isfile(p):
                    return p
    return shutil.which("code") or shutil.which("cursor")


def _cursor_largest_main_hwnd_win32() -> int | None:
    """Largest top-level Cursor.exe window (visible or minimized)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GW_OWNER = 4
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    candidates: list[tuple[int, wintypes.HWND]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd: wintypes.HWND, _lp: wintypes.LPARAM) -> bool:
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return True
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return True
        try:
            buf = ctypes.create_unicode_buffer(4096)
            sz = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                return True
            exe_path = buf.value
        finally:
            kernel32.CloseHandle(hproc)
        if os.path.basename(exe_path).lower() != "cursor.exe":
            return True
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        w, h = r.right - r.left, r.bottom - r.top
        if w < 200 or h < 200:
            return True
        candidates.append((w * h, hwnd))
        return True

    user32.EnumWindows(_enum, 0)
    if not candidates:
        return None
    return int(max(candidates, key=lambda t: t[0])[1])


def _cursor_foreground_hwnd_win32(hwnd: int) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    tid_tgt = user32.GetWindowThreadProcessId(hwnd, None)
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    if tid_fg and tid_tgt:
        user32.AttachThreadInput(tid_fg, tid_tgt, True)
    user32.SetForegroundWindow(hwnd)
    if tid_fg and tid_tgt:
        user32.AttachThreadInput(tid_fg, tid_tgt, False)


def _cursor_send_f11_fullscreen_win32(hwnd: int) -> None:
    """F11 toggles Zen/fullscreen in Cursor (Electron)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002
    VK_F11 = 0x7A
    _cursor_foreground_hwnd_win32(hwnd)
    user32.keybd_event(VK_F11, 0, 0, 0)
    user32.keybd_event(VK_F11, 0, KEYEVENTF_KEYUP, 0)


def _focus_existing_cursor_window_win32() -> bool:
    """Bring an existing Cursor.exe main window to the foreground (no new process)."""
    if sys.platform != "win32":
        return False
    hwnd = _cursor_largest_main_hwnd_win32()
    if hwnd is None:
        return False
    _cursor_foreground_hwnd_win32(hwnd)
    return True


def run_double_clap_actions() -> None:
    """Run outside the mic loop so sleeps do not stall capture."""
    print("[JARVIS] Listening for your command...")
    t0 = time.monotonic()
    audio = record_audio()
    if audio is None:
        print("[JARVIS] Recording failed. Type your command instead.")
        try:
            user_text = input("Command: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    else:
        rms = float(np.sqrt(np.mean(audio ** 2)))
        log.info("Audio captured: %d samples, rms=%.6f", len(audio), rms)
        user_text = transcribe(audio)
        if user_text:
            print(f"[JARVIS] Heard: \"{user_text}\"")
            log.info("Transcript: %s", user_text)
        else:
            print("[JARVIS] Could not understand. Type your command instead.")
            log.warning("Transcript empty (rms=%.6f)", rms)
            try:
                user_text = input("Command: ").strip()
            except (EOFError, KeyboardInterrupt):
                return
    if not user_text:
        return
    action = call_llama_for_action(user_text)
    log.info("Router JSON: %s", json.dumps(action))
    ok = action.get("action_type") != "system_message" or "not configured" not in (action.get("message") or "").lower()
    execute_action(action)
    latency = time.monotonic() - t0
    metrics.record(ok, latency)
    log.info("Executed: %s -> %s (%.1fs)", user_text, action.get("action_type"), latency)


def open_cursor_window() -> None:
    if not FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP and not OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
        return
    exe = _vscode_executable()
    if not exe:
        print("[JARVIS] VS Code not found. Set VSCODE_EXE_PATH in .env or install VS Code.")
        return
    popen_kw: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        if FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP:
            focused = (
                sys.platform == "win32" and _focus_existing_cursor_window_win32()
            )
            if not focused:
                subprocess.Popen([exe], **popen_kw)
        if OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
            subprocess.Popen([exe, "-n"], **popen_kw)
    except OSError as e:
        log.warning("Could not start or focus Cursor: %s", e)
        return
    if sys.platform == "win32" and CURSOR_OPEN_FULLSCREEN:
        time.sleep(0.5)
        hwnd = _cursor_largest_main_hwnd_win32()
        if hwnd is not None:
            _cursor_send_f11_fullscreen_win32(hwnd)
        else:
            log.warning("Cursor fullscreen: no Cursor window found to send F11.")


def run_cli_loop() -> None:
    print("[JARVIS] CLI mode. Type commands below. Ctrl+C to exit.")
    while True:
        try:
            user_text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[JARVIS] Stopped.")
            break
        if not user_text:
            continue
        action = call_llama_for_action(user_text)
        execute_action(action)


def run_tests() -> int:
    import json as _json

    cases = [
        ("open perplexity", "open_url", "https://www.perplexity.ai", None, None),
        ("launch perplexity", "open_url", "https://www.perplexity.ai", None, None),
        ("open github", "open_url", "https://github.com", None, None),
        ("open vs code", "open_app", None, "vscode", None),
        ("open my editor", "open_app", None, "vscode", None),
        ("open code", "open_app", None, "vscode", None),
        ("open spotify", "open_app", None, "spotify", None),
        ("play lofi on spotify", "open_app", None, "spotify", "lofi"),
        ("play some music", "open_app", None, "spotify", None),
        ("what is RAG?", "system_message", None, None, None),
        ("tell me a joke", "system_message", None, None, None),
        ("open notion", "open_url", "https://www.notion.so", None, None),
        # --- Whisper real-life noise tests ---
        ("perplex city", "open_url", "https://www.perplexity.ai", None, None),
        ("open the get hub", "open_url", "https://github.com", None, None),
        ("uh open my vs code please", "open_app", None, "vscode", None),
        # --- New extensions ---
        ("open notion", "open_url", "https://www.notion.so", None, None),
        ("open my project folder", "open_app", None, "vscode_project", None),
        ("open this project in code", "open_app", None, "vscode_project", None),
        # --- GitHub repo extensions ---
        ("open agent reach", "open_url", "https://github.com/Panniantong/Agent-Reach", None, None),
        ("open supertonic", "open_url", "https://github.com/supertone-inc/supertonic", None, None),
        ("open pipecat", "open_url", "https://github.com/pipecat-ai/pipecat", None, None),
        ("open openhuman", "open_url", "https://github.com/tinyhumansai/openhuman", None, None),
        # --- Agent Reach tools ---
        ("read this page https://example.com", "read_web", "https://example.com", None, None),
        ("summarize this youtube video https://youtube.com/watch?v=test123", "youtube_summary", "https://youtube.com/watch?v=test123", None, None),
        ("what is the pipecat repo", "github_info", None, None, None),
        ("tell me about agent-reach on github", "github_info", None, None, None),
    ]

    passed = 0
    failed = 0
    for user_text, expected_type, expected_url, expected_app, expected_music in cases:
        action = call_llama_for_action(user_text)
        ok = True
        reasons = []

        if action.get("action_type") != expected_type:
            ok = False
            reasons.append(f"action_type={action.get('action_type')!r} expected={expected_type!r}")

        if expected_url and action.get("url") != expected_url:
            ok = False
            reasons.append(f"url={action.get('url')!r} expected={expected_url!r}")

        if expected_app and (action.get("app") or "").lower() != expected_app:
            ok = False
            reasons.append(f"app={action.get('app')!r} expected={expected_app!r}")

        if expected_music is not None and expected_type == "open_app":
            mq = (action.get("music_query") or "").lower()
            if expected_music not in mq:
                ok = False
                reasons.append(f"music_query={action.get('music_query')!r} expected to contain {expected_music!r}")

        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        detail = f" ({'; '.join(reasons)})" if reasons else ""
        print(f"  [{status}] \"{user_text}\" => {action.get('action_type')}{detail}")

    print(f"\n  {passed} passed, {failed} failed, {len(cases)} total")
    return 0 if failed == 0 else 1


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Jarvis - local desktop assistant")
    parser.add_argument("--cli", action="store_true", help="Typed-only CLI mode (no clap detection)")
    parser.add_argument("--test", action="store_true", help="Run router test harness and exit")
    args = parser.parse_args()

    if not check_ollama():
        return 1
    check_audio_device()
    check_tools()

    if args.test:
        return run_tests()

    if args.cli:
        run_cli_loop()
        return 0

    # --- clap mode ---
    blocksize = block_samples()
    noise_floor = 1e-4
    last_logged_double = 0.0
    first_clap_time: float | None = None
    spike_armed = True

    input_device = (os.environ.get("JARVIS_INPUT_DEVICE") or "").strip()
    device_kw: dict = {}
    if input_device.isdigit():
        device_kw["device"] = int(input_device)

    print("[JARVIS] Listening. Double clap to wake. Ctrl+C to stop.")

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
            **device_kw,
        ) as stream:
            while True:
                data, overflowed = stream.read(blocksize)
                if overflowed:
                    log.warning("Input overflow; try a larger BLOCK_MS")

                level = rms_mono(data)

                quiet_gate = noise_floor * QUIET_GATE_MULT
                if level < quiet_gate:
                    noise_floor = NOISE_FLOOR_ALPHA * noise_floor + (
                        1.0 - NOISE_FLOOR_ALPHA
                    ) * level
                    noise_floor = max(noise_floor, 1e-7)

                threshold = max(noise_floor * SPIKE_RATIO, MIN_RMS)
                now = time.monotonic()
                retrigger_level = threshold * RETRIGGER_RATIO

                if level < retrigger_level:
                    spike_armed = True

                if (
                    spike_armed
                    and level >= threshold
                    and (now - last_logged_double) >= COOLDOWN_S
                ):
                    spike_armed = False
                    if first_clap_time is None:
                        first_clap_time = now
                    else:
                        gap = now - first_clap_time
                        if gap < MIN_DOUBLE_GAP_S:
                            pass
                        elif gap <= MAX_DOUBLE_GAP_S:
                            first_clap_time = None
                            last_logged_double = now
                            _play_clap_beep()
                            _speak("Yes, sir?")
                            print("[JARVIS] Yes, sir?")
                            log.info(
                                "Double clap detected (gap=%.3fs, rms=%.5f, "
                                "noise_floor=%.5f, threshold=%.5f)",
                                gap,
                                level,
                                noise_floor,
                                threshold,
                            )
                            threading.Thread(
                                target=run_double_clap_actions, daemon=True
                            ).start()
                        else:
                            first_clap_time = now

    except KeyboardInterrupt:
        print(f"\n[JARVIS] {metrics.summary()}")
        log.info("Stopped.")
        return 0
    except sd.PortAudioError as e:
        log.error("Audio error: %s", e)
        log.error("If PortAudio fails, install/repair drivers or try another SAMPLE_RATE.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
