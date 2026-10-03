# xli

**Language:** Python
**Framework:** Unknown

## Project Structure

- `xli/core/__init__.py`
- `xli/core/config.py`
- `xli/core/logger.py`
- `xli/core/env.py`
- `xli/core/skills.py`
- `xli/core/memory.py`
- `xli/core/cache.py`
- `xli/core/agent.py`
- `xli/core/chain.py`
- `xli/core/planner.py`
- `xli/core/streaming.py`
- `xli/core/diff_engine.py`
- `xli/core/sandbox.py`
- `xli/core/vector_store.py`
- `xli/core/git.py`

## Coding Patterns

### Async/Await
Uses async/await patterns

```
name for inbox")
    return parser.parse_args()


async def main():
    args = parse_args()
    config = get_config()

    if args.init:
        print("Scanning project...")
        path = init_projec
```

### OOP Style
Uses classes with methods

```
Logger

logger = StructuredLogger("xli.config")


class Config:
    """XLI Configuration"""

    DEFAULTS = {
        "provider": "mistral",
        "model": "mistral-large-latest",
        "temperatu
```

### Type Hints
Uses typing annotations

```
efault",
        "project": "default",
    }

    def __init__(self):
        self.config_dir = Path.home() / ".xli"
        self.config_file = self.config_dir / "config.json"
        self.env_file = 
```

## Conventions

- Has test directory

## Key Dependencies
