"""Exercise actual generated sync/asyncio FTP bindings through compiled libcmavsdk.

Requires candidate Python package staged exactly as upstream py/test CI, plus
pymavlink. No backend is mocked. All MAVLink traffic stays on localhost UDP.
"""
import argparse
import asyncio
import importlib
import json
from pathlib import Path
import socket
import sys
import traceback

from ftp_wire_peer import LoopbackPeer, module_origins, native_mappings, reserve_port, sha

EXPECTED_OLD = {'UNKNOWN': 0, 'SUCCESS': 1, 'NEXT': 2, 'TIMEOUT': 3, 'BUSY': 4,
                'FILE_IO_ERROR': 5, 'FILE_EXISTS': 6, 'FILE_DOES_NOT_EXIST': 7,
                'FILE_PROTECTED': 8, 'INVALID_PARAMETER': 9, 'UNSUPPORTED': 10,
                'PROTOCOL_ERROR': 11, 'NO_SYSTEM': 12}


def assert_native_origins(sdk_root, native_root):
    loader = importlib.import_module('mavsdk.cmavsdk_loader')
    library = Path(loader._cmavsdk_lib._name).resolve()
    assert library.is_relative_to((sdk_root / 'py/mavsdk/mavsdk/lib').resolve()), library
    mappings = native_mappings()
    assert any('libcmavsdk' in x['path'] for x in mappings), mappings
    for entry in mappings:
        assert (Path(entry['path']).is_relative_to(native_root)
                or Path(entry['path']).is_relative_to(sdk_root)), entry
    return {'ctypes_library': {'path': str(library), 'sha256': sha(library)},
            'actual_native_mappings': mappings}


def check_runtime_enums():
    ftp = importlib.import_module('mavsdk.plugins.ftp')
    result = ftp.FtpResult
    assert {x.name: x.value for x in result} == dict(EXPECTED_OLD, DIRECTORY_NOT_EMPTY=13)
    convention = ftp.ServerErrnoConvention
    assert {x.name: x.value for x in convention} == {'UNKNOWN': 0, 'UNSPECIFIED': 1, 'LINUX_GENERIC': 2}
    return {'actual_result_values': {x.name: x.value for x in result},
            'actual_convention_values': {x.name: x.value for x in convention},
            'old_result_numbers0_12_stable': True}


def sync_scenario(state):
    from mavsdk import ComponentType, Configuration, ConnectionResult, Mavsdk
    from mavsdk.plugins.ftp import Ftp, FtpError, FtpResult, ServerErrnoConvention

    config = Configuration.create_with_component_type(ComponentType.GROUND_STATION)
    sdk = Mavsdk(config)
    peer = None
    plugin = None
    state['cases'] = []
    try:
        port = reserve_port(socket.SOCK_DGRAM)
        assert sdk.add_any_connection(f'udpin://127.0.0.1:{port}') == ConnectionResult.SUCCESS
        peer = LoopbackPeer(port).start()
        system = sdk.first_autopilot(10.0)
        assert system is not None, 'Actual native client did not discover synthetic autopilot'
        plugin = Ftp(system)
        assert plugin.set_target_compid(1) == FtpResult.SUCCESS
        state['native_version'] = sdk.version()

        def remove(label, path, expected):
            mark = peer.mark()
            try:
                result = plugin.remove_directory(path)
            except FtpError as error:
                result = error.result
            actual = int(result)
            state['cases'].append({'label': label, 'path': path, 'expected': expected,
                                   'actual': actual, 'result_name': result.name})
            assert actual == expected, state['cases'][-1]
            state['cases'][-1]['wire'] = peer.assert_case_wire(mark, path)

        def configure(label, component, convention, expected):
            mark = peer.mark()
            try:
                result = plugin.set_server_errno_convention(component, convention)
            except FtpError as error:
                result = error.result
            state['cases'].append({'label': label, 'component_id': component,
                'convention': int(convention), 'expected': expected, 'actual': int(result),
                'result_name': result.name, 'FTP_requests_emitted': peer.mark() - mark})
            assert int(result) == expected and peer.mark() == mark, state['cases'][-1]

        remove('default-nonempty', '/nonempty', 11)
        remove('default-ack', '/empty', 1)
        remove('default-missing-file', '/missing', 7)
        configure('other-component-only', 2, ServerErrnoConvention.LINUX_GENERIC, 1)
        remove('component1-still-unspecified', '/nonempty', 11)
        configure('explicit-linux-component1', 1, ServerErrnoConvention.LINUX_GENERIC, 1)
        remove('explicit-profile-nonempty', '/nonempty', 13)
        for name, path, expected in (('profile-ack', '/empty', 1), ('profile-missing-file', '/missing', 7),
                                     ('generic-nak', '/generic', 11), ('empty-nak', '/empty-nak', 11),
                                     ('short-failerrno', '/short-errno', 11),
                                     ('other-namespace-number66', '/other-errno', 11)):
            remove(name, path, expected)
        configure('unknown-rejected', 1, ServerErrnoConvention.UNKNOWN, 9)
        remove('unknown-did-not-clear-profile', '/nonempty', 13)
        configure('broadcast-component-rejected', 0, ServerErrnoConvention.LINUX_GENERIC, 9)
        configure('wide-component-rejected', 256, ServerErrnoConvention.LINUX_GENERIC, 9)
        remove('invalid-components-did-not-clear-profile', '/nonempty', 13)
        configure('explicit-clear', 1, ServerErrnoConvention.UNSPECIFIED, 1)
        remove('cleared-profile-nonempty', '/nonempty', 11)
        state['status'] = 'passed'
    finally:
        try:
            if plugin is not None:
                plugin.destroy()
            sdk.destroy()
            config.destroy()
        finally:
            if peer is not None:
                state['wire'] = peer.snapshot()
                peer.close()


async def asyncio_scenario(state):
    from mavsdk.asyncio import ComponentType, Configuration, Mavsdk
    from mavsdk.asyncio.plugins.ftp import FtpAsync
    from mavsdk.plugins.ftp import FtpError, ServerErrnoConvention

    config = Configuration.create_with_component_type(ComponentType.GROUND_STATION)
    sdk = Mavsdk(config)
    peer = None
    plugin = None
    state['cases'] = []
    try:
        port = reserve_port(socket.SOCK_DGRAM)
        connection_handle = await sdk.add_any_connection(f'udpin://127.0.0.1:{port}')
        state['connection_handle_repr'] = repr(connection_handle)
        peer = LoopbackPeer(port).start()
        system = await sdk.first_autopilot(10.0)
        assert system is not None, 'Actual asyncio native client did not discover autopilot'
        plugin = FtpAsync(system)
        assert int(await plugin.set_target_compid(1)) == 1
        state['native_version'] = await sdk.version()

        async def remove(label, path, expected):
            mark = peer.mark()
            try:
                result = await asyncio.wait_for(plugin.remove_directory(path), timeout=10.0)
            except FtpError as error:
                result = error.result
            state['cases'].append({'label': label, 'path': path, 'expected': expected,
                                   'actual': int(result), 'result_name': result.name})
            assert int(result) == expected, state['cases'][-1]
            state['cases'][-1]['wire'] = peer.assert_case_wire(mark, path)

        async def configure(label, component, convention, expected):
            mark = peer.mark()
            try:
                result = await asyncio.wait_for(
                    plugin.set_server_errno_convention(component, convention), timeout=10.0)
            except FtpError as error:
                result = error.result
            state['cases'].append({'label': label, 'component_id': component,
                'convention': int(convention), 'expected': expected, 'actual': int(result),
                'result_name': result.name, 'FTP_requests_emitted': peer.mark() - mark})
            assert int(result) == expected and peer.mark() == mark, state['cases'][-1]

        await remove('default-nonempty', '/nonempty', 11)
        await remove('default-ack', '/empty', 1)
        await remove('default-missing-file', '/missing', 7)
        await configure('other-component-only', 2, ServerErrnoConvention.LINUX_GENERIC, 1)
        await remove('component1-still-unspecified', '/nonempty', 11)
        await configure('explicit-linux-component1', 1, ServerErrnoConvention.LINUX_GENERIC, 1)
        await remove('explicit-profile-nonempty', '/nonempty', 13)
        for name, path, expected in (('profile-ack', '/empty', 1), ('profile-missing-file', '/missing', 7),
                                     ('generic-nak', '/generic', 11), ('empty-nak', '/empty-nak', 11),
                                     ('short-failerrno', '/short-errno', 11),
                                     ('other-namespace-number66', '/other-errno', 11)):
            await remove(name, path, expected)
        await configure('unknown-rejected', 1, ServerErrnoConvention.UNKNOWN, 9)
        await remove('unknown-did-not-clear-profile', '/nonempty', 13)
        await configure('broadcast-component-rejected', 0, ServerErrnoConvention.LINUX_GENERIC, 9)
        await configure('wide-component-rejected', 256, ServerErrnoConvention.LINUX_GENERIC, 9)
        await remove('invalid-components-did-not-clear-profile', '/nonempty', 13)
        await configure('explicit-clear', 1, ServerErrnoConvention.UNSPECIFIED, 1)
        await remove('cleared-profile-nonempty', '/nonempty', 11)
        state['status'] = 'passed'
    finally:
        try:
            if plugin is not None:
                plugin.destroy()
            sdk.destroy()
            config.destroy()
        finally:
            if peer is not None:
                state['wire'] = peer.snapshot()
                peer.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sdk-root', type=Path, required=True)
    parser.add_argument('--native-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--flavour', choices=('both', 'sync', 'asyncio'), default='both')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {'status': 'running', 'hardware': False, 'transport': 'actual libcmavsdk + localhost UDP',
              'python': sys.version, 'executable': sys.executable, 'flavours': {}}
    try:
        names = ['mavsdk', 'mavsdk.cmavsdk_loader', 'mavsdk.plugins.ftp.ftp',
                 'mavsdk.asyncio', 'mavsdk.asyncio.plugins.ftp.ftp']
        report['module_origins'] = module_origins(names, args.sdk_root.resolve())
        report['native_origins'] = assert_native_origins(args.sdk_root.resolve(), args.native_root.resolve())
        report['runtime_enum_contract'] = check_runtime_enums()
        if args.flavour in ('both', 'sync'):
            report['flavours']['sync'] = {}
            sync_scenario(report['flavours']['sync'])
        if args.flavour in ('both', 'asyncio'):
            report['flavours']['asyncio'] = {}
            asyncio.run(asyncio_scenario(report['flavours']['asyncio']))
        report['status'] = 'passed'
    except BaseException as error:
        report.update(status='failed', error_class=type(error).__name__, error=str(error),
                      traceback=traceback.format_exc())
        raise
    finally:
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'flavours': list(report['flavours']),
                      'report': str(args.output)}), flush=True)


if __name__ == '__main__':
    main()
