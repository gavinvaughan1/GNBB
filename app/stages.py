"""Stage check for the long executor test (tests/test_executor.py test_execute_tpcds).

That test hands one task per TPC-DS query to a process pool. A task runs
sqlglot.executor.execute(): it optimizes the parsed query, builds a plan from the optimized
expression, and runs the plan with the pure-Python executor over the TPC-DS tables; the test
then compares the rows with what DuckDB returns for the query's DuckDB SQL text. Nearly all of
the CPU time is the executor's row work. The work from the plan on starts from two values
only: the optimized expression that execute() passes to Plan(), and the tables that it
passes to PythonExecutor(). The comparison needs the task's rows and the SQL text that
transpile() makes for DuckDB.

build_stages.py records, on the pinned commit, for every query: a fingerprint of those two
values, of the DuckDB SQL text and of the rows that the test receives, and the sqlglot lines
that run after the point where the plan is built ("late lines": plan, executor, result). At
run time the test runs once with the patched code, with a hook on its tasks:

- The patched code makes another DuckDB SQL text for a query than the pinned code: DuckDB's
  answer is not known, the check ends without a verdict.
- A task whose two input fingerprints equal the pinned ones, and whose late lines (as recorded
  on the pinned commit) hold no line that the commit changes and no line that reads a name
  whose import-time value the commit changes, stops where the plan would be built. Its later
  work would get the same input and run the same code as on the pinned commit, so it would
  give the pinned rows.
- Every other task (another input, or late lines that the commit touches) runs to its end
  right there, as in an ordinary run of the test, and the test receives its rows. When they
  equal the pinned rows (same columns, same values of the same types, same order), the
  comparison with DuckDB's answer to the same SQL text gets the same two inputs as on the
  pinned commit, where it passes; it is left out. Other rows: no verdict, the check ends.
- The runs to the end have a budget (the build-time CPU seconds of their later work; the
  selector sets it from the seconds that the test would add to the answer). When the budget
  is used up, the check ends without a verdict.

Without a verdict the selector treats the test as before the check (selected, not run). An
error in the test or in a task fails the test, as in a full run. When the test passes in the
check with every task proven in one of the two ways, and the module state after the check
differs from the state before it only as the pinned test changes it (state_ok), the test is
known to pass on the patched code."""
from __future__ import annotations

import enum
import hashlib
import io
import marshal
import os
import pickle
import sys
import time
import types
import zlib

MAP_VERSION = 7
_MISSING = "\x00unset"
_OPAQUE_TYPES = (types.FunctionType, types.BuiltinFunctionType, types.MethodType, types.ModuleType,
                 types.CodeType, types.GeneratorType, types.MethodDescriptorType, types.WrapperDescriptorType,
                 staticmethod, classmethod, property)
_BASES = ((dict, lambda o: list(dict.items(o))), (list, lambda o: list(list.__iter__(o))),
          (tuple, lambda o: list(tuple.__iter__(o))), (set, lambda o: list(set.__iter__(o))),
          (frozenset, lambda o: list(frozenset.__iter__(o))), (str, lambda o: str.__add__(o, "")),
          (bytes, lambda o: bytes.__add__(o, b"")), (float, lambda o: float.__add__(o, 0.0)),
          (int, lambda o: int.__add__(o, 0)))


def _describe(t):
    """How to write an object of type t: (kind, text, slot names, has a dict, base value)."""
    name = "%s.%s" % (t.__module__, t.__qualname__)
    if issubclass(t, type):
        return (1, name, (), False, None)
    if issubclass(t, enum.Enum):
        return (2, name, (), False, None)
    if issubclass(t, _OPAQUE_TYPES):
        return (3, name, (), False, None)
    if not (t.__module__ or "").startswith("sqlglot"):
        return (4, name, (), False, None)  # an object of another library: pickle writes its value
    slots = []
    for klass in reversed(t.__mro__):
        s = klass.__dict__.get("__slots__", ())
        for n in ((s,) if isinstance(s, str) else s):
            if n not in ("__dict__", "__weakref__") and n not in slots:
                slots.append(n)
    base = None
    for b, read in _BASES:
        if issubclass(t, b):
            base = read
            break
    return (0, "O:" + name, tuple(slots), bool(t.__dictoffset__), base)


class _Writer(pickle.Pickler):
    """Writes an object graph as bytes that depend only on its content and shape: every object
    with its exact class, every slot and attribute (for an expression: args, parent, arg_key,
    index, comments, type, meta, cached hash), containers in their iteration order, and each
    repeated reference as a reference to the first one (so shared and cyclic structure counts).
    It calls no sqlglot code: an object of a sqlglot class is written from its slots and its
    __dict__, not through its __reduce__. Functions and modules have no value here: a graph
    that holds one gets no fingerprint."""

    def __init__(self, file, leaves):
        super().__init__(file, 4)
        self.leaves = leaves
        self.info = {}
        self.opaque = False

    def reducer_override(self, obj):
        t = type(obj)
        info = self.info.get(t)
        if info is None:
            info = self.info[t] = _describe(t)
        kind = info[0]
        if kind == 0:
            if self.leaves:
                leaf = self.leaves.get(id(obj))
                if leaf is not None:
                    return (str, (leaf,))
            state = [getattr(obj, n, _MISSING) for n in info[2]]
            if info[3]:
                state.append(obj.__dict__)
            if info[4] is not None:
                state.append(info[4](obj))
            return (str, (info[1],), tuple(state))
        if kind == 4:
            return NotImplemented
        if kind == 1:
            if not (obj.__module__ or "").startswith("sqlglot"):
                return NotImplemented  # a class of another library: pickle writes its name
            return (str, ("C:%s.%s" % (obj.__module__, obj.__qualname__),))
        if kind == 2:
            return (str, ("E:%s.%s" % (info[1], obj.name),))
        self.opaque = True
        return (str, ("?",))


def fingerprint(obj, leaves=None):
    """16 bytes, or None when the object graph has a part without a value to compare."""
    buf = io.BytesIO()
    w = _Writer(buf, leaves)
    try:
        w.dump(obj)
    except Exception:  # noqa: BLE001  (an object that pickle cannot write)
        return None
    if w.opaque:
        return None
    return hashlib.blake2b(buf.getvalue(), digest_size=16).digest()


def table_leaves(tables):
    """{id(table): text} for the tables of the class fixture that are built (the fixture
    builds a table when a task first reads it, see preload._LazyTables). A task gets the
    table objects as they are; the text holds what the executor reads from one besides the
    values of its rows: the columns, the column range, the reader's column positions and the
    number of rows. None when a table does not look like one."""
    out = {}
    try:
        items = list(dict.items(tables)) if isinstance(tables, dict) else []
        for name, table in items:
            reader = sorted(table.reader.columns.items())
            out[id(table)] = "T:%s:%r" % (name, (table.columns, table.column_range, reader, len(table.rows)))
    except Exception:  # noqa: BLE001
        return None
    return out


def rows_fingerprint(table):
    """Fingerprint of the result that the test receives from a task: the columns and the rows
    (values with their types, in order). None for anything but a plain result table."""
    try:
        columns, rows = table.columns, table.rows
        if type(columns) is not tuple or type(rows) is not list:
            return None
    except Exception:  # noqa: BLE001
        return None
    return fingerprint((columns, rows))


def hash_probe():
    """The fingerprints hold string hashes, and the recorded module state holds hashes of
    code: both are valid under one hash seed and one Python version only."""
    return (hash("stage map"), hash(("G", "sqlglot", 1, 2.5, b"x", None)), sys.version_info[:3], MAP_VERSION)


def load_map(path):
    """{test id: record} written by build_stages.py; {} when there is no map, or when this
    process hashes in another way than the process that built it."""
    try:
        with open(path, "rb") as f:
            records = marshal.loads(zlib.decompress(f.read()))
    except (OSError, ValueError, EOFError, TypeError, zlib.error):
        return {}
    if type(records) is not dict:
        return {}
    probe = hash_probe()
    return {k: r for k, r in records.items() if type(r) is dict and r.get("probe") == probe}


def late_tasks(record, lines):
    """The tasks whose later work runs a changed line on the pinned commit.
    lines: {repo-relative path: set of lines} of the commit (the changed statements, and the
    statements that read a name whose import-time value the commit changes)."""
    out = set()
    for rel, lns in lines.items():
        by_line = record["late"].get(rel)
        if not by_line or not lns:
            continue
        for ln in lns:
            sid = by_line.get(ln)
            if sid is not None:
                out.update(record["sets"][sid])
    return out


class _Same(BaseException):
    """Raised where the plan would be built: the task ends there."""


class _PlanInput:
    """Stands for the plan between the two calls of execute(): Plan(expression) and
    PythonExecutor(tables=...).execute(plan)."""

    def __init__(self, expression):
        self.expression = expression


class StageCheck:
    """One run of the test with the hook run_task on its tasks (see the module text). After
    it: 'same' holds the tasks that stopped with the pinned input, 'ran' the tasks that ran
    to their end, 'proved' those of them that gave the pinned rows; 'over' is True when the
    check ended without a verdict ('why' says where)."""

    slack = 5.0  # CPU s over twice the build-time cost before a check counts as slow (None: never)

    def __init__(self, record, later, budget, active=None):
        self.record = record
        self.tasks = record["tasks"]  # {task index: (expression, tables, duckdb sql, rows) fingerprints}
        self.cpu = record["cpu"]  # {task index: (CPU s up to the plan, CPU s after it)} at build time
        self.later = later  # tasks whose later work runs changed code: they run to their end
        self.budget = budget  # build-time CPU s that the runs to the end may cost
        self.active = active  # the tasks that the test runs at all (None = all), see reduce.py
        self.spent = 0.0
        self.expected = 0.0  # build-time CPU s of the tasks so far (to the plan, or to the end)
        self.clock = None  # CPU time of this process at the first task
        self.over = False
        self.why = None
        self.same = []
        self.ran = []  # (task, why it ran to its end)
        self.proved = []
        self.seq = []  # one letter per task, in the order of the test (for the logs)
        self.task = None
        self.module = None
        self.globals = None
        self.real = None
        self.cls = None
        self.helper = None

    # -- the two names that execute() looks up in its module, and the test's comparison ----

    def install(self, test_module, cls):
        """Returns False when the test does not look like the pinned one from outside."""
        fn = getattr(test_module, "execute", None)
        g = getattr(fn, "__globals__", None)
        if not isinstance(fn, types.FunctionType) or g is None or g.get("__name__") != "sqlglot.executor":
            return False
        plan, executor = g.get("Plan"), g.get("PythonExecutor")
        if not isinstance(plan, type) or not isinstance(executor, type):
            return False
        if not isinstance(getattr(test_module, "transpile", None), types.FunctionType):
            return False
        helper = cls.__dict__.get("subtestHelper")
        if not isinstance(helper, types.FunctionType):
            return False
        self.module, self.cls, self.globals, self.real = test_module, cls, g, (plan, executor)
        self.helper = helper
        check = self

        def plan_stub(*args, **kwargs):
            if check.task is None or len(args) != 1 or kwargs:
                return plan(*args, **kwargs)
            return _PlanInput(args[0])

        class ExecutorStub:
            def __init__(self, *args, **kwargs):
                self.args, self.kwargs = args, kwargs

            def execute(self, plan_input):
                return check.at_executor(self.args, self.kwargs, plan_input)

        def subtest_helper(case, i, table, tpch=True):
            # the test calls this with the rows of a task that ran to its end
            if tpch or not check.on_result(i, table):
                return helper(case, i, table, tpch=tpch)
            return None

        g["Plan"], g["PythonExecutor"] = plan_stub, ExecutorStub
        cls.subtestHelper = subtest_helper
        return True

    def remove(self):
        if self.globals is not None:
            self.globals["Plan"], self.globals["PythonExecutor"] = self.real
            self.cls.subtestHelper = self.helper
            self.globals = None

    def stop(self, why):
        """Ends the check without a verdict: the tasks that are left do not run."""
        self.over = True
        if self.why is None:
            self.why = why
        # (the tasks that are left are not even handed over: see pool.LazyFuture)
        from reduce import STATE

        STATE.keep_tasks = frozenset()

    def affordable(self, index, whole):
        """Counts the build-time cost of running this task to its end; False (and the check is
        over) when the budget does not hold it."""
        early, late = self.cpu.get(index, (0.0, 0.0))
        cost = late + (early if whole else 0.0)
        if cost > self.budget - self.spent:
            self.stop("budget")
            return False
        self.spent += cost
        self.expected += late
        return True

    def compare(self, index, args, kwargs, plan_input):
        """None when the task's input at this point equals the pinned one, else what differs."""
        want = self.tasks[index]
        if args or list(kwargs) != ["tables"]:
            return "call"
        if fingerprint(plan_input.expression) != want[0]:
            return "expression"
        if fingerprint(kwargs["tables"], table_leaves(getattr(self.module, "_tables", None))) != want[1]:
            return "tables"
        return None

    def at_executor(self, args, kwargs, plan_input):
        plan, executor = self.real
        index = self.task
        if not isinstance(plan_input, _PlanInput):
            # execute() made its plan in another way than the pinned one (or this is not a
            # task): nothing to compare, the real executor runs
            if index is not None:
                if not self.affordable(index, False):
                    self.seq.append("o")
                    raise _Same()
                self.ran.append((index, "call"))
                self.seq.append("d")
                self.task = None
            return executor(*args, **kwargs).execute(plan_input)
        what = self.compare(index, args, kwargs, plan_input)
        if what is None:
            self.same.append(index)
            self.seq.append("s")
            raise _Same()
        # another input than on the pinned commit: the task goes on to its end, when the
        # budget allows
        if not self.affordable(index, False):
            self.seq.append("o")
            raise _Same()
        self.ran.append((index, what))
        self.seq.append("d")
        self.task = None
        return executor(*args, **kwargs).execute(plan(plan_input.expression))

    def on_result(self, index, table):
        """The test received the rows of a task that ran to its end. True: the comparison
        with DuckDB's answer is left out (the rows are the pinned rows and the comparison
        passes, or the check is over and has no verdict anyway)."""
        want = self.tasks.get(index)
        if want is not None and not self.over and rows_fingerprint(table) == want[3]:
            self.proved.append(index)
        else:
            self.stop("rows of task %d" % index)
        return True

    def sql_text_differs(self, index):
        """The DuckDB side: the same SQL text gives the same rows. True when the patched code
        makes another text for this query than the pinned code (or raises)."""
        try:
            sql = getattr(self.cls, self.record["sqls"])[index][1]
            text = self.module.transpile(sql, write="duckdb")[0]
            return hashlib.blake2b(text.encode(), digest_size=16).digest() != self.tasks[index][2]
        except Exception:  # noqa: BLE001
            return True

    # -- pool.LazyFuture calls this in place of the task ---------------------------------

    def run_task(self, index, fn, args, kwargs):
        want = self.tasks.get(index)
        if want is None:
            # a query that the pinned test does not execute: the task is short and runs as it is
            return fn(*args, **kwargs)
        if self.over:
            return None  # the test skips a task without a result
        # A check that costs far more CPU than the pinned tasks did (the patched code is slow)
        # ends here: it would cost more than it can save.
        now = time.process_time()
        if self.clock is None:
            self.clock = now
        elif self.slack is not None and now - self.clock > 2.0 * self.expected + self.slack:
            self.seq.append("t")
            self.stop("slow")
            return None
        self.expected += self.cpu[index][0] + 0.01
        if self.sql_text_differs(index):
            self.seq.append("q")
            self.stop("sql text of task %d" % index)
            return None
        if index in self.later:
            if not self.affordable(index, True):
                self.seq.append("o")
                return None
            self.ran.append((index, "later"))
            self.seq.append("l")
            return fn(*args, **kwargs)
        self.task = index
        try:
            value = fn(*args, **kwargs)
        except _Same:
            return None
        finally:
            reached = self.task is None
            self.task = None
        if not reached:
            # the task ended without asking for the executor: its value is what the test gets
            self.ran.append((index, "path"))
            self.seq.append("p")
        return value

    def probe(self, case):
        """Before the test runs: its first task by hand, as the test and its pool would run it
        (parse the query, hand it over through pickle, the worker's initializer, the task with
        the hook, the result back through pickle). Most commits that break the test break
        this task, and then the check is over before the test has parsed its other queries.
        Returns False when the check is over. Nothing of the probe counts for the verdict:
        the test runs afterwards with every task, this one too."""
        module = self.module
        index = min((i for i in self.tasks if self.active is None or i in self.active), default=None)
        if index is None:
            return True
        try:
            meta, sql = getattr(self.cls, self.record["sqls"])[index][:2]
            expression = module.parse_one(sql)
            fn, args, kwargs = pickle.loads(pickle.dumps((module.mp_execute, (expression, meta), {})))
            module.initializer(getattr(module, self.record["schema"]), getattr(case, self.record["tables"]))
            value = self.run_task(index, fn, args, kwargs)
            if value is not None:
                self.on_result(index, pickle.loads(pickle.dumps(value)))
        except Exception:  # noqa: BLE001  (the test will very likely fail in the same way)
            self.stop("probe: error in task %d" % index)
        alive = not self.over
        if alive:
            del self.same[:], self.ran[:], self.proved[:], self.seq[:]
            self.spent = self.expected = 0.0
            self.clock = None
        return alive

    def complete(self):
        """True when every task that ran to its end gave the pinned rows. (A task that ran
        and gave the test nothing to compare, where the pinned task gives rows, is not proven.)"""
        return not self.over and sorted(self.proved) == sorted(i for i, _ in self.ran)


# --- module state around the check ---------------------------------------------------
# The later work also reads module state (module globals, class attributes). The pinned test
# writes a little of it itself: the dialect registry fills when a dialect is first looked up,
# the generator's dispatch table of a generator class is made at its first use, and pickle
# stores the slot names of a class that it writes. build_stages.py records those writes
# ("effect"). After a check, every key of the state that differs from before must be one of
# them, with the pinned value; in a table (dict), every new or changed entry must be an entry
# that the pinned run adds, with the pinned value.


def code_alias(pairs):
    """{hash of new code: hash of old code} for statefp, from (old code, new code) pairs of
    functions whose body the commit changes. A function then counts as the pinned function:
    which of its lines changed is what late_tasks looks at."""
    import statefp

    f = statefp.Fingerprinter()
    alias = {}
    stack = list(pairs)
    while stack:
        old, new = stack.pop()
        if f.code(new) != f.code(old):
            alias[f.code(new)] = f.code(old)
        oc = [k for k in old.co_consts if isinstance(k, types.CodeType)]
        nc = [k for k in new.co_consts if isinstance(k, types.CodeType)]
        if len(oc) == len(nc):
            stack.extend(zip(oc, nc))
    return alias


def _entry_name(x):
    """The name of a table entry's key: a class by the plain name of classes (not through a
    __repr__ that patched code may define), anything else by its repr."""
    return type.__repr__(x) if isinstance(x, type) else repr(x)


def state_before(effect, alias=None, every_table=False):
    """The state now, and for the tables that the pinned test fills (for every table when
    build_stages.py asks, which finds out which ones those are) the fingerprint of every entry
    under the name of its key (not the values: they can change in place)."""
    import statefp

    state, f = statefp.snapshot(alias)
    keys = list(f.objects) if every_table else [k for k, e in effect.items() if e[0] == "entries"]
    tables = {k: {_entry_name(x): f.fp(v) for x, v in f.objects[k].items()} for k in keys if type(f.objects.get(k)) is dict}
    return state, tables


def hold_state(effect, alias=None):
    """Keeps the module state of this moment without computing its snapshot: a forked copy
    of this process waits, and computes state_before() only when held_state() asks for it.
    Most checks end without a verdict (a task with other rows, an error) and never ask: they
    save the snapshot. Returns the handle for held_state() / release()."""
    ask_r, ask_w = os.pipe()
    ans_r, ans_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(ask_w)
            os.close(ans_r)
            if os.read(ask_r, 1):
                view = memoryview(marshal.dumps(state_before(effect, alias)))
                while view:
                    view = view[os.write(ans_w, view):]
        except BaseException as e:  # noqa: BLE001  (no answer: the check gives no verdict)
            if os.environ.get("BR_DEBUG"):
                sys.stderr.write("stage check: no snapshot of the state before: %r\n" % (e,))
        finally:
            os._exit(0)
    os.close(ask_r)
    os.close(ans_w)
    return [pid, ask_w, ans_r]


def release(holder):
    """Ends the waiting copy (also after held_state)."""
    if holder and holder[0]:
        pid, ask_w, ans_r = holder
        holder[0] = 0
        for fd in (ask_w, ans_r):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass


def held_state(holder):
    """The state_before() of the moment of hold_state(), or None when the copy gave none."""
    chunks = []
    try:
        os.write(holder[1], b"s")
        while True:
            chunk = os.read(holder[2], 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        return marshal.loads(b"".join(chunks))
    except (OSError, ValueError, EOFError, TypeError):
        return None
    finally:
        release(holder)


def state_effect(before, alias=None):
    """What changed since state_before(): {key: ("value", fingerprint) or ("entries", {name of
    the entry's key: fingerprint}) or ("gone",)}. A table (dict) that only gained or changed
    entries gives "entries" when its entries were kept."""
    import statefp

    state0, tables0 = before
    state1, f = statefp.snapshot(alias)
    out = {}
    for k in statefp.diff(state0, state1):
        if k not in state1:
            out[k] = ("gone",)
            continue
        old, new = tables0.get(k), f.objects.get(k)
        now = {_entry_name(x): f.fp(v) for x, v in new.items()} if old is not None and type(new) is dict else None
        if now is not None and all(x in now for x in old):
            out[k] = ("entries", {x: h for x, h in now.items() if x not in old or old[x] != h})
        else:
            out[k] = ("value", state1[k])
    return out


def state_ok(effect, before, alias=None):
    """(True, []) when the state changed only as the pinned test changes it."""
    bad = []
    for k, got in state_effect(before, alias).items():
        want = effect.get(k)
        if want is None or want[0] != got[0]:
            bad.append(k)
        elif got[0] == "value" and got[1] != want[1]:
            bad.append(k)
        elif got[0] == "entries" and any(want[1].get(x) != v for x, v in got[1].items()):
            bad.append(k)
    return not bad, bad
