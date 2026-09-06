# Cosmic prompt archive (v1 — pre-migration)

Frozen copies of the Cosmic agents' prompt stack **as it stood before the engine migration
began**, kept for reference and for diffing. See [../COSMIC_ENGINE_MIGRATION.md](../COSMIC_ENGINE_MIGRATION.md).

Everything here is captured from commit **`102261ae`** ("Update: Watchlist feature and
gemini-3.5-flash-lite update"), the last commit before Phase 0. That matters: Phase 0 had already
edited the live module by the time this archive was made, so these files come from git history
rather than the working tree — they are the true originals, not the post-Phase-0 state.

## Contents

| File | What it is | Size |
|---|---|---|
| `cosmic_prompts_v1_original.py` | The complete original module, verbatim — all 9 constants including the ones since removed (`COSMIC_ASTRO_FRAMEWORK`, `COSMIC_ASTRO_QUERY`) and KB §23 | 127,062 ch / 1,842 lines |
| `cosmic_macro_system_prompt_v1_assembled.txt` | The macro system prompt **as actually sent to the model** — `COSMIC_SYNTHESIS_PROMPT` with the empty framework, the full 85KB rulebook, and `region_focus` filled in | 103,025 ch / ~25,756 tok |
| `cosmic_micro_system_prompt_v1_assembled.txt` | The same for the micro agent | 95,119 ch / ~23,779 tok |
| `cosmic_macro_user_message_v1_template.txt` | The user-message template, showing the three feeds interpolated raw and uncapped (the unbounded-context defect, §1.3a) | 517 ch |

The assembled files are the more useful reference of the two kinds: they are the actual AI context,
not the source that produces it. The `.py` file is the more useful thing to diff against.

## Rules

- **Do not edit.** These are historical records; their value is being unchanged.
- **Do not import** `cosmic_prompts_v1_original.py`. It is archive-only and is not on any import
  path. The live module is `agents/prompts/cosmic_prompts.py`.
- Nothing in `agents/` or `tools/` reads this directory. It is documentation.

## Regenerating

The assembled `.txt` files were produced by formatting the archived module the same way
`agents/cosmic_agent.py` and `agents/cosmic_micro_agent.py` did pre-migration:

```python
pdf = "\n## ADDITIONAL ASTROLOGICAL REFERENCE (from PDF source):\n%s\n" % COSMIC_PDF_AUGMENTATION_TEXT
macro = COSMIC_SYNTHESIS_PROMPT.format(
    astro_framework=COSMIC_ASTRO_FRAMEWORK,      # was always ""
    pdf_augmentation=pdf,
    region_focus="All Regions (Global + India)") # the default
```

If you need the pristine module again for any reason:

```bash
git show 102261ae:agents/prompts/cosmic_prompts.py
```
