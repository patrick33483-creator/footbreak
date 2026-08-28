"""Safe publication boundary for remote legacy-batch discovery receipts."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import tempfile
import stat
import subprocess
import signal
import time
from pathlib import Path
from typing import Any

from .legacy_batch_aggregate import (
    parse_json_bytes_v1, serialize_ledger_bytes_v1,
    validate_live_discovery, validate_sanitized_calculation,
)
from .export_legacy_batch_live_authority import (
    CAPTURE_ENVELOPE_KEYS, CAPTURE_ENVELOPE_SCHEMA,
    MAX_CAPTURE_ENVELOPE_BYTES, MAX_DISCOVERY_CAPTURE_BYTES,
    MAX_LEDGER_CAPTURE_BYTES,
)

INVALID_SCHEMA = "footbreak-legacy-batch-live-discovery-invalid-v1"
INVALID_KEYS = {
    "schema", "valid", "audited_commit", "exporter_exit_code",
    "failure_classification", "production_mutation",
}
APPROVED_FAILURES = {
    "remote_discovery_failed_closed", "receipt_validation_failed_closed",
    "discovery_commit_mismatch", "discovery_quiescence_missing",
    "discovery_lock_path_mismatch", "discovery_ledger_path_mismatch",
    "whole_workflow_gate_failed_closed",
}
Identity = tuple[int, int]


class CapturedSignal(Exception):
    def __init__(self, signum: int):
        super().__init__(f"capture_signal:{signum}")
        self.signum = signum


def reserve_private_file(path: Path) -> Identity:
    """Exclusively reserve a mode-0600 regular single-link staging object."""
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ValueError("unsafe_reserved_file")
        return opened.st_dev, opened.st_ino
    finally:
        os.close(descriptor)


def write_known_hosts_pin(host: str, value: str, output: Path) -> None:
    """Validate one canonical production host-key record and write mode 0600."""
    if (
        not host or "\n" in value or "\r" in value
        or value != value.strip() or "\t" in value
    ):
        raise ValueError("invalid_known_hosts_pin")
    parts = value.split(" ")
    if (
        len(parts) != 3 or any(not part for part in parts)
        or parts[0] != host
        or parts[1] not in {
            "ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa",
        }
    ):
        raise ValueError("invalid_known_hosts_pin")
    try:
        base64.b64decode(parts[2], validate=True)
    except Exception as exc:
        raise ValueError("invalid_known_hosts_pin") from exc
    _atomic_write_bytes(output, (value + "\n").encode("ascii"))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    descriptor, name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(name)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ValueError("unsafe_atomic_temporary")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if _read_verified(path) != data:
            raise ValueError("atomic_write_readback_mismatch")
    finally:
        if temporary.exists():
            temporary.unlink()


def _open_verified(
    path: Path, flags: int, expected: Identity | None = None,
) -> int:
    descriptor = os.open(path, flags | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        by_path = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino)
            != (by_path.st_dev, by_path.st_ino)
            or (
                expected is not None
                and (opened.st_dev, opened.st_ino) != expected
            )
        ):
            raise ValueError("unsafe_file_identity")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_verified(
    path: Path, expected: Identity | None = None, maximum: int | None = None,
) -> bytes:
    descriptor = _open_verified(path, os.O_RDONLY, expected)
    try:
        size = os.fstat(descriptor).st_size
        if maximum is not None and size > maximum:
            raise ValueError("private_file_exceeds_bound")
        chunks = []
        remaining = size + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _verified_identity(path: Path) -> Identity:
    descriptor = _open_verified(path, os.O_RDONLY)
    try:
        opened = os.fstat(descriptor)
        return opened.st_dev, opened.st_ino
    finally:
        os.close(descriptor)


def _decode_envelope_payload(
    document: dict[str, Any], prefix: str, maximum: int,
) -> bytes:
    encoded = document.get(prefix + "_base64")
    length = document.get(prefix + "_length")
    digest = document.get(prefix + "_sha256")
    if (
        not isinstance(encoded, str)
        or not isinstance(length, int) or isinstance(length, bool)
        or length < 1 or length > maximum
        or not isinstance(digest, str) or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("capture_envelope_payload_metadata_invalid")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("capture_envelope_base64_invalid") from exc
    if len(raw) != length:
        raise ValueError("capture_envelope_length_mismatch")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("capture_envelope_hash_mismatch")
    return raw


class _RetainedPrivateFile:
    """A private file and parent directory held until commit or destruction."""

    def __init__(
        self, path: Path, descriptor: int, parent_descriptor: int,
        identity: Identity,
    ) -> None:
        self.path = path
        self.name = path.name
        self.descriptor = descriptor
        self.parent_descriptor = parent_descriptor
        self.identity = identity
        self.closed = False

    @classmethod
    def _parent(cls, path: Path) -> int:
        descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()
        ):
            os.close(descriptor)
            raise ValueError("unsafe_private_capture_directory")
        return descriptor

    @classmethod
    def open_existing(
        cls, path: Path, expected: Identity | None,
    ) -> "_RetainedPrivateFile":
        parent_descriptor = cls._parent(path)
        try:
            descriptor = os.open(
                path.name,
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
        except Exception:
            os.close(parent_descriptor)
            raise
        try:
            opened = os.fstat(descriptor)
            by_path = os.stat(
                path.name, dir_fd=parent_descriptor, follow_symlinks=False,
            )
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o600
                or identity != (by_path.st_dev, by_path.st_ino)
                or expected is not None and identity != expected
            ):
                raise ValueError("unsafe_file_identity")
            return cls(path, descriptor, parent_descriptor, identity)
        except Exception:
            os.close(descriptor)
            os.close(parent_descriptor)
            raise

    @classmethod
    def create(cls, path: Path, data: bytes) -> "_RetainedPrivateFile":
        parent_descriptor = cls._parent(path)
        try:
            descriptor = os.open(
                path.name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL
                | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_descriptor,
            )
        except Exception:
            os.close(parent_descriptor)
            raise
        retained = None
        try:
            opened = os.fstat(descriptor)
            identity = (opened.st_dev, opened.st_ino)
            retained = cls(path, descriptor, parent_descriptor, identity)
            if (
                not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != 0o600
            ):
                raise ValueError("unsafe_private_capture_output")
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
            retained.verify(data)
            os.fsync(parent_descriptor)
            return retained
        except BaseException:
            if retained is not None:
                retained.destroy()
            else:
                os.close(descriptor)
                os.close(parent_descriptor)
            raise

    def read(self, maximum: int | None = None) -> bytes:
        size = os.fstat(self.descriptor).st_size
        if maximum is not None and size > maximum:
            raise ValueError("private_file_exceeds_bound")
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        chunks = []
        remaining = size + 1
        while remaining:
            chunk = os.read(
                self.descriptor, min(1024 * 1024, remaining),
            )
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def verify(self, expected_bytes: bytes | None = None) -> None:
        opened = os.fstat(self.descriptor)
        by_path = os.stat(
            self.name, dir_fd=self.parent_descriptor, follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or self.identity != (opened.st_dev, opened.st_ino)
            or self.identity != (by_path.st_dev, by_path.st_ino)
        ):
            raise ValueError("private_capture_output_identity_changed")
        if expected_bytes is not None and self.read() != expected_bytes:
            raise ValueError("private_capture_output_readback_mismatch")

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            os.close(self.parent_descriptor)
            self.closed = True

    def destroy(self) -> None:
        """Wipe through the retained FD and unlink only matching inodes."""
        if self.closed:
            return
        blocked = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
        prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
        path_replaced = False
        try:
            info = os.fstat(self.descriptor)
            os.lseek(self.descriptor, 0, os.SEEK_SET)
            remaining = info.st_size
            block = b"\0" * min(remaining, 1024 * 1024)
            while remaining:
                written = os.write(self.descriptor, block[:remaining])
                remaining -= written
            os.fsync(self.descriptor)
            os.ftruncate(self.descriptor, 0)
            os.fsync(self.descriptor)
            try:
                current = os.stat(
                    self.name, dir_fd=self.parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                path_replaced = True
            else:
                path_replaced = self.identity != (
                    current.st_dev, current.st_ino,
                )
            for name in os.listdir(self.parent_descriptor):
                try:
                    candidate = os.stat(
                        name, dir_fd=self.parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                if self.identity == (candidate.st_dev, candidate.st_ino):
                    os.unlink(name, dir_fd=self.parent_descriptor)
            os.fsync(self.parent_descriptor)
            if os.fstat(self.descriptor).st_nlink:
                raise ValueError("retained_private_inode_still_linked")
            if path_replaced:
                raise ValueError("retained_private_path_replaced")
        finally:
            self.close()
            signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)


def unpack_private_capture_envelope(
    envelope_path: Path, discovery_path: Path, ledger_path: Path,
    envelope_identity: Identity | None = None, race_hook: Any = None,
) -> tuple[Identity, Identity, str]:
    """Strictly unpack one private transport object into two mode-0600 files."""
    envelope_file = None
    output_files: list[_RetainedPrivateFile] = []
    completed = False
    prior_handlers: dict[int, Any] = {}
    def on_signal(signum: int, _frame: Any) -> None:
        raise CapturedSignal(signum)
    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        prior_handlers[signum] = signal.signal(signum, on_signal)
    try:
        envelope_file = _RetainedPrivateFile.open_existing(
            envelope_path, envelope_identity,
        )
        envelope_raw = envelope_file.read(MAX_CAPTURE_ENVELOPE_BYTES)
        envelope = parse_json_bytes_v1(envelope_raw)
        if (
            not isinstance(envelope, dict)
            or set(envelope) != CAPTURE_ENVELOPE_KEYS
            or envelope.get("schema") != CAPTURE_ENVELOPE_SCHEMA
            or serialize_ledger_bytes_v1(envelope) != envelope_raw
        ):
            raise ValueError("capture_envelope_schema_invalid")
        discovery_raw = _decode_envelope_payload(
            envelope, "discovery", MAX_DISCOVERY_CAPTURE_BYTES,
        )
        ledger_raw = _decode_envelope_payload(
            envelope, "footbreak_ledger", MAX_LEDGER_CAPTURE_BYTES,
        )
        discovery = parse_json_bytes_v1(discovery_raw)
        if (
            not isinstance(discovery, dict)
            or serialize_ledger_bytes_v1(discovery) != discovery_raw
            or discovery.get("capture", {}).get("full_pre_ledger_sha256")
            != envelope["footbreak_ledger_sha256"]
        ):
            raise ValueError("capture_envelope_discovery_binding_invalid")
        # Strict parsing here prevents a malformed private ledger from reaching
        # any audit command; its exact original bytes remain the hash authority.
        parse_json_bytes_v1(ledger_raw)
        if discovery_path.resolve() == ledger_path.resolve():
            raise ValueError("capture_outputs_alias")
        discovery_file = _RetainedPrivateFile.create(
            discovery_path, discovery_raw,
        )
        output_files.append(discovery_file)
        ledger_file = _RetainedPrivateFile.create(ledger_path, ledger_raw)
        output_files.append(ledger_file)
        if race_hook is not None:
            race_hook(envelope_file, discovery_file, ledger_file)
        envelope_file.destroy()
        envelope_file = None
        discovery_file.verify(discovery_raw)
        ledger_file.verify(ledger_raw)
        discovery_identity = discovery_file.identity
        ledger_identity = ledger_file.identity
        completed = True
        for retained in output_files:
            retained.close()
        return (
            discovery_identity, ledger_identity,
            envelope["footbreak_ledger_sha256"],
        )
    finally:
        cleanup_error = None
        if envelope_file is not None:
            try:
                envelope_file.destroy()
            except Exception as exc:
                cleanup_error = exc
        if not completed:
            for retained in output_files:
                try:
                    retained.destroy()
                except Exception as exc:
                    cleanup_error = cleanup_error or exc
        for signum, prior in prior_handlers.items():
            signal.signal(signum, prior)
        if cleanup_error is not None and completed:
            raise cleanup_error


def run_captured_command(
    command: list[str], stdout_path: Path, stderr_path: Path, *,
    stderr_identity: Identity | None = None,
) -> tuple[int, Identity, Identity]:
    """Run a command with retained no-follow output descriptors."""
    stdout_fd = os.open(
        stdout_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    stderr_fd = None
    completed = False
    created_stderr = stderr_identity is None
    child: subprocess.Popen[bytes] | None = None
    prior_handlers: dict[int, Any] = {}
    def on_signal(signum: int, _frame: Any) -> None:
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        raise CapturedSignal(signum)
    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        prior_handlers[signum] = signal.signal(signum, on_signal)
    try:
        stdout_info = os.fstat(stdout_fd)
        if not stat.S_ISREG(stdout_info.st_mode) or stdout_info.st_nlink != 1:
            raise ValueError("unsafe_capture_stdout")
        if stderr_identity is None:
            stderr_fd = os.open(
                stderr_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        else:
            stderr_fd = _open_verified(
                stderr_path, os.O_WRONLY | os.O_APPEND, stderr_identity,
            )
        stderr_info = os.fstat(stderr_fd)
        child = subprocess.Popen(
            command, stdout=stdout_fd, stderr=stderr_fd,
            start_new_session=True,
        )
        return_code = child.wait()
        for path, descriptor, expected in (
            (stdout_path, stdout_fd, (stdout_info.st_dev, stdout_info.st_ino)),
            (stderr_path, stderr_fd, (stderr_info.st_dev, stderr_info.st_ino)),
        ):
            current = os.fstat(descriptor)
            by_path = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or (current.st_dev, current.st_ino) != expected
                or expected != (by_path.st_dev, by_path.st_ino)
            ):
                raise ValueError("capture_output_identity_changed")
        completed = True
        return (
            return_code,
            (stdout_info.st_dev, stdout_info.st_ino),
            (stderr_info.st_dev, stderr_info.st_ino),
        )
    finally:
        if not completed:
            for path, descriptor in (
                (stdout_path, stdout_fd),
                (stderr_path, stderr_fd if created_stderr else None),
            ):
                if descriptor is None:
                    continue
                try:
                    info = os.fstat(descriptor)
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    remaining = info.st_size
                    block = b"\0" * min(remaining, 1024 * 1024)
                    while remaining:
                        written = os.write(descriptor, block[:remaining])
                        remaining -= written
                    os.fsync(descriptor)
                    by_path = os.stat(path, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) == (
                        by_path.st_dev, by_path.st_ino,
                    ):
                        os.unlink(path)
                except OSError:
                    pass
        os.close(stdout_fd)
        if stderr_fd is not None:
            os.close(stderr_fd)
        for signum, prior in prior_handlers.items():
            signal.signal(signum, prior)


def _atomic_write(path: Path, document: dict[str, Any]) -> None:
    _atomic_write_bytes(path, serialize_ledger_bytes_v1(document))
    directory = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _invalid(commit: str, code: int, reason: str) -> dict[str, Any]:
    if reason not in APPROVED_FAILURES:
        reason = "receipt_validation_failed_closed"
    return {
        "schema": INVALID_SCHEMA,
        "valid": False,
        "audited_commit": commit,
        "exporter_exit_code": code if code else 2,
        "failure_classification": reason,
        "production_mutation": False,
    }


def _secure_delete(path: Path, expected: Identity | None = None) -> None:
    descriptor = None
    try:
        descriptor = _open_verified(path, os.O_WRONLY, expected)
        try:
            size = os.fstat(descriptor).st_size
            block = b"\0" * min(size, 1024 * 1024)
            remaining = size
            while remaining:
                written = os.write(descriptor, block[:remaining])
                remaining -= written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        path.unlink()
    except FileNotFoundError:
        pass
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _validate(
    staging: Path, calculation_path: Path, commit: str,
    staging_identity: Identity | None = None,
    calculation_identity: Identity | None = None,
) -> dict[str, Any]:
    calculation = validate_sanitized_calculation(
        parse_json_bytes_v1(_read_verified(calculation_path, calculation_identity))
    )
    document = validate_live_discovery(
        parse_json_bytes_v1(_read_verified(staging, staging_identity)), calculation,
    )
    if document["execution_identity"]["release_commit"] != commit:
        raise ValueError("discovery_commit_mismatch")
    if document["writer_coordination"]["all_writers_quiesced"] is not True:
        raise ValueError("discovery_quiescence_missing")
    if (
        document["writer_coordination"]["canonical_lock"]["realpath"]
        != "/var/lock/footbreak.lock"
    ):
        raise ValueError("discovery_lock_path_mismatch")
    if (
        document["capture"]["ledger_object"]["realpath"]
        != "/opt/footbreak/system/sim_ledger.json"
    ):
        raise ValueError("discovery_ledger_path_mismatch")
    canonical = serialize_ledger_bytes_v1(document)
    if parse_json_bytes_v1(canonical) != document:
        raise ValueError("canonical_discovery_round_trip_failed")
    return document


def publish_remote_receipt(
    staging: Path, calculation_path: Path, publication: Path,
    commit: str, remote_exit_code: int,
    staging_identity: Identity | None = None,
    calculation_identity: Identity | None = None,
    calculation_publication: Path | None = None,
) -> bool:
    """Publish canonical discovery or an exact non-sensitive invalid document."""
    valid = False
    try:
        if remote_exit_code:
            raise ValueError("remote_discovery_failed_closed")
        document = _validate(
            staging, calculation_path, commit,
            staging_identity, calculation_identity,
        )
        if calculation_publication is not None:
            calculation = validate_sanitized_calculation(parse_json_bytes_v1(
                _read_verified(calculation_path, calculation_identity),
            ))
            _atomic_write(calculation_publication, calculation)
        _atomic_write(publication, document)
        valid = True
    except Exception as exc:
        _atomic_write(
            publication,
            _invalid(commit, remote_exit_code, str(exc)),
        )
    finally:
        try:
            _secure_delete(staging, staging_identity)
        except Exception:
            # Never overwrite the validation result or touch an alias target.
            valid = False
            _atomic_write(
                publication,
                _invalid(commit, 2, "receipt_validation_failed_closed"),
            )
    return valid


def finalize_publication(
    publication: Path, calculation_path: Path | None, commit: str,
    gates_ok: bool, expected_sha256: str | None = None,
    expected_identity: Identity | None = None,
) -> bool:
    """Revalidate after all gates or atomically invalidate before upload."""
    valid = False
    try:
        if not gates_ok:
            raise ValueError("whole_workflow_gate_failed_closed")
        if expected_sha256 is not None:
            raw = _read_verified(publication, expected_identity)
            if hashlib.sha256(raw).hexdigest() != expected_sha256:
                raise ValueError("publication_receipt_hash_mismatch")
            document = parse_json_bytes_v1(raw)
            if serialize_ledger_bytes_v1(document) != raw:
                raise ValueError("publication_not_canonical")
            if document.get("schema") != "footbreak-legacy-batch-live-discovery-v1":
                raise ValueError("publication_schema_invalid")
            if document["execution_identity"]["release_commit"] != commit:
                raise ValueError("final_discovery_commit_mismatch")
        elif calculation_path is not None:
            document = _validate(publication, calculation_path, commit)
        else:
            raise ValueError("publication_validation_evidence_missing")
        if expected_sha256 is None:
            _atomic_write(publication, document)
        valid = True
    except Exception:
        _atomic_write(
            publication,
            _invalid(commit, 2, "whole_workflow_gate_failed_closed"),
        )
    finally:
        if calculation_path is not None:
            try:
                _secure_delete(calculation_path)
            except Exception:
                valid = False
                _atomic_write(
                    publication,
                    _invalid(
                        commit, 2, "whole_workflow_gate_failed_closed",
                    ),
                )
    return valid


def seal_publication(
    publication: Path, sealed: Path, expected_sha256: str,
    expected_identity: Identity, race_hook: Any = None,
) -> tuple[str, Identity]:
    """Copy exact final bytes from a verified inode to an upload-only object."""
    raw = _read_verified(publication, expected_identity)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("publication_seal_hash_mismatch")
    document = parse_json_bytes_v1(raw)
    if serialize_ledger_bytes_v1(document) != raw:
        raise ValueError("publication_seal_not_canonical")
    sealed.parent.mkdir(mode=0o700, parents=False, exist_ok=True)
    directory_fd = os.open(
        sealed.parent,
        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    os.fchmod(directory_fd, 0o700)
    try:
        existing = os.lstat(sealed)
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISDIR(existing.st_mode):
            os.close(directory_fd)
            raise ValueError("sealed_residue_is_directory")
        os.unlink(sealed)
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=sealed.name + ".", suffix=".seal", dir=sealed.parent,
        )
    except Exception:
        os.close(directory_fd)
        raise
    temporary = Path(name)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        readback = b""
        while len(readback) <= len(raw):
            chunk = os.read(descriptor, len(raw) + 1 - len(readback))
            if not chunk:
                break
            readback += chunk
        if readback != raw:
            raise ValueError("sealed_publication_readback_mismatch")
        os.fchmod(descriptor, 0o400)
        sealed_info = os.fstat(descriptor)
        sealed_identity = (sealed_info.st_dev, sealed_info.st_ino)
        os.replace(temporary, sealed)
        if race_hook is not None:
            race_hook(sealed)
        os.fchmod(directory_fd, 0o500)
        os.fsync(directory_fd)
        by_path = os.stat(sealed, follow_symlinks=False)
        final_info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(final_info.st_mode)
            or final_info.st_nlink != 1
            or stat.S_IMODE(final_info.st_mode) != 0o400
            or sealed_identity != (by_path.st_dev, by_path.st_ino)
            or sealed_identity != (final_info.st_dev, final_info.st_ino)
        ):
            raise ValueError("sealed_publication_identity_changed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        final_bytes = os.read(descriptor, len(raw) + 1)
        if final_bytes != raw:
            raise ValueError("sealed_publication_final_hash_mismatch")
        return hashlib.sha256(final_bytes).hexdigest(), sealed_identity
    finally:
        os.close(descriptor)
        os.close(directory_fd)
        if temporary.exists():
            temporary.unlink()


def verify_root_sealed(
    sealed: Path, expected_sha256: str, expected_identity: Identity,
) -> tuple[str, Identity]:
    descriptor = os.open(
        sealed, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    directory_fd = os.open(
        sealed.parent,
        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        info = os.fstat(descriptor)
        directory_info = os.fstat(directory_fd)
        by_path = os.stat(sealed, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o444
            or directory_info.st_uid != 0 or directory_info.st_gid != 0
            or stat.S_IMODE(directory_info.st_mode) != 0o555
            or expected_identity != (info.st_dev, info.st_ino)
            or expected_identity != (by_path.st_dev, by_path.st_ino)
        ):
            raise ValueError("root_sealed_identity_or_mode_mismatch")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        digest = hashlib.sha256(b"".join(chunks)).hexdigest()
        if digest != expected_sha256:
            raise ValueError("root_sealed_hash_mismatch")
        return digest, expected_identity
    finally:
        os.close(descriptor)
        os.close(directory_fd)


def promote_root_sealed(
    sealed: Path, expected_sha256: str, expected_identity: Identity,
) -> tuple[str, Identity]:
    """Privileged promotion to an upload-readable runner-immutable object."""
    if os.geteuid() != 0:
        raise PermissionError("root_required_for_sealed_promotion")
    descriptor = os.open(
        sealed, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    directory_fd = os.open(
        sealed.parent,
        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        info = os.fstat(descriptor)
        by_path = os.stat(sealed, follow_symlinks=False)
        if (
            expected_identity != (info.st_dev, info.st_ino)
            or expected_identity != (by_path.st_dev, by_path.st_ino)
            or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o400
        ):
            raise ValueError("sealed_promotion_precondition_mismatch")
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o444)
        os.fchown(directory_fd, 0, 0)
        os.fchmod(directory_fd, 0o555)
        os.fsync(descriptor)
        os.fsync(directory_fd)
    finally:
        os.close(descriptor)
        os.close(directory_fd)
    return verify_root_sealed(sealed, expected_sha256, expected_identity)


def _receipt(path: Path, valid: bool) -> str:
    raw = _read_verified(path)
    document = parse_json_bytes_v1(raw)
    if document.get("schema") == INVALID_SCHEMA and set(document) != INVALID_KEYS:
        raise ValueError("invalid_publication_shape")
    return json.dumps({
        "schema": document["schema"],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "identity": "%s:%s" % _verified_identity(path),
        "valid": valid,
    }, sort_keys=True, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    receipt = subparsers.add_parser("receipt")
    receipt.add_argument("--staging", required=True, type=Path)
    receipt.add_argument("--calculation", required=True, type=Path)
    receipt.add_argument("--publication", required=True, type=Path)
    receipt.add_argument("--commit", required=True)
    receipt.add_argument("--remote-exit-code", required=True, type=int)
    receipt.add_argument("--staging-identity", required=True)
    receipt.add_argument("--calculation-identity", required=True)
    receipt.add_argument("--calculation-publication", type=Path)
    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--publication", required=True, type=Path)
    finalize.add_argument("--calculation", type=Path)
    finalize.add_argument("--commit", required=True)
    finalize.add_argument("--gates-ok", action="store_true")
    finalize.add_argument("--expected-sha256")
    finalize.add_argument("--expected-identity")
    invalid = subparsers.add_parser("invalid")
    invalid.add_argument("--publication", required=True, type=Path)
    invalid.add_argument("--commit", required=True)
    invalid.add_argument("--exit-code", required=True, type=int)
    invalid.add_argument("--reason", required=True, choices=sorted(APPROVED_FAILURES))
    reserve = subparsers.add_parser("reserve")
    reserve.add_argument("--path", required=True, type=Path)
    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("--path", required=True, type=Path)
    cleanup.add_argument("--identity", required=True)
    identify = subparsers.add_parser("identify")
    identify.add_argument("--path", required=True, type=Path)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--stdout", required=True, type=Path)
    capture.add_argument("--stderr", required=True, type=Path)
    capture.add_argument("--stderr-identity")
    capture.add_argument("child", nargs=argparse.REMAINDER)
    unpack = subparsers.add_parser("unpack")
    unpack.add_argument("--envelope", required=True, type=Path)
    unpack.add_argument("--envelope-identity", required=True)
    unpack.add_argument("--discovery", required=True, type=Path)
    unpack.add_argument("--ledger", required=True, type=Path)
    seal = subparsers.add_parser("seal")
    seal.add_argument("--publication", required=True, type=Path)
    seal.add_argument("--sealed", required=True, type=Path)
    seal.add_argument("--expected-sha256", required=True)
    seal.add_argument("--expected-identity", required=True)
    promote = subparsers.add_parser("promote-root")
    promote.add_argument("--sealed", required=True, type=Path)
    promote.add_argument("--expected-sha256", required=True)
    promote.add_argument("--expected-identity", required=True)
    verify_root = subparsers.add_parser("verify-root")
    verify_root.add_argument("--sealed", required=True, type=Path)
    verify_root.add_argument("--expected-sha256", required=True)
    verify_root.add_argument("--expected-identity", required=True)
    host_pin = subparsers.add_parser("host-pin")
    host_pin.add_argument("--host", required=True)
    host_pin.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    def identity(value: str) -> Identity:
        left, right = value.split(":", 1)
        return int(left), int(right)
    if args.command == "reserve":
        reserved_identity = reserve_private_file(args.path)
        print(f"{reserved_identity[0]}:{reserved_identity[1]}")
        return 0
    if args.command == "host-pin":
        value = os.environ.get("DEPLOY_SSH_KNOWN_HOSTS", "")
        write_known_hosts_pin(args.host, value, args.output)
        return 0
    if args.command == "capture":
        child = args.child[1:] if args.child[:1] == ["--"] else args.child
        if not child:
            raise ValueError("capture_command_required")
        try:
            code, stdout_identity, stderr_identity = run_captured_command(
                child, args.stdout, args.stderr,
                stderr_identity=(
                    identity(args.stderr_identity)
                    if args.stderr_identity else None
                ),
            )
        except CapturedSignal as exc:
            return 128 + exc.signum
        print(json.dumps({
            "exit_code": code,
            "stdout_identity": f"{stdout_identity[0]}:{stdout_identity[1]}",
            "stderr_identity": f"{stderr_identity[0]}:{stderr_identity[1]}",
        }, sort_keys=True, separators=(",", ":")))
        return 0
    if args.command == "unpack":
        try:
            discovery_identity, ledger_identity, digest = (
                unpack_private_capture_envelope(
                    args.envelope, args.discovery, args.ledger,
                    identity(args.envelope_identity),
                )
            )
        except CapturedSignal as exc:
            return 128 + exc.signum
        print(json.dumps({
            "discovery_identity":
                f"{discovery_identity[0]}:{discovery_identity[1]}",
            "ledger_identity": f"{ledger_identity[0]}:{ledger_identity[1]}",
            "ledger_sha256": digest,
        }, sort_keys=True, separators=(",", ":")))
        return 0
    if args.command == "seal":
        digest, sealed_identity = seal_publication(
            args.publication, args.sealed, args.expected_sha256,
            identity(args.expected_identity),
        )
        print(json.dumps({
            "sha256": digest,
            "identity": f"{sealed_identity[0]}:{sealed_identity[1]}",
        }, sort_keys=True, separators=(",", ":")))
        return 0
    if args.command in {"promote-root", "verify-root"}:
        function = (
            promote_root_sealed
            if args.command == "promote-root" else verify_root_sealed
        )
        digest, sealed_identity = function(
            args.sealed, args.expected_sha256,
            identity(args.expected_identity),
        )
        print(json.dumps({
            "sha256": digest,
            "identity": f"{sealed_identity[0]}:{sealed_identity[1]}",
        }, sort_keys=True, separators=(",", ":")))
        return 0
    if args.command == "cleanup":
        _secure_delete(args.path, identity(args.identity))
        return 0
    if args.command == "identify":
        found = _verified_identity(args.path)
        print(f"{found[0]}:{found[1]}")
        return 0
    if args.command == "invalid":
        _atomic_write(
            args.publication,
            _invalid(args.commit, args.exit_code, args.reason),
        )
        print(_receipt(args.publication, False))
        return 2
    if args.command == "receipt":
        valid = publish_remote_receipt(
            args.staging, args.calculation, args.publication,
            args.commit, args.remote_exit_code,
            identity(args.staging_identity),
            identity(args.calculation_identity),
            args.calculation_publication,
        )
    else:
        valid = finalize_publication(
            args.publication, args.calculation, args.commit, args.gates_ok,
            args.expected_sha256,
            identity(args.expected_identity) if args.expected_identity else None,
        )
    print(_receipt(args.publication, valid))
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
