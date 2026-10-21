# Pelican evaluation protocol

The user requested on 2026-10-21 that future pelican evaluations follow the reference protocol in `bbinwang/pelican-bicycle`.

- Use exactly `Generate an SVG of a pelican riding a bicycle.` for the comparison. Do not add HTML, animation, styling instructions, examples, previous outputs, or a custom system prompt.
- Disable reasoning and preserve the model's original SVG without repairs. Match the reference configuration for that model, including sampling, quantization and speculative decoding where supported. Record source revisions, actual parameters, seed, completion status and screenshots.
- Document every hardware/runtime adaptation or unmatched setting. Do not describe an adapted run as an exact reproduction. Keep earlier Chinese HTML-animation and thinking-enabled results labeled as separate custom experiments.
- Explicit later user requests may authorize additional experiments; label their differences instead of mixing them into the reference comparison.
