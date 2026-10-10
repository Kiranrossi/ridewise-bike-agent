# Design decisions

## 1. The three most important design decisions

**1. Numbers come from code, rules come from YAML: never from the LLM.**
Refunds, late fees, battery fees, rental quotes and deposits are computed by a Python policy engine that
reads every rule from validated YAML (`rules/*.yaml`, each rule citing its PDF clause). The LLM only
explains the result. *Why:* correctness carries the most weight, and policy maths has traps an LLM gets
wrong: two refund versions chosen by booking time against an **IST** cutoff while the DB stores **UTC**,
part-hour rounding, an inclusive GOLD grace period, "below 10%" vs exactly 10%. Because the engine is
deterministic it is testable: it reproduces every recorded charge and refund in the database (1,369 completed
bookings, all cancellations and no-shows). YAML also makes the system adaptable: a v3 refund policy is a
config change, not a code change.

**2. Entitlements are enforced in code, not in the prompt.**
The customer ID comes only from the login session and is bound into every query; tools take no customer-ID
argument; each role sees only its own tools (re-checked at execution). Staff analytics SQL runs on a
read-only connection with a SQLite **authorizer** that blocks writes and returns personal-data columns as
NULL, even through aliases or `SELECT *`. Free text is scrubbed and treated as untrusted data. *Why:* a
prompt can be talked around; code cannot. The data contains real traps: other customers' emails in payment
remarks and planted instructions in damage reports.

**3. A tool-calling agent with verifiable output.**
gpt-oss-120b chooses among 14 role-scoped tools in a plain Python loop. Every answer shows typed sources (PDF
file/section/page or DB table/record IDs), the calculation steps and the exact SQL run, which is reproducible
in DB Browser. A deterministic **verifier** checks every date and ₹ amount against the evidence before the
answer is shown and triggers one repair. *Why:* hidden evaluation questions are open-ended and multi-step,
which needs flexibility; transparency and verification make that flexibility trustworthy.

## 2. Alternatives considered and rejected

| Alternative | Why rejected |
|---|---|
| Fixed router pipeline (route → fetch → answer) | Breaks on multi-step and follow-up questions; safety already lives in the tools. Kept its benefits via a pre-fetched account snapshot and a rule-based fallback. |
| LangChain / LangGraph | Extra abstraction for a ~200-line loop I can fully explain. |
| Text-to-SQL for customers | Unsafe entitlement boundary. Customers get only fixed, parameterised queries. |
| LLM arithmetic / RAG-only policy answers | Wrong on version selection and boundaries; unverifiable. |
| Chroma, FAISS, Pinecone, pgvector | Missing built-in hybrid search or metadata filters, or need a server/account. Chose Qdrant local mode. |
| PyTorch embedding models | Large install threatens the 10-minute setup. Chose bge-small via ONNX/FastEmbed. |
| Runtime OCR of scanned annexures | Install risk. Transcribed the four image-only tables/figures verbatim, cited to the original page. |
| Plain SQL for "which refunds are pending" | Gives 35 bookings; policy says 2. Added an `audit_refunds` tool that recomputes all 268. |

## 3. Where the assistant currently fails or behaves poorly

- **The model can still miscopy or invent details.** The verifier catches dates and ₹ amounts (including
  impossible weekdays), but not percentages, counts or vehicle names.
- **Retrieval is English-only.** Slang ("money back if I don't turn up") or Hinglish relies on the LLM
  rephrasing the question; pure slang can miss the right clause.
- **Image-only policy content is transcribed by hand.** A new scanned annexure needs re-transcription.
- **Analytics depend on the LLM writing correct SQL.** The SQL is shown for checking, and definitions (revenue
  excludes GST) are in the tool rules, but an ambiguous question can still be interpreted differently.
- **Latency** is 2–5 s typically and up to ~10 s for multi-tool turns; rate limits could slow it further.
- **Demo-grade authentication** (mobile number as customer password); **single-process** local vector index;
  the assistant is **read-only** and cannot cancel or change bookings.

## 4. What I would build next with one more week

1. **Real authentication** (OTP / SSO) and per-staff permissions (support vs hub operations).
2. **Wider verification:** ground percentages, counts and entity names, and run the red-team suite in CI on
   every commit with a regression dashboard.
3. **Safe write actions with confirmation:** cancel a booking, raise a dispute or support ticket, with an
   audit trail.
4. **Retrieval upgrades:** multilingual embeddings (Hindi/Kannada/Hinglish), a cross-encoder reranker, and an
   OCR + human-review pipeline for scanned policy pages.
5. **An admin UI for business rules** with validation and preview, plus a semantic layer of named metrics
   (revenue, no-show rate) so analytics don't depend on free-form SQL.
6. **Production deployment:** Qdrant server mode, response caching, streaming answers, and monitoring of tool
   errors and verifier repairs.
