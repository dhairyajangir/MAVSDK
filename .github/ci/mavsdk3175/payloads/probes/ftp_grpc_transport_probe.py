"""Actual compiled mavsdk_server + temporary Python stubs + localhost FTP peer.

Python stubs are generated only inside --scratch; no checked-in C++ protobuf
source is written. This script never substitutes a backend or a service method.
"""
import argparse
import importlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import traceback

from ftp_wire_peer import LoopbackPeer, native_mappings, reserve_port, sha


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sdk-root', type=Path, required=True)
    parser.add_argument('--native-root', type=Path, required=True)
    parser.add_argument('--server', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.scratch.mkdir(parents=True, exist_ok=True)
    report = {'status': 'running', 'hardware': False, 'python': sys.version,
              'executable': sys.executable, 'transport': 'actual mavsdk_server/gRPC + localhost UDP',
              'cases': []}
    peer = None
    process = None
    channel = None
    server_log = None
    try:
        import grpc
        import grpc_tools
        proto = args.sdk_root.resolve() / 'proto/protos'
        temporary = Path(tempfile.mkdtemp(prefix='mavsdk3175-python-stubs-', dir=args.scratch.resolve()))
        command = [sys.executable, '-m', 'grpc_tools.protoc', '-I' + str(proto),
                   '-I' + str(Path(grpc_tools.__file__).resolve().parent / '_proto'),
                   '--python_out=' + str(temporary), '--grpc_python_out=' + str(temporary),
                   'mavsdk_options.proto', 'ftp/ftp.proto']
        assert not any('--cpp_out' in arg for arg in command)
        generated = subprocess.run(command, capture_output=True, text=True)
        generation_log = args.output.with_suffix('.stub-generation.log')
        generation_log.write_text(generated.stdout + generated.stderr)
        (temporary / 'generation.log').write_text(generated.stdout + generated.stderr)
        report['temporary_python_stub_generation'] = {'command': command,
            'exit_code': generated.returncode, 'path': str(temporary),
            'uploaded_log': str(generation_log),
            'checked_in_CPP_pb_written': False}
        assert generated.returncode == 0
        sys.path.insert(0, str(temporary))
        pb = importlib.import_module('ftp.ftp_pb2')
        services = importlib.import_module('ftp.ftp_pb2_grpc')
        report['stub_origins'] = {name: {'path': mod.__file__, 'sha256': sha(mod.__file__)}
                                  for name, mod in [('ftp_pb2', pb), ('ftp_pb2_grpc', services)]}
        assert pb.SERVER_ERRNO_CONVENTION_UNKNOWN == 0
        assert pb.SERVER_ERRNO_CONVENTION_UNSPECIFIED == 1
        assert pb.SERVER_ERRNO_CONVENTION_LINUX_GENERIC == 2
        assert pb.FtpResult.RESULT_DIRECTORY_NOT_EMPTY == 13
        server = args.server.resolve()
        assert server.is_file()
        udp_port = reserve_port(socket.SOCK_DGRAM)
        grpc_port = reserve_port(socket.SOCK_STREAM)
        peer = LoopbackPeer(udp_port).start()
        server_log_path = args.output.with_suffix('.server.log')
        server_log = server_log_path.open('w')
        argv = [str(server), '-p', str(grpc_port), f'udpin://127.0.0.1:{udp_port}']
        process = subprocess.Popen(argv, stdout=server_log, stderr=subprocess.STDOUT)
        report['server'] = {'command': argv, 'path': str(server), 'sha256': sha(server),
                            'pid': process.pid, 'log': str(server_log_path)}
        channel = grpc.insecure_channel(f'127.0.0.1:{grpc_port}')
        grpc.channel_ready_future(channel).result(timeout=20)
        assert process.poll() is None
        executable = Path(f'/proc/{process.pid}/exe').resolve()
        assert executable == server
        maps = native_mappings(process.pid)
        for row in maps:
            assert (Path(row['path']).is_relative_to(args.native_root.resolve())
                    or Path(row['path']).is_relative_to(args.sdk_root.resolve())), row
        report['server']['actual_native_mappings'] = maps
        report['server']['actual_executable'] = str(executable)
        stub = services.FtpServiceStub(channel)

        def configure(label, convention, expected, component=1, omitted=False):
            mark = peer.mark()
            request = pb.SetServerErrnoConventionRequest(component_id=component)
            if not omitted:
                request.convention = convention
            if convention == 999:
                assert int(request.convention) == 999
            response = stub.SetServerErrnoConvention(request, timeout=10)
            actual = int(response.ftp_result.result)
            report['cases'].append({'label': label, 'component_id': component,
                'convention_on_wire': int(request.convention), 'convention_omitted': omitted,
                'serialized_request_hex': request.SerializeToString().hex(),
                'expected': expected, 'actual': actual,
                'result_str': response.ftp_result.result_str,
                'FTP_requests_emitted': peer.mark() - mark})
            assert actual == expected and peer.mark() == mark, report['cases'][-1]

        def remove(label, path, expected):
            mark = peer.mark()
            request = pb.RemoveDirectoryRequest(remote_dir=path)
            response = stub.RemoveDirectory(request, timeout=10)
            actual = int(response.ftp_result.result)
            report['cases'].append({'label': label, 'path': path,
                'serialized_request_hex': request.SerializeToString().hex(),
                'expected': expected, 'actual': actual, 'result_str': response.ftp_result.result_str})
            assert actual == expected, report['cases'][-1]
            report['cases'][-1]['wire'] = peer.assert_case_wire(mark, path)

        remove('default-nonempty', '/nonempty', 11)
        configure('explicit-linux-component1', 2, 1)
        remove('explicit-profile-nonempty', '/nonempty', 13)
        configure('raw-unknown-enum999', 999, 9)
        remove('raw-unknown-did-not-clear-profile', '/nonempty', 13)
        configure('omitted-default-unknown', 0, 9, omitted=True)
        remove('omitted-unknown-did-not-clear-profile', '/nonempty', 13)
        configure('broadcast-component-invalid', 2, 9, component=0)
        configure('wide-component-invalid', 2, 9, component=256)
        remove('invalid-components-did-not-clear-profile', '/nonempty', 13)
        for name, path, expected in (('ack', '/empty', 1), ('missing-file', '/missing', 7),
                                     ('generic-nak', '/generic', 11), ('empty-nak', '/empty-nak', 11),
                                     ('short-failerrno', '/short-errno', 11),
                                     ('other-namespace-number66', '/other-errno', 11)):
            remove(name, path, expected)
        configure('explicit-clear', 1, 1)
        remove('cleared-profile-nonempty', '/nonempty', 11)
        assert process.poll() is None, 'Compiled server exited before probe cleanup'
        report['server']['alive_after_final_RPC'] = True
        report['status'] = 'passed'
    except BaseException as error:
        report.update(status='failed', error_class=type(error).__name__, error=str(error),
                      traceback=traceback.format_exc())
        raise
    finally:
        if channel is not None:
            channel.close()
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            report.setdefault('server', {})['exit_after_probe_cleanup'] = process.returncode
        if server_log is not None:
            server_log.close()
        if peer is not None:
            report['wire'] = peer.snapshot()
            try:
                peer.close()
            except BaseException as error:
                report.update(status='failed', peer_cleanup_error=str(error))
                args.output.write_text(json.dumps(report, indent=2) + '\n')
                raise
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'cases': len(report['cases']),
                      'report': str(args.output)}), flush=True)


if __name__ == '__main__':
    main()
