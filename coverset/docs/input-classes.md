# Input classes

Line and branch coverage say which lines ran. They do not say which kinds of value reached them. A function can sit at 100% line and branch coverage while its tests never pass in an empty list, a negative amount or an email address on a company subdomain. coverset records the shape of every argument value its tests passed in ([docs/instrumentation.md](instrumentation.md#the-shape-tracer)) and turns those shapes into **input classes**: named blocks of a parameter's input space, written `<param>:<class>`.

**Input-class coverage** is observed ÷ expected classes over a function's parameters. That is each-choice coverage: every class should be hit at least once; combinations are not counted.

The catalogue lives in `src/coverset/analysis/shapes.py` (`Catalogue`). The same derivation serves the ingest (`facts.ingest.finalize_run` calls the catalogue once per function), the verdict, the CLI (`coverset shapes`) and the UI.

## The catalogue

Each class belongs to a family. A family's classes become expected for a parameter only under the trigger in the third column: **expectations come from data**, plus the annotation rules below. The fourth column is the shape feature that makes an observed value fall into the class.

| Family | Classes | Expected when | Observed when the shape key has |
|---|---|---|---|
| type | one per arm, named by type (`str`, `int`, `none`, `list`, ...) | the annotation has two or more non-`Literal` arms | its type token (`NoneType` counts as `none`, `obj:pkg.Cls` as `Cls`) |
| none | `none` | `None` was observed, or the annotation includes `None` (unless the type family already has `none`) | type `NoneType` |
| bool | `true`, `false` | a `bool` was observed, or the annotation includes `bool` | `true` or `false` |
| number | `negative`, `zero`, `positive` | an `int` or `float` was observed | `neg`, `zero`, `pos` |
| number | `nan`, `inf` | additionally, a `float` was observed or the annotation includes `float` | `nan`, `inf` (both infinities) |
| str | `empty`, `whitespace`, `ascii`, `unicode`, `ws-edge` | a `str` was observed | `empty`; `whitespace` (only whitespace); `ascii` or `unicode` (non-empty, not only whitespace); `ws-edge` (leading or trailing whitespace) |
| email | `plain`, `multi-label-domain`, `plus-tag`, `uppercase`, `idn` | some observed string had `kind:email/...` | `kind:email/<class>` |
| quantity | `single-unit`, `multi-unit`, `multi-letter-unit`, `decimal`, `signed`, `trailing-garbage` | some observed string had `kind:quantity/...` | `kind:quantity/<class>` |
| numeric-string | `numeric-string` | some observed string had `kind:numeric` | `kind:numeric` |
| iso-date | `iso-date` | some observed string had `kind:iso-date` | `kind:iso-date` |
| url | `url` | some observed string had `kind:url` | `kind:url` |
| bytes | `empty`, `non-empty` | a `bytes` value was observed | `len:0`, or any other length |
| container | `empty`, `single`, `multiple` | a `list`, `tuple`, `set`, `frozenset` or `dict` was observed | `len:0`, `len:1`, any longer length |
| elements | `zero-element`, `negative-element`, `all-zero` | an observed container had `int` or `float` elements, or the annotation's element type is numeric | `any_zero`, `any_neg`, `all_zero` on a numeric container |
| elements | `nan-element` | additionally, `float` elements were observed or annotated | `any_nan` |
| order | `unsorted`, `duplicates` | a `list` or `tuple` (observed or annotated) with comparable elements (`int`, `float`, `str`, `bytes`, `tuple`) | `order:unsorted` or `order:reverse`; `dups` |
| pairs | `inverted-pair` | an observed sequence had `pair:...`, or the annotation's element type is a 2-tuple of numbers | `pair:gt` (every sampled pair has first > second) |
| literal | one per `Literal` value, named by its `repr` | the annotation has `Literal[...]` | the redacted example equals the value's `repr` |
| enum | one per member, `Name.MEMBER` | the annotation names an `Enum` and the catalogue was given an `enum_members` resolver | the example repr mentions `Name.MEMBER` |

Class names are prefixed by their kind only when two kinds of one parameter would produce the same name. A parameter that received both strings and lists gets `str-empty` and `container-empty` instead of two `empty` classes (`test_collisions_are_prefixed_by_kind`).

Some recorded features map to no class: `big` and `integral` on numbers, `inf-neg`, `digits` and the length buckets of strings, `sum_zero` on numeric containers, `key:` on dicts. They stay in the facts for queries and the UI.

The enum family is implemented in `Catalogue` but inactive in the shipped paths: the ingest, the verdict and `coverset shapes` all build `Catalogue` without an `enum_members` resolver.

### String kinds

A string gets at most one kind, the first that matches, and only when it is at most 2,048 characters long (`pytest_coverset/features.py`, `string_kind`).

| Order | Kind | Rule |
|---|---|---|
| 1 | `email` | the whole string matches `[^@\s]+@[^@\s]+\.[^@\s]+` and no domain label is empty |
| 2 | `numeric` | the whole string is a number, optionally signed, with an optional exponent and surrounding whitespace (`"42"`, `" -1.5e3 "`) |
| 3 | `quantity` | the string starts like `<number><letters>`: `\s*[-+]?[\d.]+\s*[A-Za-z]+` |
| 4 | `iso-date` | the whole string is `YYYY-MM-DD`, optionally with a time and a zone |
| 5 | `url` | the whole string is `<scheme>://<non-space>` |

**Email sub-classes**, first match wins:

| Sub-class | Rule | Example |
|---|---|---|
| `idn` | a non-ASCII local part or domain, or a domain label starting `xn--` | `bob@bücher.de` |
| `multi-label-domain` | three or more domain labels, where one of 39 common two-level public suffixes (`co.uk`, `com.au`, `co.jp`, ...) counts as one label | `bob@eng.acme.com` (but `c@example.co.uk` is `plain`) |
| `plus-tag` | a `+` in the local part | `bob+news@acme.com` |
| `uppercase` | any upper-case letter | `Bob@Acme.COM` |
| `plain` | everything else | `bob@acme.com` |

**Quantity sub-classes**, first match wins. The string is read as a run of `<number><unit>` segments.

| Sub-class | Rule | Example |
|---|---|---|
| `trailing-garbage` | text left after the last segment, no segment at all, or a unit that is not a known unit but starts with one (`1hx`) | `5m!`, `5m later` |
| `signed` | a leading `+` or `-` | `-5m` |
| `multi-unit` | more than one segment | `1h30m` |
| `decimal` | a segment with a decimal point | `1.5h` |
| `multi-letter-unit` | the only unit is longer than one letter | `100ms` |
| `single-unit` | one segment with a one-letter unit | `90s` |

Known units are listed in `KNOWN_UNITS` in `features.py` (time, data size, length, mass and CSS units, matched case-insensitively).

### Element summaries

Containers are summarised over their first 32 elements, read by index (`list`, `tuple`) or by builtin iteration (`set`, `dict` keys and values). Numeric features (`any_zero`, `all_zero`, `any_neg`, `any_nan`, `any_inf`, `sum_zero`) are computed only when every sampled element is exactly an `int` or a `float`. Order (`sorted`, `reverse`, `unsorted`, or `n/a` when the elements do not compare with builtin semantics, for example when a NaN is present) and `dups` are computed for sequences of at least two sampled elements. Pair features apply when every sampled element is a 2-item tuple or list of numbers: `pair:lt`, `pair:eq`, `pair:gt`, or `pair:mixed` when the pairs disagree.

`inverted-pair` is observed only from `pair:gt`. A `pair:mixed` sequence may hold only `lt` and `eq` pairs, so it proves nothing about inversion (`test_mixed_pairs_do_not_count_as_inverted`).

## How expected classes are derived

For each parameter the catalogue takes the observed shapes and the annotation text (from `ast.unparse`, collected by `annotation_index` over the head commit's sources), then:

1. **Observed kinds open families.** An observed `int` makes negative, zero and positive expected. An email-shaped string makes the five email sub-classes expected. A family whose kind never appeared stays closed: a parameter that only ever saw plain words is not charged for missing emails.
2. **The annotation adds classes the data cannot show.**

| Annotation | Adds |
|---|---|
| `A \| B`, `Union[A, B]` | one type class per arm |
| `Optional[A]`, `A \| None` | the `None` arm, so `none` |
| `Literal["a", "b"]` | one class per value: `'a'`, `'b'` |
| `bool` | `true` and `false`, even if one was never passed |
| `float` | `nan` and `inf` once the number family is open |
| `list[float]`, `Sequence[int]`, `tuple[int, ...]` | the element families, from the element type |
| `list[tuple[int, int]]` | `inverted-pair` |
| an `Enum` class | one class per member, only with a resolver (see above) |
| `Annotated[A, ...]` | whatever `A` adds |
| a string annotation (`"list[int]"`) | parsed, then as above |

Parameters named in the signature are listed even when never observed, because their annotation alone can make classes expected. The pseudo-parameters `return` and `<receiver>` are recorded but never counted as inputs.

3. **Unannotated parameters are never 100%.** When everything expected was observed but some observed parameter has no annotation, the percentage is withheld (`pct = None`) and the note says, for example, `text unannotated: observed {str}`. Without an annotation coverset cannot know which classes the author meant to accept (`test_unannotated_params_are_never_100_percent`). The per-function numbers stored at ingest (`func_input_cov.blocks_expected` and `blocks_observed`) have no room for this rule; `Catalogue.unannotated` names the parameters it applies to.

`coverset shapes FUNC --suggest` prints the narrowest annotation consistent with what was observed, flagged when it disagrees with the declared one (`suggest_annotation`). `int` folds into `float` and `bool` into either, following the numeric tower.

## Never exercised, or not observed

A missing class is labelled in one of two ways (`FunctionInputCov.missing_reason`):

| Label | When |
|---|---|
| `never exercised` | no test of the function hit the sample cap: the tracer saw every call, so no test passed a value of that class |
| `not observed in N sampled calls (K tests hit the sample cap)` | sampling of the function was capped ([adaptive sampling](instrumentation.md#adaptive-sampling-and-the-full-trace-list)), so a later call might have carried the class |

Functions in the PR's changed files and every invariant target are traced without a cap, so their missing classes are always `never exercised` (`test_missing_reason_says_never_exercised_only_without_a_sample_cap`).

The PR comment and check summary list only `never exercised` classes of functions some test actually called (`orchestrator/verdict.py`, `_missing_classes`).

## Hypothesis hints

Every class carries a description and a Hypothesis strategy that produces values of it (`_CLASS_TEXT` in `analysis/shapes.py`). The CLI prints the strategy for each missing class; the PR comment puts one snippet per class in a `<details>` block.

| Class | Strategy |
|---|---|
| `none` | `st.none()` |
| `true`, `false` | `st.just(True)`, `st.just(False)` |
| `negative`, `zero`, `positive` (ints) | `st.integers(max_value=-1)`, `st.just(0)`, `st.integers(min_value=1)` |
| `negative`, `zero`, `positive` (only floats observed) | `st.floats(max_value=0, exclude_max=True)`, `st.sampled_from([0.0, -0.0])`, `st.floats(min_value=0, exclude_min=True)` |
| `nan`, `inf` | `st.just(float("nan"))`, `st.sampled_from([float("inf"), float("-inf")])` |
| `empty` (str) | `st.just("")` |
| `whitespace` | `st.sampled_from([" ", "\t", "\n "])` |
| `ascii` | `st.text(st.characters(max_codepoint=127), min_size=1)` |
| `unicode` | `st.text(st.characters(min_codepoint=128), min_size=1)` |
| `ws-edge` | `st.text(min_size=1).map(lambda s: f" {s} ")` |
| `plain` | `st.emails()` |
| `multi-label-domain` | `st.sampled_from(["bob@eng.acme.com", "ops@mail.eu.example.org"])` |
| `plus-tag`, `uppercase`, `idn` | `st.just("bob+news@acme.com")`, `st.just("Bob@Acme.COM")`, `st.just("bob@bücher.de")` |
| `single-unit`, `multi-unit` | `st.sampled_from(["90s", "15m"])`, `st.just("1h30m")` |
| `multi-letter-unit` | `st.sampled_from(["100ms", "2min", "3hr"])` |
| `decimal`, `signed`, `trailing-garbage` | `st.just("1.5h")`, `st.sampled_from(["-5m", "+5m"])`, `st.just("5m!")` |
| `numeric-string`, `iso-date`, `url` | `st.integers().map(str)`, `st.dates().map(str)`, `st.just("https://example.com/path?q=1")` |
| `empty` / `non-empty` (bytes) | `st.just(b"")`, `st.binary(min_size=1)` |
| `empty` (container) | `st.just([])` |
| `single`, `multiple` | `st.lists(<elem>, min_size=1, max_size=1)`, `st.lists(<elem>, min_size=2)` |
| `zero-element`, `negative-element`, `nan-element` | `st.lists(<elem>, min_size=1).map(lambda xs: xs + [0])`, `... + [-1]`, `... + [float("nan")]` |
| `all-zero` | `st.lists(st.just(0), min_size=1)` |
| `unsorted` | `st.lists(<elem>, min_size=2, unique=True).map(lambda xs: sorted(xs)[::-1])` |
| `duplicates` | `st.lists(<elem>, min_size=1).map(lambda xs: xs + xs[:1])` |
| `inverted-pair` | `st.lists(st.tuples(st.integers(), st.integers()).filter(lambda p: p[0] > p[1]), min_size=1)` |
| type classes | `st.integers()`, `st.text()`, ... or `st.from_type(<Type>)` |
| literal and enum classes | `st.just(<value>)` |

`<elem>` is chosen from the observed element type: `st.floats(0, 100)` for floats, `st.text()` for strings, `st.tuples(st.integers(), st.integers())` for tuples, else `st.integers(0, 100)`.

## Worked examples from the demo

These tables come from a local demo run on 2026-10-11:

```sh
COVERSET_DATA_DIR=<scratch dir> uv run coverset demo --executor local --no-serve
COVERSET_DATA_DIR=<scratch dir> uv run coverset shapes validate_email --markdown
```

`coverset shapes` pools the five most recent runs of the repository, whatever their branch. After a full demo those are `main-4`, `main-5` and `main-6` (baselines), PR #1 "Support partial refunds" and PR #2 "Allow signups from company subdomains". Each table below is the command's output, pasted unchanged. Example values are the redacted examples recorded by the plugin. Rows without an example are container classes: the default `--coverset-repr-examples scalars` keeps no container examples.

All four functions are at 100% line and branch coverage on `main`: in the `main-6` run, `file_cov` shows `emailcheck.py`, `allocate.py`, `parse.py` and `merge.py` at 100.0% (statements and branches). The demo app's `tests/README.md` lists what the tests leave out on purpose.

### `validate_email(email: str) -> str`

`examples/demo-app/src/ledger/accounts/emailcheck.py` returns the lower-cased domain of an address that matches the email vendor's pattern.

| Input class | Observed | Example or suggested Hypothesis strategy |
|---|---|---|
| `email:empty` | yes | `''` |
| `email:whitespace` | yes | `' '` |
| `email:ascii` | yes | `'u***@example.com'` |
| `email:unicode` | yes | `'Zoë Smith <z***@example.com>'` |
| `email:ws-edge` | yes | `' d***@acme.com'` |
| `email:plain` | yes | `'u***@example.com'` |
| `email:multi-label-domain` | **no** | `st.sampled_from(["bob@eng.acme.com", "ops@mail.eu.example.org"])` |
| `email:plus-tag` | yes | `'B***@Example.ORG'` |
| `email:uppercase` | yes | `'E***@Globex.IO'` |
| `email:idn` | **no** | `st.just("bob@bücher.de")` |

8 of 10 input classes observed (80%).

The redaction keeps the domain and hides the local part, `+tag` included, which is why the `plus-tag` example shows no `+`. No test ever passes an address on a company subdomain. On `main`, `validate_email("bob@eng.acme.com")` raises `ValueError` because the pattern allows one label before the top-level domain; PR #2 widens the pattern so such addresses are accepted, and `check_if_new_user_signup_allowed("bob@eng.acme.com", {"acme.com"})` then lets a customer's employee self-sign-up. Every test still passes, and `email:multi-label-domain` is still never exercised in the PR #2 run.

### `parse_duration(text: str) -> int`

`examples/demo-app/src/ledger/durations/parse.py` sums every `(\d+)([smhd])` token it finds.

| Input class | Observed | Example or suggested Hypothesis strategy |
|---|---|---|
| `text:empty` | yes | `''` |
| `text:whitespace` | **no** | `st.sampled_from([" ", "\t", "\n "])` |
| `text:ascii` | yes | `'1h30m'` |
| `text:unicode` | **no** | `st.text(st.characters(min_codepoint=128), min_size=1)` |
| `text:ws-edge` | **no** | `st.text(min_size=1).map(lambda s: f" {s} ")` |
| `text:single-unit` | yes | `'1m'` |
| `text:multi-unit` | yes | `'1h30m'` |
| `text:multi-letter-unit` | **no** | `st.sampled_from(["100ms", "2min", "3hr"])` |
| `text:decimal` | **no** | `st.just("1.5h")` |
| `text:signed` | **no** | `st.sampled_from(["-5m", "+5m"])` |
| `text:trailing-garbage` | **no** | `st.just("5m!")` |

4 of 11 input classes observed (36.4%).

Each missing quantity class hides a silent wrong answer. Run against the demo source: `parse_duration("100ms")` returns 6000 (the regex reads `100m` and drops the `s`), `parse_duration("1.5h")` returns 18000, `parse_duration("-5m")` returns 300 and `parse_duration("5m!")` returns 300. None raises.

### `allocate_cents(total_cents: int, weights: list[float]) -> list[int]`

`examples/demo-app/src/ledger/billing/allocate.py` splits cents by the largest-remainder method.

| Input class | Observed | Example or suggested Hypothesis strategy |
|---|---|---|
| `total_cents:negative` | yes | `-90` |
| `total_cents:zero` | yes | `0` |
| `total_cents:positive` | yes | `5` |
| `weights:empty` | **no** | `st.just([])` |
| `weights:single` | yes | (no example recorded) |
| `weights:multiple` | yes | (no example recorded) |
| `weights:zero-element` | **no** | `st.lists(st.floats(0, 100), min_size=1).map(lambda xs: xs + [0])` |
| `weights:negative-element` | **no** | `st.lists(st.floats(0, 100), min_size=1).map(lambda xs: xs + [-1])` |
| `weights:all-zero` | **no** | `st.lists(st.just(0), min_size=1)` |
| `weights:nan-element` | **no** | `st.lists(st.floats(0, 100), min_size=1).map(lambda xs: xs + [float("nan")])` |
| `weights:unsorted` | yes | (no example recorded) |
| `weights:duplicates` | yes | (no example recorded) |

7 of 12 input classes observed (58.3%).

`total_cents:negative` shows as observed only because the pool includes PR #1, whose new `refund()` passes `-100` and `-90`; `coverset facts query` over `call_shapes` shows no negative total in any `main` run. Pooled over the `main` runs alone (computed with `facts.queries.shapes_for_function(..., branch="main")`, which the CLI does not expose), the same table reads `total_cents:negative` **no** and "6 of 12 input classes observed (50%)". That gap is exactly PR #1's bug: `allocate_cents(-100, [1, 2])` returns `[-32, -66]`, which sums to -98, because `int()` truncates towards zero and the negative remainder slice adds cents instead of removing them. The other missing classes fail loudly: `allocate_cents(100, [0, 0])` raises `ZeroDivisionError` and a NaN weight raises `ValueError`.

### `merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]`

`examples/demo-app/src/ledger/intervals/merge.py` merges overlapping intervals and trusts its input to be sorted.

| Input class | Observed | Example or suggested Hypothesis strategy |
|---|---|---|
| `intervals:empty` | **no** | `st.just([])` |
| `intervals:single` | yes | (no example recorded) |
| `intervals:multiple` | yes | (no example recorded) |
| `intervals:unsorted` | **no** | `st.lists(st.tuples(st.integers(), st.integers()), min_size=2, unique=True).map(lambda xs: sorted(xs)[::-1])` |
| `intervals:duplicates` | **no** | `st.lists(st.tuples(st.integers(), st.integers()), min_size=1).map(lambda xs: xs + xs[:1])` |
| `intervals:inverted-pair` | **no** | `st.lists(st.tuples(st.integers(), st.integers()).filter(lambda p: p[0] > p[1]), min_size=1)` |

2 of 6 input classes observed (33.3%).

The `pairs` and `order` families are expected from the annotation `list[tuple[int, int]]`. Unsorted input loses data silently: `merge_intervals([(5, 8), (1, 6)])` returns `[(5, 8)]`, dropping the span from 1 to 5. An inverted interval passes through unchanged: `merge_intervals([(5, 1)])` returns `[(5, 1)]`. Touching intervals merge (`[(1, 3), (3, 5)]` becomes `[(1, 5)]`), so the function silently picks closed rather than half-open semantics; touching is not an input class today, because the pair features describe each interval, not neighbouring ones.

## Limits

- **A hollow square means "never observed", not "buggy".** Many missing classes are inputs the function is right to reject or never receives in production. The grid points at where to look.
- **Shapes are sampled.** Outside the full-trace list, a function stops being sampled after 64 calls with no new shape or 2,048 calls. Missing classes of a capped function say "not observed in N sampled calls", never "never exercised".
- **Classes are heuristics.** The email and quantity rules are patterns, not parsers. `multi-label-domain` knows 39 two-level public suffixes, not the full public suffix list. A string longer than 2,048 characters gets no kind.
- **Each-choice only.** The catalogue counts each class once per parameter; it does not track combinations across parameters (a negative total with a zero weight).
- **Containers are summarised over 32 elements.** A zero at index 40 is not seen.
- **Families open from data.** A parameter annotated `int` that was never called with any number has no number classes; a never-called function reports only what its annotations imply.
- **The CLI pools across branches.** `coverset shapes` reads the last `--runs` runs of every branch, so a PR run can mark a class observed for `main`, as `allocate_cents` shows above.
