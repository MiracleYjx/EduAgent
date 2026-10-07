from pathlib import Path
import ast,dis,json,types,hashlib
from PyInstaller.archive.readers import CArchiveReader
root=Path.cwd();package=root/'.cache/t189-optimize-20261007/build-api/dist/EduAgent';out=root/'benchmark/results/v2/t189-optimize-20261007'
archive=CArchiveReader(str(package/'EduAgent.exe')).open_embedded_archive('PYZ.pyz')
module=archive.extract('gradio.component_meta')
writer=next(c for c in module.co_consts if isinstance(c,types.CodeType) and c.co_name=='create_or_modify_pyi')
assert writer.co_consts==(None,) and not writer.co_names
original=Path('.cache/t189-t191-20261006/local-build/runtime/Lib/site-packages/gradio/component_meta.py')
frozen=root/'.cache/t189-optimize-20261007/build-api/work/EduAgent/gradio-runtime/component_meta.py'
a=ast.parse(original.read_text(encoding='utf-8'));b=ast.parse(frozen.read_text(encoding='utf-8'))
for node in a.body:
 if isinstance(node,ast.FunctionDef) and node.name=='create_or_modify_pyi':node.body=[ast.Return(value=ast.Constant(value=None))]
assert ast.dump(a)==ast.dump(b)
receipt=json.loads((package/'build-receipt.json').read_text(encoding='utf-8'))
assert receipt['executable_sha256']==hashlib.sha256((package/'EduAgent.exe').read_bytes()).hexdigest()
(out/'final-frozen-module-inspection.json').write_text(json.dumps({'actual_exe_sha256':receipt['executable_sha256'],'module':'gradio.component_meta','actual_function_disassembly':dis.Bytecode(writer).dis(),'all_other_ast_unchanged':True,'source_library_sha256':hashlib.sha256(original.read_bytes()).hexdigest(),'generated_module_sha256':hashlib.sha256(frozen.read_bytes()).hexdigest(),'meaning':'Only the IDE type-hint writer is disabled; no runtime API/validation replacement'},indent=2),encoding='utf-8')
print('actual frozen module inspected; other AST unchanged')

api_module=archive.extract('gradio.blocks')
api_class=next(c for c in api_module.co_consts if isinstance(c,types.CodeType) and c.co_name=='Blocks')
api_method=next(c for c in api_class.co_consts if isinstance(c,types.CodeType) and c.co_name=='get_api_info')
api_path=root/'.cache/t189-optimize-20261007/build-api/work/EduAgent/gradio-runtime/blocks.py'
expected=compile(api_path.read_text(encoding='utf-8'),str(api_path),'exec')
expected_class=next(c for c in expected.co_consts if isinstance(c,types.CodeType) and c.co_name=='Blocks')
expected_method=next(c for c in expected_class.co_consts if isinstance(c,types.CodeType) and c.co_name=='get_api_info')
assert (api_method.co_code,api_method.co_names,api_method.co_varnames,api_method.co_consts)==(expected_method.co_code,expected_method.co_names,expected_method.co_varnames,expected_method.co_consts)
assert '_frozen_components_by_id' in api_method.co_varnames
src=ast.parse(original.with_name('blocks.py').read_text(encoding='utf-8'));gen=ast.parse(api_path.read_text(encoding='utf-8'))
def method(t):
 c=next(n for n in t.body if isinstance(n,ast.ClassDef) and n.name=='Blocks')
 return next(n for n in c.body if isinstance(n,ast.FunctionDef) and n.name=='get_api_info')
method(src).body=method(gen).body
assert ast.dump(src)==ast.dump(gen)
p=out/'final-frozen-module-inspection.json';data=json.loads(p.read_text(encoding='utf-8'));data.update(api_lookup_actual_compiled_body_matches=True,api_module_other_ast_unchanged=True,api_method_bytecode_sha256=hashlib.sha256(api_method.co_code).hexdigest(),meaning='IDE stub writer disabled and API component lookups indexed; all other module AST unchanged')
p.write_text(json.dumps(data,indent=2),encoding='utf-8')
print('actual indexed API method inspected')
