# CivicLens my entry to Civic AI – AIIdeas 10.000

> *How AI can turn bureaucratic opacity into structured, verifiable civic clarity.*

---

Most government decisions that affect our daily lives are technically public. They're open for everyone to read, archived, and searchable, but almost impossible to understand.

Council agendas run dozens of pages. Budget documents bury critical changes in dense tables. Public notices describe obligations in language only lawyers can read. The information is there — but clarity isn't provided for the average person.

That gap is where CivicLens begins.

---

## The Idea Behind CivicLens

CivicLens is a non-partisan AI assistant designed to translate complex public documents into clear, structured, plain-language insight — without losing accuracy, and without inserting opinions or judgement into the core information.

 - Upload a **council agenda**. 
 - Upload a **municipal ordinance**. 
 - Upload a **public notice**. 

Within moments, CivicLens tells you:

- **What changed**
- **Who is affected**
- **What actions or deadlines matter**
- **Where exactly** that information appears in the source document

Every key statement links back to a verifiable citation from your initially uploaded document. No summaries floating in abstraction. No *"trust the AI."* Just structured clarity anchored to the original text.

> The goal isn't persuasion. It's understanding.

A deliberate choice in CivicLens's design is to separate extraction from interpretation. The system is not asked to evaluate whether a decision is good or bad, fair or unfair. It is asked to find, structure, and cite. This architectural constraint is what keeps the tool useful across political contexts — not a claim of perfect objectivity, which no AI system can honestly make, but a structural commitment to staying close to the source.

---

## Why This Problem Matters

Transparency is often mistaken for accessibility. If a 70-page PDF is online, the political system assumes the public is informed. But in reality, the time, expertise, and patience required to decode those documents creates a silent barrier, not only for citizens whose mother tongue is not the language of the document, but for many others too. Exemplary implications of not understanding bureaucratic language can be:

- Citizens miss deadlines — facing fines, legal consequences, or permanent loss of entitlements simply because the language of the notice was inaccessible
- Journalists lose hours scanning for relevance — missing the story entirely while those in power count on complexity to avoid scrutiny.
- Community groups discover regulatory changes too late — unable to organize, protest, or challenge decisions that have already been quietly passed into law

CivicLens transforms "public but unreadable" into something actionable. It lowers the entry barrier to civic participation without altering the substance of the information itself.

CivicLens is built for anyone who has ever opened a government document and felt lost — from the first-generation citizen navigating a bureaucratic system in a second language, to the seasoned journalist racing a deadline, to the local activist trying to hold institutions accountable. If public information exists, it should be truly accessible to everyone, not just those with the time, training, or privilege to decode it.

**Clarity is power — especially when it's neutral and verifiable.**

---

## Building CivicLens: From Upload to Insight

The system behind CivicLens is intentionally simple, but carefully structured.

### Document Ingestion

When a user uploads a document, the pipeline begins with a pre-signed upload URL and a newly generated document ID. The file is stored in a raw S3 bucket — untouched and preserved exactly as received so the original document remains accessible for every subsequent analysis.

### Text Extrction 
Once the file lands in the S3 raw bucket, an event automatically triggers a Lambda function that extracts the document's text and stores it as a clean, processable plain-text artifact — ready for the analysis pipeline.

### State Machine

Every stage of the document's lifecycle is tracked in a lightweight `meta.json` file. No traditional database backend is required. The processing state, error codes, and timestamps are all stored there, implementing a small but reliable state machine:

```
UPLOADING → EXTRACTING → STRUCTURING → DONE [-> Q and A Logic ]
```

If anything fails, the system transitions to `ERROR` and the error message and code are saved in the meta file.

### Architecture Overview

```
User Browser
    |
    |-- POST /create-document --> Lambda (JWT check, rate limit)
    |                                 |-- S3 presigned POST URL
    |-- PUT (file) ------------> S3 raw bucket
    |                                 |-- triggers extract-text Lambda
    |                                 |-- triggers structured-analysis Lambda (Bedrock)
    |-- GET meta.json (poll) --> S3 processed bucket (via Cognito creds)
    |-- GET structured.json ---> S3 processed bucket (via Cognito creds)
    |-- POST /document-qa -----> Lambda (JWT check, Bedrock Claude)
```

### Structured Analysis

Once the text is extracted, a second Lambda function takes over. Using **Amazon Bedrock** with **Anthropic's Claude Opus 4.5**, CivicLens performs deterministic extraction guided by a strict JSON schema. The model is not asked to "summarize creatively." It is required to:

- Extract key decisions
- Identify affected groups
- Detect deadlines
- Provide direct evidence quotes
- Output structured JSON only

After the model's output is received, it is validated against the required schema to ensure 
all required fields are present and correctly formatted. If validation fails, a backup prompt 
is triggered on the document to ensure a correctly structured response. It is important to 
note however that structural validation alone cannot guarantee semantic correctness — the 
model may produce output that is well-formed but does not accurately reflect the content 
of the original document. This is an inherent limitation of any LLM-based extraction system 
and a known area for future improvement. The final result is a `structured.json` file — a 
verifiable representation of the document's core information.

Throughout the whole process, the UI polls the `meta.json` file and displays the current state. Once the state switches to `DONE`, the generated insights appear in a structured, human-readable layout on the frontend.

---

## Grounded Q&A — Minimizing Hallucination Risk

Beyond structured summaries, CivicLens includes an optional Q&A layer. When a user asks a question, a dedicated Lambda function calls **Amazon Bedrock's Knowledge Base** using a [retrieve-and-generate](https://de.wikipedia.org/wiki/Retrieval-Augmented_Generation) approach.

The answer is grounded in the processed document content, and citations are returned alongside every response. This dramatically reduces hallucination risk. Instead of inventing context, the model retrieves relevant passages and generates responses tied directly to them.

Answers are optionally stored under a timestamped path, creating a traceable record of every interaction with a given document.

---

## Design Decisions That Matter

Several architectural choices shaped the system — and each was as much a *trust decision* as a technical one.

| Decision | Rationale |
|---|---|
| **Event-driven serverless design** | S3 and Lambda coordinate each stage, keeping infrastructure lightweight and scalable |
| **Raw vs. processed separation** | Original files are never overwritten; derived artifacts live in a distinct namespace |
| **S3-based state tracking** | `meta.json` replaces a database — simple, deterministic, transparent |
| **Schema-enforced extraction** | Structured output is significantly more reliable than freeform summarization |
| **Evidence-required responses** | Every key statement must tie back to a source quote |

---

## What I Learned

Building CivicLens reshaped how I think about AI systems in civic contexts.

**Prompting alone is not enough.** Reliability comes from constraint. Strict schemas, required evidence, and deterministic formatting dramatically improve consistency.

**Citations change everything.** A summary without grounding feels speculative. A summary with traceable quotes feels credible.

**Serverless architecture is ideal for early-stage civic technology.** Using S3, Lambda, API Gateway, Amplify, and Bedrock made it possible to build a complete pipeline within Free Tier limits — while remaining fully scalable.

**Neutrality must be embedded into architecture.** It is not a feature you toggle on. It is enforced through structure, grounding, and the strict separation of interpretation from extraction.

---

## The Bigger Vision

CivicLens doesn't aim to replace institutions or reinterpret policy. It aims to make public information truly public.

If democratic participation depends on understanding decisions, then clarity is infrastructure. CivicLens turns bureaucratic opacity into structured visibility — grounded, neutral, and verifiable.

And that's where better civic engagement begins.

---

*Built with Amazon Bedrock, AWS Lambda, S3, API Gateway, and Amplify — entirely within the AWS Free Tier.*
