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
                    │   (an earlier applied translation of the same text joins as one more candidate)
                    ├─ deterministic checks: HTML tags, entities, %s / {} placeholders,
                    │  URLs, chatter, the script of the target language, glossary names kept
                    │  unchanged. A version that breaks them can never win.
                    ▼
              anonymised, shuffled candidates ──► every reviewer scores each one
                    │          accuracy 1-10, naturalness 1-10, favourite, suggestion,
                    │          and the kinds of error it found (mistranslation, omission ...)
                    │          (an author does not vote for its own text)
                    ▼
        rank = 0.5 × average score + 5 × share of favourite votes
                    │
      share ≥ 0.6 and average ≥ 7.0, and not half of the reviewers
      reporting a serious error ?──► consensus
                    │ no
                    ▼
   second round with the top candidates + the reviewers' suggestions
                    │ still no
                    ▼
   optional arbiter: one strong reviewer decides between the two best, only if it is
   sure (8+ for both scores, no serious error) ──► consensus
                    │ no
                    ▼
               a human decides

 Every consensus then passes the last gates (switchable per rule): a missing fixed glossary
 translation or a changed number holds it back, and, if switched on, the text is translated
 back by a provider that did not write it and compared with the source.
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

## Cross-checks

All of these are options on the rule (*Consensus* tab); none of them needs another provider
except the arbiter.

* **Serious errors** – reviewers name the kinds of error they find per candidate
  (`mistranslation`, `omission`, `addition`, `untranslated`, `meaning-changed`, `terminology`,
  `grammar`, `style`, ...). When half of the reviewers report one of the serious kinds for the
  winning text, an agreement is not applied without a human, however well it scored. The kinds are
  listed on the candidate.
* **Script and language** – a text for Russian, Arabic, Greek, Chinese ... must be written in that
  script, a text for a Latin-script language must be mostly Latin. Returning the source text
  unchanged is caught here. Languages written in several scripts (Serbian, Kazakh, ...) and very
  short texts are not judged.
* **Numbers** – compared as values, so `1.000`, `1,000` and `١٬٠٠٠` are equal. A changed or invented
  number, or a longer number that vanished, holds an agreement back; a single digit written out
  (`0 Tage` → `zero-day`) does not. A proofread must keep every number.
* **Glossary** – a name marked *keep unchanged* must appear unchanged in a Latin-script target (a
  warning in other scripts, where it may be transliterated); a missing fixed translation holds an
  agreement back.
* **Earlier translations** – the latest applied translation of the very same source text into the
  same language is offered as one more candidate, so identical texts keep identical wording across
  the website. It never counts as a translator's own agreement.
* **Arbiter** – optionally a strong provider (set per rule) that is asked only when the vote stays
  split. It sees the two best candidates and settles the text only if it rates the choice 8 or better
  and finds no serious error; it never judges a text it wrote itself.
* **Back-translation** – optionally the agreed text is translated back into the source language by a
  provider that did not write it. If the result has little in common with the source
  (*Back-translation similarity needed*, default 0.3), the text goes to a human. The similarity is a
  mix of word overlap and character similarity; the default is a first guess, so check a few
  results on the candidate (it shows the back-translation and the value) before relying on it.
  Costs one more request per group.
* **Automatic application with brakes** – *Automatic only up to (characters)* keeps long texts for a
  human, *Audit sample (%)* holds back a fixed share of the agreed texts so that the rate of
  corrections tells you how far to trust the rule.
* *Hold back agreed texts with serious errors* switches the gates above off for a rule that should
  behave like the earlier versions.

## Providers

| Kind | Translates | Reviews / proofreads | Examples |
|---|---|---|---|
| OpenAI-compatible API | yes | yes | OpenAI, Mistral, DeepSeek, xAI, Groq, Together, OpenRouter, Cerebras, Qwen, Moonshot, GitHub Models, Hugging Face, Ollama, LM Studio, vLLM, llama.cpp |
| OpenAI-compatible, no key | yes | yes | Ollama on your server, Pollinations |
| Anthropic | yes | yes | Claude |
| Google Gemini | yes | yes | |
| Cohere | yes | yes | |
| Local command | yes | yes (with `{images}`, see below) | `claude -p`, `codex exec`, Antigravity `agy -p` (preset, prompt passed as argument), any CLI that reads stdin and prints the answer |
| DeepL, Google Translate, Azure Translator | yes | no | |
| LibreTranslate, MyMemory, Lingva | yes | no | |

Presets for all of these are installed **inactive**. Enter a key (or the name of an
environment variable that holds it), press *Test connection*, activate. Model names in the
presets are starting points; check them against the vendor's current list.

### Quota, limits and outages

A provider that cannot answer right now (daily quota used up, usage limit of a signed-in CLI,
HTTP 402/429/5xx, no connection, a CLI killed by a signal or out of memory) does **not** make the
texts fail. The unit stays queued, the provider is **paused** (*Paused until* on the provider) and the
other providers carry on. The pause starts at 5 minutes and doubles with every outage in a row (at most
6 hours; a wait the service asks for, such as MyMemory's "next available in ...", is honoured up to 24
hours). A successful answer or *Test connection* ends it, as does *Use again now*. Other errors (bad
answers, a rejected key) pause the provider only after three in a row. Only texts for which a
provider answered but no usable proposal came out are marked *failed*; the dashboard counts them
separately from the queue and shows the pauses.

Texts that failed because of an outage in an earlier version (no candidates, error text of an outage)
can be queued again with the dashboard button *Retry N failed by outage* or the list action
*Retry failures caused by a provider outage*. Failures with candidates are content failures and stay.

What has been verified:

* End to end against real services, with the full pipeline: Pollinations, MyMemory and the
  Claude Code CLI (keyless / signed-in).
* The HTTP adapters of every other kind against a local mock server (request shape,
  headers, error handling, retries, secret masking). The test suite does not call the vendors'
  live APIs; if one of the presets needs a correction, please open an issue or a pull request.

**Plan usage of signed-in tools.** For Codex (`codex app-server`) and Antigravity (`agy -p /usage`) a cron
reads every 10 minutes how much of the subscription window is used up; the dashboard shows it per
provider. When a window reaches 100 % the provider is paused **until that window resets**, instead of
sending requests that are certain to be refused; the pause ends by itself when the figures show room
again (reset, or a free reset credit was redeemed). Every Codex window blocks. The windows of
Antigravity belong to model families, so name the one your command uses in the extra options of the
provider, e.g. `{"usage_window": "Gemini"}` (part of the window name); without it Antigravity is never
paused by its usage. *Use again now* lifts the pause until the next reading.

## Installation

```bash
git clone https://github.com/Alex0176/odoo-ai-translation-consensus
# put the folders `ai_translation_consensus` (and optionally `ai_translation_consensus_website`)
# on your addons path, then
odoo-bin -d <db> -i ai_translation_consensus --stop-after-init
```

Needs Odoo 19 and the Python package `requests`. German translation included.

An optional bridge module, `ai_translation_consensus_website`, is installed automatically when
`website` is present. It adds the scope **A whole website with everything on it** to the wizard
(see below).

## Quick start

The menu **AI Translation ▸ Start here** opens a wizard that explains the steps and creates the
first rule. By hand:

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

## A whole website

In the wizard choose the scope **A whole website with everything on it**, pick the website and
press *Suggest the content of the website*. The bridge module looks at every model that has
translatable fields and a `website_id` (or that belongs to the website through pages, menus,
blogs, products, events, forums, courses ...) and proposes one rule per kind of content, with
the languages of the website as targets. Whatever is installed is found; whatever is not
installed (forums, courses, galleries) simply does not appear and is picked up the day you
install it. Remove the kinds you do not want, then create. Not offered on purpose: partners,
companies, payment providers, pricelists, delivery carriers, loyalty programs and snippet
filters. URL slugs (`seo_name`) are not translated.

## Dashboard, cost and automatic start

*AI Translation ▸ Dashboard* shows for every rule how many texts are finished, waiting for a
decision, queued or failed, per language and per provider, and refreshes itself every few seconds.

* **Cost so far / until finished** and **time until finished** are estimates from what the providers
  actually reported: tokens (OpenAI, Anthropic, Gemini, Cohere) or characters (DeepL, Google,
  Azure ...). Tokens are always shown, money only when you entered the price per million
  tokens (or characters) on the provider. Services that do not report tokens are estimated from
  the text length and marked as such. The time uses the measured seconds per text, and the
  schedule of the cron when it is switched on.
* *Usage and cost* lists every batch (provider, language, tokens, seconds, cost).
* Rules with **Start by itself when a record changes** search only records changed since their
  last full scan when the cron runs and process the new texts; with the cron switched on an
  edited web page is translated again without anybody pressing a button.
* The cron processes several groups of texts (one record, one language each) **at the same time**:
  *Groups processed at the same time* on the rule (default 6) sets how many, and when several rules
  are queued they take turns. *Proposals processed per cron run* limits the texts per rule and cron
  interval; 0 means no limit, which is what you want when your API accounts have generous quotas.
  The calls to the providers run in background threads and keep going between cron calls; every
  cron call lasts only about 8 seconds, hands out new groups, stores the finished ones and returns,
  so it never holds up the other cron jobs or trips the cron worker's time limit (which in Odoo also
  covers the pause between two passes). Groups still waiting when the Odoo worker is restarted are
  lost and simply queued again. Raise the number of parallel groups only as far as the rate limits of
  your accounts allow.
* **Bulk requests** (*Bulk request (characters)* on the rule, default 0 = off): instead of small
  groups per record, the texts of all records are filled into one request per language until it
  holds about that many characters (20000 to 26000 is a good size, at most 400 texts). Hundreds of
  texts in one request are much faster than many small ones, especially with command line providers
  (Claude Code, Codex), whose start-up time dominates a small request. The providers see a short
  label and the neighbouring texts instead of the whole page. A text that comes back without a usable
  proposal is asked again later (twice at most, bulk rules only) while the rest of the request is
  kept; a bulk request may wait for its providers up to an hour and each provider call up to 15 minutes.
* When the last open text of a rule is finished, a message **"All languages translated"**
  (proofreading: "All texts proofread") is posted once in the channel *AI Translation* by the bot.
  Members of the reviewer group are subscribed automatically; the message is posted again after
  new texts have been found and finished.

## Safety and privacy

* All providers and the cron are **inactive** after installation. Nothing is sent anywhere
  until you switch it on.
* Texts **leave your server** when a hosted service is used. Mark a rule *Confidential* and
  only providers flagged as running on your own infrastructure see it.
* API keys live in an administrator-only field, or in an environment variable referenced by
  name. They are masked in error messages and sent in headers (MyMemory's optional key is the one exception: its API only takes it as a query parameter).
* **Invoices with a command line tool.** A *local command* provider can read documents when its
  command contains the placeholder `{images}`. For every document a file is written to a temporary
  directory (PNG/JPEG as they are, a PDF as PNG pages with `pdftoppm -r 150`, at most 5 pages per PDF,
  `poppler-utils` required) and `{images}` is replaced by the file paths, one argument each
  (`--image={images}` is repeated per path). The directory is deleted after the call. The prompt
  still goes through stdin. Tested template for Codex CLI (`codex exec --help`, 0.166.0):
  `codex exec --sandbox read-only --skip-git-repo-check --ephemeral - -i {images}`.
  The `-` (prompt from stdin) comes before `-i` because `-i` accepts any number of values.
  Without `{images}` the provider cannot get an invoice purpose. As such a tool is not on
  your own infrastructure, the switch *Allow invoices to leave the server* is needed, and the
  system parameter `allow_command` stays a precondition. A text-only request drops `{images}`
  (and a preceding `-i`/`--image`) from the command.
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
keep cheap or local models in the mix as reviewers. The Dashboard shows what has been spent and
forecasts what the queue will still cost; prices are entered on the providers (the presets do not
carry a price list because vendors change them).

## Tests

```bash
odoo-bin -d <test-db> -i ai_translation_consensus,ai_translation_consensus_website --test-enable \
         --test-tags /ai_translation_consensus,/ai_translation_consensus_website --stop-after-init
```

The suite covers the checks, the consensus maths, the pipeline with scripted providers,
every HTTP adapter against a local mock server, the Odoo flow (scan, process, approve,
stale sources, term and plain fields, proofreading, confidential rules), token and cost accounting,
the cross-checks, the cron with parallel groups, the dashboard figures and forecast, the completion message and the whole-website wizard.

## Limits

* Quality is only as good as the models you enable; the consensus reduces single-model
  errors but does not replace a native speaker for legal or safety-critical text.
* Only fields with `translate=True` are handled. Rich text is processed per block
  (Odoo's term mechanism).
* Provider model names and prices change; the presets are not maintained as a price list.

## License

AGPL-3.0, © Biricon IT Services GmbH. Contributions welcome.


## Purposes, document reading, privacy switch

Since 19.0.1.3.0 every provider has **purposes** (tags on the provider form, filterable in the list):

| Purpose | Meaning |
|---|---|
| Translations | Used by translation and proofreading runs. **All providers that existed before keep exactly this purpose** (set by the migration); translation behaviour is unchanged. Runs only use providers with this purpose. |
| Invoices - manual (button) | May read documents when a user presses a button (used by `account_bill_capture`). |
| Invoices - automatic | May read documents in a background job (used by `account_bill_capture` in automatic mode; it only fills drafts). |
| Other | Placeholder for other modules. |

**Reading documents.** Providers of the kinds Anthropic, OpenAI-compatible and Google Gemini can read images and PDF
files (`ChatProvider.read_document(system, user, files)` with `files=[{"mimetype", "data_b64", "name"}]`).
Anthropic gets `document`/`image` blocks, Gemini `inlineData`, OpenAI-compatible services `image_url` data URIs; PDF
goes as a file to OpenAI and OpenRouter, other services get the first 5 pages as PNG (needs `pdftoppm` from
poppler-utils, no new Python dependency). All other kinds have `can_read_documents = False` and cannot get an
invoice purpose. Usage, cost, pause and error series use the same bookkeeping as translations.

**Privacy switch.** Invoices contain data of third parties. A provider that is not marked *Runs on own
infrastructure* is only used for invoices when **Allow invoices to leave the server**
(`allow_documents_external`, default off) is set on it. The check happens again right before every call.

Upgrade note: after updating, existing providers show the purpose *Translations* only; add an invoice purpose to
a provider that should read invoices. The tests of the new part are in `tests/test_documents.py` (mocked HTTP).
