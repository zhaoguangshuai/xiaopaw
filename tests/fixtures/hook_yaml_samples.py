"""YAML configuration samples for HookLoader tests."""

VALID_HOOKS_YAML = """\
hooks:
  BEFORE_TURN:
    - handler: my_handler.on_turn
  AFTER_TURN:
    - handler: my_handler.on_after_turn
"""

VALID_HANDLER_PY = """\
_calls = []

def on_turn(ctx):
    _calls.append(("on_turn", ctx))

def on_after_turn(ctx):
    _calls.append(("on_after_turn", ctx))
"""

STRATEGIES_YAML = """\
strategies:
  - name: counter
    class: counter_strategy.Counter
    config:
      start: 10
    hooks:
      BEFORE_TURN: on_turn
"""

COUNTER_STRATEGY_PY = """\
class Counter:
    def __init__(self, start=0):
        self.count = start

    def on_turn(self, ctx):
        self.count += 1
"""

DEPS_YAML = """\
strategies:
  - name: logger
    class: logger_mod.Logger
    config: {}
    hooks: {}
  - name: gate
    class: gate_mod.Gate
    config: {}
    deps:
      logger: logger
    hooks:
      BEFORE_TOOL_CALL: check
"""

LOGGER_MOD_PY = """\
class Logger:
    def __init__(self):
        self.events = []
    def record(self, msg):
        self.events.append(msg)
"""

GATE_MOD_PY = """\
class Gate:
    def __init__(self, logger=None):
        self.logger = logger
    def check(self, ctx):
        if self.logger:
            self.logger.record("checked")
"""

INVALID_YAML = "{{{invalid yaml content"

MISSING_MODULE_YAML = """\
hooks:
  BEFORE_TURN:
    - handler: nonexistent.do_stuff
"""

MISSING_FUNC_YAML = """\
hooks:
  BEFORE_TURN:
    - handler: my_handler.missing_func
"""

PATH_TRAVERSAL_YAML = """\
hooks:
  BEFORE_TURN:
    - handler: ../../../etc/evil.do_stuff
"""
