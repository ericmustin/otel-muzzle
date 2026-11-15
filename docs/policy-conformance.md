# Telemetry Policy conformance

Policy mode implements `SpanSamplingPolicy` from the OpenTelemetry Telemetry Policy specification. The
specification is a draft: this document describes it as of [opentelemetry-proto pull request 865][pr] at commit
`04be8b6cf9649852ce5028dacbc9fb6cc00bd8a2`, package `opentelemetry.proto.policies.v1development`. Policy mode is
experimental and will change with the specification.

The specification lets a consumer support a subset, provided it documents every policy type and feature it does not
support. This is that document for the Ruby, Python and Node.js packages, which behave alike.
[design.md](design.md#policy-mode) has the algorithms.

## What is implemented

| Part of the specification | Support |
|---|---|
| `SpanSamplingPolicy` | Yes |
| `LogFilterPolicy`, `LogTransformPolicy`, `MetricFilterPolicy` | No. The packages only sample spans. Such a policy is reported as not applied. |
| `Policy`: `id`, `enabled` | Yes. A policy with `enabled: false` is absent. |
| `Policy`: `name`, `description`, `labels` | Read and ignored, as the specification says of labels. |
| `PolicySet` | Yes. A new set replaces the whole previous one, and the order of the list has no meaning. |
| `SamplingConfig`: `percentage`, `mode`, `sampling_precision` | Yes, both modes. |
| Predicates: `exists`, `equals`, `regex`, `gt`, `gte`, `lt`, `lte`, `contains`, and `negate` | Yes. `regex` accepts a subset of RE2, see [below](#regex). |
| Most restrictive policy wins; an unmatched span passes through | Yes |
| Fail-open; an invalid policy stays inert | Yes |
| Protobuf JSON | Yes, with lowerCamelCase or snake_case keys. Enum values are names. |
| Binary Protobuf, YAML | No |
| Match outcome reporting (hits and misses per policy) | No |

Selectors:

| `SpanFieldSelector` | Support |
|---|---|
| `SPAN_RECORD_FIELD_NAME` | Yes: the name the span starts with. |
| `SPAN_RECORD_FIELD_TRACE_ID` | Yes: 32 lowercase hexadecimal characters. |
| `SPAN_RECORD_FIELD_PARENT_SPAN_ID` | Yes: 16 lowercase hexadecimal characters, empty for a root span. |
| `SPAN_RECORD_FIELD_TRACE_STATE` | Yes: the members in order as `key=value`, joined by commas, with no spaces. |
| `SPAN_RECORD_FIELD_KIND` | Yes: `INTERNAL`, `SERVER`, `CLIENT`, `PRODUCER` or `CONSUMER`. |
| `span_attribute` | Yes: the attributes passed when the span starts. A path of several segments is absent, because start attributes are flat. |
| `SPAN_RECORD_FIELD_SPAN_ID` | No: a sampler decides before the span id exists. |
| `resource_attribute`, `scope_attribute`, `scope_field` | No: a sampler is not given the resource or the instrumentation scope. |

A policy that uses an unsupported selector stays inert as a whole. Ignoring one matcher would make the policy match
more spans than its author wrote.

Policies come from one JSON file, named by `OTEL_MUZZLE_POLICY_FILE` or in code. It is read once at start, and again
when the application calls the package's reload function. There is no polling, and no HTTP or OpAMP provider.

## Where the packages differ from the specification

**A dropped span does not orphan its children.** The specification says a dropped span "leaves its children behind,
with a parent span id that names a span no longer in the stream", and asks a consumer to document the effect. Here
the children of a span dropped under a sampled local parent attach to the nearest kept ancestor, so the trace stays
one tree. A span dropped with no parent or with a remote parent is left to the SDK: under a `ParentBased` sampler,
the default, the spans below it in the same process are not recorded.

**The threshold is not written to the tracestate.** The specification says a consumer MUST encode its threshold in
the `th` sub-key of the `ot` tracestate member. A tracestate is inherited: the children of a kept span, and every
service they call, would carry that threshold. Since dropped spans are transparent here, those descendants are
exported in every trace, and a threshold of 10% on them would make a backend count each one ten times. The packages
read `th` and `rv` and write neither, with one exception: a span dropped by policy carries no `th`.

**The configured sampler decides first.** The specification does not say how a policy relates to the SDK's
sampler or to the parent's sampled flag. Here the sampler the application configured decides first, a span it
drops stays dropped, and policies apply only to spans it records.

**Precision.** The specification does not define how `sampling_precision` rounds. The packages follow the
OpenTelemetry [tracestate probability sampling][ps] rule: round half up to that many hexadecimal digits, counted
after the leading `f` digits, 14 digits at most. At the default of 4, 10% is `e666` and 1% is `fd70a`.

**Stricter parsing.** A repeated `id` makes every policy with that id invalid, because keeping the first or the
last would make the outcome depend on the order of the set. Inside a span sampling policy an unknown key is an
error. A JSON number given for an `int_value` must be a whole number no larger than 2<sup>53</sup> − 1; larger
values are written as strings.

### Regex

The specification names RE2 syntax. Each package checks a pattern against the list below and translates it for the
language's own engine, so the three accept the same patterns and match the same text. A pattern outside the list
makes its policy invalid.

- Literals, `.`, `|`, groups `( )` and `(?: )`.
- Character classes with ranges and negation.
- `*`, `+`, `?`, `{n}`, `{n,}`, `{n,m}` with counts up to 1000, each optionally lazy.
- `^`, `$`, `\b`, `\B`.
- `\d`, `\D`, `\w`, `\W`, `\s`, `\S`, in their ASCII meaning; `\s` is `[\t\n\f\r ]`.
- `\t`, `\n`, `\r`, `\f`, `\v`, `\xHH` up to `\x7f`, and an escaped ASCII punctuation character.
- One flag group at the start: `(?i)`, `(?s)`, `(?m)` or a combination.

Not accepted: backreferences and lookaround, which RE2 does not have, and named groups, flags inside the pattern,
`\A`, `\z`, `\p{…}`, `\Q…\E`, octal and `\x{…}` escapes, POSIX classes and nested classes.

Known differences from RE2: with `(?i)`, letters outside ASCII fold in Ruby and Node.js and not in Python; with
`(?m)`, Node.js also treats `\r`, U+2028 and U+2029 as line ends; in Node.js a character outside the Basic
Multilingual Plane counts as two; `\B` does not match empty text in Python before 3.14. The engines backtrack, so a pattern can be slow on long input where RE2 would not
be. The policy file is written by the operator of the service; treat it as code.

A pending revision of the specification makes glob matching mandatory and regular expressions optional. A `glob`
predicate is not accepted yet.

## Gaps in the specification for this use case

**No selector for a remote parent.** otel-muzzle's built-in rule drops an INTERNAL span only under a local parent.
A matcher can ask whether a span has a parent, not where the parent is. The example policy therefore also matches
an INTERNAL span whose parent arrived over the wire, which the built-in rule keeps. That span is an entry span, so
its drop is left to the SDK and takes the rest of the service's spans for that request with it. The common
instrumentations start SERVER or CONSUMER spans at an entry point.

**Independence from head sampling.** A policy and a consistent probability head sampler read the same randomness.
With `SAMPLING_MODE_EQUALIZING`, the default, a policy at 10% behind a head sampler at 10% or less that writes
`th` keeps every span of every sampled trace. `SAMPLING_MODE_PROPORTIONAL` keeps 10% of what head sampling keeps,
which is what the built-in rule does; the example policy uses it. The Ruby and Python `TraceIdRatioBased` samplers
write no `th` and keep the traces whose ids are *low*, so behind one of them the share of verbose traces falls
short of the percentage as the head sampling rate gets small: for a 10% policy, about 9.4% at a 5% rate, 7.8% at
1%, and none under about 0.35%. The built-in rule reads other bits of the trace id and does not have this limit.

[pr]: https://github.com/open-telemetry/opentelemetry-proto/pull/865
[ps]: https://opentelemetry.io/docs/specs/otel/trace/tracestate-probability-sampling/
