# VideoForge shell

This package owns VideoForge branding, the project rail, workbench panels and VideoForge model settings.

The model settings page reads the installed Python backend's text, video, image, speech and model catalogs. Users can keep custom model IDs and existing relays, configure each speech provider independently, and set output, cache, FFmpeg and public reference paths.

Saving sends changed fields only. Blank replacement keys keep the existing secret; updates merge per-provider key and model dictionaries. Empty model/base-URL choices reset to backend defaults, while blank filesystem paths preserve existing values. The chat model remains a separate Harness setting.

Run the focused settings tests with `pnpm exec vitest run packages/client/ui-vf-brand/tests/settings-model.client.spec.ts`.
