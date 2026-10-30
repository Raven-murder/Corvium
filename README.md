# Corvium

**Your notes, organised and answerable.** Corvium is a private study assistant that reads your notes in almost any format, answers questions with citations to the exact page, remembers your deadlines, and turns your material into study plans, quizzes and diagrams.

Built for the **Nebius x NVIDIA Global AI Hackathon**, Personal AI track. Runs on **Nebius Token Factory** with **NVIDIA Nemotron**.

| | |
|---|---|
| **Live demo** | https://corvium.streamlit.app/ |
| **Demo video (3 min)** | https://youtu.be/uNrAimqsc_c |
| **Licence** | MIT |

---

## The problem

A student's notes are scattered across PDFs, slides, Word files, photos of whiteboards and scans. Finding one definition means opening five files. Exam dates live in a calendar, weak topics live in your head, and a study plan means doing the arithmetic yourself.

Corvium puts all of it in one place. It reads everything you upload, answers from your own material, shows where each answer came from, and remembers what matters, so you spend your time studying, not searching.

## Try it in 60 seconds

The demo opens with sample notes already loaded (database normalisation and transactions), so you do not need to upload anything.

1. Ask **"What is second normal form?"** and open **Sources** under the answer.
2. Open **Memory** and notice the saved exam date and weak topic.
3. Open **Tools**, run **Study plan**, then run a **Quiz**.
4. Type **"Make a flowchart for normalization"** in the chat.
5. Upload your own PDF, slide deck or photo of notes and ask about it.

*The demo is rate limited per visitor, because every question spends hackathon credits.*

## Hackathon track

**Personal AI.** Corvium is a private study assistant with persistent memory (deadlines, weak topics and preferences you can view and edit), controlled access to the information you choose (your own uploaded documents, in your own workspace), and tasks it carries out for you: study plans, quizzes, diagrams, notes and exports.

## Testing instructions for judges

- Open the demo link. No login is needed. The app starts with sample notes and sample memories already loaded.
- If you see "This app has gone to sleep", click **Yes, get this app back up** and wait about a minute.
- Use **Reset demo** at the bottom of the sidebar to restore the sample data at any time.
- To test your own material, upload a PDF, slide deck, Word file or photo of notes.
- To protect the shared hackathon credits, each workspace has daily usage limits (see `src/config.py`). Contact the author if you need them raised for testing.

## Nebius services

Nebius Token Factory only (chat, embeddings and vision). Hosting is on Streamlit Community Cloud and Supabase, which are not Nebius services.

---

## Features

### 1. Answers you can check

Every answer is built only from your documents and carries numbered citations. One click opens the source list, grouped by document, with the page, slide or section.

1. **Citation** after each claim, naming the document and page.
2. **Sources** button under the answer.
3. **Passage list**, grouped by document, with each passage cut cleanly at a sentence boundary.

### 2. Open the original, inside the app

No downloads and no outside viewers. Click a source and Corvium opens the document at the cited page.

1. **Page controls** to move through the document.
2. **Add bookmark** to save this spot for later.
3. **Open original** to download the exact file.

PDFs and scanned pages are shown as page images. Images are shown directly. Word, PowerPoint, HTML, text and Markdown are shown as sectioned text.

### 3. Every kind of note, including photos and scans

Upload **PDF, DOCX, PPTX, TXT, Markdown, HTML, PNG, JPG and WEBP**. Scanned PDF pages and images are read with a vision model, so a photographed whiteboard becomes searchable text.

1. **Upload** button.
2. **Image** stored in its own section of the library.
3. **Answer** that quotes text read from the image, with its source.

### 4. A library you organise your way

Folders, renaming, moving and reordering for every document. Images get their own section.

1. **Folders**, which collapse and expand.
2. **Edit menu** for rename, move and reorder.
3. **Images** grouped by type.

### 5. Memory that persists

Tell Corvium "my DBMS exam is on 5 November" or "I am weak at normalisation". It remembers, and uses it in later answers and plans. You see every memory and can edit or delete it.

1. **Memory kinds**: deadline, weak topic, preference and fact.
2. **Edit** a memory in place.
3. **Add** one manually, or **clear all**.

### 6. A study plan that knows your deadlines

The plan counts the days until your nearest remembered deadline, focuses on your weak topics, and is built from the documents you pick. Plans are saved and can be pinned.

1. **Days** defaulting to the time left before your deadline.
2. **Focus topics** pre-filled from memory.
3. **Saved plans** you can pin and reopen.

### 7. Quizzes from your own notes

Choose the topic, difficulty, number of questions and which documents to draw from. Each question is graded and linked to its source page.

1. **Options** for topic, difficulty and sources.
2. **Question** with multiple choice answers.
3. **Source** for each answer, so you can read the original passage.

### 8. Diagrams of your material

Ask for a flowchart or a mind map of any topic. The model describes the structure, the app checks it, and Graphviz draws it as an image. Colours stay consistent in every theme, and you can enlarge or download it.

1. **Diagram** drawn from your notes.
2. **Enlarge** and **Download PNG**.
3. **Sources** for the passages the diagram came from.

### 9. Notes, with export

A full-page notes editor with live preview. Save any answer as a note, insert saved diagrams, then export to **TXT, Markdown or PDF** (diagrams included).

1. **Note list** with search and pins.
2. **Editor** and live preview.
3. **Export** buttons.

### 10. Search, pins and bookmarks

Search across every document by topic or filename. Pin chats and messages you want to keep, and bookmark exact pages.

1. **Search box**.
2. **Matching documents** with the best passages.
3. **Open** at the exact page.

### 11. Made for long study sessions

- **Background answers.** Answers keep generating while you change settings or open other panels, and a **Stop** button cancels them.
- **Themes.** Auto (follows your system), Light, Dark and Eye comfort, with six accent colours.
- **Separate chats**, with rename, pin and delete.

---

## How it works

```mermaid
flowchart LR
  U["Student"] --> UI["Streamlit app"]
  UI --> ING["Ingestion: PDF, DOCX, PPTX, HTML, TXT, MD, images"]
  ING -->|"scanned pages and images"| OCR["Qwen3.8-27B vision on Token Factory"]
  ING --> EMB["Qwen3-Embedding-8B on Token Factory"]
  EMB --> DB[("PostgreSQL or SQLite: chunks, files, memory, chats, notes")]
  UI --> RET["Retrieve the most relevant passages"]
  RET --> DB
  RET --> LLM["Nemotron 3.5 Lightning on Token Factory"]
  LLM --> UI
```

1. **Ingest.** Each file is converted to text (pages, slides or sections are kept for citations), split into chunks and embedded.
2. **Retrieve.** A question is embedded and compared with the chunks of the selected documents by cosine similarity.
3. **Generate.** Nemotron answers using only the retrieved passages and cites them.
4. **Remember.** Durable facts you mention are saved to memory and used in later answers.

### Nebius and NVIDIA models

Everything runs on **Nebius Token Factory** through its OpenAI-compatible API, on public pay-per-token endpoints. The app manages no GPUs.

| Task | Model |
|---|---|
| Answers, study plans, quizzes, diagram descriptions | `nvidia/Nemotron-3_5-Lightning` (NVIDIA Nemotron) |
| Embeddings for retrieval and search | `Qwen/Qwen3-Embedding-8B` |
| OCR of images and scanned pages | `Qwen/Qwen3.8-27B` (vision) |

**What Token Factory gave us:** one endpoint and one client library cover chat, embeddings and vision, so the whole retrieval pipeline needed no model hosting. Nemotron's reasoning and tool-calling support suit planning and structured output. Because Nemotron is a reasoning model, the app sets generous output limits so answers are not cut off.

### Built with

Python 3.12, Streamlit, PostgreSQL on Supabase (SQLite for local use), Graphviz, pypdfium2, pypdf, python-docx, python-pptx, reportlab.

---

## Storage and privacy

- Your documents, chats, notes and memory are stored in a **private workspace** that belongs to the link you opened, and every database table is filtered by workspace. **Anyone who has your workspace link can open that workspace.** There is no login.
- **Text you upload and the passages used for each question are sent to Nebius Token Factory** for embedding, OCR and answer generation. Corvium does not run models locally.
- Inactive workspaces are deleted automatically.
- API keys and the database URL live in environment variables or host secrets and are never stored in the repository.
- The public demo has usage limits to protect shared credits.

## Known limitations

- Word and PowerPoint files open in the viewer as extracted text, not in their original layout. Use **Open original** for the exact file.
- Notes can embed a saved diagram at the end of the note only, not at the cursor.
- No voice input and no image generation: neither is offered by the Token Factory models available to this project.
- The free database tier may pause after inactivity, so the first load after a long break can be slow.
- OCR on poor photos can be inaccurate. Always check the cited source.

---

## Self-hosting (optional)

Corvium needs your own Nebius Token Factory API key, and Nebius bills usage per token. Most people should just use the live demo.

Requirements: Python 3.12, [Graphviz](https://graphviz.org/download/) with `dot` on your PATH, and a Nebius API key.

```
   git clone https://github.com/Raven-murder/Corvium.git
   cd Corvium
python -m venv .venv
.venv\Scripts\Activate.ps1        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root (it is git-ignored):

```
NEBIUS_API_KEY=your_key_here
NEBIUS_VISION_MODEL=Qwen/Qwen3.8-27B
# Optional: use PostgreSQL instead of local SQLite
# DATABASE_URL=postgresql://user:password@host:5432/postgres
```

```
python -m streamlit run app.py
```

Without `DATABASE_URL`, everything is stored under `data/` on your machine and stays saved between runs. On Streamlit Community Cloud, put the same values in the app's Secrets (TOML, for example `NEBIUS_API_KEY = "..."`) and keep `packages.txt` so Graphviz is installed.

| Variable | Purpose |
|---|---|
| `NEBIUS_API_KEY` | Token Factory API key (required) |
| `NEBIUS_BASE_URL` | Overrides the default Token Factory URL |
| `NEBIUS_MODEL` | Overrides the main chat model |
| `NEBIUS_VISION_MODEL` | Vision model for OCR. Without it, images and scanned pages are skipped with a notice |
| `DATABASE_URL` | PostgreSQL connection string (Session pooler recommended) |

Checks: `python -m unittest discover tests`, and `python tools/db_check.py` (needs `DATABASE_URL`) for migrations, workspace isolation and cleanup.

## About this repository

The repository was created during the submission period. All application code, including the Corvium logo (generated by `tools/make_logo.py`), was written during the hackathon.

## Licence

MIT. See `LICENSE`.
