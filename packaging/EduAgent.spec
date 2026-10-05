# -*- mode: python ; coding: utf-8 -*-
"""T188 onedir bundle; configuration, weights and business files stay external."""
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
with_local = os.environ.get('EDUAGENT_BUILD_LOCAL_MODELS') == '1'
data = [(str(root / 'alembic.ini'), '.'), (str(root / 'migrations'), 'migrations')]
hidden = collect_submodules('backend')
for package in ('gradio', 'gradio_client', 'rapidocr', 'onnxruntime', 'pypdfium2', 'pypdfium2_raw', 'safehttpx', 'groovy'):
    # RapidOCR SDK includes default OCRv6 weights: omit every weight; use T155 external v5.
    data += [item for item in collect_data_files(package, include_py_files=(package == "gradio")) if Path(item[0]).suffix not in {'.onnx', '.pt', '.pth', '.safetensors'}]
for package in ('langgraph', 'langchain', 'langchain_core', 'langchain_openai', 'rapidocr', 'uvicorn'):
    hidden += collect_submodules(package)
for distribution in ('gradio', 'gradio-client', 'rapidocr', 'onnxruntime', 'langchain', 'langchain-core', 'langchain-openai', 'langgraph', 'langgraph-checkpoint', 'langgraph-prebuilt', 'openai', 'pydantic', 'psycopg', 'pypdfium2'):
    data += copy_metadata(distribution)
excludes = ['pytest', 'mypy', 'black', 'ruff', 'tensorflow', 'paddle', 'paddleocr']
if with_local:
    hidden += collect_submodules('sentence_transformers')
    data += collect_data_files('sentence_transformers') + copy_metadata('sentence-transformers')
else:
    excludes += ['torch', 'torchvision', 'torchaudio', 'sentence_transformers', 'transformers', 'scipy', 'matplotlib']
a = Analysis([str(root / 'scripts' / 'launch_windows.py')], pathex=[str(root)], binaries=[], datas=data, hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=excludes, noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='EduAgent', debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='EduAgent')
