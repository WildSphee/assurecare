from __future__ import annotations

import argparse
import math
import subprocess
import sys
import tempfile
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

from assurebot import (
    chat_with_openai,
    elevenlabs_transcribe,
    elevenlabs_tts_to_audio,
    load_config,
    parse_tts_output_format,
    play_pcm_16k_mono,
    play_wav,
    require_command,
    save_pcm_as_wav,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ASSURECare always-listening prototype (auto-start on speech, auto-stop on silence)"
    )
    parser.add_argument("--once", action="store_true", help="Run one detected utterance and exit")
    parser.add_argument("--sample-rate", type=int, default=16000, help="Mic recording sample rate (Hz)")
    parser.add_argument("--channels", type=int, default=1, help="Mic channels")
    parser.add_argument("--mic-device", help="ALSA device string for arecord, e.g. plughw:1,0")
    parser.add_argument("--speaker-device", help="ALSA device string for aplay, e.g. plughw:0,0")
    parser.add_argument("--stt-language-code", help="Optional ElevenLabs STT language code (eng, zho, yue, etc.)")
    parser.add_argument("--no-tts", action="store_true", help="Print bot text only; do not synthesize/play audio")
    parser.add_argument("--vad-threshold", type=int, default=650, help="RMS threshold for speech detection")
    parser.add_argument("--chunk-ms", type=int, default=30, help="Audio chunk size in ms for VAD")
    parser.add_argument("--preroll-ms", type=int, default=300, help="Audio kept before speech trigger")
    parser.add_argument("--start-speech-ms", type=int, default=180, help="Voiced ms required to start a turn")
    parser.add_argument("--end-silence-ms", type=int, default=900, help="Silence ms to end a turn")
    parser.add_argument("--max-utterance-seconds", type=int, default=15, help="Hard limit per utterance")
    parser.add_argument("--debug-vad", action="store_true", help="Print VAD state transitions and levels")
    return parser.parse_args()


def write_wav_from_frames(
    output_path: Path,
    frames: list[bytes],
    sample_rate: int,
    channels: int,
    sample_width: int = 2,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        for frame in frames:
            wav_file.writeframes(frame)


def rms_s16le(chunk: bytes, channels: int) -> int:
    frame_width = 2 * channels
    if not chunk or len(chunk) % frame_width != 0:
        return 0
    samples = memoryview(chunk).cast("h")
    total_sq = 0
    count = 0
    for sample in samples:
        value = int(sample)
        total_sq += value * value
        count += 1
    if count == 0:
        return 0
    return math.isqrt(total_sq // count)


def capture_until_silence(
    output_path: Path,
    *,
    sample_rate: int,
    channels: int,
    device: str | None,
    chunk_ms: int,
    vad_threshold: int,
    preroll_ms: int,
    start_speech_ms: int,
    end_silence_ms: int,
    max_utterance_seconds: int,
    debug_vad: bool,
) -> None:
    require_command("arecord")
    bytes_per_sample = 2
    bytes_per_second = sample_rate * channels * bytes_per_sample
    chunk_bytes = max(bytes_per_second * chunk_ms // 1000, bytes_per_sample * channels)
    chunk_bytes -= chunk_bytes % (bytes_per_sample * channels)
    if chunk_bytes <= 0:
        raise RuntimeError("Invalid audio chunk size; adjust --chunk-ms/--sample-rate/--channels.")

    cmd = [
        "arecord",
        "-q",
        "-f",
        "S16_LE",
        "-r",
        str(sample_rate),
        "-c",
        str(channels),
        "-t",
        "raw",
    ]
    if device:
        cmd.extend(["-D", device])
    cmd.append("-")

    pre_chunks = max(1, preroll_ms // max(chunk_ms, 1))
    preroll: deque[bytes] = deque(maxlen=pre_chunks)
    frames: list[bytes] = []
    speaking = False
    voiced_ms = 0
    silence_ms = 0
    utterance_ms = 0
    peak_rms = 0

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    if proc.stdout is None:
        proc.kill()
        raise RuntimeError("Failed to capture audio stream from arecord.")

    try:
        if debug_vad:
            print(f"[vad] Listening (threshold={vad_threshold}, chunk_ms={chunk_ms})...")
        while True:
            chunk = proc.stdout.read(chunk_bytes)
            if not chunk:
                raise RuntimeError("arecord stopped while waiting for speech.")

            if len(chunk) % (bytes_per_sample * channels) != 0:
                continue

            rms = rms_s16le(chunk, channels)
            peak_rms = max(peak_rms, rms)
            is_voiced = rms >= vad_threshold

            if not speaking:
                preroll.append(chunk)
                if is_voiced:
                    voiced_ms += chunk_ms
                else:
                    voiced_ms = 0
                if voiced_ms >= start_speech_ms:
                    speaking = True
                    frames.extend(preroll)
                    utterance_ms = len(frames) * chunk_ms
                    silence_ms = 0
                    if debug_vad:
                        print(f"[vad] Speech start (rms={rms}, peak={peak_rms})")
            else:
                frames.append(chunk)
                utterance_ms += chunk_ms
                if is_voiced:
                    silence_ms = 0
                else:
                    silence_ms += chunk_ms
                if silence_ms >= end_silence_ms:
                    if debug_vad:
                        print(f"[vad] Speech end by silence (peak={peak_rms})")
                    break
                if utterance_ms >= max_utterance_seconds * 1000:
                    if debug_vad:
                        print(f"[vad] Speech end by max length (peak={peak_rms})")
                    break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)

    if not frames:
        raise RuntimeError("No speech captured.")

    write_wav_from_frames(output_path, frames, sample_rate=sample_rate, channels=channels)


def main() -> int:
    args = parse_args()
    try:
        cfg = load_config()
    except Exception as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 1

    history: list[dict[str, str]] = []
    print("ASSURECare dynamic voice bot prototype")
    print("Always listening. Speak to start; pause to send. Press Ctrl+C to quit.")

    with tempfile.TemporaryDirectory(prefix="assurecare_dynamic_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        saved_tts_dir = Path("outputs") / "tts_replies"
        saved_tts_dir.mkdir(parents=True, exist_ok=True)
        turn = 0

        while True:
            turn += 1
            wav_path = tmp_path / f"input_turn{turn}.wav"
            try:
                capture_until_silence(
                    wav_path,
                    sample_rate=args.sample_rate,
                    channels=args.channels,
                    device=args.mic_device,
                    chunk_ms=args.chunk_ms,
                    vad_threshold=args.vad_threshold,
                    preroll_ms=args.preroll_ms,
                    start_speech_ms=args.start_speech_ms,
                    end_silence_ms=args.end_silence_ms,
                    max_utterance_seconds=args.max_utterance_seconds,
                    debug_vad=args.debug_vad,
                )
                print("[stt] Transcribing with ElevenLabs...")
                user_text, detected_lang = elevenlabs_transcribe(
                    wav_path,
                    cfg,
                    args.stt_language_code,
                )
                if detected_lang:
                    print(f"[stt] Detected language: {detected_lang}")
            except KeyboardInterrupt:
                print()
                break
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
                audio_path = tmp_path / f"reply_turn{turn}.{temp_ext}"
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
