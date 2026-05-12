# Primary School Tutoring Assistant

A single-page AI-powered assistant for primary school tutors (Australian curriculum, students aged 5–11). Runs entirely in the browser — no backend, no build step.

## Setup

### 1. Get an Anthropic API Key
- Sign up at [console.anthropic.com](https://console.anthropic.com)
- Create an API key under **API Keys**
- You'll need credits loaded; see [Anthropic's pricing](https://www.anthropic.com/pricing)

### 2. Host on GitHub Pages
1. Fork this repository
2. Go to **Settings → Pages**
3. Set **Source** to `Deploy from a branch`, select `main`, folder `/` (root)
4. Save — your site will be live at `https://<your-username>.github.io/<repo-name>/`

### 3. First Run
- Open the site, enter your API key when prompted, and start chatting

## Cost

Each conversation uses the Anthropic API, which is billed per token. A typical tutoring session (a few back-and-forth messages) costs well under $0.10 USD with Sonnet or Haiku. Opus is more capable but costs more — check [anthropic.com/pricing](https://www.anthropic.com/pricing) for current rates.

## Privacy

- Your API key is stored **only in your browser's localStorage** — it never leaves your device except in direct API calls to Anthropic
- Conversation history is stored in localStorage — it stays on your device
- No analytics, no third-party tracking, no server

## Local Development

No build step required. Just open `index.html` in a browser, or serve with any static file server:

```bash
npx serve .
# or
python3 -m http.server
```
