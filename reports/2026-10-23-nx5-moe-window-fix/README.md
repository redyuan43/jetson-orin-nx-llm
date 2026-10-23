# NX5 MoE decode-window metadata repair

Native source fix: `95749f5ef655bde8be34e99aeb33d33b5a81f047`, based on clean native source `b75b05f667af094df8db3268c5af7ba71c7ed79f`. The patch adds per-layer metadata capacity for the resolved decode window while preserving the smaller independent prefill bank. Its CPU regression covers a 64-slot prefill bank, a 176-slot decode view, candidate 68, full-window selection/eviction and transition cleanup.

`native.patch` is the exact binary diff from the fixed base to the native commit. SHA-256: `2fabf59678c601a2131d61bd580238dcba16ca8697cf2541593fa26bbbb6bf41`.

The live NX5 native checkout is at `a64932ec615e1463158129feaf2f0752d0c07a2e` and has five unrelated dirty files. In particular, `src/llama-moe-slot.cpp` contains existing Linux `posix_fadvise` handling that is absent from clean b75. That source tree was left untouched. The build used a private overlay made from the live runtime source plus only the committed metadata-resize hunk and header. See `build-manifest.json` for the overlay/source hashes, compiler command, all 161 linked objects, 299 header dependencies, library SHA/build ID, and original runtime binary identity. Existing objects remain hash-locked; only the MoE slot object was newly compiled for this repair, alongside the previously fixed graph object.

The resulting library and copied CUDA library are in the new NX5 build directory named in the manifest. The application profile points to that directory and records both library hashes. The native runtime binary and all unrelated libraries are unchanged. `--version` succeeded with the new library directory first in `LD_LIBRARY_PATH`; this is a loader smoke check, not model acceptance.

The native source CPU regression passed before commit. Real text and Comfy/text-cycle acceptance are recorded separately after deployment; this report does not claim those results.
