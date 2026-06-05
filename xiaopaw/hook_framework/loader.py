"""HookLoader: 解析 hooks.yaml + 两层加载 + 策略实例化 + deps 依赖注入。

【三段式 YAML 结构】
    hooks:        # 观测层（无状态函数）→ register 后用 dispatch
    strategies:   # 策略层（有状态对象）→ register 后用 dispatch_gate
      - deps:     # 依赖注入：被依赖的策略必须先声明（按列表顺序实例化）

【关键设计：观测段必须整体先于策略段】
load_from_directory() 严格调用顺序：
    _load_hooks_section() → _load_strategies_section()
这样即使策略层 deny 阻断了链路，观测 handler 也已经写过 trace/log，攻击行为留有完整证据链。

"""

import importlib.util
import sys
from pathlib import Path

import yaml

from .registry import EventType, HookRegistry


class HookLoader:
    def __init__(self, registry: HookRegistry):
        self._registry = registry
        # 已实例化的策略集合，供后声明的策略通过 deps 引用
        self.strategies: dict[str, object] = {}
        # 模块缓存：同一文件被多个 handler 引用时只加载一次
        self._module_cache: dict[Path, object] = {}

    def load_from_directory(
        self, hooks_dir: Path, layer_name: str = "", fail_closed_names: set[str] | None = None
    ):
        """从一个目录加载 hooks.yaml。

        【加载顺序硬编码】
        先 hooks 段后 strategies 段——这是"约束一"的代码级实现。
        即使策略层抛 deny，观测 handler 已经执行完，Langfuse 里有完整记录。
        """
        yaml_path = hooks_dir / "hooks.yaml"
        if not yaml_path.exists():
            return

        try:
            with open(yaml_path) as f:
                config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            print(f"[HookLoader] YAML parse error in {yaml_path}: {e}", file=sys.stderr)
            return

        if not isinstance(config, dict):
            return

        fail_closed_names = fail_closed_names or set()

        # 顺序不能换：观测段必须先于策略段
        self._load_hooks_section(config, hooks_dir, layer_name, fail_closed_names)
        self._load_strategies_section(config, hooks_dir, layer_name, fail_closed_names)

    def _load_hooks_section(
        self, config: dict, hooks_dir: Path, layer_name: str, fail_closed_names: set[str]
    ):
        for event_name, handler_list in config.get("hooks", {}).items():
            event_type = EventType(event_name.lower())
            if not isinstance(handler_list, list):
                continue
            for entry in handler_list:
                handler_ref = entry if isinstance(entry, str) else entry.get("handler", "")
                if not handler_ref:
                    continue
                handler_fn = self._resolve_handler(handler_ref, hooks_dir)
                if handler_fn is None:
                    continue
                display = f"[{layer_name}] {handler_ref}" if layer_name else handler_ref
                self._registry.register(
                    event_type,
                    handler_fn,
                    name=display,
                    fail_closed=handler_ref in fail_closed_names,
                )

    def _load_strategies_section(
        self, config: dict, hooks_dir: Path, layer_name: str, fail_closed_names: set[str]
    ):
        """加载 strategies 段：实例化策略对象 + 注入依赖 + 注册方法到事件。

        【依赖注入约束】
        strategies 是有序列表，按声明顺序逐个实例化。
        当一个策略声明 deps 时，被依赖的策略必须已经在前面实例化完成。
        否则只打 WARNING（fail-open），但运行时调用 self.audit.xxx 会 AttributeError —
        因为安全 handler 是 fail_closed=True，会被翻译成 deny，把所有请求都拒掉。
        所以 hooks.yaml 里 audit_logger 必须排在 sandbox_guard / permission_gate 之前。
        """
        for entry in config.get("strategies", []):
            name = entry.get("name", "")
            class_ref = entry.get("class", "")
            strategy_config = entry.get("config", {}) or {}
            hooks_map = entry.get("hooks", {}) or {}
            deps_map = entry.get("deps", {}) or {}

            cls = self._resolve_class(class_ref, hooks_dir)
            if cls is None:
                continue

            # 依赖解析：从已实例化的 strategies 字典里查找
            # 关键陷阱：找不到只打 WARNING 不抛错（fail-open，开发友好）
            #          但 None 注入后，运行时第一次调用就会 AttributeError——
            #          且因为 fail_closed，会变成 GuardrailDeny 把所有请求拒绝
            resolved_deps = {}
            for param, strategy_key in deps_map.items():
                dep = self.strategies.get(strategy_key)
                if dep is None:
                    print(
                        f"[HookLoader] WARNING: dependency '{strategy_key}' not found for {class_ref}.{param}",
                        file=sys.stderr,
                    )
                resolved_deps[param] = dep

            try:
                # config 字段直接展开为构造参数；deps 字段也展开为构造参数
                # 例：sandbox_guard 的 __init__(self, audit) 接收 audit_logger 实例
                instance = cls(**strategy_config, **resolved_deps)
            except Exception as e:
                print(
                    f"[HookLoader] failed to instantiate {class_ref}: {e}",
                    file=sys.stderr,
                )
                continue

            # 存入字典，供后续声明的策略通过 deps 引用
            self.strategies[name] = instance

            for event_name, method_name in hooks_map.items():
                event_type = EventType(event_name.lower())
                method = getattr(instance, method_name, None)
                if method is None:
                    print(
                        f"[HookLoader] method not found: {class_ref}.{method_name}",
                        file=sys.stderr,
                    )
                    continue
                display = f"[{layer_name}] {name}.{method_name}" if layer_name else f"{name}.{method_name}"
                self._registry.register(
                    event_type,
                    method,
                    name=display,
                    fail_closed=name in fail_closed_names,
                )

    def _resolve_handler(self, handler_ref: str, hooks_dir: Path):
        parts = handler_ref.rsplit(".", 1)
        if len(parts) != 2:
            print(f"[HookLoader] invalid handler ref (expected module.function): {handler_ref}", file=sys.stderr)
            return None
        module_name, func_name = parts
        module_path = (hooks_dir / f"{module_name}.py").resolve()
        if not module_path.is_relative_to(hooks_dir.resolve()):
            print(
                f"[HookLoader] path traversal blocked: {handler_ref}",
                file=sys.stderr,
            )
            return None
        if not module_path.exists():
            print(
                f"[HookLoader] module not found: {module_path}",
                file=sys.stderr,
            )
            return None
        module = self._load_module(module_name, module_path, hooks_dir)
        if module is None:
            return None
        fn = getattr(module, func_name, None)
        if fn is None:
            print(
                f"[HookLoader] function not found: {handler_ref}",
                file=sys.stderr,
            )
        return fn

    def _resolve_class(self, class_ref: str, hooks_dir: Path):
        parts = class_ref.rsplit(".", 1)
        if len(parts) != 2:
            print(f"[HookLoader] invalid class ref (expected module.Class): {class_ref}", file=sys.stderr)
            return None
        module_name, class_name = parts
        module_path = (hooks_dir / f"{module_name}.py").resolve()
        if not module_path.is_relative_to(hooks_dir.resolve()):
            print(
                f"[HookLoader] path traversal blocked: {class_ref}",
                file=sys.stderr,
            )
            return None
        if not module_path.exists():
            print(
                f"[HookLoader] module not found: {module_path}",
                file=sys.stderr,
            )
            return None
        module = self._load_module(module_name, module_path, hooks_dir)
        if module is None:
            return None
        cls = getattr(module, class_name, None)
        if cls is None:
            print(
                f"[HookLoader] class not found: {class_ref}",
                file=sys.stderr,
            )
        return cls

    def _load_module(self, module_name: str, module_path: Path, hooks_dir: Path):
        resolved = module_path.resolve()
        if resolved in self._module_cache:
            return self._module_cache[resolved]
        fq_name = f"hooks_dynamic.{id(hooks_dir)}.{module_name}"
        spec = importlib.util.spec_from_file_location(fq_name, module_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[fq_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            sys.modules.pop(fq_name, None)
            print(f"[HookLoader] error loading {module_path}: {e}", file=sys.stderr)
            return None
        self._module_cache[resolved] = module
        return module

    def load_two_layers(self, global_dir: Path, workspace_dir: Path, fail_closed_names: set[str] | None = None):
        """两层加载：先全局（shared_hooks），后 workspace 用户级。

        全局层：所有用户共享的安全/可观测策略（sandbox_guard 等）
        Workspace 层：单个用户私有的扩展 handler（如自定义审计、用户级权限策略）
        Workspace 层 handler 会追加到全局之后执行，但同样受 dispatch_gate 顺序约束。
        """
        self.load_from_directory(global_dir, layer_name="global", fail_closed_names=fail_closed_names)
        ws_hooks = workspace_dir / "hooks"
        if ws_hooks.exists():
            self.load_from_directory(ws_hooks, layer_name="workspace", fail_closed_names=fail_closed_names)
