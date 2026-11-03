# AI Translation Consensus for Odoo 19

Several language models and translation services work on the same text, rate each
other's results anonymously and agree on the wording that is both **accurate** and
**natural**. Nothing is written without a rule, and by default a human approves every
proposal.

It does two jobs:

* **Translate** – fill in missing (or outdated) translations of any translatable field,
  with the surrounding page as context, not sentence by sentence in a vacuum.
* **Proofread** – check the text in its own language and suggest better wording or
  corrections, again block by block in the context of the whole page.

Keyless and keyed services can be mixed: a free public endpoint, a locally installed
model, a command line tool you are already signed in to, and paid APIs can all take part
in the same vote.

## How a text is processed

```
 source text ──► every translator writes a version (in parallel)
                    │
                    ├─ deterministic checks: HTML tags, entities, %s / {} placeholders,
                    │  URLs, chatter. A version that breaks them can never win.
                    ▼
              anonymised, shuffled candidates ──► every reviewer scores each one
                    │          accuracy 1-10, naturalness 1-10, favourite, suggestion
                    │          (an author does not vote for its own text)
                    ▼
        rank = 0.5 × average score + 5 × share of favourite votes
                    │
      share ≥ 0.6 and average ≥ 7.0 ?──► consensus
                    │ no
                    ▼
   second round with the top candidates + the reviewers' suggestions
                    │ still no
                    ▼
               a human decides
```

* **Without reviewers** (only translation engines such as DeepL) a result counts as agreed
  only when at least two of them produced the same text.
* **Proofreading** adds the original as a candidate. If the original wins, nothing is changed.
* **Proofreading** results must stay recognisably close to the original (similarity ≥ 0.6, same
  numbers). Small free models sometimes answer a batch with shifted or foreign texts; such an
  answer is rejected instead of being voted on. Keep *Texts per request* small with weak models.
* If every reviewer is unavailable (rate limit, outage) no pointless second round is run.
* Context: each request carries the neighbouring blocks and a slice of the page text
  (configurable) so that terms, tone and gender agree across the page.
* A **glossary** pins brand and product names and fixed terms.

## Providers

| Kind | Translates | Reviews / proofreads | Examples |
|---|---|---|---|
| OpenAI-compatible API | yes | yes | OpenAI, Mistral, DeepSeek, xAI, Groq, Together, OpenRouter, Cerebras, Qwen, Moonshot, GitHub Models, Hugging Face, Ollama, LM Studio, vLLM, llama.cpp |
| OpenAI-compatible, no key | yes | yes | Ollama on your server, Pollinations |
| Anthropic | yes | yes | Claude |
| Google Gemini | yes | yes | |
| Cohere | yes | yes | |
| Local command | yes | yes | `claude -p`, any CLI that reads stdin and prints the answer |
| DeepL, Google Translate, Azure Translator | yes | no | |
| LibreTranslate, MyMemory, Lingva | yes | no | |

Presets for all of these are installed **inactive**. Enter a key (or the name of an
environment variable that holds it), press *Test connection*, activate. Model names in the
presets are starting points; check them against the vendor's current list.

What has been verified:

* End to end against real services, with the full pipeline: Pollinations, MyMemory and the
  Claude Code CLI (keyless / signed-in).
* The HTTP adapters of every other kind against a local mock server (request shape,
  headers, error handling, retries, secret masking). They have **not** been run against
  the vendors' live APIs in this repository's test suite; if one of the presets needs a
  correction, please open an issue or a pull request.

## Installation

```bash
git clone https://github.com/Alex0176/odoo-ai-translation-consensus
# put the folder `ai_translation_consensus` on your addons path, then
odoo-bin -d <db> -i ai_translation_consensus --stop-after-init
```

Needs Odoo 19 and the Python package `requests`. German translation included.

## Quick start

1. *AI Translation ▸ Providers*: activate and test the services you want to use.
2. *AI Translation ▸ Glossary* (optional): fixed terms and names.
3. *AI Translation ▸ Rules*: choose the model, a domain, the fields, the source language
   and the target languages (or *Proofread*). If the text under the source language code is
   really in another language (e.g. German kept in the `en_US` slot), set *Text is really
   written in* so the providers are told the truth. Add style instructions, e.g. "formal address,
   IT company". Press *Scan now*, then *Process one batch*.
4. *AI Translation ▸ Proposals*: every proposal shows the candidates, who wrote them, the
   scores and remarks. Apply the proposal, apply a candidate, type your own wording, or reject.
5. When you trust a rule, switch on *Apply consensus automatically* and the cron
   (deactivated on installation) to run it unattended. Proposals without agreement still
   wait for a human.

## Safety and privacy

* All providers and the cron are **inactive** after installation. Nothing is sent anywhere
  until you switch it on.
* Texts **leave your server** when a hosted service is used. Mark a rule *Confidential* and
  only providers flagged as running on your own infrastructure see it.
* API keys live in an administrator-only field, or in an environment variable referenced by
  name. They are masked in error messages and sent in headers (MyMemory's optional key is the one exception: its API only takes it as a query parameter).
* The *local command* kind executes a program. It is disabled until the system parameter
  `ai_translation_consensus.allow_command` is set to `True`. The command is split without
  a shell and runs in an empty temporary directory.
* Prompts mark the text as data. Instructions inside a translated text are not followed,
  and a result is rejected if it breaks tags or placeholders.
* Applying a proposal re-checks that the source text did not change in the meantime,
  runs the markup checks again and writes without tracking and without notifying followers.
* Free tiers have limits (Pollinations: one reply every 45 seconds). The provider option
  `min_interval` spaces calls and "retry after" answers are honoured.

## Costs

A text costs one call per translator, plus one review call per reviewer, plus a second
round when the vote is split. Page context adds input tokens. Use *Records per scan*,
*New proposals per scan* and *Proposals processed per cron run* to keep runs bounded, and
keep cheap or local models in the mix as reviewers.

## Tests

```bash
odoo-bin -d <test-db> -i ai_translation_consensus --test-enable \
         --test-tags /ai_translation_consensus --stop-after-init
```

The suite covers the checks, the consensus maths, the pipeline with scripted providers,
every HTTP adapter against a local mock server, and the Odoo flow (scan, process, approve,
stale sources, term and plain fields, proofreading, confidential rules).

## Limits

* Quality is only as good as the models you enable; the consensus reduces single-model
  errors but does not replace a native speaker for legal or safety-critical text.
* Only fields with `translate=True` are handled. Rich text is processed per block
  (Odoo's term mechanism).
* Provider model names and prices change; the presets are not maintained as a price list.

## License

AGPL-3.0, © Biricon IT Services GmbH. Contributions welcome.
