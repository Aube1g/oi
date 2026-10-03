#!/usr/bin/env python3
"""
XLI Templates v4 — TemplateEngine: fastapi_crud, react_component, cli_tool
"""

import re

from xli.paths import xli_path
from xli.core.logger import StructuredLogger

logger = StructuredLogger("xli.templates")

BUILT_IN_TEMPLATES = {
    "fastapi_crud": '''from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List

app = FastAPI()

class {{model_name}}Create(BaseModel):
    {{fields}}

class {{model_name}}({{model_name}}Create):
    id: int

    class Config:
        from_attributes = True

# In-memory storage (replace with DB)
db: List[{{model_name}}] = []
next_id = 1

@app.post("/{{endpoint}}/", response_model={{model_name}})
def create_{{endpoint}}(item: {{model_name}}Create):
    global next_id
    new_item = {{model_name}}(id=next_id, **item.dict())
    db.append(new_item)
    next_id += 1
    return new_item

@app.get("/{{endpoint}}/", response_model=List[{{model_name}}])
def list_{{endpoint}}():
    return db

@app.get("/{{endpoint}}/{item_id}", response_model={{model_name}})
def get_{{endpoint}}(item_id: int):
    for item in db:
        if item.id == item_id:
            return item
    raise HTTPException(status_code=404, detail="Not found")

@app.put("/{{endpoint}}/{item_id}", response_model={{model_name}})
def update_{{endpoint}}(item_id: int, item: {{model_name}}Create):
    for i, existing in enumerate(db):
        if existing.id == item_id:
            db[i] = {{model_name}}(id=item_id, **item.dict())
            return db[i]
    raise HTTPException(status_code=404, detail="Not found")

@app.delete("/{{endpoint}}/{item_id}")
def delete_{{endpoint}}(item_id: int):
    for i, item in enumerate(db):
        if item.id == item_id:
            db.pop(i)
            return {"ok": True}
    raise HTTPException(status_code=404, detail="Not found")
''',

    "react_component": '''import React, { useState, useEffect } from 'react';

interface {{component_name}}Props {
  {{props}}
}

export const {{component_name}}: React.FC<<{{component_name}}Props> = (props) => {
  const [state, setState] = useState({{initial_state}});

  useEffect(() => {
    {{effect_code}}
  }, []);

  return (
    <div className="{{component_name | lower}}">
      {{jsx_content}}
    </div>
  );
};
''',

    "cli_tool": '''#!/usr/bin/env python3
"""
{{tool_name}} — {{description}}
"""

import argparse
import sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="{{description}}")
    parser.add_argument("input", help="Input file")
    parser.add_argument("-o", "--output", help="Output file")
    parser.add_argument("-v", "--verbose", action="store_true")
    
    args = parser.parse_args()
    
    {{main_logic}}
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
''',

    "python_package": '''"""
{{package_name}} — {{description}}
"""

__version__ = "0.1.0"
__author__ = "{{author}}"

from .core import {{main_class}}

__all__ = ["{{main_class}}"]
''',
}


class TemplateEngine:
    """Template rendering engine"""

    def __init__(self):
        self.templates: dict[str, str] = dict(BUILT_IN_TEMPLATES)
        self.user_templates_dir = xli_path("templates")
        self.user_templates_dir.mkdir(parents=True, exist_ok=True)
        self._load_user_templates()
        logger.log_structured("INFO", "templates",
                             f"Loaded {len(self.templates)} templates")

    def _load_user_templates(self):
        """Load user-defined templates"""
        if not self.user_templates_dir.exists():
            return

        for template_file in self.user_templates_dir.glob("*.tmpl"):
            name = template_file.stem
            try:
                self.templates[name] = template_file.read_text(encoding="utf-8")
                logger.log_structured("DEBUG", "templates",
                                     f"Loaded user template: {name}")
            except Exception as e:
                logger.log_error("templates", f"Load failed: {template_file}", exc=e)

    def render(self, template_name: str, **kwargs) -> str:
        """Render template with variable substitution"""
        if template_name not in self.templates:
            logger.log_structured("ERROR", "templates",
                                 f"Template not found: {template_name}")
            return f"# Error: Template '{template_name}' not found"

        template = self.templates[template_name]

        # Simple variable substitution: {{var_name}}
        def replace_var(match):
            var_name = match.group(1).strip()

            # Handle filters: var|filter
            if "|" in var_name:
                var_name, filter_name = var_name.split("|", 1)
                var_name = var_name.strip()
                filter_name = filter_name.strip()

            if var_name in kwargs:
                value = str(kwargs[var_name])

                # Apply filters
                if 'filter_name' in locals():
                    if filter_name == "lower":
                        value = value.lower()
                    elif filter_name == "upper":
                        value = value.upper()
                    elif filter_name == "title":
                        value = value.title()

                return value

            logger.log_structured("WARN", "templates",
                                 f"Missing variable: {var_name}")
            return f"{{{{{var_name}}}}}"

        result = re.sub(r'\{\{(\s*[\w|]+\s*)\}\}', replace_var, template)

        logger.log_structured("DEBUG", "templates",
                             f"Rendered: {template_name}")
        return result

    def list_templates(self) -> list[str]:
        """List available templates"""
        return sorted(self.templates.keys())

    def register_template(self, name: str, template: str):
        """Register new template"""
        self.templates[name] = template

        # Save to user directory
        template_file = self.user_templates_dir / f"{name}.tmpl"
        try:
            template_file.write_text(template, encoding="utf-8")
            logger.log_structured("INFO", "templates",
                                 f"Registered: {name}")
        except Exception as e:
            logger.log_error("templates", f"Save failed: {name}", exc=e)

    def get_template_info(self, name: str) -> dict | None:
        """Get template info"""
        if name not in self.templates:
            return None

        template = self.templates[name]
        variables = re.findall(r'\{\{(\s*[\w|]+\s*)\}\}', template)

        return {
            "name": name,
            "builtin": name in BUILT_IN_TEMPLATES,
            "variables": list(set(v.strip() for v in variables)),
            "size": len(template)
        }


def get_templates() -> TemplateEngine:
    """Get singleton TemplateEngine"""
    return TemplateEngine()

