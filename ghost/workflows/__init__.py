"""
Ghost workflow registry.

Workflows are discovered dynamically from:
1. This package (ghost/workflows/*.py)
2. Plugins directory (plugins/*/workflows/*.py) — if configured

Each workflow module should provide either:
- run(tg, llm_client, config) — async function for self-managed workflows
- create_agent(tg, config) — returns (AgentState, AgentCallbacks) for agent-based workflows

Optional:
- should_run_check() — return False to skip this run (e.g., preconditions not met)
"""

import importlib
import logging
import pkgutil

logger = logging.getLogger(__name__)

# Registry: name -> module
WORKFLOWS: dict = {}


def _discover_builtin():
    """Auto-discover workflow modules in this package."""
    import ghost.workflows as pkg
    for importer, modname, ispkg in pkgutil.iter_modules(pkg.__path__):
        if modname.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"ghost.workflows.{modname}")
            WORKFLOWS[modname] = mod
        except Exception as e:
            logger.warning(f"Failed to load workflow {modname}: {e}")


def register_workflow(name: str, module) -> None:
    """Register a workflow module by name. Used by plugins."""
    WORKFLOWS[name] = module
    logger.info(f"Registered workflow: {name}")


def get_workflow(name: str):
    """Get workflow module by name."""
    return WORKFLOWS.get(name)


# Auto-discover on import
_discover_builtin()
