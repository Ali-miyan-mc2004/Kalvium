# Demo video outline

Aim for 6 to 8 minutes. Generation takes a few minutes, so run one lesson before recording and use
it for the review steps. Show the second lesson finished, not running. Say on camera that these
lessons were generated beforehand. Everything shown must be real output: test mode's placeholder
text does not belong in the video.

Before recording, confirm the status bar at the top of the app shows the LLM and web search as on.

## 1. What it is (30 seconds)

Show the pipeline diagram in the README. Say what goes in, what comes out, and that the two QC layers
are independent.

## 2. Input and generation (1 minute)

- Click **Research and generate lesson** with the fields empty to show the validation message.
- Enter the sample title and objective and start the run.
- Show the Workflow tab updating, then switch to the finished lesson.

## 3. Research and references (1 minute)

- Open **Sources**. Point out websites, books and videos, the reliability tiers, and which sources
  are usable as evidence.
- Open the research log.
- In **Preview**, click an inline citation to jump to its reference.

## 4. QC Layer 1 (1.5 minutes)

- Open **QC 1: Facts**. Explain the support rate.
- Show a supported claim with its evidence quote, then a claim that was not supported.
- Explain that code confirms each quote exists in the source.
- Show the source audit.

**Before recording, check the factual accuracy yourself.** The brief asks for this. Read the lesson
against the sources, and mention in the video what you checked and what you found.

## 5. QC Layer 2 (1 minute)

- Open **QC 2: Teaching**. Show objective coverage, the rubric and the time budget.
- Say that this reviewer never sees the sources or Layer 1's report.

## 6. Review, edit and export (1.5 minutes)

- Apply a recommendation, and show the change in the lesson and the new version.
- Dismiss one you disagree with, and say why.
- Open **Edit**, change a sentence and save.
- Show the "report reviewed an earlier version" notice, then rerun a QC layer.
- Restore an older version in **History**.
- Click **Export ZIP** and open `lesson.html` and a QC report.

## 7. Error handling (45 seconds)

Pick one, and prepare it before recording:

- Set a wrong API key in `.env`, restart, and start a lesson. Show the readable error. Put the correct
  key back, restart, and press **Retry** to show the saved research being reused.
- Or type in the editor without saving, refresh the page, and show the draft being restored.

## 8. Second lesson (30 seconds)

Show the finished Big-O lesson and its two QC reports.

## 9. Tools and decisions (45 seconds)

Name the stack: Python, FastAPI, SQLite, your LLM, and the research APIs. Pick your two favourite
decisions from `DECISIONS.md` and explain them in your own words. Say that AI tools helped build the
project, as the README acknowledges.
