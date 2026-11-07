---
name: findling-skill-creator
description: Create or rebuild a Findling domain skill (law, medicine, papers, company docs …) from the indexed folders; one skill per field.
---

# Create a Findling domain skill

Findling's tools already tell the assistant how to search, read and link (server
instructions). A domain skill only adds what is specific to one field: which folders
hold which kind of source, how their sections are named, how sources are cited, and
what a good answer looks like. Most of that can be learned from the index itself, so
look before you ask. `examples/findling-recht/SKILL.md` in the Findling repo is a
finished example.

## 1. Find the domain's folders

`browse()` lists the indexed folders. If Findling isn't running, stop and say so: the
skill can't be built without looking at the files. If folders from several fields are
indexed, ask which ones belong to this skill, and offer one skill per field. For each
folder, note what kind of source it holds (laws vs. decisions, guidelines vs. studies,
policies vs. tickets …) and a name pattern the skill can recognise it by.

## 2. Learn the files' conventions

For each kind of source, look at 3–5 files, chosen to differ (old and new, short and long):

- `browse(<folder>)` and `browse(<folder>, sort="modified")`: file naming; which facts
  live in the name (date, author, ID, court, version); how current the files are.
- `browse(<file>)`: the section list. Which headings are units people ask for by
  name (§ 573, Art. 3, "Dosierung", "4.2 Results", an error code)? Which heading
  marks the decisive part (Tenor, Leitsatz, Abstract, Conclusion)?
- `read(<file>, section=…)` with the forms users will type: with and without dots,
  abbreviations, numbering ("Art. 3" vs. "Art 3", "Section 4" vs. "4."). Note what
  works, what fails, and what `Similar:` suggests.
- `search(<typical question>, within=<folder>)` two or three times: are the hits the
  right kind of source, and what does `match: "meaning"` turn up?

Write down only what an assistant would get wrong without being told. Record
conventions, never the current stock: no file counts, date ranges or lists of what is
indexed. Users add and remove files at any time; the skill checks with `browse` when
it runs.

## 3. Ask what you can't see

Ask in one message, offering what you learned as defaults:

- What a question in this field looks like, and which terms it uses (for triggering).
- What counts as a source, and what must never be quoted from memory (IDs, numbers,
  doses, dates).
- How to cite: format for each kind of source, and whether to quote verbatim.
- Answer shape: short answer first? Structure? Language?
- What to do when the files don't cover it: say so, ask before searching the web, or
  answer from general knowledge with a label.
- A disclaimer, if the field needs one (legal, medical, financial), in one sentence.

## 4. Write the skill

One `SKILL.md`, no scripts, in the language the user asks in (or the files' language).
Keep it under ~60 lines; every line must change behaviour.

- `name`: `findling-<domain>`.
- `description`: one line, written for the model, not for people: it sits in every
  conversation's context. The field, "with Findling", a few typical topics in the
  users' language, and "also without mentioning Findling" so it triggers on plain
  domain questions. No explanations, no long term lists.
- Body: one short paragraph on why sources matter in this field, then numbered steps:
  check the folders with `browse()` (recognise them by name, don't hard-code paths, so
  the skill works on other machines; say clearly if one is missing), how to find and
  read each kind of source (with the section quirks from step 2), how to treat
  `match: "meaning"`, and the web rule. End with the answer format: structure, citation
  format with a linked example, quoting rules, labelling of unsourced claims, disclaimer.
- Don't repeat the general tool usage from the server instructions (search syntax,
  `next` cursors, what `url` is); only domain rules.

Save it to the place the user names, or `examples/findling-<domain>/SKILL.md` in the Findling repo, or
`~/.claude/skills/findling-<domain>/SKILL.md` for Claude Code.

## 5. Test it

Answer two or three real questions with the new skill, using the actual tools, and
check: every claim linked, section lookups work, unsourced points labelled. Fix the
skill where the answer went wrong. For evals and a tuned description, hand the
finished skill to the general skill-creator.
