# comfyui-mailbot

Email a prompt, get a ComfyUI render back as a reply.

The bot watches an IMAP mailbox (via IDLE), takes the body of each new email
from an allowed sender, feeds it into a ComfyUI workflow exported in API
format, waits for the result and replies to the email with the output
attached. Processed mails are moved to a `Processed` folder.

One instance serves one mailbox and one workflow. Run several instances, each
with its own config file, to offer different workflows on different
addresses, e.g. one address for images and one for text-to-speech.

## Modes

| `OUTPUT_KIND` | Email body                                                              | Reply attachment          |
|---------------|-------------------------------------------------------------------------|---------------------------|
| `image`       | The prompt, or `Prompt: ...` / `Negative: ...` lines                    | Image from a `SaveImage` node |
| `audio`       | The text to speak (whole body, quoted replies and signature stripped)   | Audio from a `SaveAudio` / `SaveAudioMP3` node |

### Choosing the voice (TTS)

With `VOICE_NODE_ID` set, the **subject** picks the voice sample: subject
`anna` uses `anna.wav` (or `.m4a`, `.mp3`, ...) from `ComfyUI/input`, matched
by file name without extension, ignoring case and `Re:`/`SV:`/`Fwd:`
prefixes. An empty or unknown subject falls back to the workflow's default
voice, and the reply lists the voices available. To add a voice, drop a
clean sample of the speaker (about 10 seconds or more) into `ComfyUI/input`;
no restart needed.

## Setup

Requires Python 3 and a running ComfyUI.

```bash
git clone https://github.com/MagnusNordin/comfyui-mailbot.git ~/comfyui-mailbot
cd ~/comfyui-mailbot
python3 -m venv venv
venv/bin/pip install imapclient python-dotenv requests
```

### Image bot

1. In ComfyUI, export your workflow with **Workflow -> Export (API format)**.
2. `cp .env.example .env && chmod 600 .env`, then fill in the mailbox
   credentials, `ALLOWED_SENDERS`, `WORKFLOW_PATH` and the node IDs (each
   node is a numbered key in the exported JSON; its `class_type` says what
   it is). Example workflows are in [`workflows/`](workflows/).
3. Install the service:

   ```bash
   sudo cp comfyui-mailbot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now comfyui-mailbot
   ```

### TTS bot

1. The included [`workflows/chatterbox_tts_api.json`](workflows/chatterbox_tts_api.json)
   uses [ComfyUI_Fill-ChatterBox](https://github.com/filliptm/ComfyUI_Fill-ChatterBox)
   (`FL_ChatterboxMultilingualTTS`, Swedish) and clones the voice from a
   sample loaded by its Load Audio node: put your own samples in
   `ComfyUI/input` and set the default one in node `4` (see
   [Choosing the voice](#choosing-the-voice-tts)). Output is MP3.
   To use another TTS workflow, export it in API format instead.
2. `cp tts.env.example tts.env && chmod 600 tts.env`, then fill in the
   mailbox settings. The node IDs are already set for the included
   workflow; for your own, `POSITIVE_NODE_ID` is the node that takes the
   text, `PROMPT_INPUT_NAME` the name of its text input, and
   `SAVE_IMAGE_NODE_ID` the save-audio node. Prefer MP3 output, which plays
   inline in more mail clients than FLAC.
3. Install the service, which runs the same script with `ENV_FILE=tts.env`:

   ```bash
   sudo cp comfyui-mailbot-tts.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now comfyui-mailbot-tts
   ```

The service files assume the user `magnor` and `/home/magnor/comfyui-mailbot`;
adjust `User` and the paths for your machine.

Logs: `journalctl -u comfyui-mailbot -f` (or `comfyui-mailbot-tts`).

## Configuration

| Variable | Default | |
|---|---|---|
| `ENV_FILE` | `.env` | Config file to load (set in the service file) |
| `IMAP_HOST`, `IMAP_PORT` | -, `993` | |
| `SMTP_HOST`, `SMTP_PORT` | -, `465` | |
| `EMAIL_USER`, `EMAIL_PASS` | - | Mailbox login |
| `ALLOWED_SENDERS` | empty (anyone) | Comma-separated sender addresses |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | |
| `WORKFLOW_PATH` | - | API-format workflow JSON |
| `OUTPUT_KIND` | `image` | `image` or `audio` |
| `POSITIVE_NODE_ID` | - | Node that receives the prompt/text |
| `PROMPT_INPUT_NAME` | `text` | Input on that node to set |
| `NEGATIVE_NODE_ID` | unset | Image mode only |
| `VOICE_NODE_ID` | unset | Load Audio node whose file the subject picks |
| `VOICE_INPUT_NAME` | `audio` | Input on that node to set |
| `DEFAULT_NEGATIVE_PROMPT` | see script | Image mode only |
| `SAVE_IMAGE_NODE_ID` | - | Save Image / Save Audio node |
| `RENDER_TIMEOUT_SECONDS` | `300` | |
| `PROCESSED_FOLDER` | `Processed` | |

## Security

Never commit a filled-in `.env` / `tts.env`; they are gitignored. Always set
`ALLOWED_SENDERS`: the check is against the `From` header, which can be
forged, so also keep the bot's address private.

## License

[MIT](LICENSE)
