# GavinNicoBR

Gavin Vaughan's entry for the RWX Blast Radius track of the Builders Buildathon 2026.
For each code change to sqlglot (commit `eb11aec`), the program prints the tests that fail on it and a split across workers.

## Method in one paragraph

The program applies each patch in memory, runs the tests that can be affected, and selects the tests that fail. The tests run directly with `unittest`. Maps of the pinned commit say which tests can be affected. When a map proves that a patch cannot change any test, the program selects nothing and runs nothing.

## How one batch runs

1. **Plan** (`app/selector.py`, `make_plan`). The program parses the patch (`app/engine.py`, `parse_patch`). Each changed file gets candidate tests from the maps. Edits in function bodies use line coverage. Edits of import-time code use the def-time map. Fixture edits use the readers of the data file. Comment-only edits compile to the same code and get no candidates.
2. **Prove what the maps can prove without a run.**
   - Operand outcome maps (`app/infect.py`). The program uses them when the edit changes one comparison operator, one and/or operator, or one literal operand. The maps say in which tests the new code gives another result on a recorded evaluation. No such test means that nothing can change (`plan_python_file_infect`).
   - Cache-hidden tests (`infect.hidden_tests`). The outcome maps come from whole-suite runs in one process. A cache such as `_DISPATCH_CACHE` shows an evaluation only in the first test that needs it. `app/maps/infect_hidden.bin` lists the later tests that depend on the kept result. Those tests are added back as candidates.
   - Table entries (`app/keymap.py`, the class attribute map). An import-time edit can change only some entries of class-level tables or class constants. Then only the tests that read those entries or attributes stay.
   - Long tests (`plan_python_file`, `app/tokcheck.py`). A few long tests have exact line sets (`tight_lines.bin`). Such a test stays a candidate only when it runs a line with a changed instruction (`engine.changed_lines`). After a tokenizer edit, `tokcheck.changed` tokenizes the recorded inputs of those tests again. A test whose token stream differs is selected when it is too costly to run.
3. **Run** (`app/runner.py`, `app/reduce.py`, `app/pool.py`). One parent process imports sqlglot and the test modules once. It forks one child per patch.
   - Edit in a function body. The child swaps the code objects of the live functions (`engine.plan_swaps`).
   - Edit of import-time code. The child imports the patched tree again and compares the import state with the pinned one (`app/statefp.py`).
   - Importer cone (`app/cone.py`, `selector.cone_plan`). An import-time edit can touch only modules that `app/maps/cone.json` lists. Then the child imports again only the patched module and its importers (`cone.reimport`). The map lists a module only when this gave the same import state as a full import on the pinned tree. The child checks the patched code again and falls back to the full import when a check fails.
   - Fixture-only commits. Some commits change only entries of `.sql` fixture files (`fixture_only` in `run`). One shared child serves all of them in turn (`fixture_batch_child`). After each commit it restores the state and checks it (`shared_state_change`). A failed check ends the shared child, and the rest get their own children.
   - A test runs whole, or reduced to the statements, fixture entries and pool tasks that reach the changed code.
4. **Select.** The program selects the failing tests. It packs the answer over up to 8 workers.

## Cost controls

- **CPU guard** (`selector.py`, `GUARD`). Each test gets a CPU limit. The limit is never below what selecting the test unrun can cost. A test that passes its limit is stopped. Its run status is `COST`, and the test is selected without a verdict.
- **One CPU** (`pin_to_one_cpu`). The process and its children stay on one CPU, since they work one after the other. `release_pin_if_slow` allows all CPUs again when wall time runs far ahead of CPU time.
- **DuckDB answer replay** (`app/duckreplay.py`, `app/build_duck_replay.py`). A database state is the connect arguments plus the ordered statements that changed the database. A recorded SELECT text on a recorded state gets the recorded answer. Any other text, parameter or method opens a real DuckDB connection, replays the state, and runs there. The image build records the answers from the pinned tests and checks them with a rehearsal. The rehearsal runs the tests with the replay on and needs every verdict to match the pinned run. If it fails, every query runs in DuckDB.
- **Lazy executor tables** (`app/preload.py`, `app/build_exec_cache.py`). The image build packs the TPC-H and TPC-DS rows of `TestExecutor.setUpClass`, one table at a time. `_LazyTables` unpacks a table at its first read by name. Any other use of the dict unpacks all tables first. The build checks the cache against the real `setUpClass` and drops it on a mismatch.
- **Lazy numpy, pandas and duckdb** (`app/lazymods.py`). They load only when a test really needs them.
- **Saved base state** (`app/build_base_state.py`). The image build saves the import-state fingerprint of the pinned tree. The selector checks the seed and Python version before it uses the file.

- **Loop filter** (`app/iterred.py`, `app/maps/i/`). Some tests loop over many inputs in one statement. The map records, per loop pass of the pinned run, the lines that the pass runs. A reduced run keeps only the passes that reach the changed lines.
- **Class route** (`app/classroute.py`, `app/maps/class_route.json`, `app/maps/import_exec.json`). An import-time edit can change only plain attributes of one class body. Then the child runs that class body again and sets the changed attributes, without a new import. The maps list the classes for which this gave the same import state on the pinned tree.
- **Parent cost** (`app/rawz.py`, `app/rawmaps.py`, `app/astwalk.py`, `app/build_docs_read.py`). The image build stores the maps without compression and saves one fact about the pinned test files. The selector prepares the reduced test code once in the parent (`prepare_keeps`). One helper process serves the importer-cone commits.

## Rules for unusual patches

`app/patchrules.py` holds the rules for new files, renames, pytest configuration, removed names and test files that cannot be imported. Other safety rules are in `app/selector.py` and `app/engine.py`.

- **Mixed edits** (`refine_with_diff`, `_edits_own_definition`). A patch can hold a call-time edit and an import-time edit. The reduced runs keep the lines of the call-time edit, and a changed function allows reduced runs only when the edit holds its own definition.
- **Closures** (`engine.plan_swaps`). New code can read free variables with other names than the pinned code. A live closure keeps the old cells, so the child does a fresh import instead of a code swap.
- **Indentation-only edits** (`_indentation_changed`, `_file_compiles`). An edit can move only the indentation of a line and still break the file. Then the child imports the patched tree, as a full run would.
- **Hung tests and pool tasks** (`conclude` in `run`). A test that does not end counts as a failure. A hung pool task blocks a worker of the class pool in a real run. Thus the later tests of that class are selected unrun.
- **Lazy module `__getattr__`** (`_module_getattr_span`, `_lazy_attr_users`). A changed module `__getattr__` can fail every lazy lookup. Every test that loads the module becomes a candidate, and no reduced run applies.
- **`if TYPE_CHECKING:` bodies** (`type_checking_groups`). Such code never runs, so an edit there changes no code that runs.

## The interpreter

Dockerfile step 0 adds a python-build-standalone build of the same CPython version (3.13.18). `build/fetch_python.py` downloads one file from a GitHub release at a fixed tag and checks its sha256. `build/sitecustomize.py` makes its `sqlite3` module load the pinned image's own SQLite extension. `build/check_python.py` compares the two interpreters at build time. It checks the version, bytecode magic, hashes, limits, float output and SQLite behavior. On any failure, the image keeps its own interpreter.

## The maps

Every map in `app/maps` comes from the pinned commit only. Each comes from its source or from runs of its own unchanged test suite. No map holds anything computed from a patch, a kind of edit, or test results on changed code. Builders are in `app/tools/`.

| Map | Content | Builder |
|---|---|---|
| `f/`, `sets.bin`, `sets.idx`, `meta.bin`, `readers.bin` | line coverage per test, statement spans, importers, files that tests open, readers of class constants | `trace_tests.py`, `build_agg.py`, `build_maps.py` |
| `t/`, `u/`, `classes.bin` | lines that each part of a test runs, classes that it creates | `trace_units.py`, `run_units.py`, `build_unitmap.py` |
| def-time sets in `f/` | for each line of import-time code, the tests that use what the line defines | `build_defmap.py` |
| `cost.bin` | CPU cost of each test in our runner | `time_all.py`, `build_costs.py` |
| `infect.bin` | for each comparison, the operand relations that each test produced | `build_infect.py`, `instrument.py`, `plugin/` |
| `infect2.bin` | for each and/or tree, the leaf outcomes per test, and the value classes of the other operand at each comparison with a literal | `infect2/build_infect.py` (`all2`), `infect2/instrument.py`, `infect2/plugin/` |
| `infect_hidden.bin` | for each mapped site, the tests that reach it more often alone than in the whole-suite run | `infect_hidden/build_hidden.py`, `instrument_reach.py`, `build_hidden.sh`, `cold_all.sh` |
| `classmap.bin` | which test reads which attribute through which class | `classtrace.py`, `build_classmap.py` |
| `keymap.bin` | which test reads which key of which class-level table | `keytrace.py`, `build_keymap.py` |
| `cone.json` | modules whose importer cone imports alone to the same state, each cone in import order | `build_cone.py` |
| `tight_lines.bin` | every sqlglot line that each of a few long tests runs, from exact traces | `tight/xmon.py`, `tight/xrun.py`, `build_tight_lines.py` |
| `tok_inputs.bin` | tokenizer inputs of those long tests, with token-stream digests on the pinned tree | `tight/tokrec.py`, `tight/xtplugin.py`, `build_tok_inputs.py` |
| `registry_spans.json` | line spans of the registry lookup methods in `dialect.py` | `build_registry_spans.py` |
| `tc_spans.bin` | spans of `if TYPE_CHECKING:` bodies in each pinned file | `build_tc_spans.py` |
| `pandas_tests.json` | tests that load numpy, pandas or duckdb | `build_pandas_tests.py` |
| `class_attr_reads.json`, `test_readers.json`, `registry_start.json` | fixture lists that a test reads, helper data that a test references, dialects registered at the start of a run | `build_attr_reads.py`, `build_test_readers.py`, `build_registry_start.py` |
| `faithful.json` | test methods that compile alone to the pinned code | `build_faithful.py` |

The span maps (`registry_spans.json`, `tc_spans.bin`) store the length and checksum of each file. The selector uses an entry only when the file text matches. Otherwise it computes the spans.

The image build also writes the executor row cache, the DuckDB answer recording and the saved base state to `/app/cache`. Each comes from the pinned tree.

## What is not in the repository

- No table from patches, or from kinds of edit, to tests.
- No answer recorded for a patch. Every map and recording comes from the pinned commit's source or from runs of its own unchanged test suite.
- No labeled practice patches. We made such patches to test the selector. They are not in the image, and the program does not read them at run time.

## Layout

| Path | Content |
|---|---|
| `Dockerfile` | the image: pinned Python image, interpreter step, PyPI packages, sqlglot at the pinned commit, bytecode, program |
| `requirements.txt` | exact PyPI versions from the kit's lock file |
| `build/` | build steps: `fetch_python.py`, `sitecustomize.py`, `check_python.py`, `fetch_sqlglot.py` |
| `app/main.py`, `app/selector.py` | entry point and the selector |
| `app/*.py` | run-time modules (`engine`, `patchrules`, `infect`, `keymap`, `cone`, `statefp`, `stmtmap`, `reduce`, `runner`, `pool`, `preload`, `lazymods`, `duckreplay`, `tokcheck`) and build-time scripts (`build_exec_cache.py`, `build_duck_replay.py`, `build_base_state.py`) |
| `app/maps/` | the maps |
| `app/tools/` | the scripts that built the maps, with `infect2/`, `infect_hidden/`, `tight/` and `plugin/` |
| `sample/`, `score.py`, `Makefile`, `SCORING.md` | the kit of the organizers, unchanged. `make sample` builds the image, runs it on the 30 sample commits, and scores the output |

## License

MIT. See `LICENSE`.
