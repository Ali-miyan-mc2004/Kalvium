# Technical decisions

Why the project is built the way it is, and what each choice gave up.

## A fixed pipeline, not an autonomous agent

The steps are known in advance, so code controls the order and each step has one job. That makes
every step saveable, retryable and testable on its own. An agent loop might explore more freely,
but its failures are harder to reproduce and its cost harder to predict.

## Code checks what code can check

A model is unreliable at counting and at knowing whether a citation exists, and it is confidently
wrong in the same way each time. So these are deterministic: citation validity, the time budget,
lesson structure, link status and source tiers. Models do only what needs judgement.

The clearest example is quote verification. The fact-checking judge must give a verbatim evidence
quote, and code then searches for it in the passage. A judge that invents evidence loses its verdict
and the claim is marked `unverified`. The checker is itself checked.

## Fact-checking is closed-book

The judge may use only the retrieved passages, not its own knowledge. A model marking its own
homework from memory would tend to pass whatever it wrote. The cost is false alarms: a true claim
whose evidence was not retrieved shows as `unsupported`. That is the safer way to be wrong, because
an instructor can dismiss a false alarm but would never see a false pass.

To limit the false alarms, failed claims get an independent lookup outside the lesson's sources.
Only a clear result (`supported` or `contradicted`) replaces the first verdict.

## Keeping the two QC layers independent

The layers use separate prompts and fresh contexts, and neither sees the generator's reasoning.
Layer 2 also never sees the sources or Layer 1's findings, so it reads the lesson as a learner would.
`QC_MODEL` can run QC on a different model, so that the checker does not share the writer's blind
spots. A test asserts that Layer 2's input contains neither sources nor Layer 1 output.

## Official APIs only

Every connector uses an official API or a page that robots.txt allows, and identifies itself
honestly. Scraping search-engine or YouTube result pages would avoid needing keys, but it breaks
those sites' terms and fails unpredictably. Wikimedia's policy requires contact details in the user
agent, so that is a setting (`LESSONFORGE_CONTACT`), and Wikipedia requests run one at a time as the
policy asks. The cost is that the best results need a key or two.

## Books and videos are held to what we actually have

Open Library's full-text search returns real passages, so those books count as evidence. A book with
metadata only appears as further reading, and citing one raises a QC issue, because nothing could
confirm what it says. Videos are checked from metadata only and are never evidence. YouTube's
transcript endpoints are not an official API, so they are not used.

## BM25 rather than embeddings

A lesson's corpus is tens of documents, and technical terms such as `404` or `POST` match well on
keywords. BM25 is about 40 lines with no dependency or extra API call, and it is easy to explain.
Embeddings would handle paraphrase better, which is the most likely upgrade.

## Append-only versions

Nothing overwrites lesson content. Edits, applied fixes, restores and regeneration all add a version.
That single rule delivers several requirements at once: saved work survives any failed step, a fix can
be undone, and history is complete. Saves carry the version they started from, so a stale browser tab
gets a conflict instead of overwriting newer work.

## QC fixes are exact-match patches

Each recommendation carries `find` and `replace` text. Applying one replaces that exact text, once.
If the text changed or appears twice, the fix is refused. Fuzzy matching would apply more fixes, but
it could also change the wrong sentence in a lesson someone has already reviewed.

## Web content is treated as untrusted

Research text comes from the open web and could contain instructions aimed at the model. Prompts mark
it as reference data, schemas constrain every model output, Markdown renders with raw HTML disabled,
and the fetcher refuses private network addresses.

## Simple infrastructure

FastAPI, SQLite and plain JavaScript mean one process, no build step and one command to run. Background
work uses a thread per lesson. A multi-user deployment would want a real job queue and authentication,
and the brief asks for neither.

## What the first live runs changed

Tests with a fake model passed, yet the first real runs found problems only a real model could show:

- The OpenAI-compatible provider ignored the `effort` setting, so reviews ran at the default depth.
- A lesson planned at 43 minutes had text for about 33. The budget trusted high planned figures, so
  reading sections are now capped at twice their reading time.
- The instructional reviewer gave a perfect score with no issues. It now tests the lesson against a
  specific checklist and must name a weakness for each criterion, and code caps the score at 4 when
  it does.
- Long model-written book queries found nothing, so they are now short topics plus the lesson title.
- Gemini's free tier allows 20 requests per day per model, so fact-checking now batches more claims,
  and the app says when a daily quota is spent instead of suggesting a short wait.

This is why `evals/seeded_errors.py` exists: a QC layer that runs is not proof of one that works.

## Where this would go next

1. Grow the seeded-error set across more subjects and track the catch rate over time.
2. Add embedding retrieval beside BM25, for paraphrased claims.
3. Offer section-level regeneration that keeps instructor edits elsewhere.
4. Let instructors maintain their own trusted-source list.
