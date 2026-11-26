# ARM64 session log — how the Windows-on-ARM port was found

The story behind [ARM64-changes.md](ARM64-changes.md).  That file says *what* the eight changes are; this one says how
each was found, which wrong turns cost time, and which test settled each question — so a fresh session on a new PC can
pick this up cold instead of rediscovering it.  Three days on a Windows-on-ARM demo laptop, starting from a fresh clone
of the official repo at v1.1.70, ending at v1.1.74 plus one ARM64 commit.

Where the work lives: branch `windows-arm64` (one commit on top of the real tag `v1.1.74`), tagged `v1.1.74-arm64`.
The unsorted state it was assembled from is parked on `wip-demo-laptop`.

## Making it compile at all

The first failure is the whole reason this port exists: ggml's ARM64 CPU backend **refuses MSVC** —
"MSVC is not supported for ARM, use clang" — which surfaces generically as the configure dying at `enable_language(C)`
because the compiler test program does not build.  The way out is that Visual Studio ships clang for ARM64
(`VC\Tools\Llvm\ARM64\bin\clang-cl.exe`), so the build ends up with **two compilers**: clang-cl for C and C++, and
Microsoft's own `Hostarm64\arm64\cl.exe` as nvcc's host compiler for the `.cu` files.  The environment comes from
`vcvarsarm64.bat`, not `vcvars64.bat` — the setup found the x64 one first and built the wrong thing until it was told
to look for the ARM64 component.

Then: which sources assume x86?  The method was to grep for `<immintrin.h>`, `__cpuid` and `_mm_` and then ask a second
question about every hit — *does CMake already exclude this file off x86?*  Most did: `expert.cpp`, `q2_avx2.cpp`,
`iq_avx512.cpp` and friends sit under `if(STRATA_CPU_X86)` with `portable.cpp` as the else branch, and the ds4/hy4
sources already guarded themselves with `__x86_64__ || _M_X64`.  Only the few that were genuinely unguarded needed
work.  That question — is this file even compiled here? — is the one that keeps this port small.

The strangest error of the lot: clang-cl's `-O2` failing to *assemble* two engine files with
`assembler label '' can not be undefined`.  That is a COFF exception-handling problem (a catch handler's cleanup moved
out of its funclet leaves an `adrp/add` onto a block label without its `:lo12:` page offset), not a C++ error, so it
looks unrelated to the code.  `/O1` on those two files is the fix, and the reason is written in the comment above the
block so nobody has to derive it twice.

That was enough: the engine built, loaded the model, warmed 86 GB of experts in 34 seconds and printed READY.

## The vision encoder that would not say anything

Symptom: the server sat at "loading the vision encoder …" and `maya-vision.exe` ended with exit `-1073741515`
(`0xC0000135`) and **no output whatsoever**.  Running it by hand in a console teaches you nothing, because it dies
before it can print — the loader fails first.  The tool that actually answers is `dumpbin /dependents`, which showed
`libomp140.aarch64.dll`: clang-cl's OpenMP is LLVM's own runtime, and on this machine it exists only inside the Visual
Studio install, under `debug_nonredist`, never on a normal session's PATH.  x86 never hits this because MSVC's
`/openmp` uses the `vcomp140.dll` that Windows ships itself.

Fix: `GGML_OPENMP OFF` for a non-x86 Windows build in `tools/vision/CMakeLists.txt`.  Proven by running the encoder by
hand until it printed `READY` and `MEM`, then encoded a real picture (`OK 256 256 1 70`) and wrote its 4,194,324-byte
embedding file.

Rule that came out of it: **on Windows, an exit of `-1073741515` with no output is a missing DLL, not a crash.**  Go
straight to `/dependents`.

## A false alarm worth recording

Mid-test the engine reported `the expert pool (88369 MB) did not allocate`, which read like a port bug and cost some
looking.  It was the *other* engine — the released app on the machine was holding the unified memory.  Only one engine
can hold that pool at a time.  Check for a second engine before blaming the code, and never stop the user's app to
make room.

## "Nothing else will download"

The report was vague: it "keeps stopping with errors when building the model JSON's and whatnot".  The clue came from
asking the setup's own probe out loud:

```
python -c "import setup as S; print(S.gpus())"
ValueError: invalid literal for int() with base 10: '[N/A]'
```

`nvidia-smi` on this PC returns **two** rows — the RTX Spark, and an "NVIDIA NPU" whose `compute_cap` is `[N/A]`.  The
probe did `int(g["arch"])` on every row, so the entire probe blew up and every consumer concluded there was no CUDA
GPU on the machine.  One skip in `setup.gpus()` (the same thing `amd_gpus()` already did) took the model catalog from
empty to 36 entries, and every family's setup got as far as its download prompt.  The "model JSON" wording was a
red herring: nothing was wrong with the JSON, the PC simply appeared to have no GPU.

Same pass, a bug in my own earlier patch: `compile_vision` called `stop(...)`, but `stop` is a nested helper of
`compile_engine` — off the happy path that would have raised `NameError` instead of reporting a missing clang-cl.
`compile_vision` reports by returning `False`, so it is `fail(...)` now.  Worth writing down as a lesson: when copying
an error path into a second function, check the helpers actually exist there.

## The v1.1.74 update, which arrived as a folder

No git ref, just an unzipped release inside the repo — so the first job was "what really differs?", and that produced
three dead ends:

1. A naive comparison claimed **1259 changed files**.  It was line endings: `core.autocrlf=true` writes the worktree
   with CRLF, the drop has LF.  Comparing with CR stripped gave the honest answer: **77 changed, 60 new, 0 deleted**.
2. Backups made with `git show HEAD:path > file` through PowerShell were **corrupt** — a 128,453-byte `CMakeLists.txt`
   came out at 260,718 bytes and a 15,800-byte patch at 32,098.  PowerShell re-encodes what flows through its
   redirection.  Byte-exact output needs `cmd /c "git show ... > file"`.
3. Counting carriage returns per byte with `Where-Object` on a half-megabyte file ran for minutes; that job got
   killed.  A string `-replace` does the same in an instant.

Five files carried both the ARM64 edits and upstream changes, so they were merged by hand — every anchor was still
there upstream, and the merge was checked by compiling both Python files and grepping for each ARM64 marker.  The
proper verification came later, on day 3 (see the audit below).

## Two more walls of the same shape

Both new-in-1.1.74 families stopped with the same class of bug as day 1: source asking *which compiler* instead of
*which architecture*.

- **Inkling 2** (`maya-ik2`): `immintrin.h(14,2): error: "This header is only meant to be used on x86 and x64
  architecture"` in `ik2_cpu.cpp` and `ik2_lane.cpp`, with a spray of `hresetintrin.h` / `crc32intrin.h` noise behind
  it.  Both files already ship the plain-C reference rows and already answer "not available" off x86, so the fix was
  fencing headers, `__cpuid`, the `target("avx512…")` attributes and the kernel blocks — no new arithmetic.  Small
  trap: `ik2_cpu.cpp` spins in **two** places; fixing one still left `use of undeclared identifier '_mm_pause'` at
  line 42.
- **DeepSeek** (`maya-ds4`): `identifier "_mm_pause" is undefined` at `src/ds4/ds4_pool.hpp(27)`, where
  `cpu_relax()` chose `_mm_pause` under `#if defined(_MSC_VER)` — true for Microsoft's ARM compilers too.  Because the
  `.cu` files' host halves go through ARM `cl.exe`, the ARM branch needed `__yield()`, not GCC-style inline asm.
- **MiniMax** (`maya-mm3`) came up in the same grep and was **left alone on purpose**: `mm3_host.cpp`'s `dot`,
  `rms_rows` and `sum_into` are AVX2 with no plain-C fallback, so a port means writing numerics whose rounding then has
  to be checked against the GPU.  Its models start at 194 GB, which is academic on a 185 GB disk.

## Proving a build instead of hoping

`exit 0` from a build is not "it runs".  Three checks, cheap and worth the minute:

- `dumpbin /dependents` on every engine — none imports `libomp*.dll`.
- Start each with a nonsense argument.  `unknown argument --help` is **success**: the image loaded and reached its
  argument parser.  A missing DLL would have returned `-1073741515` before printing anything.
- The vision encoder with `--measure`: `MEM 2332033024 6052515841 116920352768`.

After that the user ran GLM-5.3-Flash, Qwen-3.8-Flash (CYBER-FROST, pictures working), DeepSeek V4.1 Flash, Hy4-Flash
and Inkling 2 Mini on the machine.

## Making it a release

The official repo's tags turned out to be reachable from this laptop, which made the update verifiable instead of
merely believed: fetch `refs/tags/v1.1.74`, hash all 1807 upstream files against the tree (bytes first, and only
re-compare the mismatches with CR removed) → **1798 identical, 8 different, 0 missing**, and the 8 are the port.
That is the sentence the release rests on.

One more dead end while proving it: `git diff --stat v1.1.74` taken from the tree where the update had been *copied*
in claimed 49 files and 26,903 deletions.  Git cannot see untracked files, so upstream's new files counted as deleted.
From a branch actually based on the tag the same command reads correctly: the eight files, plus `.gitignore` and these
two documents.

`.gitignore` grew rules for what a running PC leaves in the tree and shouldn't ship — the drafter calibration it
measures for itself (`data/hy4-drafting.txt`, `data/ik2-drafting.txt`, the `*-ids-*.txt` keyed off the tokenizer),
`*.bak`, and the unzipped release folder, which has been moved out of the repo.

## Environment notes that cost time

- PowerShell's `>` **re-encodes**; for byte-exact file content go through `cmd /c "prog > file"`.
- A confined shell may refuse a child program whose output is captured through a pipe (`EPERM`).  Run the `.bat` with
  its output redirected to files instead; for a long build, a hidden `Start-Process` with `-RedirectStandardOutput`
  plus `echo EXIT=%ERRORLEVEL%` in the script, and then read the log.
- The shell here reports `AMD64` on an ARM machine (emulation).  Never infer the host architecture from it — ask the
  setup's own probe.
- Long compiles belong in a `.bat` with a log file, not in a foreground command.
- Only one engine can hold the big memory pool; a load failure is often the other app, not the code.

## First things on the new PC

1. CUDA toolkit (ARM64), and Visual Studio 2022 with **C++ ARM64 build tools** and **C++ Clang Compiler for Windows**.
   These are the two components the setup's own messages name when they are missing.
2. Clone the fork, `git switch windows-arm64`, then `python maya.py --setup --model <id>` from the repo root.
3. Two failure signatures to recognise instantly: an empty model list or "no NVIDIA GPU found" is the `nvidia-smi`
   NPU probe (`python -c "import setup as S; print(S.gpus())"` should print one row with a numeric `arch`); an
   executable that vanishes with `-1073741515` is a missing DLL (`dumpbin /dependents`).
4. To move the port onto a newer upstream release: branch from the new tag and port by hand.  A `cherry-pick`
   applies five of the eight files *cleanly* and the build then dies, because v1.1.81 renamed `include/strata`
   to `include/maya` and `STRATA_*` to `MAYA_*` - section 0 of ARM64-changes.md is that audit, and the port of
   1.1.74 to 1.1.81 is what it was written from.  Rebuild, then re-run the audit above.
5. Hardware ceilings measured here, for calibration: 30.4 GB of system RAM, 185 GB of free disk, one GPU with no
   second device worth counting.  Families that need more than that are out of reach for reasons that have nothing to
   do with ARM.

## A second account, and the one failure that was not in the code

The port was proven on one laptop.  Then a second Windows account on the *same* machine - a fresh profile, a
fresh clone of the fork, the same Visual Studio, the same CUDA - failed the build with about four hundred lines
of

    immintrin.h(14,2): error: "This header is only meant to be used on x86 and x64 architecture"

in `expert.cpp`, `q2_avx512.cpp`, `iq_avx512.cpp`, `kq_avx512.cpp` and `iq_avx2.cpp`, plus
`use of undeclared identifier 'kGemmK'` in `exl3_cpu.cpp`.  Every one of those files is something an ARM build
never compiles.  Nothing in the repo differed between the two accounts.

**Wrong turns.**  The errors name intrinsics and CUDA-adjacent files, so it reads as a missing Visual Studio
component or a CUDA install problem.  Neither: the identical machine built the whole thing in the first
account.  An ARM64 Windows runs an x64 program fine, so nothing looks wrong on the surface either - and the
setup's own PC check passes, because it checks GPUs, RAM and Visual Studio, not the bitness of its own
interpreter.

**What it was.**  CMake decides the CPU family from the architecture of *cmake.exe itself*, and Maya's setup
installs CMake with pip into `.venv` - so the tools inherit the bitness of the Python that installed them.
The second account had an **x64 Python 3.12**, which Windows on ARM runs under emulation:

    x64 Python  ->  x64 cmake.exe  ->  CMAKE_SYSTEM_PROCESSOR: AMD64  ->  GGML_SYSTEM_ARCH: x86
                ->  ggml adds its AVX-512 and AVX2 kernel files  ->  ARM clang-cl cannot compile them

Setting `PROCESSOR_ARCHITECTURE=ARM64` in the environment does **not** help: tested with CMake 4.4.5, it
reports ARM64 from the variable and still picks x86, because its own binary is x64.

**Read these two lines first in any build log of this repo** - they answer the question before the errors
start:

    -- CMAKE_SYSTEM_PROCESSOR: ARM64
    -- GGML_SYSTEM_ARCH: ARM

`x86` in the second line on an ARM PC means the tooling is the wrong bitness, and no change to the source will
fix it.

**The fix**, which is what the second account did: install Python's ARM64 build (3.12 or newer; python.org's
downloads list it as "Windows installer (ARM64)"), delete `.venv` **and** `build`, make the environment again
with the ARM64 interpreter, and check before configuring:

    py -3.14-arm64 -m venv .venv
    .venv\Scripts\python.exe -c "import platform; print(platform.machine())"      -> ARM64

`build` has to go with it: the `CMakeFiles` cache holds the x86 decision, and a reconfigure keeps it.

**What is in the code now because of it.**  Step 1 asks the question that account had to guess at, before it
asks anything else or downloads a byte.  `setup.x64_on_arm64()` reads the PE header of the interpreter running
the setup - emulation hides every softer answer, and `platform.machine()` answers `ARM64` even to an x64
process, which is why upstream's own `ARM` flag is `True` in the broken case too - and step 1 stops with the
error line, the reason and the command that undoes it.  `IsWow64Process2` is the call that tells the truth: it
names both the process's machine and the host's.


## The Update button, and why it could not point at us

About > Updates had nagged for months about releases this PC cannot run, and the install's own log had the reason in
a line that meant nothing until it was read next to the code:

    [maya] the update did not happen: git fetch failed: ... and the repository exists.

`serve/update.py` asks GitHub for the latest release of *the repository git calls `origin`*. This folder has no
`origin` - the remotes are named `fork` and `upstream` - so the code fell back to its hard-coded
`mw00/project-maya`: every check described a build that cannot compile here, and the click then died at the fetch
before any of that could matter. `git remote add origin https://github.com/Obscuras211/project-maya.git` is the
whole of that half of the fix, and it is worth checking on any folder that misbehaves: `git remote get-url origin`.

Two quieter blockers were waiting behind it, both about a tag carrying a suffix:

* `TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")` - to that pattern `v1.1.81-arm64` is not a version at all, and the
  page answered "the latest release's tag is not a version". The regex now takes an optional suffix and `vtuple`
  reads only the three numbers, which keeps the ordering honest: a build of 1.1.81 is never offered as "newer" to a
  folder already running 1.1.81, so between ports the page stays quiet instead of nagging.
* The update itself is `git merge --ff-only refs/tags/<tag>`, which moves only a folder standing *behind* the tag on
  the same line of history. A port branch cut fresh from upstream `v1.1.81` is not behind the previous ARM release,
  it is a different line, so the button would have found a dead end even with a perfectly readable tag.

So the release is a merge commit, built after the build was verified, out of the tree that was compiled and run:

    $tree = git rev-parse windows-arm64-1181^{tree}    # the tested tree, engines and all
    $p1   = git rev-parse windows-arm64                # where a v1.1.74-arm64 install stands
    $p2   = git rev-parse windows-arm64-1181           # the port, on top of upstream v1.1.81
    git commit-tree $tree -p $p1 -p $p2 -m "Upstream v1.1.81, ported to Windows on ARM64"

and the proof is short because the button is short: clone the folder, stand at the old release, run the page's two
commands - `git fetch --tags origin`, `git merge --ff-only refs/tags/v1.1.81-arm64` - in the throwaway copy. It
fast-forwards, the tree equals the built one, `VERSION` reads 1.1.81 and `git status` is clean. A Windows trap while
checking any of this: `git merge-base --is-ancestor` is silent and answers through its exit code, so
`if (git merge-base --is-ancestor A B)` in PowerShell is *always* false - read `$LASTEXITCODE`.

The ported build did run, too: Inkling 2 Mini, 4,388 tokens in 66 s (76 tok/s) at 32K context, against 45 tok/s at
262K on the v1.1.74 build. Those two numbers are the context sizes talking, not the port; a fair comparison sets
both the same.

One small thing that looked like a build failure and was not: deleting `.venv` from a folder that already has
`build/` and `build-vision/` leaves those caches pointing at a cmake and ninja that no longer exist, and the next
configure dies in two seconds with

    CMake Error: CMake was unable to find a build program corresponding to "Ninja".  CMAKE_MAKE_PROGRAM is not set.

Maya sees it, clears the cache and builds the whole thing, so the run goes on and finishes - but "delete `.venv` and
try again" always means deleting the build folders with it.
