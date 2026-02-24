## ASSURECare Raspberry Pi 4 Voice Chatbot Prototype

This repo is now set up for a **manual Raspberry Pi 4 test prototype**:

- `arecord` captures microphone audio
- **ElevenLabs STT** transcribes speech
- **OpenAI (ChatGPT API)** generates the response
- **ElevenLabs TTS** synthesizes the reply
- `aplay` plays the spoken reply

This is intentionally simple so you can verify the API connections and audio path first.

There are now two runnable prototypes:

- `assurebot.py`: manual turn-taking (`Enter` to record or type text)
- `assure_dynamic_bot.py`: always-listening prototype (auto-start on speech, auto-send on silence)

## Why not OpenAI Agents SDK / Realtime (for now)?

You do **not** need the OpenAI Agents SDK or realtime streaming for this prototype.
For Raspberry Pi manual testing, a straightforward request/response loop is easier to debug:

1. Record audio
2. Transcribe (ElevenLabs)
3. Generate text (OpenAI)
4. Synthesize audio (ElevenLabs)
5. Play audio

Realtime/streaming can be added later after the basic pipeline is stable.

## Raspberry Pi 4 notes

- Target environment: **Raspberry Pi 4**
- Manual testing only (no automated audio tests)
- Uses ALSA CLI tools: `arecord`, `aplay`

Install ALSA tools if needed:

```bash
sudo apt-get update
sudo apt-get install -y alsa-utils
```

Check devices:

```bash
arecord -l
aplay -l
```

## Environment variables

Your `.env` already contains:

- `ELEVENLABS_API_KEY`
- `OPENAI_API_KEY`

Optional (recommended for stable TTS voice selection):

- `ELEVENLABS_VOICE_ID=<your_voice_id>`
- `OPENAI_MODEL=gpt-4o-mini`
- `ELEVENLABS_STT_MODEL=scribe_v2` (default in code)
- `ELEVENLABS_TTS_MODEL=eleven_flash_v2_5` (default in code; lower latency)
- `ELEVENLABS_TTS_OUTPUT_FORMAT=pcm_16000`

If `ELEVENLABS_VOICE_ID` is not set, the script will fetch your voice list and use the first available voice.

## Poetry + `venv/` setup

This project uses Poetry for dependency management, but you asked to use the existing `venv/`.

```bash
source venv/bin/activate
pip install poetry
poetry config virtualenvs.create false
poetry install
```

If `poetry` is already installed globally, still run `poetry config virtualenvs.create false` while `venv/` is active so packages install into the current environment.

## Run the bots

Manual interactive voice mode (`assurebot.py`):

```bash
source venv/bin/activate
python assurebot.py
```

One-turn text-only test (good for checking APIs before mic/speaker):

```bash
source venv/bin/activate
python assurebot.py --once --no-tts --text "Good morning, I checked my blood pressure, it is 128 over 78."
```

One-turn voice test (records 6 seconds, then speaks reply):

```bash
source venv/bin/activate
python assurebot.py --once
```

Use explicit ALSA devices if needed:

```bash
python assurebot.py --mic-device plughw:1,0 --speaker-device plughw:0,0
```

Always-listening mode (`assure_dynamic_bot.py`):

```bash
source venv/bin/activate
python assure_dynamic_bot.py
```

With explicit ALSA devices and VAD debugging:

```bash
python assure_dynamic_bot.py --mic-device plughw:1,0 --speaker-device plughw:0,0 --debug-vad
```

Tune speech detection if needed:

- Increase `--vad-threshold` if background noise triggers recording
- Increase `--end-silence-ms` if it cuts off too early
- Decrease `--end-silence-ms` if replies feel delayed after you stop talking

Example:

```bash
python assure_dynamic_bot.py --vad-threshold 900 --end-silence-ms 700
```

Note: `assure_dynamic_bot.py` is an auto-turn prototype, not full realtime streaming. It still uses batch ElevenLabs STT + blocking OpenAI + blocking ElevenLabs TTS, but removes the manual "press Enter to record" step.

## What the prompt is hard-coded to do

The bot is hard-coded as an **ASSURECare elderly care companion prototype** for Mr. Tan:

- BP check-in (morning/evening)
- Medication adherence reminders/checks
- Symptom check (dizziness, headache, chest discomfort)
- Simple rotating context (sleep / salty meal / stress / exercise)
- Calm, concise responses
- Emergency escalation language for urgent symptoms (without diagnosing)

This keeps the behavior aligned with your user story while staying lightweight for Raspberry Pi testing.

## API docs used (for this implementation and next steps)

- Speech to Text API (`/v1/speech-to-text`)
- Text to Speech API (`/v1/text-to-speech/{voice_id}`)
- ElevenLabs models (for `scribe_v2`, `eleven_flash_v2_5`)
- Realtime STT / WebSocket docs (for future latency work)
- ElevenLabs docs portal and Python SDK repo for reference
- OpenAI models docs (for `gpt-4o-mini` / GPT-5 variants)
- OpenAI Realtime / Voice Agent docs (future path)

Links:

- https://elevenlabs.io/docs/api-reference/speech-to-text/convert
- https://elevenlabs.io/docs/api-reference/text-to-speech/convert
- https://elevenlabs.io/docs/overview/models
- https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime
- https://elevenlabs.io/docs/websockets
- https://github.com/elevenlabs/elevenlabs-python
- https://developers.openai.com/api/docs/models/gpt-4o-mini
- https://developers.openai.com/api/docs/models/gpt-5-mini
- https://developers.openai.com/api/docs/models/gpt-5-nano
- https://platform.openai.com/docs/guides/realtime
