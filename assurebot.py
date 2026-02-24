from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"

SYSTEM_PROMPT = """
You are ASSURECare, a calm voice companion for elderly cardiac care check-ins.

Context:
- The patient is Mr. Tan, 68, living alone on weekday mornings.
- He has hypertension and a past mild heart attack.
- Family caregiver (Ms. Tan) and/or helper may also use this chatbot.
- This prototype focuses on: BP readings, medication adherence, dizziness, headache,
  chest discomfort, and one simple context question (sleep / salty meal / stress / exercise).

Behavior rules:
- Be simple, concise, and supportive. This is a prototype connection test.
- Respond in the same language as the user when possible (English or Mandarin).
- Do not give a medical diagnosis.
- If symptoms sound urgent (severe chest pain, fainting, breathing difficulty, confusion,
  extreme BP), clearly advise immediate emergency help.
- For routine check-ins, ask at most one follow-up question.
- If a user reports a BP reading, acknowledge and briefly interpret in plain language
  (e.g., in range / a bit high / low) without overclaiming.
- Prefer actionable, caregiver-friendly wording.
""".strip()


@dataclass
class Config:
    elevenlabs_api_key: str
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"
    elevenlabs_stt_model: str = "scribe_v1"
    elevenlabs_tts_model: str = "eleven_multilingual_v2"
    elevenlabs_voice_id: str | None = None
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


def elevenlabs_transcribe(audio_path: Path, cfg: Config, language_code: str | None) -> str:
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
    return text


def elevenlabs_pick_first_voice(cfg: Config) -> str:
    if cfg.elevenlabs_voice_id:
        return cfg.elevenlabs_voice_id

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
    voice = voices[0]
    voice_id = voice.get("voice_id")
    if not voice_id:
        raise RuntimeError("Voice lookup response missing voice_id.")
    print(f"[voice] Using ElevenLabs voice: {voice.get('name', 'unknown')} ({voice_id})")
    return voice_id


def elevenlabs_tts_to_pcm(text: str, cfg: Config, output_path: Path) -> None:
    voice_id = elevenlabs_pick_first_voice(cfg)
    url = f"{ELEVENLABS_BASE_URL}/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": cfg.elevenlabs_api_key,
        "Accept": "audio/pcm",
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": cfg.elevenlabs_tts_model,
        "output_format": cfg.tts_output_format,
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.75,
        },
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    if not resp.ok:
        raise RuntimeError(f"ElevenLabs TTS error {resp.status_code}: {resp.text[:500]}")
    output_path.write_bytes(resp.content)


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
        elevenlabs_stt_model=os.getenv("ELEVENLABS_STT_MODEL", "scribe_v1"),
        elevenlabs_tts_model=os.getenv("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2"),
        elevenlabs_voice_id=os.getenv("ELEVENLABS_VOICE_ID", "").strip() or None,
        tts_output_format=os.getenv("ELEVENLABS_TTS_OUTPUT_FORMAT", "pcm_16000"),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ASSURECare Raspberry Pi voice chatbot prototype")
    parser.add_argument("--once", action="store_true", help="Run one turn and exit")
    parser.add_argument("--text", help="Skip recording and send text directly")
    parser.add_argument("--record-seconds", type=int, default=6, help="Recording duration for arecord")
    parser.add_argument("--sample-rate", type=int, default=16000, help="Mic recording sample rate (Hz)")
    parser.add_argument("--channels", type=int, default=1, help="Mic channels")
    parser.add_argument("--stt-language-code", help="Optional ElevenLabs STT language code (e.g. en, zh)")
    parser.add_argument("--mic-device", help="ALSA device string for arecord, e.g. plughw:1,0")
    parser.add_argument("--speaker-device", help="ALSA device string for aplay, e.g. plughw:0,0")
    parser.add_argument("--no-tts", action="store_true", help="Print bot text only; do not synthesize/play audio")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        cfg = load_config()
    except Exception as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 1

    history: list[dict[str, str]] = []

    print("ASSURECare voice chatbot prototype")
    print("Enter text to test without mic, or press Enter to record.")
    print("Type 'q' to quit.")

    with tempfile.TemporaryDirectory(prefix="assurecare_") as tmp_dir:
        tmp_path = Path(tmp_dir)
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
                    user_text = elevenlabs_transcribe(wav_path, cfg, args.stt_language_code)
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
                pcm_path = tmp_path / "reply.pcm"
                try:
                    print("[tts] Synthesizing with ElevenLabs...")
                    elevenlabs_tts_to_pcm(reply, cfg, pcm_path)
                    print("[play] Playing reply...")
                    play_pcm_16k_mono(pcm_path, device=args.speaker_device)
                except Exception as exc:
                    print(f"[tts/play] {exc}", file=sys.stderr)
                    if args.once:
                        return 1

            if args.once:
                break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
