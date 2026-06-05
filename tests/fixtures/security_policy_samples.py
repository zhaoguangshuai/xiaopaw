"""Security policy YAML samples for PermissionGate tests."""

DEFAULT_ASK_POLICY = """\
permissions:
  default: ask
  tools:
    skill_loader: allow
    baidu_search: allow
    shell_executor: deny
"""

DEFAULT_DENY_POLICY = """\
permissions:
  default: deny
  tools:
    skill_loader: allow
"""

DEFAULT_ALLOW_POLICY = """\
permissions:
  default: allow
  tools:
    shell_executor: deny
"""
