"""Verify one exact known_hosts entry for the deployment host and port."""
from __future__ import annotations

import argparse
import os
import stat
import subprocess
from pathlib import Path


def verify(path: Path, host: str, port: int, expected: str) -> None:
    if (
        not host
        or any(char.isspace() for char in host)
        or not 1 <= port <= 65535
        or not expected.startswith("SHA256:")
    ):
        raise ValueError("host_key_authority_invalid")
    before = os.lstat(path)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) & 0o077
    ):
        raise ValueError("known_hosts_file_not_private_regular_single_link")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValueError("known_hosts_identity_changed")
        chunks = bytearray()
        while len(chunks) <= 64 * 1024:
            block = os.read(fd, min(4096, 64 * 1024 + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        if len(chunks) > 64 * 1024:
            raise ValueError("known_hosts_too_large")
        lines = [
            line.strip() for line in chunks.decode("utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if len(lines) != 1:
            raise ValueError("known_hosts_must_contain_exactly_one_entry")
        fields = lines[0].split()
        if len(fields) != 3 or "," in fields[0] or fields[0].startswith("|"):
            raise ValueError("known_hosts_entry_not_exact")
        expected_host = host if port == 22 else f"[{host}]:{port}"
        if fields[0] != expected_host:
            raise ValueError("known_hosts_target_mismatch")
        result = subprocess.run(
            ["ssh-keygen", "-lf", f"/proc/self/fd/{fd}", "-E", "sha256"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            pass_fds=(fd,),
        )
        fingerprints = [
            line.split()[1] for line in result.stdout.splitlines() if line.strip()
        ]
        after_fd = os.fstat(fd)
        after_path = os.lstat(path)
        os.lseek(fd, 0, os.SEEK_SET)
        final_chunks = bytearray()
        while len(final_chunks) <= 64 * 1024:
            block = os.read(fd, min(4096, 64 * 1024 + 1 - len(final_chunks)))
            if not block:
                break
            final_chunks.extend(block)
        if (
            (after_fd.st_dev, after_fd.st_ino) != (before.st_dev, before.st_ino)
            or (after_path.st_dev, after_path.st_ino)
            != (before.st_dev, before.st_ino)
            or after_fd.st_nlink != 1
            or after_path.st_nlink != 1
            or after_fd.st_size != before.st_size
            or after_fd.st_mtime_ns != before.st_mtime_ns
            or after_fd.st_ctime_ns != before.st_ctime_ns
            or bytes(final_chunks) != bytes(chunks)
        ):
            raise ValueError("known_hosts_identity_changed")
        if fingerprints != [expected]:
            raise ValueError("known_hosts_fingerprint_mismatch")
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--expected-fingerprint", required=True)
    args = parser.parse_args(argv)
    verify(args.known_hosts, args.host, args.port, args.expected_fingerprint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
