#!/usr/bin/env bash
# Run: source .venv/bin/activate && bash check-env.sh
echo "python:  $(command -v python)"
python -V
echo "uvicorn: $(command -v uvicorn 2>/dev/null || echo '(none)')"
python -c "import sys; print('sys.executable:', sys.executable)"
python -c "import uvicorn; print('uvicorn from:', uvicorn.__file__)"
python -c "import pydantic; import pydantic._internal; print('pydantic', pydantic.__version__)"
