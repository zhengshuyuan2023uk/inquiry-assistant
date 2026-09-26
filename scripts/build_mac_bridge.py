#!/usr/bin/env python3
"""Build the pinned WhatsApp bridge from the repository's source-only snapshot.

The source snapshot contains no account or message data. Its imported inputs are
checked against PROVENANCE.json. Corresponding vendor source and licenses ship
with the binary; third-party dependencies retain their individual licenses.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / 'third_party/whatsapp-mcp'
SOURCE_FILES = ('main.go', 'go.mod', 'go.sum')


def run(args, cwd=None, env=None):
    result = subprocess.run(args, cwd=cwd, env=env, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def source_provenance(source):
    source = Path(source)
    provenance_file = source / 'PROVENANCE.json'
    if provenance_file.is_symlink() or not provenance_file.is_file():
        raise ValueError('Missing regular source provenance: PROVENANCE.json')
    provenance = json.loads(provenance_file.read_text())
    if not provenance.get('upstream') or not provenance.get('upstream_commit'):
        raise ValueError('Provenance must identify the upstream and its base commit')
    hashes = provenance.get('source_input_sha256', {})
    for name in SOURCE_FILES:
        path = source / 'whatsapp-bridge' / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Missing regular build source: {name}')
        if hashlib.sha256(path.read_bytes()).hexdigest() != hashes.get(name):
            raise ValueError(f'Source provenance hash mismatch: {name}')
    license_file = source / 'LICENSE'
    if license_file.is_symlink() or not license_file.is_file():
        raise ValueError('Missing regular upstream LICENSE')
    if hashlib.sha256(license_file.read_bytes()).hexdigest() != provenance.get('license_sha256'):
        raise ValueError('Source provenance hash mismatch: LICENSE')
    return provenance


def prepare_source(source, destination):
    source = Path(source)
    source_provenance(source)
    bridge = source / 'whatsapp-bridge'
    destination.mkdir(parents=True)
    for name in SOURCE_FILES:
        path = bridge / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Missing regular build source: {name}')
        shutil.copyfile(path, destination / name)
    shutil.copyfile(source / 'LICENSE', destination / 'LICENSE')
    shutil.copyfile(source / 'PROVENANCE.json', destination / 'PROVENANCE.json')
    if (ROOT / 'LICENSE').is_file():
        shutil.copyfile(ROOT / 'LICENSE', destination / 'InquiryAssistant-Bridge-MIT.txt')
    code = (destination / 'main.go').read_text()
    start = code.index('func startRESTServer(')
    end = code.index('// GetChatName determines', start)
    # Remove both the upstream REST handler registrations and original main.
    code = code[:start] + '// Product main lives in bridge_main.go; REST send/download removed.\n\n' + code[end:]
    for imported in ('encoding/json', 'net/http', 'os/signal', 'syscall',
                     'github.com/mdp/qrterminal', 'rsc.io/qr', 'go.mau.fi/whatsmeow/store/sqlstore'):
        code = code.replace(f'\t"{imported}"\n', '')
    (destination / 'main.go').write_text(code)
    for name in ('bridge_main.go', 'bridge_main_test.go'):
        shutil.copyfile(ROOT / 'deploy/macos' / name, destination / name)
    (destination / 'INQUIRY-MODIFICATIONS.md').write_text(
        '# Inquiry Assistant packaged bridge\n\n'
        'Based on https://github.com/lharries/whatsapp-mcp; its MIT license is retained in LICENSE.\n'
        'The imported snapshot includes local compatibility changes and pinned newer WhatsMeow dependencies.\n'
        'PROVENANCE.json records the upstream base commit and exact imported input hashes;\n'
        'it does not claim these inputs are unchanged files from that commit.\n'
        'Inquiry Assistant additions use the project MIT license, included separately when available.\n'
        'The original REST API and main function have been removed from main.go.\n'
        'bridge_main.go starts a receive-only application: it stores incoming messages,\n'
        'shows pairing QR locally, and exposes authenticated loopback status/shutdown only.\n'
        'No send or download HTTP endpoints; no MCP server needed.\n'
        'Dependencies and their corresponding source/licenses are in vendor/.\n'
        'They include GPL-3.0 and MPL-2.0 components as well as permissively licensed packages.\n'
        'This is a mixed-license build; the whole executable is not described as MIT-only.\n'
        'To rebuild on Apple Silicon with Go and Xcode Command Line Tools:\n\n'
        '    MACOSX_DEPLOYMENT_TARGET=14.0 CGO_ENABLED=1 GOOS=darwin GOARCH=arm64 '
        'CGO_CFLAGS=-mmacosx-version-min=14.0 CGO_LDFLAGS=-mmacosx-version-min=14.0 '
        'go build -mod=vendor -trimpath -ldflags="-s -w" -o whatsapp-bridge .\n'
        '\nDo not run an unconfigured binary; use the Inquiry Assistant launcher.\n')


def build(source, output):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise ValueError('Build this CGO-enabled artifact on an Apple Silicon Mac')
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Output must be a new directory; existing files are never overwritten')
    go = shutil.which('go')
    if not go:
        raise ValueError('Go is needed only on the build machine')
    provenance = source_provenance(source)
    output.mkdir(parents=True)
    for name in ('bin', 'source', 'LICENSES'):
        (output / name).mkdir()
    with tempfile.TemporaryDirectory(prefix='inquiry-bridge-build-') as temporary:
        prepared = Path(temporary) / 'whatsapp-bridge'
        prepare_source(source, prepared)
        env = os.environ.copy()
        env.update(GOOS='darwin', GOARCH='arm64', CGO_ENABLED='1', MACOSX_DEPLOYMENT_TARGET='14.0',
                   CGO_CFLAGS='-mmacosx-version-min=14.0', CGO_LDFLAGS='-mmacosx-version-min=14.0')
        run([go, 'fmt', './...'], cwd=prepared, env=env)
        run([go, 'mod', 'vendor'], cwd=prepared, env=env)
        test_env = dict(env, GOPROXY='off', GOSUMDB='off')
        test_output = run([go, 'test', '-mod=vendor', './...'], cwd=prepared, env=test_env)
        binary = output / 'bin/whatsapp-bridge'
        run([go, 'build', '-mod=vendor', '-trimpath', '-ldflags=-s -w', '-o', str(binary), '.'], cwd=prepared, env=test_env)
        version = run([str(binary), '--version'])  # Does not create a DB or contact WhatsApp.
        modules = (prepared / 'vendor/modules.txt').read_text()
        shutil.copyfile(prepared / 'LICENSE', output / 'LICENSES/whatsapp-mcp-MIT.txt')
        if (prepared / 'InquiryAssistant-Bridge-MIT.txt').is_file():
            shutil.copyfile(prepared / 'InquiryAssistant-Bridge-MIT.txt',
                            output / 'LICENSES/InquiryAssistant-Bridge-MIT.txt')
        # Keep dependency licenses at their module-relative names. Full corresponding
        # source is included, including MPL-governed packages, not merely URLs.
        for path in (prepared / 'vendor').rglob('*'):
            if path.is_file() and (path.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE'))):
                target = output / 'LICENSES/vendor' / path.relative_to(prepared / 'vendor')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        (output / 'LICENSES/dependencies.txt').write_text(modules + '\n')
        go_root = Path(run([go, 'env', 'GOROOT']))
        for name in ('LICENSE', 'PATENTS'):
            if (go_root / name).is_file():
                shutil.copyfile(go_root / name, output / 'LICENSES' / ('Go-' + name + '.txt'))
        with tarfile.open(output / 'source/whatsapp-bridge-source.tar.gz', 'w:gz') as archive:
            archive.add(prepared, arcname='whatsapp-bridge')
        metadata = dict(upstream=provenance['upstream'], upstream_commit=provenance['upstream_commit'],
                        source_description=provenance.get('source_description', ''),
                        source_input_sha256=provenance['source_input_sha256'],
                        upstream_license_sha256=provenance['license_sha256'],
                        platform='darwin-arm64', macos_minimum='14.0',
                        go_version=run([go, 'version']), version=version,
                        binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                        source_sha256=hashlib.sha256((output / 'source/whatsapp-bridge-source.tar.gz').read_bytes()).hexdigest(),
                        tests=test_output)
        (output / 'bridge-build.json').write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + '\n')
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=UPSTREAM)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output), indent=2, ensure_ascii=False))

if __name__ == '__main__':
    main()
