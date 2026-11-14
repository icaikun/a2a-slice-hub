# A2A Slice Hub Live E2E Showcase (`epic/live-e2e-showcase`)

This isolated orphan branch demonstrates a live end-to-end run of `a2a-slice-hub` v0.2.0:

- Protected slice governance (`slices/orders.yaml`, `slices/billing.yaml`, `.hub/tests.json`, `.hub/skills.lock.json`)
- Cross-platform Feishu + DingTalk L0 interactive card approval sync
- Real `CodexCliAdapter` implementing the `orders` slice under ADR `0001`
- Real `ClaudeCliAdapter` implementing `billing`, catching a QA edge-case bug (`FAIL -> REWORK`), fixing it with injected rework evidence, and promoting a verified case + EverOS export
