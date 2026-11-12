"""Execute literal generated enum definitions and inspect actual wrapper API shapes.

Asyncio uses FtpAsync and imports the same enums from the synchronous binding.
No native loader, callback, gRPC or Kotlin runtime is replaced or simulated.
"""
import ast
from enum import IntEnum
import hashlib
import json
from pathlib import Path
import sys


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def enum_from_source(path, name):
    tree = ast.parse(path.read_text(), filename=str(path))
    node = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == name)
    assert len(node.bases) == 1 and isinstance(node.bases[0], ast.Name) and node.bases[0].id == 'IntEnum'
    for entry in node.body:
        assert (isinstance(entry, ast.Expr) and isinstance(entry.value, ast.Constant)
                and isinstance(entry.value.value, str)) or (
            isinstance(entry, ast.Assign) and all(isinstance(t, ast.Name) for t in entry.targets)
            and isinstance(entry.value, ast.Constant) and type(entry.value.value) is int)
    namespace = {'IntEnum': IntEnum, '__name__': 'actual_generated_enum_probe'}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


def probe(baseline, candidate):
    old_path = baseline / 'py/mavsdk/mavsdk/plugins/ftp/ftp.py'
    new_path = candidate / 'py/mavsdk/mavsdk/plugins/ftp/ftp.py'
    old = enum_from_source(old_path, 'FtpResult')
    new = enum_from_source(new_path, 'FtpResult')
    convention = enum_from_source(new_path, 'ServerErrnoConvention')
    expected = {'UNKNOWN': 0, 'SUCCESS': 1, 'NEXT': 2, 'TIMEOUT': 3, 'BUSY': 4,
                'FILE_IO_ERROR': 5, 'FILE_EXISTS': 6, 'FILE_DOES_NOT_EXIST': 7,
                'FILE_PROTECTED': 8, 'INVALID_PARAMETER': 9, 'UNSUPPORTED': 10,
                'PROTOCOL_ERROR': 11, 'NO_SYSTEM': 12}
    assert {x.name: x.value for x in old} == expected
    assert {x.name: x.value for x in new} == dict(expected, DIRECTORY_NOT_EMPTY=13)
    assert {x.name: x.value for x in convention} == {'UNKNOWN': 0, 'UNSPECIFIED': 1, 'LINUX_GENERIC': 2}
    try:
        old(13)
    except ValueError:
        old13 = 'ValueError'
    else:
        raise AssertionError('Old result enum unexpectedly accepted13')
    assert new(13).name == 'DIRECTORY_NOT_EMPTY'
    for number in expected.values():
        assert old(number).name == new(number).name
    report = {'scope': 'Actual literal enum execution and wrapper AST; no native-backend substitution.',
              'baseline_sync_enum_file': str(old_path), 'baseline_sha256': sha(old_path),
              'candidate_sync_enum_file': str(new_path), 'candidate_sha256': sha(new_path),
              'old_result_numbers0_12_stable': True, 'old_frontend_new13': old13,
              'new_frontend_new13': new(13).name, 'convention_values': {x.name: x.value for x in convention},
              'compatibility_limit': 'Old Python enum rejects13; default unspecified behaviour and matched bindings are separate guarantees.',
              'wrappers': {}}
    for flavour, relative, cls in (
        ('sync', 'py/mavsdk/mavsdk/plugins/ftp/ftp.py', 'Ftp'),
        ('asyncio', 'py/mavsdk/mavsdk/asyncio/plugins/ftp/ftp.py', 'FtpAsync')):
        path = candidate / relative
        tree = ast.parse(path.read_text(), filename=str(path))
        plugin = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == cls)
        setter = next(x for x in plugin.body if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and x.name == 'set_server_errno_convention')
        assert [x.arg for x in setter.args.args] == ['self', 'component_id', 'convention']
        if flavour == 'asyncio':
            assert isinstance(setter, ast.AsyncFunctionDef)
            imports = [x for x in tree.body if isinstance(x, ast.ImportFrom) and x.module == 'mavsdk.plugins.ftp']
            imported = {n.name for x in imports for n in x.names}
            assert {'Ftp', 'FtpResult', 'ServerErrnoConvention'} <= imported
        report['wrappers'][flavour] = {'file': str(path), 'sha256': sha(path), 'class': cls,
            'setter_arguments': [x.arg for x in setter.args.args], 'setter_node': type(setter).__name__,
            'shares_sync_enum': flavour == 'asyncio'}
    return report


if __name__ == '__main__':
    assert len(sys.argv) in (3, 4), 'usage: probe BASELINE_SDK CANDIDATE_SDK [REPORT_JSON]'
    result = probe(Path(sys.argv[1]), Path(sys.argv[2]))
    text = json.dumps(result, indent=2) + '\n'
    if len(sys.argv) == 4:
        Path(sys.argv[3]).write_text(text)
    print(text, end='')
