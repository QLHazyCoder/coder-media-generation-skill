# Gateway API and Credential Contract

The client calls the user's gateway, not a vendor endpoint. `CODER_API_BASE_URL` selects the API base and defaults to `https://api.qlhazycoder.tech/v1`. The public catalog is read without credentials from `https://coderapi.vip/api/pricing`; only model names, endpoint types, and enabled group names are retained. Price fields are ignored.

## Credential Map

The local config is outside the repository, normally `~/.config/coder-media-generation/credentials.json`. If the previous `~/.config/coder-api-image/credentials.json` exists, the skill keeps using it so the saved image credential remains available. The config file is mode `0600`, its directory mode `0700`.

```json
{
  "api_key": "<image-generation-key>",
  "video_group_api_keys": {
    "视频模型": "<key-for-this-exact-group>",
    "seedance官渠 720p 海外版": "<key-for-this-exact-group>"
  }
}
```

The image key is independent. Every exact public video group has a separate entry in `video_group_api_keys`. A missing group entry never falls back to `api_key` or another group's value. An existing legacy `new_api_image_upload_key` field is not read or used by this skill; the selected video-group key also authenticates the fixed image-upload and media-upload requests, which only verify that the caller is a gateway user.

Local reference videos and audio use the fixed media host:

- Endpoint: `POST https://api.qlhazycoder.tech/media-upload/upload`
- Authentication: `Authorization: Bearer <selected video-group key>`
- Body: one multipart `file` field, streamed without redirect following
- Accepted containers: MP4/WebM/MOV video and MP3/WAV/M4A audio, at most 512 MiB per file
- Accepted response URL: `https://api.qlhazycoder.tech/media-upload/<v_or_a_ULID>` with no query or fragment

Uploaded reference URLs are cached in the workflow state by file path, size, and mtime so an interrupted submission does not upload the same file twice. A failed upload stops before the billable video POST. `--publish --file <mp4> --group <exact group>` publishes an already downloaded local video through the same endpoint and returns its public URL; it is always explicit and never retries a task.

Environment overrides are scoped: `CODER_IMAGE_API_KEY` (or legacy `CODER_API_KEY`) is image-only. `CODER_VIDEO_GROUP_API_KEYS_JSON` may provide an exact JSON map of video group names to keys. Never store a key in a workflow state, argument, source file, log, or repository.

If a user gives a key in chat, pass it through non-terminal stdin to `--api-key-stdin`; never include it as a command argument. A video key save operation is associated with the already selected group and updates only that entry.

## Catalog and Group Membership

The live response's `supported_endpoint_types` and `enable_groups` determine current availability. Capability data is curated separately in `model-catalog.json`. Only the intersection of a public model and a verified local profile is selectable. Newly published groups with known models appear without code changes; newly published models remain visible as unconfigured and cannot be submitted until their parameter contract is reviewed and added.

The last verified public catalog contained these five video groups:

| Public group | Public video models in that group |
| --- | --- |
| `视频模型` | `grok-imagine-video`, `grok-imagine-video-1.5`, `H3`, `seedance-2.0`, `seedance-2.5`, `seedance-2.0-fast-offical`, `seedance-2.0-mini-offical`, `seedance-2.0-offical`, `doubao-seedance-2-0-260128`, `doubao-seedance-2-0-fast-260128`, `doubao-seedance-2-0-mini-260615`, `doubao-seedance-2-5-260628` |
| `seedance官渠 720&1080p 国内` | Four `doubao-seedance-*` models listed below |
| `视频-seedance官渠-全分辨率-国内` | Four `doubao-seedance-*` models listed below |
| `seedance官渠 全分辨率 海外版` | Four `doubao-seedance-*` models listed below |
| `seedance官渠 720p 海外版` | Four `doubao-seedance-*` models listed below |

The four models shared by the latter groups are `doubao-seedance-2-0-260128`, `doubao-seedance-2-0-fast-260128`, `doubao-seedance-2-0-mini-260615`, and `doubao-seedance-2-5-260628`. This table is an offline fallback snapshot, not a replacement for live group discovery.

## Image Requests

- Generate: `POST /images/generations` with the exact selected model and its catalog-defined layout fields.
- Edit: `POST /images/edits` multipart. Only `gpt-image-2` accepts editing in this skill. Local PNG/JPEG/WebP input is limited to 50 MiB.
- All image calls use one output image and the selected image credential. The client accepts a base64 response or a URL response and saves a local file.
- GPT Image variants use `size`; Gemini and Grok image profiles use `aspect_ratio` plus `resolution`. The script supplies `quality=auto` and `output_format=png` only for GPT Image models.

## Video Requests

- Create: `POST /videos` with JSON or, for Grok local-image input, multipart.
- Status: `GET /videos/{id}`.
- Content: authenticated `GET /videos/{id}/content`, saved as a local MP4 after signature and size checks.
- Grok receives `seconds`; the ZeeQi and RollDek adapters receive `duration`. The public group and model name remain the exact names selected by the user.
- Normalized reference fields are `input_reference`, `reference_image_urls`, `reference_video_urls`, `reference_audio_urls`, `first_frame_image_url`, and `last_frame_image_url`. The skill does not send vendor-native `content` arrays. The installed task plugin is responsible for upstream format conversion.
- RollDek profiles use JSON public URLs; local image files are uploaded first because the adapter does not accept local multipart. ZeeQi profiles also use a public URL for local images. Grok accepts a local image as multipart. Upload uses the same selected video-group key as the video request; the token is sent only to the fixed upload endpoint and the configured gateway host.
- Supported parameter values and confirmed per-model duration/reference limits are in `model-catalog.json`. Do not infer options from names, pricing expressions, or another model's profile.
- The two base ZeeQi Seedance profiles pass `aspect_ratio` through as a non-empty string because the installed adapter does not declare an enum for them. For those models, ask the user for an exact value rather than inventing a list. Official ZeeQi and H3 profiles use an explicit five-value ratio enum; Grok and RollDek use their profile-specific enums.

## Task Safety

Before a video POST, persist the `submitting` state. Once a request may have reached the gateway, do not submit again automatically, including on timeout, network error, `429`, or `5xx`. Retain the state for task-ID recovery. Polling and downloading the same task can be retried; the skill never creates a second task to recover a lost response.

Status and content requests use the same selected group's key as creation. Redirects are not followed, preventing bearer-token forwarding. MP4 output is capped at 512 MiB and is created exclusively, so an existing output file is not overwritten.
