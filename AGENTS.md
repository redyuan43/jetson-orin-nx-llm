# Pelican evaluation protocol

The user requested on 2026-10-21 that future pelican evaluations follow the reference protocol in `bbinwang/pelican-bicycle`.

- Use exactly `Generate an SVG of a pelican riding a bicycle.` for the comparison. Do not add HTML, animation, styling instructions, examples, previous outputs, or a custom system prompt.
- Disable reasoning and preserve the model's original SVG without repairs. Match the reference configuration for that model, including sampling, quantization and speculative decoding where supported. Record source revisions, actual parameters, seed, completion status and screenshots.
- Document every hardware/runtime adaptation or unmatched setting. Do not describe an adapted run as an exact reproduction. Keep earlier Chinese HTML-animation and thinking-enabled results labeled as separate custom experiments.
- Explicit later user requests may authorize additional experiments; label their differences instead of mixing them into the reference comparison.

# Inspecting live OpenWebUI conversations

- Inspect an existing user conversation read-only. Do not send Escape, Enter, stop, or regenerate actions while checking its UI: Escape can cancel the active OpenWebUI response even though the nx5 inference continues.
- Dismiss a modal only through its explicitly identified close control. Do not use a global keyboard shortcut to dismiss an assumed popup.
- Check nx5 admission state before starting a synthetic inference test. An active user request takes priority.
