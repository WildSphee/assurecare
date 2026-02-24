from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from openai import OpenAI
from prompt import SYSTEM_PROMPT

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"
DEFAULT_ELEVENLABS_VOICE_ID = "SDNKIYEpTz0h56jQX8rA"


@dataclass
class Config:
    elevenlabs_api_key: str
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"
    elevenlabs_stt_model: str = "scribe_v2"
    elevenlabs_tts_model: str = "eleven_flash_v2_5"
    elevenlabs_voice_id: str | None = None
    elevenlabs_voice_name: str | None = None
    tts_output_format: str = "pcm_16000"


def require_command(cmd: str) -> None:
    if shutil.which(cmd):
        return
    raise RuntimeError(f"Required command not found: {cmd}")


def run_cmd(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Command failed ({exc.returncode}): {' '.join(cmd)}") from exc


def record_audio_wav(output_path: Path, seconds: int, rate: int, channels: int, device: str | None) -> None:
    require_command("arecord")
    cmd = [
        "arecord",
        "-q",
        "-d",
        str(seconds),
        "-f",
        "S16_LE",
        "-r",
        str(rate),
        "-c",
        str(channels),
        "-t",
        "wav",
    ]
    if device:
        cmd.extend(["-D", device])
    cmd.append(str(output_path))
    run_cmd(cmd)


def play_pcm_16k_mono(input_path: Path, device: str | None) -> None:
    require_command("aplay")
    cmd = [
        "aplay",
        "-q",
        "-t",
        "raw",
        "-f",
        "S16_LE",
        "-r",
        "16000",
        "-c",
        "1",
    ]
    if device:
        cmd.extend(["-D", device])
    cmd.append(str(input_path))
    run_cmd(cmd)


def play_wav(input_path: Path, device: str | None) -> None:
    require_command("aplay")
    cmd = ["aplay", "-q"]
    if device:
        cmd.extend(["-D", device])
    cmd.append(str(input_path))
    run_cmd(cmd)


def save_pcm_as_wav(pcm_path: Path, wav_path: Path, sample_rate: int = 16000, channels: int = 1) -> None:
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)  # 16-bit PCM
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_path.read_bytes())


def parse_tts_output_format(output_format: str) -> tuple[str, int]:
    parts = output_format.split("_")
    if len(parts) < 2:
        raise RuntimeError(f"Invalid ELEVENLABS_TTS_OUTPUT_FORMAT: {output_format}")
    codec = parts[0].lower()
    try:
        sample_rate = int(parts[1])
    except ValueError as exc:
        raise RuntimeError(f"Invalid sample rate in ELEVENLABS_TTS_OUTPUT_FORMAT: {output_format}") from exc
    return codec, sample_rate


def normalize_lang_code(code: str | None) -> str | None:
    if not code:
        return None
    c = code.strip().lower()
    aliases = {
        "en": "eng",
        "eng": "eng",
        "zh": "zho",
        "zh-cn": "zho",
        "zh-sg": "zho",
        "cmn": "zho",
        "zho": "zho",
        "yue": "yue",
        "zh-hk": "yue",
        "zh-yue": "yue",
    }
    return aliases.get(c, c)


def elevenlabs_transcribe(
    audio_path: Path,
    cfg: Config,
    language_code: str | None,
) -> tuple[str, str | None]:
    url = f"{ELEVENLABS_BASE_URL}/speech-to-text"
    headers = {"xi-api-key": cfg.elevenlabs_api_key}
    data = {
        "model_id": cfg.elevenlabs_stt_model,
        "tag_audio_events": "false",
    }
    if language_code:
        data["language_code"] = language_code

    with audio_path.open("rb") as audio_file:
        files = {"file": (audio_path.name, audio_file, "audio/wav")}
        resp = requests.post(url, headers=headers, data=data, files=files, timeout=120)

    if not resp.ok:
        raise RuntimeError(f"ElevenLabs STT error {resp.status_code}: {resp.text[:500]}")

    payload = resp.json()
    text = (payload.get("text") or "").strip()
    if not text:
        raise RuntimeError(f"ElevenLabs STT returned no text: {json.dumps(payload)[:500]}")
    detected_lang = normalize_lang_code(payload.get("language_code"))
    return text, detected_lang


def elevenlabs_get_voices(cfg: Config) -> list[dict[str, Any]]:
    url = f"{ELEVENLABS_BASE_URL}/voices"
    headers = {"xi-api-key": cfg.elevenlabs_api_key}
    resp = requests.get(url, headers=headers, timeout=60)
    if not resp.ok:
        raise RuntimeError(
            "ELEVENLABS_VOICE_ID is not set and voice lookup failed: "
            f"{resp.status_code} {resp.text[:300]}"
        )
    voices = resp.json().get("voices", [])
    if not voices:
        raise RuntimeError("No ElevenLabs voices found. Set ELEVENLABS_VOICE_ID in .env.")
    return voices


def voice_search_blob(voice: dict[str, Any]) -> str:
    labels = voice.get("labels") or {}
    parts = [
        str(voice.get("name") or ""),
        str(voice.get("description") or ""),
        str(voice.get("category") or ""),
    ]
    if isinstance(labels, dict):
        parts.extend(str(v) for v in labels.values())
        parts.extend(str(k) for k in labels.keys())
    return " ".join(parts).lower()


def print_voices(voices: list[dict[str, Any]]) -> None:
    print("\nAvailable ElevenLabs voices:")
    for voice in voices:
        labels = voice.get("labels") or {}
        gender = labels.get("gender") if isinstance(labels, dict) else None
        accent = labels.get("accent") if isinstance(labels, dict) else None
        print(
            f"- {voice.get('name', 'unknown')} | id={voice.get('voice_id', 'n/a')} "
            f"| gender={gender or 'n/a'} | accent={accent or 'n/a'}"
        )


def elevenlabs_pick_voice(cfg: Config) -> str:
    if cfg.elevenlabs_voice_id:
        return cfg.elevenlabs_voice_id

    print(f"[voice] Using default ElevenLabs voice ID: {DEFAULT_ELEVENLABS_VOICE_ID}")
    return DEFAULT_ELEVENLABS_VOICE_ID


def elevenlabs_tts_to_audio(text: str, cfg: Config, output_path: Path) -> dict[str, Any]:
    voice_id = elevenlabs_pick_voice(cfg)
    url = f"{ELEVENLABS_BASE_URL}/text-to-speech/{voice_id}"
    codec, sample_rate = parse_tts_output_format(cfg.tts_output_format)
    accept_map = {
        "pcm": "audio/pcm",
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "ulaw": "audio/basic",
        "alaw": "audio/basic",
        "opus": "audio/ogg",
    }
    headers = {
        "xi-api-key": cfg.elevenlabs_api_key,
        "Content-Type": "application/json",
    }
    if codec in accept_map:
        headers["Accept"] = accept_map[codec]
    payload = {
        "text": text,
        "model_id": cfg.elevenlabs_tts_model,
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.75,
        },
    }
    resp = requests.post(
        url,
        headers=headers,
        params={"output_format": cfg.tts_output_format},
        json=payload,
        timeout=120,
    )
    if not resp.ok:
        raise RuntimeError(f"ElevenLabs TTS error {resp.status_code}: {resp.text[:500]}")
    output_path.write_bytes(resp.content)
    return {
        "codec": codec,
        "sample_rate": sample_rate,
        "content_type": resp.headers.get("Content-Type", ""),
        "bytes": len(resp.content),
    }


def chat_with_openai(user_text: str, history: list[dict[str, str]], cfg: Config) -> str:
    client = OpenAI(api_key=cfg.openai_api_key)
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history[-8:])
    messages.append({"role": "user", "content": user_text})

    completion = client.chat.completions.create(
        model=cfg.openai_model,
        messages=messages,
    )

    reply = completion.choices[0].message.content
    if not reply:
        raise RuntimeError("OpenAI returned an empty response.")
    return reply.strip()


def load_config() -> Config:
    load_dotenv()
    elevenlabs_api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not elevenlabs_api_key:
        raise RuntimeError("Missing ELEVENLABS_API_KEY in .env")
    if not openai_api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in .env")

    return Config(
        elevenlabs_api_key=elevenlabs_api_key,
        openai_api_key=openai_api_key,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        elevenlabs_stt_model=os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2"),
        elevenlabs_tts_model=os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5"),
        elevenlabs_voice_id=os.getenv("ELEVENLABS_VOICE_ID", "").strip() or None,
        elevenlabs_voice_name=os.getenv("ELEVENLABS_VOICE_NAME", "").strip() or None,
        tts_output_format=os.getenv("ELEVENLABS_TTS_OUTPUT_FORMAT", "pcm_16000"),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ASSURECare Raspberry Pi voice chatbot prototype")
    parser.add_argument("--once", action="store_true", help="Run one turn and exit")
    parser.add_argument("--text", help="Skip recording and send text directly")
    parser.add_argument("--record-seconds", type=int, default=6, help="Recording duration for arecord")
    parser.add_argument("--sample-rate", type=int, default=16000, help="Mic recording sample rate (Hz)")
    parser.add_argument("--channels", type=int, default=1, help="Mic channels")
    parser.add_argument(
        "--stt-language-code",
        help="Optional ElevenLabs STT language code (ISO 639-1/3), e.g. eng, zho, yue",
    )
    parser.add_argument("--mic-device", help="ALSA device string for arecord, e.g. plughw:1,0")
    parser.add_argument("--speaker-device", help="ALSA device string for aplay, e.g. plughw:0,0")
    parser.add_argument("--no-tts", action="store_true", help="Print bot text only; do not synthesize/play audio")
    parser.add_argument("--list-voices", action="store_true", help="List available ElevenLabs voices and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        cfg = load_config()
    except Exception as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 1

    if args.list_voices:
        try:
            print_voices(elevenlabs_get_voices(cfg))
        except Exception as exc:
            print(f"[voices] {exc}", file=sys.stderr)
            return 1
        return 0

    history: list[dict[str, str]] = []
    print("ASSURECare voice chatbot prototype")
    print("Enter text to test without mic, or press Enter to record.")
    print("Type 'q' to quit.")

    with tempfile.TemporaryDirectory(prefix="assurecare_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        saved_tts_dir = Path("outputs") / "tts_replies"
        saved_tts_dir.mkdir(parents=True, exist_ok=True)
        turn = 0

        while True:
            turn += 1
            if args.text and turn == 1:
                typed = args.text
            else:
                try:
                    typed = input("\n[ready] Press Enter to record, or type a message: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break

            if typed.lower() in {"q", "quit", "exit"}:
                break

            if typed:
                user_text = typed
            else:
                wav_path = tmp_path / "input.wav"
                print(f"[record] Recording {args.record_seconds}s...")
                try:
                    record_audio_wav(
                        wav_path,
                        seconds=args.record_seconds,
                        rate=args.sample_rate,
                        channels=args.channels,
                        device=args.mic_device,
                    )
                    print("[stt] Transcribing with ElevenLabs...")
                    user_text, detected_lang = elevenlabs_transcribe(
                        wav_path,
                        cfg,
                        args.stt_language_code,
                    )
                    if detected_lang:
                        print(f"[stt] Detected language: {detected_lang}")
                except Exception as exc:
                    print(f"[audio/stt] {exc}", file=sys.stderr)
                    if args.once:
                        return 1
                    continue

            print(f"[you] {user_text}")

            try:
                reply = chat_with_openai(user_text, history, cfg)
            except Exception as exc:
                print(f"[openai] {exc}", file=sys.stderr)
                if args.once:
                    return 1
                continue

            print(f"[bot] {reply}")

            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": reply})

            if not args.no_tts:
                codec, sample_rate = parse_tts_output_format(cfg.tts_output_format)
                temp_ext = "pcm" if codec == "pcm" else codec
                audio_path = tmp_path / f"reply.{temp_ext}"
                try:
                    print("[tts] Synthesizing with ElevenLabs...")
                    tts_meta = elevenlabs_tts_to_audio(reply, cfg, audio_path)
                    if codec == "pcm" and audio_path.read_bytes()[:4] in {b"RIFF", b"ID3\x00"}:
                        raise RuntimeError(
                            "Expected raw PCM but received a containerized format. "
                            "Try ELEVENLABS_TTS_OUTPUT_FORMAT=wav_16000."
                        )
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    if codec == "pcm":
                        saved_path = saved_tts_dir / f"reply_{timestamp}_turn{turn}.wav"
                        save_pcm_as_wav(audio_path, saved_path, sample_rate=sample_rate)
                    elif codec == "wav":
                        saved_path = saved_tts_dir / f"reply_{timestamp}_turn{turn}.wav"
                        saved_path.write_bytes(audio_path.read_bytes())
                    elif codec == "mp3":
                        saved_path = saved_tts_dir / f"reply_{timestamp}_turn{turn}.mp3"
                        saved_path.write_bytes(audio_path.read_bytes())
                    else:
                        saved_path = saved_tts_dir / f"reply_{timestamp}_turn{turn}.{codec}"
                        saved_path.write_bytes(audio_path.read_bytes())
                    print(
                        f"[save] TTS reply saved to {saved_path} "
                        f"(codec={tts_meta['codec']}, content-type={tts_meta['content_type'] or 'unknown'})"
                    )
                    print("[play] Playing reply...")
                    if codec == "pcm":
                        if sample_rate != 16000:
                            raise RuntimeError(
                                "PCM playback helper is currently fixed to 16kHz. "
                                "Set ELEVENLABS_TTS_OUTPUT_FORMAT=pcm_16000 or wav_16000."
                            )
                        play_pcm_16k_mono(audio_path, device=args.speaker_device)
                    elif codec == "wav":
                        play_wav(audio_path, device=args.speaker_device)
                    else:
                        print(f"[play] Skipping playback for codec '{codec}' (use pcm_16000 or wav_16000).")
                except Exception as exc:
                    print(f"[tts/play] {exc}", file=sys.stderr)
                    if args.once:
                        return 1

            if args.once:
                break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
