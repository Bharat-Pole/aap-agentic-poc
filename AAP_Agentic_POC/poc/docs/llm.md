# The LLM layer (Phase 3)

The language model is used at **exactly two human-facing edges**, and nowhere
else. Every detection, forecast, and routing decision is deterministic Python —
the LLM only turns already-computed facts into readable prose. It is always
behind a deterministic template fallback, so the POC completes every scenario
even with no API key, no internet, and no local model.

## Where the LLM is — and is not — used

| Where | Module | What the LLM does | Fallback |
|---|---|---|---|
| **Draft-PO justification** | [`agents/approval.py`](../agents/approval.py) | Writes a 3–4 sentence rationale for the Demand Planner on the **draft path only** (why the PO, urgency, vendor caveat). | Deterministic template narrative built from the same fields. |
| **Notification body** | [`agents/notification.py`](../agents/notification.py) | Rephrases the message body with a tier-appropriate tone. | Deterministic template body. |

**The LLM is never used for:**

- Stock detection, demand forecasting, MOQ/vendor checks, or the autonomy-tier
  routing — all deterministic (see [`decision_logic.md`](decision_logic.md)).
- The PO write decision / guardrail — purely structural (see
  [`orchestration.md`](orchestration.md)).
- Any number, date, SKU, vendor, quantity, or cost. The prompts pass the
  computed facts in and **forbid the model from inventing data**; the model only
  phrases them. The notification **subject**, channel, and urgency stay
  deterministic — only the body prose is (optionally) rephrased.

Because the narrative is generated in the Approval Agent's `run()` *after*
`classify()` has already fixed the tier, the LLM cannot change a routing
outcome. `classify()` itself stays pure and LLM-free, so direct callers and the
unit checks remain byte-stable.

## The fallback chain (never raises)

[`llm/provider.py`](../llm/provider.py) exposes one entry point,
`generate(prompt, system=None, fallback=None) -> str` (and a
`generate_with_provider(...) -> GenResult` variant that also reports the path
used). It tries, in order, and **never raises** — a provider failure is logged
and the chain moves on:

```
USE_LLM=false ──────────────────────────────► template (no network call at all)
        │ true
        ▼
1. Google Gemini 2.5 Flash   (only if GEMINI_API_KEY is set)
        │ unavailable / empty
        ▼
2. Local Ollama  POST /api/generate   (short connect timeout; skips fast if down)
        │ unavailable / empty
        ▼
3. Deterministic template  ── the caller's `fallback` string (its own template
                               render), or an echo of the prompt if none given.
```

The caller-supplied `fallback` is why the provider module holds **no domain
knowledge**: the Approval and Notification agents own their templates and pass
them in. Each call logs which path produced the text, e.g.:

```
INFO llm.provider: LLM text via gemini (412 chars).
INFO llm.provider: LLM provider ollama unavailable (...); trying next.
INFO llm.provider: No LLM provider available -> deterministic template fallback.
```

The path is also persisted for audit/UI: `ApprovalDecision.narrative_source` and
`NotificationResult.body_source` carry `gemini` / `ollama` / `template`, and both
land in the `audit_log` `details` JSON.

## The prompts

**Justification (Approval Agent).** System instruction pins the model to the
supplied facts:

> You are a supply-chain assistant writing for a Demand Planner at an
> automotive-parts distributor. Write a concise 3-4 sentence justification for a
> draft purchase order that needs human approval. Cover: why the PO is needed,
> how urgent it is, and the vendor caveat that blocked auto-issue. **Use ONLY the
> figures provided below — do NOT invent SKUs, quantities, dates, costs, or
> vendor names.** Plain professional prose, no bullet points, no headings, no
> preamble.

The user prompt is a labelled `- key: value` facts block (SKU, on-hand,
threshold, effective stock, days-to-stockout, weekly avg, recommended qty,
promo/season uplift, vendor + status + MOQ + cost, draft reason) — easy for the
model to stay on, easy for a reviewer to confirm nothing was invented.

**Notification (Notification Agent).** System instruction forbids new facts and
caps length to 2–4 sentences; the user prompt supplies the tone (by tier),
channel, urgency, and the deterministic draft body to rewrite:

| Tier | Tone |
|---|---|
| AUTO-ISSUE | calm and informational (FYI; no action needed) |
| DRAFT-FOR-APPROVAL | urgent and direct (action required: approve or reject) |
| SUPPRESS | reassuring and brief (no action; a false alarm was avoided) |

## Switching providers via `.env`

Copy [`.env.example`](../.env.example) to `.env`. Nothing is required — with no
key and no Ollama, you get the template path. Relevant variables:

| Variable | Default | Effect |
|---|---|---|
| `USE_LLM` | `true` | Master switch. `false` → template-only, fully offline and byte-stable. |
| `LLM_PROVIDER` | `gemini` | Provider tried first; the chain still falls through to the other and then the template. Set `ollama` to prefer the local model. |
| `GEMINI_API_KEY` | _(empty)_ | Enables Gemini. Get one at <https://aistudio.google.com/app/apikey>. Read from env only — never hardcoded. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model id. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server. Run `ollama serve` + `ollama pull qwen2.5`. |
| `OLLAMA_MODEL` | `qwen2.5` | Local model name. |
| `POC_OLLAMA_CONNECT_TIMEOUT` | `1.0` | Seconds to wait for an Ollama connection before failing over. Keep low so an offline demo doesn't stall. |

**Demo recipes**

- *Fully offline / fastest, byte-stable:* `USE_LLM=false`.
- *Real LLM via Gemini free tier:* set `GEMINI_API_KEY`, leave `USE_LLM=true`.
- *Local model:* run Ollama, set `LLM_PROVIDER=ollama` (or just leave Gemini
  unkeyed so the chain falls to Ollama).

## Determinism note

With `USE_LLM=false` (or offline), the two edges use templates and the whole POC
is byte-stable — the same seed reproduces the same narratives. With a live
provider, the **prose** varies run-to-run but the **decisions, quantities, POs,
and guardrail do not**: the acceptance gates assert routing/PO/audit invariants,
not narrative wording.

## Verifying

```bash
python scripts/validate_phase3.py
```

13 checks: the wrapper never raises and always returns text; offline degrades to
the template; a draft surfaces a fact-only narrative that is persisted on the
approved PO; the source is recorded in the audit log; and the template path is
identical across two runs. The gate auto-detects whether `GEMINI_API_KEY` is set
and asserts `gemini` vs `template` accordingly.

## Dependency note

Per the project brief this uses the **`google-generativeai`** SDK (pinned in
[`requirements.txt`](../requirements.txt)). Google has since deprecated that
package in favour of **`google-genai`**; it still works for `gemini-2.5-flash`
but prints a deprecation warning on import. Migration is a small, localised
change confined to `_try_gemini` in [`llm/provider.py`](../llm/provider.py):

```python
# google-genai equivalent
from google import genai
client = genai.Client(api_key=config.GEMINI_API_KEY)
resp = client.models.generate_content(
    model=config.GEMINI_MODEL,
    contents=prompt,
    config={"system_instruction": system} if system else None,
)
return resp.text
```
