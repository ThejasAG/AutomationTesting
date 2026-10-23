
# Implementation Plan — Ticket-Driven Auto-Testing + Fast Free AI

**Date:** 2026-07-27
**Goal:** Paste a ticket → the platform drafts tests, you approve once, and every PR linked to that ticket is auto-tested — with fast, free AI, calculation/API checks, and crash detection.

## STATUS (updated)
- ✅ **Phase 0.1 — Groq** (AI 0.26s vs ~47s; all AI routed through it)
- ✅ **Phase 0.2 — Secrets untracked** (`.env`/`.db` removed from git) — 👤 **you still must ROTATE the token** (it's in history)
- ✅ **Phase 1 — Ticket core** (table + API + Tickets tab in Scripts page + PR-push auto-run)
- ✅ **Phase 2 — AI drafting** (✨ draft from ticket, asserts the fix, classifies type)
- ✅ **Phase 3 — Calc / API / Crash** — DONE incl. crash hardening (native+redbox detection, "verify app did not crash" auto-guard, sim crash-log collection into evidence, 💥 crash badge)
- 🔧 **Phase 4 — partial**: Business building-block scenarios created (login ✅ verified; book+ need per-screen tuning). **Sequential cross-app = the Workflow path-composer** (`/workflow/recreate` reads the graph → composes → runs building blocks in order).
- ✅ **BONUS — Workflow/Coverage view**: interconnected cross-app graph (Consumer⇄Business + branches: pay via B-App/C-App), 126-scenario coverage matrix, and the agent path-planner that recreates a flow from the workflow.

### What remains (mostly not code)
- 👤 **Rotate the GitHub token** (only you can).
- 🔧 **Tune the Business spine per-screen** (book→assign→…→pay) — slow on this Intel Mac (~5–10 min/run); one screen per sitting.
- 👤 **Record more Business flows** (the app-side recording).
- ⚙️ **Faster hardware** (Apple-Silicon) is the real accelerator for execution — out of scope for code.

**Guiding principles**
- **Free** — no paid services (Groq free API + local sims + existing GitHub webhook).
- **Honest** — never false-green; flag what can/can't be verified per ticket.
- **Reuse** — build on the existing webhook, runner, RCA, and comment bot.
- **Human-in-the-loop** — AI drafts, a person approves before it becomes a saved test.

---

## Phase 0 — Foundation (do first; small; unblocks everything)

### 0.1 Wire Groq (fast, free AI)  🔧 small
- **Why:** Ollama is ~30–60s per call on this Intel Mac. Groq (free tier, you already have the key) is ~1–2s and OpenAI-compatible.
- **Do:**
  - Add a `base_url` option to `OpenAIProvider` (`automation/ai/provider.py`) so it can point at `https://api.groq.com/openai/v1`.
  - Read `LLM_API_BASE` in `default_config` / `create_provider`.
  - Set env: `LLM_PROVIDER_TYPE=openai`, `OPENAI_API_KEY=<groq key>`, `LLM_API_BASE=https://api.groq.com/openai/v1`, `LLM_MODEL_NAME=llama-3.1-8b-instant`.
- **Verify:** RCA/summary generate in ~1–2s instead of ~47s.
- **Caveats:** data leaves the machine (cloud); free-tier rate limits. Ollama stays as offline fallback.

### 0.2 Rotate the leaked GitHub token  👤 you
- Still in git history. Revoke + reissue on GitHub, put the new one in `.env` (now git-ignored). Prerequisite for anything that comments on PRs.

---

## Phase 1 — Ticket store + manual auto-test flow (the reliable core)

*This is the minimum that delivers the feature. No AI required — everything here is deterministic.*

### 1.1 Data model  🔧 small
- New `Ticket` table: `id, key/title, description (full text), pr_number/pr_url, project_id, linked_scenario_ids (JSON), ticket_type (ui|calc|data|crash|mixed), status (untested|passed|failed), last_run_id, created_at`.
- Auto-migrates on startup (existing pattern).

### 1.2 API endpoints  🔧 small
- `POST /api/v1/tickets` (create/paste), `GET /api/v1/tickets`, `GET /api/v1/tickets/{id}`, `PUT` (link PR/scenarios), `POST /api/v1/tickets/{id}/run` (manual run).

### 1.3 Ticket UI — repurpose the Scripts page  🔧 medium
- **Decision:** the Scripts page is currently underused, so the ticket feature lives **there** (rename it e.g. "Scripts / Tickets" or "Test Authoring") instead of a new page.
- New section on that page:
  - **Paste ticket** — Title + full Description (free text).
  - **PR number / URL field** — to link the ticket to a specific pull request.
  - **Linked scenarios** — multi-select from the library (or AI-draft in Phase 2).
  - **Save** → creates a Ticket record.
- Below it: a **ticket list** — each ticket + its linked PR + latest **✅/❌** + report link (the traceability matrix).
- Keep the existing script editor/AI-generate below or as a tab, so nothing is lost.

### 1.4 PR → ticket → auto-run link  🔧 medium
- On PR push (existing `webhooks.py`): find tickets whose `pr_number` matches (manual link, per your preference) → run their linked scenarios → set ticket status → **comment verdict on the PR** (reuse `pr_comment.py`).
- Results also save as normal runs (reuse the scenario persistence we just built).

**Deliverable:** paste ticket → link PR + scenarios → push PR → tests run → PR comment + ticket flips ✅/❌.
**Depends on:** Phase 0.2 (token). Not AI.

---

## Phase 2 — AI drafts scenarios from the ticket (uses Groq)

### 2.1 Draft-from-ticket  🔧 medium
- "✨ Draft scenarios" button on a ticket → Groq reads the description's structured steps → returns **draft scenario steps** in the platform's plain-English format → land them in the editor for **human review** before saving.
- Reuses the existing scenario format + the `suggest-covers` pattern.

### 2.2 Ticket-type detection  🔧 small
- Groq (or keyword rules) classifies each ticket/scenario: **UI-checkable / calculation / needs-API-data / crash / mixed** — shown as a badge so nobody trusts a data-logic test that can't actually read the backend value.

**Deliverable:** paste ticket → AI drafts scenarios + labels what's verifiable → you approve → Phase 1 runs them.
**Depends on:** Phase 0.1 (Groq), Phase 1.

---

## Phase 3 — Assertion power-ups (calc / data / crash tickets)

### 3.1 Generic "verify calculation" step  🔧 medium
- New step type: read two/more on-screen numbers → compute (sum, %, ×, −) → compare within tolerance → PASS/FAIL with breakdown. Generalizes the existing `bill_validator` pattern to any calc (e.g., "lost 30 XP", "inventory = start − qty").

### 3.2 API assertion step  🔧 medium
- New step type: call a backend endpoint (auth as needed) and assert a value — so **backend-only** values (inventory stock, XP-vs-transaction) can be checked when they're not on screen.
- Needs: the app's API base URL + a read endpoint per value. Config per project.

### 3.3 Crash detection hardening  🔧 small
- Auto-insert a **"verify app did not crash"** guard into generated scenarios.
- Collect **simulator crash reports** (`~/Library/Logs/DiagnosticReports/`) into the evidence bundle → RCA gets the native stack trace.
- **Crash badge** on runs where a crash is detected.

**Deliverable:** calc tickets (bills/XP), data tickets (inventory via API), and crash tickets are all properly verified — not just UI wording.
**Depends on:** Phase 1 (runner), Phase 0.1 (RCA speed).

---

## Phase 4 — Coverage & cross-app (mostly your recording + my scaffolding)

### 4.1 Business app scenarios  👤 mostly you + 🔧 scaffolding
- Business app has 0 scenarios. Record its key flows (assign table, add items, kitchen ready, serve) so ticket-linked tests exist for it.

### 4.2 (Optional) Sequential cross-app orchestrator  🔧 medium
- Refactor the parallel orchestrator to **sequential**: Consumer (book) → Waiter (assign, add/select-all → send) → Kitchen (login, mark ready) → Waiter (serve). The flow you described earlier.

**Depends on:** stable single-app execution first.

---

## Sequencing & rationale

```
Phase 0  (Groq + token)      ← do first; fast AI + unblocks PR comments
   │
Phase 1  (ticket core)       ← the feature works here, no AI needed
   │
Phase 2  (AI drafting)       ← makes it low-effort to create tests
   │
Phase 3  (calc/API/crash)    ← covers the hard ticket types honestly
   │
Phase 4  (coverage + xapp)   ← breadth; partly your recording work
```

- **Phases 0–1 alone** deliver a usable, honest, free feature.
- **Phase 2** removes the manual scenario-writing effort.
- **Phase 3** is what makes it credible for calculation/inventory/crash tickets.
- **Phase 4** is breadth and depends on the app-side recording.

## Effort (relative)
| Phase | Size | Notes |
|---|---|---|
| 0.1 Groq | Small | ~10 LOC + env |
| 1 Ticket core | Medium | model + 4 endpoints + 1 page + webhook hook |
| 2 AI drafting | Medium | reuses Groq + scenario format |
| 3 Calc/API/crash | Medium | 2 step types + crash collection |
| 4 Coverage/xapp | Large (mostly recording) | your flows + optional refactor |

## The underlying constraint (unchanged)
Everything that **runs** a test still pays the **one-time WebDriverAgent build** (~3–5 min) on this 2019 Intel Mac. Groq fixes AI speed, not execution speed. For heavy use, faster hardware (or a Mac mini M-series runner) is the real fix — separate from this plan.

## Cost
**$0.** Groq free tier (AI), local simulators (execution), existing GitHub webhook + token (PR integration). Only cost is Groq's rate limits and (optional) faster hardware later.

---

## Proposed start order
1. **Phase 0.1 (Groq)** — small, immediate win, helps the demo too.
2. **Phase 1 (ticket core)** — the feature, end-to-end, no AI dependency.
3. Then Phase 2 → 3 → 4 as above.

*Awaiting go-ahead to start with Phase 0.1 (Groq) + Phase 1.*
