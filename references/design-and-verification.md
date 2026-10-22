# Media Skill: Design and Verification

## Project Overall Analysis

This repository is a Codex skill and local client for the user's media API gateway, not a task plugin. The gateway owns provider adaptation, channel routing, task billing, and the public API contract. The skill collects user choices, selects the matching local credential, validates known model parameters, submits through the standard API, and retrieves results.

The previous skill had one shared generation key and a separate New API upload key. That cannot represent one credential per video group and can accidentally route a request through a key from the wrong group. The refactor keeps a single image key, stores video keys by exact public group name, and reuses the selected video-group key for image-host identity verification.

The public pricing page is a client-rendered SPA. Its unauthenticated `/api/pricing` response is the runtime source for public model names, endpoint types, and `enable_groups`. The skill ignores all price data. A local capability profile is still required because public pricing does not define request parameters. Unknown models are not offered for submission.

## Refactor Design

### Group and Model Discovery

- Fetch the public catalog with a bounded timeout and response size.
- Filter video models by `openai-video` and image models by `image-generation`.
- Map exact public group names to models, then intersect with `model-catalog.json` profiles.
- Show newly listed groups immediately, even when they use a previously configured model.
- Show newly listed models as unconfigured until their request contract and limits are verified.
- If the public catalog is unavailable, use the bundled five-group snapshot and clearly mark it as potentially stale.

### Credential Routing

```json
{
  "api_key": "<image credential>",
  "video_group_api_keys": {
    "<exact public group name>": "<that group's credential>"
  }
}
```

`api_key` remains the image credential for backward compatibility. Video lookup requires an exact group key; there is no fallback to the image key or a different video group. A new key overwrites only the selected scope. The key store stays outside the repository with `0600` file and `0700` directory permissions. Secrets are read from hidden input or non-terminal stdin, never from CLI arguments.

The existing `new_api_image_upload_key` field, if present in an older config, is preserved but ignored. Image uploads for video use the selected video group's key to establish gateway-user identity; there is no fourth credential requirement.

### User Workflow

The skill identifies image generation, image editing, or video generation; loads the corresponding catalog; asks one consolidated question for the selected model's required fields; and optionally asks whether the user wants prompt optimization. Optimized candidates require the user's selection before generation. Missing group credentials stop before upload or generation.

Video references use normalized public fields. Local images are multipart only for Grok; other current video adapters convert local images to a validated public URL using the fixed image-upload endpoint. Local reference videos and audio are converted to public URLs through the fixed media host using the selected video-group key, with per-model reference limits checked before upload and uploaded URLs cached in workflow state. Text-to-video and image/video/audio reference generation all use the standard `/v1/videos` API. Provider-specific conversion stays in the gateway plugin.

Video submission remains at-most-once from the client's perspective. The state is written as `submitting` before POST; uncertain outcomes are never retried automatically. Task polling and same-task content downloads remain resumable. Legacy version-1 states are accepted only for already-submitted tasks and use their original image credential for polling; an old unsubmitted state must restart with an explicit group selection.

## File Structure Design

| Path | Responsibility |
| --- | --- |
| `SKILL.md` | Skill selection metadata and user-facing image/video workflows, group-key routing, prompt confirmation, and billable-call safeguards. |
| `agents/openai.yaml` | Codex display name and automatic invocation metadata. |
| `scripts/generate_image.py` | Image generation/edit flow, layout validation, image credential storage, and backward-compatible credential-path selection. |
| `scripts/generate_video.py` | Dynamic group selection, exact per-group key use, 12 verified video profiles, references, video task state, polling, recovery, and MP4 download. |
| `scripts/media_catalog.py` | Bounded public catalog fetch, image/video endpoint filtering, live group mapping, unknown-model quarantine, and offline snapshot fallback. |
| `references/model-catalog.json` | Verified model parameter profiles and the current five-group offline snapshot; no price values or credentials. |
| `references/api.md` | Gateway endpoints, credential schema, group mapping, image upload behavior, and task recovery contract. |
| `references/design-and-verification.md` | This architecture, ownership boundaries, implementation checklist, and audit record. |
| `tests/test_generate_image.py` | Image compatibility, credential permissions, image behavior, and workflow tests. |
| `tests/test_generate_video.py` | Group-key isolation, model-specific validation, RollDek references, task lifecycle, upload boundaries, and no-duplicate-submit tests. |
| `tests/test_media_catalog.py` | Live catalog parsing, unknown model handling, malformed group names, and fallback snapshot tests. |
| `.gitignore` | Excludes Python caches, local config fragments, workflow state, and generated media. |
| `README.md` | Repository landing page and skill purpose. |

## Development Checklist

| Phase | Task | Status | Result |
| --- | --- | --- | --- |
| 1 | Inspect installed image/video clients, public catalog response, and deployed adapter contracts | complete | Used the installed skill as source because the previous public skill repository was older. |
| 2 | Design dynamic group-to-model catalog and per-scope credentials | complete | Five current video groups each map to an independent key; image key remains separate. |
| 3 | Refactor image/video clients without losing retry, task recovery, output validation, or legacy image credential | complete | Existing image tests and video task lifecycle tests retained. |
| 4 | Add twelve verified video capability profiles, reference media, RollDek support, and prompt-confirmation instructions | complete | Unknown models stay non-selectable; video request uses normalized public fields. |
| 5 | Test, audit public files, initialize new repository, and push | complete | 49 mock/offline tests pass; live catalog read shows five groups; package validator passes; public repo is pushed on `main`. |
| 6 | Add local reference video/audio upload and explicit generated-MP4 publishing through the media host | complete | Uploads use the selected group key, check model reference limits first, cache URLs in state, stop before billable submissions on failure, and never auto-publish; 54 mock/offline tests pass. |

## Phase Self-Check

- Public catalog was checked on 2026-10-20. It exposed five `openai-video` groups and twelve distinct supported video model IDs. The four Seedance public groups each exposed the same four `doubao-seedance-*` names; the `视频模型` group exposed all twelve.
- Video parameters come from the locally installed Grok, ZeeQi, and RollDek plugin contracts. RollDek's four model profiles are included without inventing a reference-media count limit. H3 has its own 720p-only and reference limits. ZeeQi duration ranges use plugin limits rather than a generic 1-3600 range.
- Group names are treated as identifiers from a public catalog, not instructions. Prices and billing expressions are discarded. The skill does not change gateway prices or model metadata.
- New video credentials are keyed by exact group name. A missing selected group key cannot fall back to another group's key. Image upload receives the current selected video key only for gateway-user validation.
- Live catalog verification on 2026-10-20 returned five groups: four Seedance groups with four models each, and `视频模型` with twelve models. The current image catalog returned eight configured profiles and surfaced two additional image models as unconfigured; they cannot be submitted until capabilities are verified.
- Existing `api_key` remains the image key. The old upload-key config field is not read or emitted. No credential value is included in source or tests.
- A previous `api_key` environment override remains image-only. Video automation uses an exact JSON group map, preserving group isolation.
- Existing image-generation request logic and the at-most-once video submit, status polling, content recovery, and MP4 safety rules remain in place.
- Local reference media and generated-output publishing use the fixed media host on `api.qlhazycoder.tech`. The selected video-group key is the only upload credential, uploaded URLs are cached by path/size/mtime, and a failed upload never reaches the billable video POST.

## Final Review

The full test suite passes (49 tests), the skill validator accepts the package, the live catalog read returns the current five video groups, and the public repository audit found no credentials or private operational paths. The repository is committed and pushed to `main`. No paid image or video request was made as part of verification.
