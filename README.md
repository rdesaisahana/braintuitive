# Braintuitive

Homework built from your child's own school curriculum. A parent uploads the
school's PDF; the app turns it into quizzes that answer back the moment a child
chooses, and keeps celebration for genuine mastery.

---
Flowchart:
<img width="1215" height="1295" alt="Braintuitive flowchart1" src="https://github.com/user-attachments/assets/8215e03d-5fe8-44bb-9cf7-4c95e6534920" />


## Start here

| Question | Answer |
|----------|--------|
| Who signs in? | A parent. Children have no login of their own yet: a parent signs in and the child works on that device. |
| Where do the questions come from? | Only the curriculum that was uploaded. A new account is empty until then — nothing is borrowed from another school. |
| What does a child see? | One question at a time, feedback the instant they choose, an explanation only when they are wrong, a hint if they ask for one, then points, characters and badges. |
| What does a parent see? | Progress skill by skill, why a child is stuck, and what to revise before a test. |
| When is a topic mastered? | It is quizzed at three levels — Easy, Medium and Tricky — each worth a third: 0 → 33 → 67 → 100%. |

### The app, screen by screen

| Screen | Route | What happens there |
|--------|-------|--------------------|
| Sign up / log in | `/` | Parent's name, the child's name and grade, email, password. |
| Your curriculum | `/curriculum` | Upload the school PDF, confirm what was found inside it, add or remove a child. |
| Let's learn | `/learn` | The child's units and topics, each with its three levels. |
| Quiz | `/quiz/:id` | The core loop: choose, find out at once, ask for a hint if needed. |
| See how I did | `/result/:id` | Score, what it means, points earned, new badges. |
| Progress | `/progress` | Three tabs: the overview, accuracy skill by skill, and a revision plan for a test. |
| My rewards | `/rewards` | Points, level, characters to unlock, badges earned. |

---

## Status

| Phase | Scope | State |
|-------|-------|-------|
| 1 | Foundation — FastAPI, typed config, 16-table schema, ReAct base agent | **Done** |
| 2 | RAG — PDF parsing, chunking, Nebius embeddings, Pinecone, retrieval | **Done** |
| 3 | Agents — quiz writer, answer checker, gap detector, test prep | **Done** |
| 4 | API — 42 endpoints | **Done** |
| 5 | Web app — the whole learning loop, redesigned | **Done**; quiz history has no screen of its own |
| 6 | Tests — 614 passing across 16 files | **Done** |
| 7 | Evals — golden dataset, two measured runs of the quiz writer | **First loop complete** |
| 8 | Launch — hosting, a real secret key, MongoDB | Not started |

**Curriculum belongs to the family that uploaded it.** A parent uploads their
school's guide at `/curriculum` and their children work through that, and only
that. See [Curriculum ownership](#curriculum-ownership).

**Questions are written ahead of time** into a question bank, so a child never
waits on the model. See [The question bank](#the-question-bank).

### Changed recently

- **True/false questions.** About one question in five is a statement to judge
  true or false, at most three in ten. See [Question types](#question-types).
- **Hints in a child's words.** The writing prompt now asks for the first real
  step ("Start at -150. Rising 80 means add 80") instead of the name of a
  strategy ("undo subtraction first"). Measured: see [Evals](#evals).
- **Every screen redesigned** around one frame with a sidebar, built to fit the
  window rather than scroll. See [Frontend](#frontend).
- **15 characters** to unlock with points, and badges by tier, on My rewards.
- **Children are managed on the curriculum page** — add one, pick a free
  starting character, or remove one with everything that belongs to them. The
  separate Profile and Students screens are gone.
- **"Easy" no longer hangs.** Priming fills one quiz for every topic before
  going deeper, so a new curriculum can be started the moment it finishes.
- **Progress shows skills and a revision plan.** Accuracy per skill, weakest
  first, and a plan for a test in 3, 7 or 14 days — the two things the API
  could answer that no screen asked. See [Frontend](#frontend).
- **Evals**, a new way to measure what the AI writes, in `backend/evals`.

---

## Setup

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

```bash
pip install -r backend/requirements.txt
```

Copy the environment template and fill in your keys:

```bash
copy backend\.env.example backend\.env
```

The app boots with **no** third-party keys set — SQLite alone is enough for
local development. Missing keys disable the feature that needs them and log a
warning rather than crashing:

| Key | Unlocks | Required for |
|-----|---------|--------------|
| `NEBIUS_API_KEY` | LLM + embeddings | Phase 2 onward |
| `PINECONE_API_KEY` | Vector search | Phase 2 onward |
| `MONGODB_URI` | Curriculum chunk documents | Phase 2 onward |
| `YOU_API_KEY` | Web search tool | Phase 3 |
| `SECRET_KEY` | JWT signing | Phase 4 (**must** be changed before deploy) |

Generate a real secret with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Run

Two processes. The API first:

```bash
cd backend && uvicorn main:app --reload
```

Then the web app, in a second terminal:

```bash
cd frontend && npm install && npm run dev
```

- Web app: <http://localhost:5173>
- API docs: <http://localhost:8000/docs>
- Health + dependency status: <http://localhost:8000/health>

Vite proxies `/api` to port 8000, so the browser sees one origin and there is
no CORS configuration and no API URL baked into the bundle.

The SQLite file is created automatically at `data/braintuitive.db` on first
start.

## Test

```bash
cd backend && pytest -q
```

614 tests across 16 files, all with the AI replaced by canned answers, so they
are fast and deterministic. What the real model produces is measured separately:
see [Evals](#evals).

```bash
cd frontend && npx tsc -b && npm run build
```

## Starting over

To wipe every account and start from an empty app — stop the API first, then:

```bash
rm data/braintuitive.db data/braintuitive.db-wal data/braintuitive.db-shm
```

```bash
cd backend && alembic upgrade head
```

Uploaded PDFs live in `data/uploads` and can go with it. Curriculum vectors live
in Pinecone, outside this database; clear the ones belonging to deleted accounts
with `PineconeStore().delete_namespace("math-grade6-u<owner>")`.

## Deploy

The website runs on **Vercel** and the API on **Render**, both built straight
from this repository. Vercel forwards `/api` to Render, so the browser still
sees a single address and no cross-site setup is needed.

### 1. The API, on Render

1. Sign in at [render.com](https://render.com) with GitHub.
2. **New → Blueprint**, and choose this repository. Render reads `render.yaml`.
3. When asked, paste `NEBIUS_API_KEY` and `PINECONE_API_KEY` from your local
   `.env`. `SECRET_KEY` is generated for you.
4. Deploy, then open `https://<your-service>.onrender.com/health` — it should
   answer.

`render.yaml` uses Render's **free** plan, so no payment details are needed.
Two things come with that:

- **It sleeps** after 15 minutes without visitors, and the next visit waits
  about a minute while it wakes. Open the site a minute before a demo.
- **Nothing is kept between restarts.** There is no disk on the free plan, so
  every restart or deploy wipes the database — accounts, the uploaded
  curriculum and all written questions. Sign up and upload again afterwards.

To keep data and stay awake, change `plan: free` to `plan: starter` and add a
disk mounted at `/var/data` with `BRAINTUITIVE_DATA_DIR=/var/data` (both paid).

### 2. The website, on Vercel

1. Sign in at [vercel.com](https://vercel.com) with GitHub.
2. **Add New → Project**, import this repository, and set **Root Directory** to
   `frontend`. Everything else comes from `frontend/vercel.json`.
3. If Render named the API anything other than
   `braintuitive-api.onrender.com`, put its address in the `destination` in
   `frontend/vercel.json`, commit and push.
4. Deploy, and open the Vercel address.

### After the first deploy

- The live database starts empty: sign up again and upload the curriculum.
- Upload about ten minutes before a demo, so the first quizzes are already
  written — the curriculum page shows which topics are ready.
- Local development and the live site share one Pinecone index. Each family's
  curriculum has its own namespace, so the two never mix.

---

## Architecture

### Two databases, by shape of data

**SQLite** holds anything relational and constrained — accounts, quizzes,
progress, gamification. **MongoDB** holds schema-loose documents — parsed
curriculum chunks, agent traces, generation caches. Mongo is optional; the app
degrades instead of failing when it is absent.

### Progression rules

Encoded in `SubUnitProgress` (`backend/db/models.py`), not scattered across
route handlers:

- Each sub-unit is quizzed at three difficulties, each worth one third:
  **0 → 33 → 67 → 100 %**.
- A difficulty counts only when its own bar is cleared:
  beginner **70 %**, intermediate **80 %**, proficient **90 %**.
- A `CHECK` constraint makes any percentage other than 0/33/67/100 unwritable,
  so a buggy code path cannot invent a 50 % state.
- `celebration_shown` guarantees the 100 % celebration fires exactly once —
  the product promise is that celebration means something.
- Units unlock sequentially (Unit 1 → 2 → 3); sub-units *within* a unit may be
  attempted in any order.

### Agents

Every agent subclasses `BaseAgent` (`backend/agents/base_agent.py`) and
implements two methods:

```python
class QuizGeneratorAgent(BaseAgent):
    def get_tools(self) -> List[Tool]: ...
    def get_system_prompt(self) -> str: ...
```

The base class owns the ReAct loop, the executor, timeouts, parsing-error
recovery, step tracing and JSON extraction. The ReAct prompt is defined
in-repo rather than pulled from LangChain Hub, so behaviour is reviewable and
boot needs no network.

Nebius speaks the OpenAI wire protocol, so the LLM is `ChatOpenAI` pointed at
`NEBIUS_BASE_URL`.

### RAG pipeline

```
PDF -> parse -> chunk -> embed -> Pinecone
             \-> curriculum_units / curriculum_sub_units (SQLite)
             \-> curriculum_chunks / parsed_documents (MongoDB, optional)
```

Check every integration before ingesting:

```bash
cd backend && python -m utils.check_services
```

Dry run (parse + chunk only — no API calls, no writes):

```bash
cd backend && python -m rag.pipeline "../06 Pre-Algebra H&A.pdf" --dry-run
```

Full ingest:

```bash
cd backend && python -m rag.pipeline "../06 Pre-Algebra H&A.pdf" --grade 6
```

**Sub-units come from Objectives.** District curriculum guides number units but
not sub-units. This pipeline derives sub-units from each unit's
`Objectives / We are learning to/that:` bullets and numbers them `1.1`, `1.2`,
…  Each objective is one teachable skill, which is the right granularity for a
quiz. For `06 Pre-Algebra H&A.pdf` that yields **6 units, 47 sub-units, 104
chunks**.

**Two chunk types.** `objective` chunks are one per sub-unit, carrying unit
context so a bare verb phrase does not match every unit; they are filterable by
`sub_unit_number`. `prose` chunks are the unit's narrative sections, split on
token count and never spanning a section boundary.

**Namespacing** is `{subject}-grade{n}` (e.g. `math-grade6`), so multiple
grades share one free-tier index without their vectors matching each other.

**Dimension safety.** `verify_embedding_config()` probes the live model and
refuses to proceed when `EMBEDDING_DIMENSIONS` disagrees. A wrong dimension
does not fail at embed time — it fails much later at upsert, or silently
degrades retrieval.

### Quiz generation

`QuizGeneratorAgent` (`backend/agents/quiz_generator.py`) has two entry points
sharing one tool set:

- `generate()` — the deterministic pipeline routes use: retrieve → draft →
  validate → top up. Predictable latency and cost.
- `execute()` — the inherited ReAct loop, for open-ended requests.

Structured generation happens inside a focused tool call rather than in the
ReAct scratchpad; a ten-question JSON payload squeezed through
Thought/Action/Observation parsing is a reliability problem, not a design.

**Every question carries its own feedback**: an `explanation` addressed to the
student, a `distractor_rationale` for each wrong option naming the specific
misconception, and a `hint`. This is what makes a wrong click a teaching
moment rather than a red X.

**Three tiers differ in kind, not number size.** Beginner is single-step
recall; intermediate is two-to-three-step application; proficient is reasoning,
transfer and error analysis. The specs drive the prompt.

**Answer keys are rebalanced mechanically.** A live run produced
`AAAAAAAAAA` — a student could score 100% by clicking A ten times and be
credited with mastery. Prompting against this is unreliable, so
`rebalance_answer_keys()` permutes each question's options onto a shuffled
round-robin over A–D, moving option text and its rationale together. Questions
containing position-dependent options ("all of the above") are skipped.

**Validation rejects** wrong option counts, bad keys, a `correct_answer` not in
the options, duplicate options, missing or stub explanations, and missing
distractor rationales. Invalid questions are discarded and topped up over
repeated attempts; a partial set returns with a warning rather than failing.

### Question types

Two formats, both stored the same way — a question with lettered options and one
correct key — so answering, feedback, hints, the bank and resuming a quiz work
without knowing which format they are handling.

| Format | Options | Stored as |
|--------|---------|-----------|
| Multiple choice | Exactly four, keys A–D | `question_type = "multiple_choice"` |
| True/false | Exactly two: A "True", B "False" | `question_type = "true_false"` |

**True is always A and False always B.** A child should never have to read the
buttons to know which is which, and the quiz screen shows them as two large
buttons, ✓ True and ✗ False, rather than lettered rows.

**About one question in five is asked for as true/false, and a set holds at most
three in ten.** A guess is right half the time on true/false against a quarter
of the time on four options, so too many would let a child pass a level without
knowing the material. The cap is enforced when questions are accepted, not
merely requested in the prompt.

**They are excluded from the answer-key checks that assume four options.**
Rebalancing leaves them alone — which of True or False is right is the
statement's business, not a slot the model favours — and the lopsided-key
warning ignores them, since their answer can only ever be A or B.

A false statement has to be false for one specific, common reason, named in its
rationale; "sometimes true" statements are rejected.

### Verification

`QuestionVerifierAgent` (`backend/agents/question_verifier.py`) independently
solves each question and flags any whose answer key looks wrong — the check
structural validation cannot make.

Enable it per-agent; it is off by default because it roughly doubles cost and
latency. Anything student-facing should turn it on:

```python
agent = QuizGeneratorAgent(verify=True)
```

**Verification is blind.** The verifier never sees the claimed answer. Shown
the key, a model rationalises its way into agreeing, and the whole step becomes
theatre. `test_verifier_never_sees_the_claimed_answer` pins this.

**Escalation keeps cost sane.** Solve once at temperature 0; agreement stops
there. On disagreement, take additional higher-temperature samples and decide
by majority vote. One dissent is usually the verifier slipping; a consistent
dissent is worth a human's attention.

Verdicts are `AGREED`, `DISPUTED`, `UNCERTAIN` or `ERROR`. Disputed questions
are dropped and regenerated by the existing top-up loop. An `ERROR` question is
*kept* — a transient API outage should not silently shrink a student's quiz —
and the gap is recorded in the report.

Beyond the answer key the verifier also reports whether exactly one option is
defensible, and whether the question is well posed at all.

**Measured on this curriculum** (sub-unit 1.2, 10 questions):

| Tier | Agreement | Rejected |
|------|-----------|----------|
| beginner | 100% | 0 |
| proficient | 69–90% | 3 of 13 |

Beginner questions are reliably correct. Proficient questions — multi-step
reasoning and error analysis — are where defects concentrate. One real catch:

> *"Which of the following expressions has a value of 0?"* with **both A and D
> equal to 0.** Unscorable. The generator's own explanation visibly noticed
> mid-sentence and shipped it anyway: *"...wait, that also seems correct? But
> recheck: ... Let's fix this in logic."*

That failure also drove `find_meta_commentary()`, which now rejects any
explanation or rationale containing model self-talk before it can reach a
student.

> **Still not fully guaranteed.** Verification raises confidence
> substantially; it does not make correctness certain. Both models can be
> wrong together. `source_chunk_ids` remains the audit trail for a human
> reviewer.

### Model choice

`Qwen/Qwen3-235B-A22B-Instruct-2507`, chosen by measurement, not parameter
count. On the same generation prompt on this account:

| Model | Throughput | 5 questions |
|-------|-----------|-------------|
| Qwen3-235B-A22B | ~171 tok/s | 4.8 s |
| Qwen3-30B-A3B | ~55 tok/s | 21.5 s |
| Llama-3.3-70B | ~7 tok/s | 180 s |

Llama-3.3-70B is 37× slower here and blew the request timeout. Serving
capacity, not model size, decided this — re-measure before switching.

### The question bank

Generation is slow enough to notice. Measured on this curriculum and model:

| Path | Measured | Per question |
|------|----------|--------------|
| Live generation, no verification | 36s for 10 questions | 3.6s |
| Bank fill, verification included | 127s for 25 questions | 5.1s |
| Serving a banked quiz | 14-47 ms | — |

Verification roughly doubles the per-question cost, which is why the bank runs
it and the live path does not: a background job can afford a second opinion,
a child staring at a spinner cannot.

Filling one whole curriculum is **47 sub-units x 3 tiers x 30 questions =
4,230 questions, about six hours** of continuous generation -- or ~35 hours at
the throttled rate below. That arithmetic is why nothing pre-fills a whole
curriculum on upload.

The bank moves that cost off the request path entirely:

```
Background (APScheduler, every 30 min):
    refill_question_bank() -> generate + verify -> question_bank

Request:
    create_quiz() -> SELECT 10 FROM question_bank -> copy    ~14-54 ms
```

**Measured end to end** on sub-unit 1.2:

| | Before | After |
|---|--------|-------|
| First quiz | ~120,000 ms | **54 ms** |
| Retry | ~120,000 ms | **14 ms** |

`quiz_questions` is unchanged — it is still the student's own snapshot, so
answers, scoring and history work exactly as before. The bank adds one table
and one nullable column, `quiz_questions.bank_question_id`, recording which
bank item each copy came from.

**Retries hand out fresh questions.** That column is what lets a returning
student get questions they have not seen. A 30-deep slot gives three
completely non-repeating attempts; after that the least-recently-served items
come back, because a repeat beats no quiz.

**Practice mode.** A retry of a tier the student has already cleared is marked
`is_practice`. It is scored and recorded in `quiz_attempts` for history, but
`record_attempt()` deliberately does not touch `SubUnitProgress` at all. The
promise is that practising cannot cost you anything, and not writing to
progress is a stronger guarantee than relying on the "best score only" guards.
Verified: a 20% practice run after a 100% pass leaves the score at 100%.

**Shared questions raise the stakes**, so bank generation always runs with
verification on and quarantines disputed items (`is_active=False`) for review
rather than serving them. Retirement is soft — student history points at these
rows.

**Filling manually.** The scheduler tops the bank up gradually, but you can
stock a unit immediately — before a pilot, or after ingesting a new
curriculum:

```bash
cd backend && python utils/fill_bank.py --unit 1 --dry-run
```

```bash
cd backend && python utils/fill_bank.py --unit 1 --depth 30 --workers 3
```

Slots fill concurrently; each worker opens its own session and its own agent,
since neither is safe to share across threads. SQLite is in WAL mode with a
30s busy timeout, which handles the concurrent writes.

**Staying ahead.** The refill job decides its own scope:

| | Kept stocked |
|---|---|
| Unit 1 | always — a new student never waits |
| Student's current unit | always |
| The next unit | only once they pass `BANK_LOOKAHEAD_TRIGGER` (default 70%) |

Completion is averaged across a unit's sub-units, so partial progress counts
proportionally. With 7 sub-units, Unit 2 begins filling at roughly the fifth
completed one.

Deferring the next unit matters for cost: filling it the moment someone starts
the previous one spends hours of generation on units a student who churns in
week one never sees. Sequential unlocking guarantees they cannot outrun the
job — they physically cannot enter Unit N+1 until Unit N is at 100%, which is
strictly after the 70% trigger.

Each run is budgeted (`BANK_REFILL_BUDGET`, default 60 questions, ~5 min) so
no single run holds resources for hours.

**Filling is scoped per curriculum owner.** Curriculum is per family, so unit
numbers are not global -- one family's Unit 3 is a different Unit 3 from
another's. The filler therefore selects work per owner and never by unit
number alone. It did once, and the consequence was concrete: one family
reaching their Unit 3 queued Unit 3 fills for the shared sample and for every
other family at that grade, none of whom were near it, all competing for the
same budget.

The budget is a **global ceiling shared between owners with work to do**, not a
per-owner allowance. Giving each owner the full budget is how a background job
becomes an unbounded bill -- one run's cost would scale with the number of
families. Sharing it means each family fills more slowly as more join, which is
the honest trade; the lever is raising `BANK_REFILL_BUDGET`, not letting the
job decide to spend more.

**A new upload is primed immediately.** A parent who has just uploaded expects
their child to start tonight, and an empty bank means the first quiz spends 36
seconds on the model. `prime_new_curriculum` fills `BANK_PRIME_BUDGET` worth of
**Unit 1 only**, right after the upload is marked complete, and the scheduler
covers the rest within its interval. Deliberately not the whole curriculum:
that is the six hours above, most of it on units sequential unlocking makes
unreachable for weeks, and all of it wasted if the parent re-uploads a
corrected PDF.

**Tuning**, all via `.env`:

| Setting | Default | Effect |
|---------|---------|--------|
| `BANK_DEPTH` | 30 | Questions per slot; 30 = three fresh retries |
| `BANK_LOOKAHEAD_TRIGGER` | 0.7 | How far into a unit before the next one fills |
| `BANK_REFILL_BUDGET` | 60 | Questions per scheduled run, shared across owners |
| `BANK_PRIME_BUDGET` | 90 | Questions filled immediately after an upload; 0 disables |

**A note on concurrency.** `fill_slot` commits after every batch. It must:
SQLite allows one writer, and the lock is taken at the first `INSERT` and held
until commit — so leaving the transaction open across the next model call
holds the write lock for *minutes* and every concurrent filler dies with
`database is locked`. That is not hypothetical; it killed three slots on the
first real run. Note also that `Session.begin_nested()` is not a fix here:
SAVEPOINT runs in autocommit on SQLite (a pysqlite quirk), which silently
commits every insert and defeats the batching.

### Migrations

Alembic owns the schema. `init_db()` runs `alembic upgrade head` on startup.

```bash
cd backend && alembic revision --autogenerate -m "what changed"
```

```bash
cd backend && alembic upgrade head
```

This is not optional tidiness. `create_all()` silently skips tables that
already exist and **cannot add a column to one** — adding the question bank
created the new table fine and then failed at runtime with
`no such column: quiz_questions.bank_question_id`. `init_db()` falls back to
`create_all` only if Alembic is unavailable, and logs loudly that the schema
may be stale.

### API

```
POST   /api/v1/auth/signup | /login | /refresh | /logout | /logout-all
GET    /api/v1/auth/me     | /students | /students/{id}
POST   /api/v1/auth/students                     -> add a child
DELETE /api/v1/auth/students/{id}                -> remove a child and their work

POST   /api/v1/curriculum/upload                 -> 202; parsing runs after the reply
GET    /api/v1/curriculum/uploads | /uploads/{id}
POST   /api/v1/curriculum/uploads/{id}/confirm   -> build it (costs model calls)
POST   /api/v1/curriculum/uploads/{id}/cancel
GET    /api/v1/curriculum/status
GET    /api/v1/curriculum/deletion-preview       -> what deleting would destroy
DELETE /api/v1/curriculum/
GET    /api/v1/curriculum/students/{id}/units    -> the unit map
GET    /api/v1/curriculum/students/{id}/units/{n}
GET    /api/v1/curriculum/students/{id}/next     -> resume, start, or nothing
POST   /api/v1/curriculum/students/{id}/celebrations/{sub_unit_id}/ack

POST   /api/v1/quiz/start                        -> questions, no answers
POST   /api/v1/quiz/unit-test                    -> a cumulative test for one unit
GET    /api/v1/quiz/{id}
POST   /api/v1/quiz/{id}/questions/{qid}/hint    -> only when asked
POST   /api/v1/quiz/{id}/answer                  -> feedback, one question at a time
POST   /api/v1/quiz/{id}/complete                -> score, progress, celebration
POST   /api/v1/quiz/{id}/complete-unit-test

GET    /api/v1/progress/students/{id}            -> parent dashboard
GET    /api/v1/progress/students/{id}/skills     -> per-skill accuracy
GET    /api/v1/progress/students/{id}/attempts   -> history
GET    /api/v1/progress/students/{id}/sub-units/{id}
POST   /api/v1/progress/students/{id}/gaps       -> why a child is stuck
POST   /api/v1/progress/students/{id}/study-plan -> what to revise, in order

GET    /api/v1/gamification/students/{id}        -> points, level, streak
GET    /api/v1/gamification/students/{id}/badges
GET    /api/v1/gamification/students/{id}/avatars        -> the character shop
POST   /api/v1/gamification/students/{id}/avatars/{key}/buy
POST   /api/v1/gamification/students/{id}/avatar         -> wear one already owned
GET    /api/v1/gamification/avatars/starters     -> the free characters
GET    /api/v1/gamification/points-guide         -> how points are earned
```

**Characters are bought with points, never granted.** Two are free from the
start (Sunny and Luna); thirteen more cost 300 to 2,000 points. Buying checks
the balance, spends it and records ownership in one transaction, so a character
cannot be worn without having been paid for.

**Points are only ever awarded by completing a quiz.** There is no endpoint
that grants them, and a test asserts the only POST under `/gamification` is
avatar selection — an endpoint that grants points on request is an endpoint
that grants points on request.

**Practice earns nothing.** A retry of an already-passed tier is deliberately
low-stakes, so if it also paid points the whole economy could be farmed by
replaying one easy sub-unit and a level would stop meaning anything. Practice
still costs nothing; it simply also earns nothing.

**A badge is awarded once, ever**, enforced by a unique constraint on
`(student_id, badge_key)` and checked against what the student already holds.

**No leaderboard, deliberately.** Ranking children against each other turns a
personal mastery signal into a comparison a child can lose, and the whole
progression is about your own 0/33/67/100.

Points: 10 per correct answer, +25 pass, +25 perfect, +50 first time a tier is
cleared, +100 sub-unit at 100%, +250 unit complete, plus badge values. Eleven
badges cover first steps, perfect scores, unaided perfect scores, comebacks
after a failure, proficient passes, mastery, and 3/7/30-day streaks. Avatars
unlock by level.

**Practice is excluded from statistics by default.** A retry after mastery is
deliberately low-stakes and children click through them, so folding those
scores into "average" would understate what a child knows. Practice stays
visible in history, labelled; `include_practice=true` folds it back in.

**Skill stats come from individual responses, not the `skill_breakdown` JSON
on `quiz_attempts`** — responses carry `hint_used`, and "right but needed the
hint" is a materially different signal from "right".

**Skills use a controlled vocabulary per sub-unit** (`services/skill_taxonomy.py`).
Left free-form, the generator invented a tag per question: 396 distinct tags
across 683 questions, so every accuracy read `1/1 = 100%` and nothing could
ever be flagged. The vocabulary is derived once from the learning objective,
validated, and stored on `CurriculumSubUnit.skill_tags`; question generation
must pick from it, and anything that drifts is snapped back.

Deriving once and reusing matters — a vocabulary that changed between batches
would re-fragment the data. Matching compares word *forms* (`comparison` ~
`compare`, `multiply` ~ `multiplication`) but refuses opposites
(`rational` ≁ `irrational`).

```bash
cd backend && python utils/retag_bank.py --dry-run
```

```bash
cd backend && python utils/retag_bank.py --refresh --reclassify
```

`--reclassify` re-derives each tag from the **question text** rather than from
its old tag, which is both more accurate and the only way back once old tags
have been overwritten. Measured on the live bank: 391 tags → 26, ~26 questions
per tag, and sub-units where a single tag held over 60% of questions dropped
from 5 of 7 to 1 of 7.

**The curriculum map is batch-loaded.** `check_unit_unlocked()` costs three
queries per unit, so calling it in a loop makes one dashboard render 20+ round
trips and worsens as the curriculum grows. `build_curriculum_map()` issues a
fixed handful regardless of size and applies the identical rule in memory via
the shared `compute_unlock_status()`. A test pins the query count.

**Celebrations are acknowledged, not consumed on read.** Reporting
`should_celebrate` never spends it; the client calls the `ack` endpoint once
the animation has played. A dropped response or a tab closed mid-animation
therefore cannot cost a child the one moment the whole progression builds
towards — they see it on the dashboard next time instead.

**`ready_difficulties`** tells the UI which tiers the bank can serve
instantly. A tier not listed still works but falls back to live generation and
takes ~36 seconds, which the interface needs to say rather than freeze a
button.

**A quiz is resumable, unconditionally.** `POST /quiz/start` always hands
back an unfinished quiz rather than minting a second one, so a child who
closes the tab mid-quiz returns to the same questions with their answers
intact. There is deliberately no way to discard a half-finished attempt:
minting a fresh quiz would strand their work on an orphaned row, burn ten more
bank questions, and make them redo questions they had already answered
correctly.

`GET /curriculum/students/{id}/next` returns `action: "resume" | "start" |
"none"`, and an unfinished quiz always wins — logging in the next day sends
the child straight back to that unit, that quiz, that question, rather than
pointing at something new while half-finished work sits invisible.

Answered questions carry an `answered` sub-object with what the child chose
and the feedback they already saw. It is **nested rather than flattened** on
purpose: as a sub-object it is `null` for an unanswered question, so the
strings `correct_answer` and `explanation` never appear in the payload at all.
Flattened, those field names would ship with every question — harmless today,
and exactly the shape a future bug fills in by accident.

**Feedback concentrates on mistakes.** A correct answer returns no
explanation and no `correct_answer` — the student just demonstrated they
understand it, and explaining anyway buries the feedback that matters. Both
fields are populated only when the answer is wrong.

**Hints are pulled, not pushed.** The question payload carries `has_hint`, not
the hint text, so the UI can render the button without giving away the nudge.
Serving the hint from its own endpoint is also what makes `hint_used`
trustworthy: the server records that it was served rather than believing a
client-supplied flag, which matters because the Gap Detector will read it as
evidence a skill is shaky.

**The answer key never reaches the student before they answer.**
`QuestionForStudent` has no `correct_answer`, no `explanation` and no
`distractor_rationales`; those appear only in the response to
`POST /answer`. Re-answering a question is rejected, or a student could read
the answer from the first response and resubmit. Both are pinned by tests.

**Ownership is enforced once, in `api/deps.py`.** Every quiz and progress
route is addressed by `student_id`, so without it any authenticated parent
could reach another family's child by guessing an id. A student belonging to
someone else returns **404, not 403** — distinguishing them would confirm the
id is real and allow enumeration.

**Refresh tokens rotate.** Each refresh revokes the presented token, so a
replay fails and a theft surfaces instead of granting quiet 30-day access.
Sessions are stored hashed, which is what makes server-side logout possible.
A refresh token is also rejected where an access token is expected — without
that check a leak grants 30 days instead of 60 minutes.

### Anti-hallucination

`QuizQuestion.source_chunk_ids` records the Pinecone vectors each question was
generated from, and `Quiz.generation_metadata` records the model and retrieval
parameters. Any question can be traced back to the curriculum page it came
from.

---

### Curriculum ownership

Curriculum used to be global — one operator-loaded set that every account
shared. It is now owned: `curriculum_units.user_id` names the parent who
uploaded it, and the identity constraint is
`(user_id, subject, grade_level, unit_number)`. Without the owner in that key,
the second family to upload a grade-6 maths guide would collide with the first,
because their Unit 1 is a different Unit 1.

`user_id` is **nullable, and NULL means the shared sample**. A brand new
account with an empty dashboard looks broken rather than new, so a family sees
the sample until they upload their own — and then their own *instead of*, not
as well as. Two interleaved Unit 3s would break sequential progression, which
assumes one ordered course.

That visibility rule lives in `services/curriculum_scope.py` and nowhere else.
Five hand-written copies of it across the routes, services and agents that
query units would drift, and the direction they drift is one family seeing
another's material.

**Vectors are isolated by namespace, not by a metadata filter.** Shared
curriculum keeps the bare `math-grade6` form so vectors indexed before
ownership existed stay reachable; an upload gets `math-grade6-u<owner>`. A
forgotten filter would leak across families; a wrong namespace simply returns
nothing. Retrieval reads the namespace off `CurriculumSubUnit.vector_namespace`
rather than recomputing it from subject and grade — recomputing would send an
uploaded curriculum's search to the shared namespace and confidently return
material from a different course.

**Known gap:** deleting an account cascades its curriculum in SQLite but leaves
its Pinecone namespace behind. There is no account-deletion endpoint yet, so
nothing reaches this today, but one would need to drop the namespace too.

### Uploading a curriculum

`POST /curriculum/upload` (multipart PDF) returns **202, not 201**: the file is
accepted and stored, but the curriculum does not exist yet. Parsing, chunking,
embedding and indexing take minutes, so they run after the response and the
`curriculum_uploads` row is polled at `GET /curriculum/uploads/{id}`.

That row is a real table rather than in-memory job state: a restart mid-ingest
would otherwise leave a parent watching a spinner with nothing behind it, and
"what did I upload, and did it work?" should still be answerable tomorrow.

**Everything that can fail in the background lands on the row.** Nothing else
is listening — an exception in a background task vanishes into the worker
thread. The first live run proved the point: an `ImportError` escaped before
the row was marked, and the upload sat on `pending` forever with no error
anywhere. The whole body is now inside the try, and a test pins it.

Files are validated **before** anything is stored: magic bytes rather than the
filename or the content type, both of which the client chooses, plus a size cap
and a filename sanitiser so a crafted `../../` cannot escape the owner's upload
directory.

One ingestion runs per account at a time. Two concurrent ones would race on the
same `(user, subject, grade, unit)` rows and interleave two curricula into one.

The operator CLI still exists and still writes the shared sample — it simply
passes no owner:

```bash
cd backend && python -m rag.pipeline "../06 Pre-Algebra H&A.pdf" --grade 6
```

### Frontend

React 19 + TypeScript, built with Vite. `frontend/src` holds the whole app;
there is no state library, because there is no shared mutable state worth one —
each screen owns its own fetch.

**One frame, six screens.** `components/SideShell.tsx` draws the top bar and the
sidebar — Curriculum, Learn, Progress, Rewards — and every screen renders inside
it. Each screen owns a scoped stylesheet (`.bt-app .bt-*`), so restyling one
cannot reach another.

**Screens fit the window instead of scrolling.** Sizes are written as
`compact + range * var(--k)`, where `--k` grows with the window height, so a
page tightens up on a short laptop screen rather than cutting a sticker in half
or pushing a button out of view. Children lose things that move below the fold.

**The quiz screen is where the product rules live** (`pages/QuizPage.tsx`), and
each is easy to break by writing the obvious version instead:

- **No submit button.** Choosing an option *is* the answer, and the choice
  cannot be taken back.
- **Explanations only when wrong.** The server omits `correct_answer` and
  `explanation` entirely for a correct answer, so the client cannot show them
  even by accident. Explaining a right answer teaches a child to skip
  explanations, including the ones that matter.
- **Hints only on request.** Never shown up front, and the request is recorded
  server-side because the Gap Detector reads "needed a hint" as evidence.
- **Resume exactly where they stopped.** The server returns already-answered
  questions carrying the feedback the child was shown, so a quiz reopened the
  next day looks as they left it.
- **True/false looks different on purpose.** Two large buttons, ✓ True and
  ✗ False, and the wrong-answer feedback names the answer ("The answer is
  False") rather than a letter, which means nothing on a two-option question.

**Progress is three tabs, not three pages** (`pages/ProgressPage.tsx`). Overview
holds the headline numbers, each unit's progress and recent quizzes. Skills
lists accuracy per skill, weakest first, with how many questions it rests on and
how often a hint was needed — "right, but needed a hint" is a different signal
from "right", and the server flags the ones worth attention. Revision plan asks
how long there is until the test and then calls the test-prep agent.

The plan runs **on the button, never on load**: it costs a model call, and a
page that quietly spends one every time it opens is a page nobody can afford to
leave open. It shows its reasoning too — which topics were chosen and why —
because a revision schedule a parent cannot question is one they cannot trust.
Each tab's own list scrolls inside its card, so the page itself still fits the
window.

**The child is set up where the parent already is** (`components/ChildBox.tsx`,
on the curriculum page): add a child with a name, grade and free starting
character, switch that character, or remove a child. Removing says exactly what
it destroys, in words, and asks again before doing it.

**Rewards are a shop, not a scoreboard** (`pages/RewardsPage.tsx`). Characters
and badges sit in side-scrolling rows, four cards to a row, and a locked
character shows its price rather than hiding it. There is no leaderboard
anywhere in the app.

**The celebration fires on `should_celebrate` only** — set when all *three*
tiers of a sub-unit are complete, not on a passing score. It is acknowledged
after the animation has played rather than when it is sent, so a tab closed
mid-animation costs the child nothing.

**Refresh is single-flight.** The backend rotates refresh tokens, so two
requests expiring at once must not both try to refresh — the second would
present a token the first already spent and the child would be thrown back to
the login screen mid-quiz. `api/client.ts` lets one refresh run and has the
others await it.

**Colour tokens are named `correct`/`incorrect`, not `right`/`wrong`.**
`text-right` is a built-in Tailwind alignment utility, so a colour token of
that name loses to it silently — the text just quietly aligns right.

## Evals

Tests check the code with the AI replaced by canned answers. Evals check what
the real model actually writes, against a fixed dataset a person has reviewed.

**The golden dataset** is `evals/quiz_generator_golden_v1.xlsx`: one row per
topic and level — 47 topics × Easy/Medium/Tricky, plus a topic that does not
exist and must produce a clear error. Each row carries the learning objective,
what a good question looks like at that level, what it must and must not
contain, and a sample question. A row is used only once a person has reviewed it
and set `reviewed by you? = Y`; 15 rows are marked for the first run.

```bash
cd backend && python -m evals.run_quiz_generator_eval --owner parent@example.com
```

Fifteen rows take about ten minutes and spend model credits. The run works on a
temporary copy of the database, so an eval can never change real data, and it
writes a workbook of its own into `evals/runs/`:

| Sheet | Holds |
|-------|-------|
| Summary | Every score against its target |
| Results | One row per question written, with empty Y/N columns to grade |
| Cases | What was measured automatically for each row |

Seven checks are graded by a person — correct key, exactly one right answer, on
topic, right level, plausible wrong answers, explanation, hint — and seven are
measured by the script: completion, format pass rate, checker agreement,
attempts, answer-letter balance, true/false share and time taken.

```bash
cd backend && python -m evals.combine_runs --master evals/runs/<earlier>.xlsx \
    --run evals/runs/<later>.xlsx --suffix 2
```

That copies a later run's sheets beside an earlier one and builds a Comparison
sheet. Comparing is the point: measure, change one thing, measure again.

**What the first loop found.** Run 1 keyed every answer correctly but scored
91.4% on hints: the rejected ones named a strategy ("undo subtraction first,
then undo multiplication") rather than the step to take. Rule 8 of the writing
prompt was rewritten to ask for the first real step in a child's own words, with
examples of both. Run 2 scored 100% on all seven checks, and nothing else
dropped — which is why all seven are graded every time, not just the one being
worked on.

Two things worth knowing before reading any eval result: the model writes
differently every run, so a number that moves without a matching change is
noise; and a run where everything scores 100% has stopped telling you anything,
which is the signal to widen the dataset rather than to celebrate.

## Layout

```
braintuitive/
├── backend/
│   ├── main.py              FastAPI app, lifespan, health, router mounting
│   ├── config.py            Typed settings singleton (all env vars)
│   ├── migrations/          Alembic versions (8)
│   ├── db/
│   │   ├── models.py        16 tables + enums + progression logic
│   │   └── database.py      SQLite engine, session helpers, Mongo client
│   ├── agents/
│   │   ├── base_agent.py         ReAct base class
│   │   ├── quiz_generator.py     Question writing, validation, answer balance
│   │   ├── question_verifier.py  Blind independent answer-key check
│   │   ├── gap_detector.py       Why a child is stuck, from their wrong answers
│   │   └── test_prep.py          Revision plan: what to study, in what order
│   ├── rag/
│   │   ├── pdf_parser.py    PDF -> units + sub-units
│   │   ├── chunking.py      Token-aware, section-aware chunking
│   │   ├── embeddings.py    Nebius embeddings + dimension verification
│   │   ├── pinecone_client.py  Index lifecycle, upsert, filtered query
│   │   ├── retrieval.py     Scoped semantic search (agent-facing)
│   │   └── pipeline.py      Ingestion orchestration + CLI
│   ├── api/
│   │   ├── deps.py          Auth + ownership dependencies
│   │   ├── schemas.py       Pydantic request/response models
│   │   └── routes/          auth, quiz, curriculum, progress, gamification
│   ├── services/
│   │   ├── curriculum_scope.py    Which curriculum a student sees
│   │   ├── curriculum_upload.py   Validation, storage, background ingestion
│   │   ├── quiz_service.py     Unit locking, bank draw, practice mode
│   │   ├── question_bank.py    Pre-generated pool: fill, draw, retire
│   │   ├── unit_test.py        Interleaved cumulative test over a whole unit
│   │   ├── gamification.py     Points, badges, streaks
│   │   ├── avatars.py          The 15 characters and what they cost
│   │   ├── skill_taxonomy.py   Controlled skill vocabulary per sub-unit
│   │   └── scheduler_jobs.py   Per-owner refill + priming a new upload
│   ├── evals/               Measuring what the AI writes (see Evals)
│   │   ├── run_quiz_generator_eval.py   Run the golden dataset, write a workbook
│   │   ├── combine_runs.py              Put runs side by side and compare
│   │   └── xlsx.py                      Read and write .xlsx, standard library only
│   ├── utils/
│   │   ├── check_services.py   Preflight diagnostics
│   │   ├── fill_bank.py        Manual bank fill by unit
│   │   ├── retag_bank.py       Re-derive skill tags onto the vocabulary
│   │   └── security.py         Hashing + JWT primitives
│   └── tests/                  614 tests
│       ├── test_api.py                  138
│       ├── test_quiz_generator.py       113
│       ├── test_curriculum_upload.py     42
│       ├── test_test_prep.py             41
│       ├── test_question_bank.py         38
│       ├── test_rag.py                   37
│       ├── test_unit_test.py             36
│       ├── test_skill_taxonomy.py        31
│       ├── test_gap_detector.py          28
│       ├── test_foundation.py            25
│       ├── test_avatars.py               25
│       ├── test_question_verifier.py     22
│       ├── test_scheduler_scope.py       18
│       ├── test_evals.py                  8
│       ├── test_curriculum_delete.py      8
│       └── test_write_lock.py             4
├── frontend/
│   ├── vite.config.ts       Tailwind plugin + /api proxy to port 8000
│   ├── public/              Artwork: stickers, banners, characters
│   └── src/
│       ├── api/
│       │   ├── client.ts    fetch wrapper, single-flight token refresh
│       │   └── types.ts     Types mirroring the OpenAPI schema
│       ├── auth/AuthContext.tsx  Session, which child is working, add/remove
│       ├── components/
│       │   ├── SideShell.tsx     Top bar + sidebar, the frame every screen uses
│       │   ├── ChildBox.tsx      Add, restyle or remove a child
│       │   ├── CharacterImage.tsx, badgeIcon.ts, PointsGuide.tsx, ui.tsx
│       └── pages/
│           ├── LoginPage.tsx      Sign in / create account with the child
│           ├── CurriculumPage.tsx Upload the school's guide; manage children
│           ├── LearnPage.tsx      Units, topics and their three levels
│           ├── QuizPage.tsx       The core loop -- immediate feedback
│           ├── ResultPage.tsx     "See how I did": score, points, badges
│           ├── ProgressPage.tsx   Overview, skills, and a revision plan
│           └── RewardsPage.tsx    Characters to unlock, badges earned
├── evals/
│   ├── quiz_generator_golden_v1.xlsx   The reviewed golden dataset
│   └── runs/                            One workbook per eval run
├── data/                    SQLite file + uploads (gitignored)
└── 06 Pre-Algebra H&A.pdf   The curriculum this was built against
```

`components/PaletteSwitcher.tsx` is still wired up for trying colour schemes on
the pre-redesign layout; everything else in `pages/` is reachable from the app.

---

## Conventions

- Configuration is read **only** through `from config import settings` — never
  `os.environ` directly.
- Routes take `db: Session = Depends(get_db)`; background jobs and agent tools
  use the `session_scope()` context manager instead.
- Enums store their value (`"beginner"`), not the member name, so the raw
  SQLite file stays readable.
- Schema changes go through Alembic. Never hand-edit a database; write a
  migration so every environment converges on the same schema.
