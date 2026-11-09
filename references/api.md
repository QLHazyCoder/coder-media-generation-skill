# Gateway API and Credential Contract

The client calls the user's gateway, not a vendor endpoint. `CODER_API_BASE_URL` selects the API base and defaults to `https://wcf.coderapi.vip/v1`. The public catalog is read without credentials from `https://wcf.coderapi.vip/api/pricing`; only model names, endpoint types, and enabled group names are retained. Price fields are ignored.

## Credential Map

The local config is outside the repository, normally `~/.config/coder-media-generation/credentials.json`. If the previous `~/.config/coder-api-image/credentials.json` exists, the skill keeps using it so the saved image credential remains available. The config file is mode `0600`, its directory mode `0700`.

```json
{
  "api_key": "<image-generation-key>",
  "audio_group_api_keys": {
    "音频模型": "<key-for-this-exact-group>"
  },
  "video_group_api_keys": {
    "视频模型": "<key-for-this-exact-group>",
    "seedance官渠 720p 海外版": "<key-for-this-exact-group>"
  }
}
```

The image key is independent. Every exact public video group has a separate entry in `video_group_api_keys`. A missing group entry never falls back to `api_key` or another group's value. An existing legacy `new_api_image_upload_key` field is not read or used by this skill; the selected video-group key also authenticates the fixed image-upload and media-upload requests, which only verify that the caller is a gateway user.

Every exact public audio group has a separate entry in `audio_group_api_keys`. A missing audio group entry never falls back to the image key or a video-group key. `CODER_AUDIO_GROUP_API_KEYS_JSON` provides the same exact-name mapping for automation.

Local reference uploads use the direct upload origin and return public URLs from the public media host:

- Endpoint: `POST https://wcf.coderapi.vip/media-upload/upload` by default
- Authentication: `Authorization: Bearer <selected video-group key>`
- Body: one multipart `file` field, streamed without redirect following
- Accepted containers: MP4/WebM/MOV video and MP3/WAV/M4A audio, at most 512 MiB per file
- Accepted response URL: `https://coderapi.vip/media-upload/<v_or_a_ULID>` by default, with no query or fragment

The upload origin is configurable with `CODER_MEDIA_UPLOAD_ORIGIN`. Image uploads use its `/image-upload/upload` path; video and audio uploads use its `/media-upload/upload` path. Public image and media bases are configurable with `CODER_IMAGE_PUBLIC_BASE_URL` and `CODER_MEDIA_PUBLIC_BASE_URL`. The client validates all three values as HTTPS URLs and never derives them from a request host.

Uploaded reference URLs are cached in the workflow state by file path, size, and mtime so an interrupted submission does not upload the same file twice. A failed upload stops before the billable video POST. `--publish --file <mp4> --group <exact group>` publishes an already downloaded local video through the same endpoint and returns its public URL; it is always explicit and never retries a task.

Environment overrides are scoped: `CODER_IMAGE_API_KEY` (or legacy `CODER_API_KEY`) is image-only. `CODER_AUDIO_GROUP_API_KEYS_JSON` and `CODER_VIDEO_GROUP_API_KEYS_JSON` provide exact JSON maps of audio or video group names to keys. Never store a key in a workflow state, argument, source file, log, or repository.

If a user gives a key in chat, pass it through non-terminal stdin to `--api-key-stdin`; never include it as a command argument. A video key save operation is associated with the already selected group and updates only that entry.

## Catalog and Group Membership

The live response's `supported_endpoint_types` and `enable_groups` determine current availability. Capability data is curated separately in `model-catalog.json`. Only the intersection of a public model and a verified local profile is selectable. Newly published groups with known models appear without code changes; newly published models remain visible as unconfigured and cannot be submitted until their parameter contract is reviewed and added.

The last verified public catalog contained these five video groups:

| Public group | Public video models in that group |
| --- | --- |
| `视频模型` | `grok-imagine-video`, `grok-imagine-video-1.5`, `H3`, `seedance-2.0`, `seedance-2.5`, `seedance-2.0-fast-offical`, `seedance-2.0-mini-offical`, `seedance-2.0-offical`, `wan3.0-official`, `doubao-seedance-2-0-260128`, `doubao-seedance-2-0-fast-260128`, `doubao-seedance-2-0-mini-260615`, `doubao-seedance-2-5-260628` |
| `seedance官渠 720&1080p 国内` | Four `doubao-seedance-*` models listed below |
| `视频-seedance官渠-全分辨率-国内` | Four `doubao-seedance-*` models listed below |
| `seedance官渠 全分辨率 海外版` | Four `doubao-seedance-*` models listed below |
| `seedance官渠 720p 海外版` | Four `doubao-seedance-*` models listed below |

The four models shared by the latter groups are `doubao-seedance-2-0-260128`, `doubao-seedance-2-0-fast-260128`, `doubao-seedance-2-0-mini-260615`, and `doubao-seedance-2-5-260628`. This table is an offline fallback snapshot, not a replacement for live group discovery.

The current audio fallback group is `音频模型`. Its verified models are `Suno v5.5`, `suno_music`, `suno-v6-mini`, `suno-v6-wild`, `gemini-3.1-flash-tts`, and `speech-2.8`. Live catalog membership remains authoritative.

## Image Requests

- Generate: `POST /images/generations` with the exact selected model and its catalog-defined layout fields.
- Edit: `POST /images/edits` multipart. Only `gpt-image-2` accepts editing in this skill. Local PNG/JPEG/WebP input is limited to 50 MiB.
- Image generation and editing are synchronous; this skill does not poll a separate image task URL. The client waits up to 300 seconds per request by default, with a hard maximum of 300 seconds. It requests one `b64_json` image and saves it locally; if the gateway returns `data[0].url` instead, it downloads that exact returned URL with the same timeout.
- All image profiles use `aspect_ratio` plus `resolution`. The gateway converts the shared layout to `size` for OpenAI-compatible channels and keeps the native fields for Gemini and xAI. The script supplies `quality=auto` and `output_format=png` only for GPT Image models. xAI profiles currently expose `1K` and `2K`; the gateway's global `4K` option does not imply that every upstream accepts it.

## Audio Requests

- TTS: `POST /audio/speech` with `model`, `input`, `voice`, `response_format`, `speed`, and optional `instructions`. The response is saved as a local audio file after format and size checks.
- Music: `POST /chat/completions` with the selected music model and one user message containing the music description. The client accepts returned audio data or a public audio URL and saves it locally.
- The exact group key is used for creation and any same-task query. Audio requests never use the image key or a video-group key.
- TTS voice names are model and service dependent. `gemini-3.1-flash-tts` exposes the verified common voice list in `model-catalog.json`; `speech-2.8` requires an explicitly supplied service-supported voice.
- Supported output formats and defaults are in `model-catalog.json`. The client does not infer a voice, format, or music setting from the model name.

## Video Requests

- Create: `POST /videos` with JSON or, for Grok local-image input, multipart.
- Status: `GET /videos/{id}`.
- Content: authenticated `GET /videos/{id}/content`, saved as a local MP4 after signature and size checks.
- Grok receives `seconds`; the ZeeQi and RollDek adapters receive `duration`. The public group and model name remain the exact names selected by the user.
- Normalized reference fields are `input_reference`, `reference_image_urls`, `reference_video_urls`, `reference_audio_urls`, `first_frame_image_url`, and `last_frame_image_url`. The skill does not send vendor-native `content` arrays. The installed task plugin is responsible for upstream format conversion.
- RollDek profiles use JSON public URLs; local image files are uploaded first because the adapter does not accept local multipart. ZeeQi profiles also use a public URL for local images. Grok accepts a local image as multipart. Upload uses the same selected video-group key as the video request; the token is sent only to the fixed upload endpoint and the configured gateway host.
- Supported parameter values and confirmed per-model duration/reference limits are in `model-catalog.json`. Do not infer options from names, pricing expressions, or another model's profile.
- The two base ZeeQi Seedance profiles pass `aspect_ratio` through as a non-empty string because the installed adapter does not declare an enum for them. For those models, ask the user for an exact value rather than inventing a list. Official ZeeQi and H3 profiles use an explicit five-value ratio enum; Grok and RollDek use their profile-specific enums.
- `wan3.0-official` is the public alias for ZeeQi's `wan3.0-yuanlian` upstream model. It accepts 2–30 seconds, `-1` for automatic duration, 480p/720p/1080p, the seven documented ratios (`adaptive`, `21:9`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16`), up to 10 images/5 videos/5 audio references, and the optional `prompt_extend`, `watermark`, `generate_audio`, and `seed` fields.

## Task Safety

Before a video POST, persist the `submitting` state. Once a request may have reached the gateway, do not submit again automatically, including on timeout, network error, `429`, or `5xx`. Retain the state for task-ID recovery. Polling and downloading the same task can be retried; the skill never creates a second task to recover a lost response.

Status and content requests use the same selected group's key as creation. Redirects are not followed, preventing bearer-token forwarding. MP4 output is capped at 512 MiB and is created exclusively, so an existing output file is not overwritten.

Before an audio POST, persist the `submitting` state. A timeout, network error, `429`, or `5xx` moves the workflow to an uncertain state and never triggers an automatic second submission. Audio output files are capped at 512 MiB and are created exclusively.
