# LessonForge

An AI-powered lesson creation tool. Give it a lesson title and a learning objective, and it
researches books, websites and YouTube videos, writes a cited 45-minute lesson, and audits that
lesson with two independent quality-control layers. An instructor then reviews, edits, applies
fixes and exports the result.

```
Title + objective
      |
  1. Research ----- Wikipedia, MDN, web search, Open Library, YouTube
      |             filtered for relevance, ranked by reliability, numbered S1, S2, ...
  2. Generate ----- structured lesson with inline [S#] citations, fitted to 45 minutes
      |
      +-- 3. QC Layer 1: research and factual accuracy   -> separate audit report
      +-- 4. QC Layer 2: instructional quality           -> separate audit report
      |
  Review -> edit, apply or dismiss fixes, preview, restore versions, export ZIP
```

## Setup

You need Python 3.11 or newer and one LLM API key.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # macOS/Linux: .venv/bin/python
cp .env.example .env
```

Edit `.env` and set `ANTHROPIC_API_KEY`, then start the app:

```bash
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

Open <http://localhost:8000>.

### Configuration

| Setting | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | One LLM is required | Default provider. Also enables web search with no extra key. |
| `LLM_PROVIDER=openai` with `OPENAI_MODEL`, `OPENAI_API_KEY`, `OPENAI_BASE_URL` | Alternative | Any OpenAI-compatible service, such as OpenAI, Gemini, Groq, OpenRouter or Ollama. |
| `QC_MODEL` | No | Runs the QC layers on a different model from the writer. |
| `LESSONFORGE_CONTACT` | Recommended | Your repository URL or email. Wikipedia's robot policy requires contact details, and it refuses clients without them. |
| `TAVILY_API_KEY` | No | Web search, and required for it when using `LLM_PROVIDER=openai`. |
| `YOUTUBE_API_KEY` | Recommended | Official YouTube Data API: gives duration and view counts. Without it, videos come from web search and are confirmed through YouTube's oEmbed endpoint. |

MDN and Open Library need no key. A missing optional key never stops the workflow. It shows
as a warning on the Sources tab, and research carries on with what is available.

To use Gemini, set these in `.env`:

```
LLM_PROVIDER=openai
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
OPENAI_MODEL=gemini-3.7-flash
```

A lesson takes roughly 12 to 15 model calls. Gemini's free tier allowed only 20 requests per day
per model when this was tested, so expect about one lesson per model per day. Setting `QC_MODEL`
to a different model splits the calls across two daily allowances.

## How it works

**Research** (`app/research/`). An LLM breaks the objective into subtopics and search queries.
Connectors run in parallel, and one failing does not stop the others. Results are de-duplicated,
filtered for relevance and ranked by a source-reliability tier. Book text comes from Open Library's
full-text search, so the tool holds real passages. Books with metadata only are listed as further
reading and are never used as evidence.

**Generation** (`app/pipeline.py`). The model receives the passages most relevant to each subtopic
and must cite them. Code then measures the lesson's length. If it runs over 45 minutes, the model
gets one automatic chance to cut it. References are built from the citations themselves, so the
model cannot invent one.

**QC Layer 1: research and factual accuracy** (`app/qc/factual.py`)

1. Code checks for invented citations, uncited concept sections, dead links, and weak, dated or
   over-relied-on sources.
2. A model extracts every checkable claim, including those in examples and quiz answers.
3. A separate judge rates each claim using only retrieved passages. Its own knowledge is ruled out.
4. Code confirms that each evidence quote really occurs in the passage. If it does not, the verdict
   is discarded and the claim is marked unverified.
5. Claims that fail get an independent lookup outside the lesson's own sources, so a gap in the
   original research is not mistaken for an error.
6. A reviewer turns the results into issues with concrete fixes.

**QC Layer 2: instructional quality** (`app/qc/instructional.py`)

1. Code measures the time budget and the lesson structure.
2. A reviewer who sees only the lesson and objective maps each sub-objective to where it is taught
   and whether a question actually assesses it, then scores a six-point rubric and reports issues.

This reviewer never sees the sources or Layer 1's findings, so its judgement stays independent.

**Review.** Every edit, applied fix and restore creates a new version, and nothing is overwritten.
A fix replaces exact text only: if the instructor has changed that text, the fix is refused rather
than guessed at. Reports are marked out of date when the lesson moves on.

**Time budget.** Code measures time rather than trusting the model's plan. No section counts for
less than its reading time at 180 words per minute, so a low planned figure cannot hide a long
section. Sections the learner reads are also capped at twice their reading time, so a high planned
figure cannot make a thin lesson look full. Activities and knowledge checks keep their planned time.
A draft outside the budget gets one automatic correction. Supplementary videos are optional and are
not counted in the 45 minutes.

**Error handling**

| Situation | What happens |
|---|---|
| Empty input | Rejected in the browser and again by the API. |
| One research connector fails | Recorded in the research log; the others continue. |
| Too few sources | Research fails with the reason and can be retried. |
| Query planning fails | Falls back to simple queries and shows a warning. |
| Any step fails | Earlier output stays saved, the error is shown, and only that step needs a retry. |
| One QC layer fails | The other still runs. |
| Regenerating over instructor edits | Asks first. The edits stay in History. |
| Two tabs edit the same lesson | The second save is refused rather than overwriting the first. |
| Browser closes during an edit | The unsaved draft is restored from the browser. |
| Server stops during a step | The step is marked interrupted on restart and can be retried. |

## Sample inputs

The brief's lesson:

- **Title:** HTTP Requests and Responses
- **Objective:** Explain GET and POST methods and distinguish between 2xx, 4xx, and 5xx response
  status categories.

A second lesson from a different subject area:

- **Title:** Big-O Notation
- **Objective:** Explain Big-O notation and compare the growth of O(1), O(log n), O(n) and O(n²)
  algorithms.

## Tests

```bash
.venv/Scripts/python -m pytest
```

The suite needs no key or network. It uses a fake LLM to run the whole workflow through the HTTP
API, including failures and retries, and runs both real provider classes against stub transports.
These tests prove the workflow's logic. They say nothing about lesson quality, which only a real
model run can show.

`evals/seeded_errors.py` measures the QC layers with a live model. It plants known false statements
and teaching flaws in a copy of a saved lesson, then records what each layer catches:

```bash
.venv/Scripts/python -m evals.seeded_errors 1
```

## Known limitations

- Source tiers come from a hand-kept domain list. They signal reliability but do not prove it.
- Videos are assessed from metadata. Transcripts are not read, so videos are never used as evidence.
- Book evidence is limited to the short passages full-text search returns.
- Passage retrieval is keyword-based (BM25), so a claim worded very differently from its source can
  be missed and reported as unsupported. The independent lookup reduces this but does not remove it.
- QC verdicts are model judgements backed by checked quotes. They help an instructor review the
  lesson and do not replace that review.
- The app is a single-user local tool with no login. Do not expose it to a network.

## Project layout

```
app/
  main.py           HTTP API
  pipeline.py       research -> generate -> QC, with retries
  review.py         edits, applying fixes, restoring versions
  exporter.py       Markdown, HTML and ZIP export
  llm.py            provider interface
  prompts.py        every prompt, in one file
  db.py             SQLite persistence
  research/         connectors, ranking, retrieval
  qc/               deterministic checks and the two QC layers
static/             the instructor interface
tests/              test suite
evals/              QC evaluation with planted errors
DECISIONS.md        technical decisions and trade-offs
DEMO_SCRIPT.md      outline for the demo video
```

## Acknowledgements

Built with [FastAPI](https://fastapi.tiangolo.com/), [Pydantic](https://docs.pydantic.dev/),
[HTTPX](https://www.python-httpx.org/), [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/),
[markdown-it-py](https://github.com/executablebooks/markdown-it-py), the Anthropic and OpenAI Python
SDKs and SQLite.

Research data comes from the [Wikipedia API](https://www.mediawiki.org/wiki/API:Main_page),
[MDN Web Docs](https://developer.mozilla.org/), the [Open Library API](https://openlibrary.org/developers/api),
the [YouTube Data API](https://developers.google.com/youtube/v3) and [Tavily](https://tavily.com/).

The BM25 ranking follows the standard Okapi BM25 formula.

AI assistance: Claude Code (Anthropic) was used to help design, write and test this project.
