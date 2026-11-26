# ARM64 changes — Project Maya on Windows on ARM

Everything this fork changes to run on **Windows 11 on ARM** (Snapdragon X-series + an NVIDIA GPU), against
upstream **v1.1.74**.  No model math was changed: every edit is a compiler, toolchain, device-enumeration or
architecture-detection fix, and where a file has both a vectorized and a plain-C path, this port takes the plain-C
one rather than inventing new arithmetic.  Status on the machine below: GLM-5.3-Flash, Qwen-3.8-Flash
(CYBER-FROST) with pictures, DeepSeek V4.1 Flash, Hy4-Flash and Inkling 2 Mini all set up and run.

Compared with the upstream tag itself: `git diff --stat v1.1.74` is 8 files (section 3).  Nothing else drifted.
[ARM64-session-log.md](ARM64-session-log.md) is the other half: how each of these was found, the error strings to
recognise, the wrong turns, and the checks that settled each question.

## 0. Carried onto upstream v1.1.81 (2026-11-25)

This branch (`windows-arm64-1181`, tagged `v1.1.81-arm64`) is the same eight changes on upstream **v1.1.81** -
165 commits and 868 files later.  The first release, `v1.1.74-arm64`, stays exactly as it was released; every
section below still describes the port, and this one says what the move showed.  For this port the
interesting part of the new release is what it did *not* do.

**Upstream's ARM64 is not Windows on ARM.**  `_M_ARM64` - the macro every Microsoft compiler on ARM defines -
appears in no file of v1.1.81, and the one aarch64 path in `cpu_relax.hpp` is GCC inline asm, which `cl.exe`
cannot parse.  The ARM64 support that landed is for Linux on aarch64 (its own comment names a GB10 and reads
the kernel's HWCAP list) and for Macs; `setup.py`'s new `ARM =` flag is used under `if ARM and not WIN:`.  So
this branch is still the only thing that builds on Windows on ARM, and a future re-port should assume the same.

**What moved, and what did not:**

| the port's fix | upstream v1.1.81 | this branch |
|---|---|---|
| vision's `GGML_OPENMP OFF` (the missing `libomp140.aarch64.dll`) | **only half** - v1.1.81 turns OpenMP off inside its *x86 portable* branch; the non-x86 branch it added beside it leaves it on | kept: the `if(WIN32)` inside that non-x86 branch, which is where an ARM build lands |
| the `nvidia-smi` NPU row (`compute_cap` `[N/A]`) | **half fixed** - `gpus()` no longer crashes (it keeps `arch` a string and catches the `[N/A]` memory), but it still hands the NPU back as a GPU | kept: the filter that leaves it out is what makes `gpus()` answer one device here instead of two |
| clang-cl for ggml's ARM CPU backend | not touched: `find_vcvars` still looks only for `vcvars64.bat` | kept, and still most of the port |
| `ds4_pool.hpp`'s spin hint | not touched: still `#if defined(_MSC_VER)` -> `_mm_pause()` | kept |
| `ik2_cpu.cpp`, `ik2_lane.cpp` x86 intrinsics | not touched: `<immintrin.h>` still behind a bare `_MSC_VER` | kept |
| `expert_layout.cpp`, the same trap | not touched: still `#if defined(_MSC_VER)` around `<immintrin.h>` | kept |
| `/O1` for the two COFF-funclet files | the file around them was rewritten (1,078 lines) | re-inserted by hand, not replayed |

**Names moved**, which is what turned a cherry-pick into a hand merge.  `git cherry-pick` (or `git apply -3`) of
the old commit applies five of the eight files *cleanly* and the build then dies, because the hunks it carries
refer to names v1.1.81 deleted:

| v1.1.74 | v1.1.81 |
|---|---|
| `include/strata/...` | `include/maya/...` - the whole tree |
| `STRATA_CPU_ARM64`, `STRATA_X86` | `MAYA_CPU_ARM64`, `MAYA_X86` |
| `strata_cpu_pause()` | `maya_cpu_pause()` |

After that the port is the same eight files as before, nothing added to them and nothing dropped.

**New here: step 1 checks the Python's own architecture.**  An x64 Python running under emulation makes pip
install an x64 CMake into `.venv`, CMake decides the CPU family from its own bitness, and the build is then
told this is an x86 machine - a hundred files of `immintrin.h` errors, none of which mention Python.
`setup.x64_on_arm64()` reads the PE header of the interpreter running the setup (emulation hides every softer
answer: `platform.machine()` reports `ARM64` even to an x64 process, so upstream's `ARM` flag is `True` in the
broken case too) and step 1 stops with the error line and the fix *before* it asks a question or downloads a
byte.  The last section of [ARM64-session-log.md](ARM64-session-log.md) is how that was found.

## 1. The PC this was built and measured on

| | |
|---|---|
| machine | HP OmniBook X (Windows on ARM demo hardware) |
| OS | Windows 11, ARM64 |
| GPU | NVIDIA RTX Spark N1X — Blackwell, compute capability **121** (`sm_121`), 96 GB reported, ~106-117 GB visible to CUDA (unified with system memory) |
| system RAM | **30.4 GB** |
| CUDA | 13.5 (ARM64), `nvcc` at `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.5` |
| compiler | Visual Studio 2022 Community (MSVC 14.44.35207) for CUDA's host compiler, **clang-cl 19.1.5** (`VC\Tools\Llvm\ARM64\bin\clang-cl.exe`) for C and C++ |
| build | CMake 4.4 + ninja (both from `.venv`), Python 3.14 ARM64 |
| models | `C:\Users\Demo\Maya-data\models`, about 185 GB free at the time of the port |

A different Windows-on-ARM PC should need only the same eight files; `sm_121` and CUDA 13.5 are this PC's specifics,
and the setup reads both from `nvidia-smi` rather than from a hard-coded value.

## 2. The five things that stopped it, and what each fix does

### 2.1 ggml's ARM64 CPU backend refuses MSVC, so C and C++ need clang-cl
**Symptom:** the configure dies at `enable_language(C)` — the compiler test program does not build — and ggml's own
message is "MSVC is not supported for ARM, use clang".
**Fix:** `setup.py` learned that a build has an architecture: `VC_TOOLS` (x64/arm64 → the Visual Studio component id
and the `vcvars64.bat` / `vcvarsarm64.bat` that belongs to it), `vcvars_arch()`, and `find_vcvars()` now looks for the
arm64 component first.  `find_clang_cl()` locates `VC/Tools/Llvm/<ARM64|x64>/bin/clang-cl.exe` through `vswhere`.
`maya.py` has `clang_cl_defs()`, which returns the two `-DCMAKE_{C,CXX}_COMPILER=` defines on Windows-on-ARM and `[]`
everywhere else, and `NO_CLANG_CL` for the message when clang-cl is not installed; both the engine configure and the
vision configure use it.  CUDA's host compiler stays MSVC's own `Hostarm64/arm64/cl.exe` — nvcc wants it, and it is
what compiles the `.cu` files' host half.

### 2.2 clang-cl's OpenMP runtime is never on PATH, so the vision encoder died before printing READY
**Symptom:** the server stopped at "loading the vision encoder …" and `maya-vision.exe` ended with exit
`-1073741515` (`0xC0000135`, a DLL was not found) without printing a line.  The missing DLL is
`libomp140.aarch64.dll` — clang-cl's OpenMP is LLVM's own runtime, and on this machine it exists only inside the
Visual Studio install (`VC\Redist\MSVC\<ver>\debug_nonredist\arm64\Microsoft.VC143.OpenMP.LLVM`), never on a plain
session's PATH.  x86 never hits this because MSVC's `/openmp` uses the `vcomp140.dll` that Windows itself ships.
**Fix:** `tools/vision/CMakeLists.txt` sets `GGML_OPENMP OFF` for a non-x86 Windows build.  The engine's ggml is
already built without OpenMP, so a CPU encode costs the same threads either way.

### 2.3 `nvidia-smi` on this PC lists the NPU as a second device, and the setup read its arch as a number
**Symptom — this is the one that made models impossible to add:** the model list came up empty and every download
stopped early while the JSON was being built.  `nvidia-smi --query` returns *two* rows here: the RTX Spark, and an
"NVIDIA NPU" with `compute_cap = [N/A]` and `pci = [N/A]`.  `setup.gpus()` did `int(g["arch"])` on every row, so the
whole probe raised `ValueError: invalid literal for int() with base 10: '[N/A]'`, and everything downstream concluded
there was no CUDA GPU on the PC.
**Fix:** `setup.gpus()` skips a device whose reported compute capability is not a number — the same way `amd_gpus()`
already skips devices with no arch — because a device without a compute capability cannot run CUDA.  The model
catalog went from empty to 36 entries.  `maya.py` also reads the arch defensively
(`int(g["arch"]) if str(g.get("arch")).isdigit() else 0`) at the points that compare against a minimum capability.

### 2.4 Four sources asked *which compiler* they were in, instead of *which architecture*
`#if defined(_MSC_VER)` is true for Microsoft's ARM compilers too, and that is where these went wrong.  Each now
tests the architecture (`__x86_64__` / `_M_X64` / `_M_ARM64` / `__aarch64__`), which is also how `hy4_lane.cpp` and
the ds4 CPU files already spelled it:

- `src/kernels/cpu/expert_layout.cpp` — includes `<intrin.h>` / `<immintrin.h>` only under `STRATA_X86`.
- `src/ds4/ds4_pool.hpp` — `cpu_relax()` used `_mm_pause()` under `_MSC_VER`.  It takes `__yield()` (MSVC's ARM
  intrinsic) or `yield` in asm now.  This one is reached by the `.cu` files too, whose host half the ARM `cl.exe`
  compiles, so both spellings are there.  Symptom before the fix: `identifier "_mm_pause" is undefined` while
  building `maya-ds4`.
- `src/ik2/ik2_cpu.cpp` and `src/ik2/ik2_lane.cpp` (Inkling 2's CPU lane) — `<immintrin.h>`, `<intrin.h>`'s
  `__cpuid`, the `target("avx512…")` / `target("avx2,…")` attributes and the AVX-512/AVX2 kernels are now behind an
  x86-64 test, and the spin hints come from `strata/platform/cpu_relax.hpp`.  Nothing new was written: both files
  already carry the plain-C reference rows (`rows_scalar`, `nvfp4_rows_scalar`), and `detect()` already answers
  "scalar" off x86, so on ARM the lane runs the reference at reference speed.  Both files' `available()` already
  said false off x86, so the CUDA path is the one carrying the load.
- The same trap in `ik2_lane.cpp`'s `detect()`: its `__cpuid` branch is now `_MSC_VER && (_M_X64 || _M_IX86)`, so
  clang-cl on ARM falls to the scalar answer instead of failing to compile.

### 2.5 clang-cl's -O2 cannot assemble two engine files on ARM64 Windows
**Symptom:** `assembler label '' can not be undefined` from clang-cl while compiling `src/core/layer.cpp` and
`src/core/verify.cpp` at Release's `/O2`.
**Fix:** the root `CMakeLists.txt` gives those two files `/O1` when (and only when) building with clang-cl on
Windows on ARM64.  The reason is in the comment above the block: once block placement moves a catch handler's
cleanup out of its funclet, the address the handler hands the unwinder is formed as `adrp/add` onto a block label
without the `:lo12:` page offset, which COFF cannot express.  `/O1` keeps that address reachable.  Every other C++
source, and every CUDA source, still assembles at `-O2`.

## 3. Every file that differs from upstream v1.1.74

| file | change |
|---|---|
| `setup.py` | arm64 toolchain detection (`VC_TOOLS`, `vcvars_arch`, `find_vcvars`, `find_clang_cl`); `gpus()` skips devices with no compute capability |
| `maya.py` | `clang_cl_defs()` + `NO_CLANG_CL`, used by the engine and vision configures; arch-isolation guards wherever a GPU's `arch` is compared to a minimum |
| `CMakeLists.txt` | `/O1` for `src/core/layer.cpp` and `src/core/verify.cpp` under clang-cl on ARM64 Windows |
| `tools/vision/CMakeLists.txt` | `GGML_OPENMP OFF` for a non-x86 Windows build |
| `src/kernels/cpu/expert_layout.cpp` | x86 intrinsic headers behind `STRATA_X86` |
| `src/ds4/ds4_pool.hpp` | `cpu_relax()` by architecture, `__yield` / `yield` on ARM |
| `src/ik2/ik2_cpu.cpp` | x86 header and `_mm_pause` guarded; scalar rows off x86 |
| `src/ik2/ik2_lane.cpp` | x86 headers, `__cpuid`, target attributes and vector kernels guarded; reference rows off x86 |

To check it yourself, from the `windows-arm64` branch (which is based on the tag, so upstream's own new files are
tracked there and read correctly):

```
git fetch upstream refs/tags/v1.1.74:refs/tags/v1.1.74
git diff --stat v1.1.74..windows-arm64     ->  the eight files above, plus .gitignore and the two arm64 documents
```

(Taken from a tree where the update was copied in by hand instead, the same command also lists upstream's new files -
git cannot see untracked ones, so it counts them as deletions.  Against the pristine tag the real difference is those
eight files: a file-by-file hash of all 1807 upstream files gives 1798 identical, 8 different, 0 missing.)

## 4. What builds here

| target | serves | result |
|---|---|---|
| `maya` | GLM-5.3-Flash, Qwen-3.8-Flash, EXL3 quants | builds, 90.3 MB, launches |
| `maya-ds4` | DeepSeek V4.1 Flash (Maya/GGUF quants) | builds, 36.5 MB, launches |
| `maya-hy4` | Hy4-Flash | builds as-is, 15.3 MB, launches |
| `maya-ik2` | Inkling 2 Mini | builds, 8.9 MB, launches |
| `maya-vision` | the picture encoder (GLM and Inkling 2 towers) | builds, 55.7 MB; `--measure` reports `MEM` and encodes |
| `maya-mm3` | MiniMax M3.1 | **not ported** (section 5) |

Checked by building from the generated `build-maya.bat` / `build-maya-vision.bat`, running `dumpbin /dependents` on
each (no `libomp*.dll` in any of them), and starting each with a bad argument to prove the image loads and reaches
its argument parser — a missing DLL would have died with `-1073741515` before printing anything.
The configure line this PC produces is worth knowing: `-DCMAKE_CUDA_ARCHITECTURES=121`,
`GGML_SYSTEM_ARCH: ARM`, `-march=armv8.6-a+dotprod+i8mm+fp16`, clang-cl for C/C++, CUDA's host compiler from
`Hostarm64\arm64`.

## 5. What is deliberately not ported

- **`maya-mm3` (MiniMax M3.1).** `src/mm3/mm3_host.cpp`'s `dot`, `rms_rows` and `sum_into` are AVX2 with no plain-C
  fallback, and `mm3_cpu.cpp` spins with `_mm_pause()`.  Porting them means writing new numeric kernels, whose
  rounding then has to be checked against the GPU path — not something to do untested.  It is also academic on this
  PC: the MiniMax quants start at 194 GB.
- **DeepSeek's EXL3 quants** are refused on ARM by upstream itself (`maya.py`): those expert kernels are
  AVX2/AVX-512.  The message points at the Maya GGUF quants, which do work here.
- **Anything bigger than the disk.** 185 GB free; DeepSeek Maya-XS is 277 GB of files and the tiers above it more.
  System RAM is 30.4 GB, and the DeepSeek tiers are written around a 128 GB PC.

## 6. Building this on another Windows-on-ARM PC

1. Install the ARM64 CUDA toolkit (13.5 here), and Visual Studio 2022 with: Desktop C++ workload, **C++ ARM64 build
   tools** (`Microsoft.VisualStudio.Component.VC.Tools.ARM64`) and **C++ Clang Compiler for Windows**
   (`Microsoft.VisualStudio.Component.VC.Llvm.Clang`).  Missing the clang component is the one that makes the setup
   stop with `NO_CLANG_CL`'s message; missing the ARM64 tools makes it find `vcvars64.bat` and build the wrong thing.
2. Clone this branch, then `python maya.py --setup --model <id>` from the repo root.  It finds `vcvarsarm64.bat` and
   clang-cl by itself and writes `build-maya.bat` / `build-maya-vision.bat` for this machine.
3. First compile is 10-30 minutes; after an update it is a few, and the bat files resume where a stopped build ended.
4. A Maya executable that vanishes instantly with `-1073741515` is a missing DLL, not a crash: `dumpbin
   /dependents` on it, and `libomp140.aarch64.dll` means OpenMP came back in (2.2).
5. "no NVIDIA GPU found", or an empty model list, is the `nvidia-smi` probe (2.3) — check what
   `python -c "import setup as S; print(S.gpus())"` prints: one row, with `arch` a number.
6. To redo the ARM64 commit on top of a newer upstream tag: branch from the tag, drop these eight files back in,
   rebuild, and read `git diff --stat <tag>`.  Keeping it as one commit on a tag is what makes that a five-minute job.

## 7. Not in the repository on purpose

`build/`, `build-vision/`, `.venv/`, `third_party/llama.cpp/` (the setup fetches and patches it), the models,
`maya-*.json`, `build-maya*.bat`, `*.log` and `__pycache__/` are gitignored.  The release ships source, and every PC
compiles its own engine - which is the right shape on ARM, where CUDA and Visual Studio have to be installed on the
target anyway, and the generated `.bat` files and configs carry that machine's absolute paths.

Four more things a running PC leaves in the tree that are its own, not the project's: the drafter calibration it
measures for itself (`data/hy4-drafting.txt`, `data/ik2-drafting.txt`, and the `data/hy4-draft-ids-*.txt`,
`data/hy4-measure-ids-*.txt`, `data/ik2-draft-ids-*.txt` they key off the tokenizer) and any `*.bak`.  Upstream
ignores its other per-PC files in `data/` one by one; these were added to `.gitignore` the same way:

```
/data/*-drafting.txt
/data/*-draft-ids-*.txt
/data/*-measure-ids-*.txt
*.bak
```

## 8. Rebuilding this port on a new machine, or recovering it

- The branch and its tag *are* the backup: `git log --oneline -3` and the branch showing on the remote is the check.
- Worth copying by hand: the `maya-*.json` configs (your settings per model — their paths and GPU numbers are
  machine-specific, and the setup rewrites them), `data/expert-profile-*.bin`, and any `--vram-reserve-mib` or
  environment flags you settled on.
- The expensive ones are the models: about 200 GB here.  They are plain files in the models folder, and the setup
  matches them by size and sha256 — `START-MAYA.bat --import-model <folder>` takes files downloaded by anything,
  under any names.  Copy them only if the drive can hold them; otherwise re-download.
- Not worth copying: the build directories and `.venv` (both regenerated, and their caches hold this machine's
  absolute CUDA and Visual Studio paths), and logs unless one is the failure you are chasing.
- This file is the part to keep.  Even with nothing else, the port is eight files and each one's reason is above.
