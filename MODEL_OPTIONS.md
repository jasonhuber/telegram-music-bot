# Model Options For Better Music Generation

The Telegram loop is now plumbing: Dubo routes messages, downloads audio, normalizes clips with FFmpeg, and sends jobs to the local generator. The main quality jump comes from the generator behind `MUSIC_GENERATOR_COMMAND`.

## Best First Choices

### ACE-Step

Good fit for full text-to-music songs and promptable music generation. The repo exposes an `acestep` app command and supports local checkpoint loading/download. It is the best first model to wire for complete tracks.

Repo: https://github.com/ace-step/ACE-Step

### AudioCraft / MusicGen

Good fit for prompt-to-music and melody-conditioned generation. This is attractive for uploaded clips because MusicGen has melodic conditioning, so a short hum/riff can steer the generated result.

Repo: https://github.com/facebookresearch/audiocraft

### Stable Audio Tools

Good fit for experimenting with Stability AI audio-generation models and longer-form conditional audio workflows. Worth testing if the machine has enough GPU headroom.

Repo: https://github.com/Stability-AI/stable-audio-tools

## Useful Companion Tools

### Basic Pitch

Converts uploaded audio into MIDI. Useful when the Telegram clip is a sung/hummed melody or simple instrument riff and you want the generated loop to follow the notes.

Repo: https://github.com/spotify/basic-pitch

### DDSP

Useful for timbre transfer and sound transformation. It is less of a "make me a song" engine and more of a way to turn a source recording into a different playable texture.

Repo: https://github.com/magenta/ddsp

### Demucs

Useful if uploaded audio contains mixed material and you want to separate drums, bass, vocals, or other stems before looping or conditioning another model.

Repo: https://github.com/facebookresearch/demucs

## Recommended Direction

Start with one of these two paths:

1. **ACE-Step first** for full prompt-driven songs.
2. **MusicGen melody first** for Telegram audio clips, riffs, hums, and loop sketches.

The service already passes:

```text
MUSICBOT_PROMPT_TEXT
MUSICBOT_PROMPT_JSON
MUSICBOT_REFERENCE_PATH
MUSICBOT_OUTPUT_PATH
MUSICBOT_DURATION_SECONDS
MUSICBOT_SEED
```

So the next code layer should be a generator wrapper that reads those values and writes the final audio to `MUSICBOT_OUTPUT_PATH`.
